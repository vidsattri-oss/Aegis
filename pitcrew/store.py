"""Persistent local workspace, telemetry and weighted knowledge graph."""
import hashlib
import json
import os
import re
import sqlite3
import threading
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .contracts import require
from .usage import usd_amount


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def uid():
    return uuid4().hex[:16]


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


REDACT = re.compile(
    r"(?i)(?:bearer[ \t]+[\w./+=-]{8,}|(?:sk-(?:proj-|ant-)?|gh[pousr]_|github_pat_|AKIA)[\w-]{10,}|"
    r"(?:(?:api[-_ ]?key|password|secret|token)\b[\"']?\s*[:=]\s*[\"']?)[\w./+=-]{8,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----)"
)


class Redactor:
    def __init__(self):
        self.keys = set()
        self.lock = threading.Lock()

    def add(self, value):
        if value:
            with self.lock:
                self.keys.add(value)

    def __call__(self, value):
        if isinstance(value, dict):
            return {key: self(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self(item) for item in value]
        if not isinstance(value, str):
            return value
        with self.lock:
            for key in sorted(self.keys, key=len, reverse=True):
                value = value.replace(key, "[REDACTED]")
        return REDACT.sub("[REDACTED]", value)


SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL, created TEXT NOT NULL,
 github_url TEXT, github_branch TEXT, budget_usd REAL, budget_hold TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), prompt TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'queued', stage TEXT NOT NULL DEFAULT 'lead', branch TEXT NOT NULL,
 created TEXT NOT NULL, updated TEXT NOT NULL, plan TEXT, preview TEXT, bundle TEXT, error TEXT,
 cancellation INTEGER NOT NULL DEFAULT 0, provider TEXT NOT NULL,
 github_base TEXT, github_commit TEXT, github_published TEXT
);
CREATE INDEX IF NOT EXISTS sessions_project ON sessions(project_id, created);
CREATE TABLE IF NOT EXISTS attempts (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), stage TEXT NOT NULL,
 role TEXT NOT NULL, number INTEGER NOT NULL, status TEXT NOT NULL, profile TEXT NOT NULL,
 started TEXT NOT NULL, ended TEXT, persona TEXT NOT NULL, input TEXT NOT NULL, trace TEXT,
 input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
 model_cost REAL NOT NULL DEFAULT 0, compute_cost REAL NOT NULL DEFAULT 0,
 vendor_cost REAL NOT NULL DEFAULT 0, vendor_estimate_cost REAL NOT NULL DEFAULT 0,
 unpriced_calls INTEGER NOT NULL DEFAULT 0, metering_pending INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS attempts_session ON attempts(session_id, started);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), attempt_id TEXT,
 time TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_session ON events(session_id, id);
CREATE TABLE IF NOT EXISTS messages (
 id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
 speaker TEXT NOT NULL, text TEXT NOT NULL, created TEXT NOT NULL,
 reply_to INTEGER REFERENCES messages(id)
);
CREATE TABLE IF NOT EXISTS gates (
 session_id TEXT NOT NULL REFERENCES sessions(id), stage TEXT NOT NULL, state TEXT NOT NULL,
 started TEXT NOT NULL, ended TEXT, commit_sha TEXT, artifact TEXT, details TEXT,
 PRIMARY KEY(session_id, stage)
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memory_nodes (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, label TEXT NOT NULL, body TEXT NOT NULL,
 weight REAL NOT NULL DEFAULT 1, created TEXT NOT NULL, last_used TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_projects (
 node_id TEXT REFERENCES memory_nodes(id) ON DELETE CASCADE, project_id TEXT REFERENCES projects(id),
 PRIMARY KEY(node_id, project_id)
);
CREATE TABLE IF NOT EXISTS memory_edges (
 source TEXT REFERENCES memory_nodes(id) ON DELETE CASCADE,
 target TEXT REFERENCES memory_nodes(id) ON DELETE CASCADE,
 kind TEXT NOT NULL, weight INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(source, target, kind)
);
CREATE TABLE IF NOT EXISTS memory_usage (
 node_id TEXT REFERENCES memory_nodes(id) ON DELETE CASCADE, session_id TEXT REFERENCES sessions(id),
 stage TEXT NOT NULL, PRIMARY KEY(node_id, session_id, stage)
);
CREATE TABLE IF NOT EXISTS dream_runs (
 id INTEGER PRIMARY KEY, at TEXT NOT NULL, pruned INTEGER NOT NULL, synthesized INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_learning (
 attempt_id TEXT PRIMARY KEY REFERENCES attempts(id), project_id TEXT NOT NULL REFERENCES projects(id),
 role TEXT NOT NULL, stage TEXT NOT NULL, lesson TEXT NOT NULL, evidence TEXT NOT NULL, created TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS agent_learning_role ON agent_learning(project_id,role,created);
CREATE TABLE IF NOT EXISTS agent_playbooks (
 project_id TEXT REFERENCES projects(id), role TEXT, revision INTEGER NOT NULL,
 lessons TEXT NOT NULL, PRIMARY KEY(project_id,role)
);
"""

JSON_COLUMNS = {"provider", "plan", "profile", "input", "trace", "payload", "artifact", "details"}


def unpack(row):
    if row is None:
        return None
    return {key: json.loads(value) if key in JSON_COLUMNS and value is not None else value
            for key, value in dict(row).items()}


class Store:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.file = self.directory / "workspace.sqlite3"
        self.redact = Redactor()
        with self.db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            db.execute("BEGIN IMMEDIATE")
            if "reply_to" not in {column["name"] for column in db.execute("PRAGMA table_info(messages)")}:
                db.execute("ALTER TABLE messages ADD COLUMN reply_to INTEGER REFERENCES messages(id)")
            for table, fields in (("projects", ("github_url", "github_branch", "budget_hold")),
                                  ("sessions", ("github_base", "github_commit", "github_published"))):
                columns = {column["name"] for column in db.execute(f"PRAGMA table_info({table})")}
                for field in fields:
                    if field not in columns:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN {field} TEXT")
            if "budget_usd" not in {column["name"] for column in db.execute("PRAGMA table_info(projects)")}:
                db.execute("ALTER TABLE projects ADD COLUMN budget_usd REAL")
            attempt_columns = {column["name"] for column in db.execute("PRAGMA table_info(attempts)")}
            for column, kind in (("vendor_cost", "REAL"), ("vendor_estimate_cost", "REAL"),
                                 ("unpriced_calls", "INTEGER"), ("metering_pending", "INTEGER")):
                if column not in attempt_columns:
                    db.execute(f"ALTER TABLE attempts ADD COLUMN {column} {kind} NOT NULL DEFAULT 0")
            db.execute("CREATE INDEX IF NOT EXISTS messages_reply ON messages(session_id, reply_to)")
            db.execute("""INSERT INTO dream_runs(at,pruned,synthesized)
                SELECT json_extract(value,'$.at'),json_extract(value,'$.pruned'),json_extract(value,'$.synthesized')
                FROM settings WHERE key='last_dream' AND NOT EXISTS(SELECT 1 FROM dream_runs)""")
        os.chmod(self.file, 0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.file, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def rows(self, sql, args=()):
        with self.db() as db:
            return [unpack(row) for row in db.execute(sql, args)]

    def row(self, sql, args=()):
        with self.db() as db:
            return unpack(db.execute(sql, args).fetchone())

    def write(self, sql, args=()):
        with self.db() as db:
            return db.execute(sql, args).rowcount

    def setting(self, key, default=None):
        result = self.row("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(result["value"]) if result else default

    def set(self, key, value):
        self.write("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, encode(value)))

    def workspace_id(self):
        identity = self.setting("workspace_id")
        if identity is not None:
            return identity
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO settings VALUES ('workspace_id',?)", (encode(uid()),))
            return json.loads(db.execute("SELECT value FROM settings WHERE key='workspace_id'").fetchone()[0])

    def project(self, name, description, *, project_id=None, github_url=None, github_branch=None):
        project_id = project_id or uid()
        self.write("""INSERT INTO projects(id,name,description,created,github_url,github_branch)
                      VALUES (?,?,?,?,?,?)""",
                   (project_id, self.redact(name), self.redact(description), now(), github_url, github_branch))
        return self.row("SELECT * FROM projects WHERE id=?", (project_id,))

    def projects(self):
        return self.rows("""
            SELECT p.*, (SELECT COUNT(*) FROM sessions WHERE project_id=p.id) AS session_count,
            COALESCE(SUM(a.input_tokens+a.output_tokens),0) AS tokens,
            COALESCE(SUM(a.model_cost),0) AS model_cost, COALESCE(SUM(a.compute_cost),0) AS compute_cost,
            COALESCE(SUM(a.status='Running'),0) AS active_agents,
            COALESCE(SUM(a.status IN ('Failed','Retried')),0) AS failed_agents,
            (SELECT state FROM sessions WHERE project_id=p.id ORDER BY created DESC LIMIT 1) AS health
            FROM projects p LEFT JOIN sessions s ON s.project_id=p.id
            LEFT JOIN attempts a ON a.session_id=s.id GROUP BY p.id ORDER BY p.created DESC
        """)

    def budget(self, project_id):
        result = self.row("""SELECT p.budget_usd,p.budget_hold,ROUND(COALESCE(SUM(a.model_cost),0),8) AS model_cost,
            ROUND(COALESCE(SUM(a.vendor_cost),0),8) AS vendor_cost,
            ROUND(COALESCE(SUM(a.vendor_estimate_cost),0),8) AS vendor_estimate_cost,
            COALESCE(SUM(a.unpriced_calls),0) AS unpriced_calls
            FROM projects p LEFT JOIN sessions s ON s.project_id=p.id
            LEFT JOIN attempts a ON a.session_id=s.id WHERE p.id=? GROUP BY p.id""", (project_id,))
        if result is not None:
            limit, spend = result["budget_usd"], result["model_cost"]
            result["remaining_usd"] = None if limit is None else round(max(0, limit - spend), 8)
            result["reached"] = limit is not None and spend >= limit
        return result

    def set_budget(self, project_id, limit):
        require(limit is None or usd_amount(limit) is not None and limit == round(limit, 2),
                "Enter a USD budget from 0 to 1,000,000 with up to two decimal places")
        require(self.write("UPDATE projects SET budget_usd=?,budget_hold=NULL WHERE id=?", (limit, project_id)),
                "RaceDay does not exist")
        return self.budget(project_id)

    def reconcile_model_usage(self, db):
        reason = "A model request was interrupted before its charge was recorded. Review the vendor usage and save the RaceDay budget before resuming."
        db.execute("""UPDATE projects SET budget_hold=? WHERE budget_usd IS NOT NULL AND id IN (
                      SELECT s.project_id FROM sessions s JOIN attempts a ON a.session_id=s.id
                      WHERE a.metering_pending=1)""", (reason,))
        db.execute("UPDATE attempts SET metering_pending=0,unpriced_calls=unpriced_calls+1 WHERE metering_pending=1")

    def record_model_usage(self, attempt, usage, amount, source, hold=None):
        with self.db() as db:
            db.execute("""UPDATE attempts SET input_tokens=input_tokens+?,output_tokens=output_tokens+?,
                       model_cost=model_cost+?,vendor_cost=vendor_cost+?,vendor_estimate_cost=vendor_estimate_cost+?,
                       unpriced_calls=unpriced_calls+?,metering_pending=0 WHERE id=?""",
                       (*usage, amount, amount if source == "vendor_charge" else 0,
                        amount if source in ("vendor_estimate", "azure_retail") else 0, source == "unpriced", attempt))
            if hold:
                return db.execute("""UPDATE projects SET budget_hold=? WHERE budget_usd IS NOT NULL AND id=(
                                  SELECT s.project_id FROM sessions s JOIN attempts a ON a.session_id=s.id WHERE a.id=?)""",
                                  (hold, attempt)).rowcount > 0
        return False

    def session(self, project_id, prompt, provider):
        session_id = uid()
        stamp = now()
        with self.db() as db:
            db.execute("""INSERT INTO sessions (id,project_id,prompt,branch,created,updated,provider)
                          VALUES (?,?,?,?,?,?,?)""",
                       (session_id, project_id, self.redact(prompt), f"session/{project_id}/{session_id}",
                        stamp, stamp, encode(provider)))
            db.execute("INSERT INTO messages (session_id,speaker,text,created) VALUES (?,?,?,?)",
                       (session_id, "user", self.redact(prompt), stamp))
        return session_id

    def event(self, session_id, kind, payload, attempt=None):
        self.write("INSERT INTO events (session_id,attempt_id,time,kind,payload) VALUES (?,?,?,?,?)",
                   (session_id, attempt, now(), kind, encode(self.redact(payload))))

    def telemetry(self, session_id, *, role=None, stage=None, attempt=None, before=None, after=None, limit=24):
        filters = ["e.session_id=?"]
        args = [session_id]
        event_role = "COALESCE(a.role, 'lead')"
        event_stage = """COALESCE(a.stage, json_extract(e.payload, '$.stage'),
            CASE WHEN e.kind IN ('released','preview_stopped')
                 OR json_extract(e.payload, '$.tool')='preview.start' THEN 'deploy' END)"""
        for column, value in ((event_role, role), (event_stage, stage), ("e.attempt_id", attempt)):
            if value:
                filters.append(f"{column}=?")
                args.append(value)
        if before is not None:
            filters.append("e.id<?")
            args.append(before)
        if after is not None:
            filters.append("e.id>?")
            args.append(after)
        order = "ASC" if after is not None else "DESC"
        scoped = self.rows(f"""
            SELECT e.id,e.attempt_id,e.kind,e.time,e.payload,{event_role} AS role,{event_stage} AS stage
            FROM events e LEFT JOIN attempts a ON a.id=e.attempt_id AND a.session_id=e.session_id
            WHERE {" AND ".join(filters)} ORDER BY e.id {order} LIMIT ?
        """, (*args, limit + 1))
        events = []
        for row in scoped[:limit]:
            payload = row.pop("payload")
            output = payload.get("output")
            error = payload.get("error")
            if isinstance(error, dict):
                error = error.get("message")
            excerpt = next((item for item in (output, payload.get("message"), error, payload.get("event"),
                                              payload.get("status"), payload.get("commit"), payload.get("url"))
                            if isinstance(item, str) and item), "")
            events.append({**row, "tool": payload.get("tool"), "model": payload.get("model"), "excerpt": excerpt[:650]})
        return {"events": events, "before": events[-1]["id"] if after is None and len(scoped) > limit else None}

    def release(self, session_id, commit, build, preview, github_commit=None):
        stamp = now()
        event = encode(self.redact({"commit": commit, "build": build, "preview": preview, "github_commit": github_commit}))
        with self.db() as db:
            db.execute("UPDATE sessions SET state='completed',bundle=?,preview=?,github_commit=?,updated=? WHERE id=?",
                       (build, preview, github_commit, stamp, session_id))
            db.execute("INSERT INTO events (session_id,time,kind,payload) VALUES (?,?,'released',?)",
                       (session_id, stamp, event))
            message = "All six stages passed. The build is available in Parc Fermé."
            if github_commit:
                message += " The GitHub work is committed locally and ready to publish on its lap branch."
            db.execute("INSERT INTO messages (session_id,speaker,text,created) VALUES (?,'lead',?,?)",
                       (session_id, message, stamp))

    def cancel(self, session_id):
        with self.db() as db:
            stamp = now()
            changed = db.execute("""UPDATE sessions SET cancellation=1,updated=?
                                    WHERE id=? AND state IN ('queued','running') AND cancellation=0""",
                                 (stamp, session_id)).rowcount
            if changed:
                db.execute("INSERT INTO messages (session_id,speaker,text,created) VALUES (?,?,?,?)",
                           (session_id, "lead", "Cancellation requested. I’ll stop at the next safe boundary.", stamp))
            return bool(changed)

    def message(self, session_id, speaker, text, *, reply_to=None):
        with self.db() as db:
            return db.execute("INSERT INTO messages (session_id,speaker,text,created,reply_to) VALUES (?,?,?,?,?)",
                              (session_id, speaker, self.redact(text), now(), reply_to)).lastrowid

    def chat_reply(self, session_id, message_id):
        return self.row("SELECT text FROM messages WHERE session_id=? AND reply_to=?", (session_id, message_id))

    def chat_history(self, session_id, before):
        return self.rows("""SELECT question.text AS question, answer.text AS answer FROM messages AS answer
                          JOIN messages AS question ON question.id=answer.reply_to
                          WHERE answer.session_id=? AND question.id<? ORDER BY question.id DESC LIMIT 4""",
                         (session_id, before))

    def update(self, session_id, **changes):
        changes["updated"] = now()
        columns = ", ".join(f"{key}=?" for key in changes)
        values = [encode(item) if key in JSON_COLUMNS and item is not None else item for key, item in changes.items()]
        self.write(f"UPDATE sessions SET {columns} WHERE id=?", (*values, session_id))

    def gate(self, session_id, stage, state, **values):
        update = ", ".join([*(f"{key}=excluded.{key}" for key in values), "state=excluded.state"])
        columns = ",".join(values)
        placeholders = ",".join("?" for _ in values)
        data = [encode(self.redact(value)) if key in JSON_COLUMNS else value for key, value in values.items()]
        suffix = f",{columns}" if values else ""
        parameters = f",{placeholders}" if values else ""
        self.write(f"""INSERT INTO gates (session_id,stage,state,started{suffix}) VALUES (?,?,?,?{parameters})
                    ON CONFLICT(session_id,stage) DO UPDATE SET {update}""",
                   (session_id, stage, state, now(), *data))

    def summary(self, session_id):
        session = self.row("SELECT * FROM sessions WHERE id=?", (session_id,))
        if not session:
            return None
        session["agents"] = self.rows("""SELECT id,stage,role,number,status,profile,started,ended,
            input_tokens,output_tokens,model_cost,compute_cost,vendor_cost,vendor_estimate_cost,unpriced_calls
            FROM attempts WHERE session_id=? ORDER BY started,rowid""",
            (session_id,))
        stages = self.rows("SELECT * FROM gates WHERE session_id=? ORDER BY rowid", (session_id,))
        for gate in stages:
            artifact = gate.get("artifact")
            if isinstance(artifact, dict):
                gate["artifact"] = {k: v for k, v in artifact.items() if k != "files"}
                if "files" in artifact:
                    gate["artifact"]["paths"] = sorted(artifact["files"])
        session["gates"] = stages
        session["messages"] = self.rows("SELECT * FROM messages WHERE session_id=? ORDER BY id", (session_id,))
        session["event_revision"] = self.row(
            "SELECT COALESCE(MAX(id),0) AS revision FROM events WHERE session_id=?", (session_id,)
        )["revision"]
        session["cost"] = round(sum(a["model_cost"] + a["compute_cost"] for a in session["agents"]), 8)
        session["tokens"] = sum(a["input_tokens"] + a["output_tokens"] for a in session["agents"])
        return session

    def retrieve(self, query, limit=6, *, project=None):
        terms = set(re.findall(r"[a-z0-9]{4,}", query.lower()))
        ranked = []
        candidates = self.rows("""SELECT * FROM memory_nodes n WHERE
            (? IS NOT NULL AND EXISTS (SELECT 1 FROM memory_projects m WHERE m.node_id=n.id AND m.project_id=?))
            OR (? IS NULL AND NOT EXISTS (SELECT 1 FROM memory_projects m JOIN projects p ON p.id=m.project_id
                                         WHERE m.node_id=n.id AND p.github_url IS NOT NULL))
            ORDER BY weight DESC,last_used DESC LIMIT 500""", (project, project, project))
        for node in candidates:
            text = set(re.findall(r"[a-z0-9]{4,}", (node["label"] + " " + node["body"]).lower()))
            overlap = len(terms & text)
            if overlap:
                ranked.append((overlap * (1 + node["weight"]) ** 0.5, node["id"], node))
        return [node for _, _, node in sorted(ranked, reverse=True)[:limit]]

    @staticmethod
    def _node(db, kind, label, body, project):
        identity = hashlib.sha256((kind + "\0" + " ".join(body.lower().split())).encode()).hexdigest()[:24]
        db.execute("INSERT OR IGNORE INTO memory_nodes VALUES (?,?,?,?,1,?,?)",
                   (identity, kind, label[:150], body[:3000], now(), now()))
        if project:
            db.execute("INSERT OR IGNORE INTO memory_projects VALUES (?,?)", (identity, project))
        return identity

    def remember(self, project, records, applied=(), session_id=None, stage=None, problem=None):
        with self.db() as db:
            saved = [self._node(db, kind, self.redact(label), self.redact(body), project)
                     for kind, label, body in records]
            for node in applied:
                cursor = db.execute("INSERT OR IGNORE INTO memory_usage VALUES (?,?,?)", (node, session_id, stage))
                if cursor.rowcount:
                    db.execute("UPDATE memory_nodes SET weight=weight+1,last_used=? WHERE id=?", (now(), node))
            if problem and saved:
                source = self._node(db, "problem", "Recovered stage failure", self.redact(problem), project)
                self._edge(db, source, saved[0], "resolved_by")
            for source, target in zip(saved, saved[1:]):
                if source != target:
                    self._edge(db, source, target, "related")

    @staticmethod
    def _edge(db, source, target, kind):
        db.execute("""INSERT INTO memory_edges VALUES (?,?,?,1)
                      ON CONFLICT(source,target,kind) DO UPDATE SET weight=weight+1""", (source, target, kind))

    def learn(self, project, attempt, role, stage, lesson, evidence):
        self.write("""INSERT OR IGNORE INTO agent_learning
                      (attempt_id,project_id,role,stage,lesson,evidence,created)
                      VALUES (?,?,?,?,?,?,?)""",
                   (attempt, project, role, stage, self.redact(lesson), self.redact(evidence), now()))

    def playbook(self, project, role):
        record = self.row("SELECT revision,lessons FROM agent_playbooks WHERE project_id=? AND role=?", (project, role))
        trials = self.rows("""SELECT attempt_id,stage,lesson,evidence FROM agent_learning
                              WHERE project_id=? AND role=? ORDER BY created DESC,rowid DESC LIMIT 4""", (project, role))
        lessons = json.loads(record["lessons"]) if record else []
        consolidated = {item["attempt_id"] for item in lessons}
        return {"revision": record["revision"] if record else 0, "lessons": lessons,
                "recent_trials": [trial for trial in trials if trial["attempt_id"] not in consolidated]}

    def playbooks(self, project=None):
        records = self.rows("""SELECT b.*,p.name FROM agent_playbooks b JOIN projects p ON p.id=b.project_id
                               WHERE (? IS NULL OR b.project_id=?) ORDER BY p.name,b.role LIMIT 100""", (project, project))
        return [{**record, "lessons": json.loads(record["lessons"])} for record in records]

    def dream(self, retain_days=90):
        """Consolidate recurring decisions locally while no session is executing."""
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM sessions WHERE state IN ('queued','running')").fetchone():
                return None
            count = db.execute("""DELETE FROM memory_nodes WHERE weight<=1
                AND julianday(last_used) < julianday('now',?)
                AND NOT EXISTS (SELECT 1 FROM memory_edges WHERE source=memory_nodes.id OR target=memory_nodes.id)""",
                               (f"-{retain_days} days",)).rowcount
            groups = defaultdict(list)
            for row in db.execute("""SELECT n.id,n.body,GROUP_CONCAT(p.project_id) AS projects
                FROM memory_nodes n JOIN memory_projects p ON p.node_id=n.id
                JOIN projects owning ON owning.id=p.project_id
                WHERE owning.github_url IS NULL AND n.kind IN ('decision','pattern','fix')
                GROUP BY n.id ORDER BY n.weight DESC LIMIT 500"""):
                words = re.findall(r"[a-z]{5,}", row["body"].lower())
                for pair in set(zip(words, words[1:])):
                    groups[pair].append(row)
            synthesized = 0
            for terms, rows in sorted(groups.items(), key=lambda item: (-len(item[1]), item[0])):
                projects = set().union(*(row["projects"].split(",") for row in rows))
                if len(projects) < 2 or len(rows) < 2:
                    continue
                phrase = " ".join(terms)
                evidence = list(dict.fromkeys(row["body"] for row in rows))[:4]
                body = f"Recurring practice concerning {phrase}. " + " ".join(evidence)
                pattern = self._node(db, "cross-project", phrase, body, None)
                for row in rows[:12]:
                    db.execute("INSERT OR IGNORE INTO memory_edges VALUES (?,?,?,1)",
                               (pattern, row["id"], "supported_by"))
                synthesized += 1
                if synthesized == 12:
                    break
            db.execute("""UPDATE memory_edges SET weight=MAX(weight, (SELECT CAST(MIN(a.weight,b.weight) AS INTEGER)
                FROM memory_nodes a,memory_nodes b WHERE a.id=source AND b.id=target))
                WHERE kind='resolved_by'""")
            playbooks = defaultdict(list)
            for row in db.execute("""SELECT * FROM (
                SELECT project_id,role,attempt_id,stage,lesson,evidence,
                       ROW_NUMBER() OVER (PARTITION BY project_id,role ORDER BY created DESC,rowid DESC) AS rank
                FROM agent_learning)
                WHERE rank<=12 ORDER BY project_id,role,rank"""):
                records = playbooks[row["project_id"], row["role"]]
                record = {key: row[key] for key in ("attempt_id", "stage", "lesson", "evidence")}
                if len(records) < 4 and all(item["lesson"].casefold() != record["lesson"].casefold() for item in records):
                    records.append(record)
            revised = 0
            for (project, role), lessons in playbooks.items():
                old = db.execute("SELECT lessons FROM agent_playbooks WHERE project_id=? AND role=?", (project, role)).fetchone()
                if old and [(item["stage"], item["lesson"]) for item in json.loads(old["lessons"])] == [
                    (item["stage"], item["lesson"]) for item in lessons
                ]:
                    continue
                revised += db.execute("""INSERT INTO agent_playbooks VALUES (?,?,1,?)
                    ON CONFLICT(project_id,role) DO UPDATE SET revision=agent_playbooks.revision+1,lessons=excluded.lessons
                    WHERE agent_playbooks.lessons<>excluded.lessons""", (project, role, encode(lessons))).rowcount
            result = {"at": now(), "pruned": count, "synthesized": synthesized, "playbooks_revised": revised}
            db.execute("INSERT INTO dream_runs(at,pruned,synthesized) VALUES (?,?,?)", (result["at"], count, synthesized))
            db.execute("INSERT INTO settings VALUES ('last_dream',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (encode(result),))
        return result

    def memory_health(self):
        nodes = self.rows("SELECT kind,COUNT(*) AS count FROM memory_nodes GROUP BY kind")
        return {"nodes": sum(n["count"] for n in nodes), "kinds": dict((n["kind"], n["count"]) for n in nodes),
                "agent_playbooks": self.row("SELECT COUNT(*) AS count FROM agent_playbooks")["count"],
                "links": self.row("SELECT COUNT(*) AS count FROM memory_edges")["count"],
                "applications": self.row("SELECT COUNT(*) AS count FROM memory_usage")["count"],
                "last_dream": self.setting("last_dream")}

    def dream_history(self, page):
        rows = self.rows("SELECT at,pruned,synthesized FROM dream_runs ORDER BY id DESC LIMIT 26 OFFSET ?", (page * 25,))
        return {"history": rows[:25], "more": len(rows) > 25}

    def memory_browser(self, query, page):
        records = self.rows("""SELECT n.id,n.kind,n.label,n.body,n.weight,n.last_used,
            (SELECT GROUP_CONCAT(p.name, ', ') FROM memory_projects m JOIN projects p ON p.id=m.project_id
             WHERE m.node_id=n.id) AS projects,
            (SELECT COUNT(*) FROM memory_usage u WHERE u.node_id=n.id) AS uses
            FROM memory_nodes n WHERE instr(lower(n.label || ' ' || n.body || ' ' || n.kind),lower(?)) > 0
            ORDER BY n.last_used DESC,n.id LIMIT 26 OFFSET ?""", (query, page * 25))
        for item in records[:25]:
            item["links"] = self.rows("""SELECT e.kind,n.label FROM memory_edges e JOIN memory_nodes n
                ON n.id=CASE WHEN e.source=? THEN e.target ELSE e.source END
                WHERE e.source=? OR e.target=? ORDER BY e.weight DESC,n.label LIMIT 12""", (item["id"],) * 3)
        return {"records": records[:25], "more": len(records) > 25}
