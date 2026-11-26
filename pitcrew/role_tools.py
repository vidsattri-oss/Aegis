"""Fixed, offline tools available to each role's container."""
import ast
import hashlib
import html
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from .contracts import MAX_BYTES, STAGES, manifest, require, strings, validate

FRAME_LIMIT = MAX_BYTES * 6
ROLE_TOOLS = {
    "lead": ("plan_matrix", "Index stage assignments and acceptance criteria from the draft plan."),
    "research": ("source_index", "Index Lead-verified citations by source and check the citation set."),
    "architecture": ("runtime_matrix", "Compare the draft architecture with supported runtime and mode combinations."),
    "design": ("wireframe", "Render the draft wireframe as standalone, accessible HTML."),
    "development": ("source_check", "Check draft source syntax and report file size, imports, and identical files."),
    "qa": ("test_matrix", "Compare the draft test plan with the approved acceptance criteria."),
    "deploy": ("release_manifest", "Hash the approved release files and identify the runtime entry point."),
    "review": ("evidence_check", "Check that the Lead supplied a passing contract and required execution evidence."),
    "specialist": ("specialist_report", "Check the delegated specialist report format."),
}


def file_manifest(files):
    config = manifest(files)
    entries = [
        {"path": path, "bytes": len(content.encode()), "sha256": hashlib.sha256(content.encode()).hexdigest()}
        for path, content in sorted(files.items())
    ]
    return {"runtime": config["runtime"], "mode": config["mode"], "entry": config.get("entry", "index.html"),
            "files": entries, "bytes": sum(item["bytes"] for item in entries)}


def check_source(files):
    report = file_manifest(files)
    grouped = {}
    imports = {}
    syntax = []
    with tempfile.TemporaryDirectory(prefix="source-check-", dir="/work" if Path("/work").is_dir() else None) as directory:
        root = Path(directory)
        for path, text in files.items():
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        for item in report["files"]:
            path = item["path"]
            grouped.setdefault(item["sha256"], []).append(path)
            if path.endswith(".py"):
                tree = ast.parse(files[path], filename=path)
                imports[path] = sorted({alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                                        for alias in node.names} |
                                       {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
                                        and node.module})
                syntax.append(path)
            elif path.endswith((".js", ".mjs", ".cjs")):
                check = subprocess.run(["node", "--check", path], cwd=root, capture_output=True, text=True,
                                       errors="replace", stdin=subprocess.DEVNULL, timeout=8)
                require(check.returncode == 0, f"JavaScript syntax check failed for {path}: {check.stderr[-1200:]}")
                syntax.append(path)
    return {**report, "syntax_checked": syntax, "python_imports": imports,
            "identical_files": [paths for paths in grouped.values() if len(paths) > 1]}


def invoke(role, tool, arguments):
    require(role in ROLE_TOOLS and isinstance(arguments, dict), "Invalid role tool request")
    draft = arguments.get("draft", {})
    require(isinstance(draft, dict), "Tool draft must be an object")
    if tool == "submit":
        validate(role, draft)
        return draft
    require(tool == ROLE_TOOLS[role][0], "Tool is outside this role's allowlist")

    if role == "lead":
        validate(role, draft)
        return {"stages": [{"stage": stage, "assignment": draft["stages"][stage]} for stage in STAGES],
                "criteria": [item["id"] for item in draft["criteria"]]}
    if role == "research":
        sources = arguments.get("sources", [])
        require(isinstance(sources, list) and len(sources) <= 12, "Invalid brokered sources")
        indexed = [{"url": source["url"], "host": urlsplit(source["url"]).hostname,
                    "characters": len(source["excerpt"])}
                   for source in sources if isinstance(source, dict) and "url" in source and "excerpt" in source]
        citations = strings(draft, "sources", minimum=0) if "sources" in draft else []
        require(set(citations) <= {item["url"] for item in indexed}, "Citation lacks Lead-verified evidence")
        return {"sources": indexed, "citations": citations}
    if role == "architecture":
        choices = {"static": ["web"], "python": ["web", "artifact"], "node": ["web", "artifact"]}
        if draft:
            validate(role, draft)
            require(draft["mode"] in choices[draft["runtime"]], "Runtime cannot deliver the selected mode")
        return {"runtime_modes": choices, "web_binding": "127.0.0.1:8080",
                "entry_required": ["python", "node"]}
    if role == "design":
        screens = strings(draft, "wireframes")
        sections = "".join(f"<li>{html.escape(screen)}</li>" for screen in screens)
        wireframe = ('<!doctype html><html lang="en"><meta charset="utf-8"><title>Wireframe</title>'
                     f"<main><h1>Proposed layout</h1><ol>{sections}</ol></main></html>")
        return {"screens": len(screens), "wireframe_html": wireframe,
                "accessibility": strings(draft, "accessibility")}
    if role == "development":
        return check_source(draft.get("files"))
    if role == "qa":
        validate(role, draft)
        expected = arguments.get("criteria", [])
        actual = [item["criterion"] for item in draft["checks"]]
        require(isinstance(expected, list) and len(expected) == len(actual) and set(expected) == set(actual),
                "Test matrix must cover each approved criterion once")
        return {"criteria": actual, "checks": draft["checks"],
                "journeys": [{"name": journey["name"], "steps": len(journey["steps"]),
                              "assertions": sum(step["action"] in {"visible", "text", "title"} for step in journey["steps"])}
                             for journey in draft["journeys"]]}
    if role == "deploy":
        return file_manifest(arguments.get("files"))
    if role == "specialist":
        validate(role, draft)
        return {"findings": len(draft["findings"]), "recommendations": len(draft["recommendations"])}

    evidence = arguments.get("evidence", {})
    stage = arguments.get("stage")
    require(isinstance(evidence, dict) and evidence.get("contract") == "passed", "Missing passing contract evidence")
    if stage in ("development", "qa", "deploy"):
        require(evidence.get("exitCode") == 0, "Missing successful sandbox execution")
    return {"reviewed_stage": stage, "contract": "passed", "execution": evidence.get("execution"),
            "evidence_keys": sorted(evidence)}
