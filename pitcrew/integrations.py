"""Bounded Lead-only provider, research and advisory requests."""
import http.client
import ipaddress
import json
import os
import re
import socket
import time
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from .agent_cli import AGENT_CLIS, complete_with_cli
from .config import environment
from .contracts import INJECT, MAX_BYTES, require
from .usage import MeteredModelError, Usage, usd_amount

DEFAULT_HOSTS = (
    "developer.mozilla.org", "docs.python.org", "nodejs.org", "playwright.dev", "web.dev",
    "learn.microsoft.com", "docs.docker.com", "www.sqlite.org", "www.w3.org", "owasp.org",
    "pypi.org", "registry.npmjs.org", "advisories.gitlab.com",
)
OSV = "https://api.osv.dev/v1/querybatch"
NAME = re.compile(r"(?:@[a-z0-9._-]+/)?[a-z0-9][a-z0-9_.-]{0,125}\Z", re.I)
PYTHON_PIN = re.compile(r"([A-Za-z0-9][A-Za-z0-9_.-]{0,125})==([0-9][A-Za-z0-9_.+!-]{0,79})\Z")
VERSION = re.compile(r"[0-9][A-Za-z0-9.+-]{0,79}\Z")


def public_socket(address, timeout, source_address=None):
    host, port = address
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    require(addresses and all(ipaddress.ip_address(addr[4][0]).is_global for addr in addresses),
            "Research hosts must resolve only to public addresses")
    expires = time.monotonic() + timeout
    last_error = None
    for family, kind, protocol, _, target in addresses:
        with_socket = socket.socket(family, kind, protocol)
        try:
            remaining = expires - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Research connection timed out")
            with_socket.settimeout(remaining)
            if source_address:
                with_socket.bind(source_address)
            with_socket.connect(target)
            return with_socket
        except OSError as exc:
            with_socket.close()
            last_error = exc
    raise last_error or OSError("No research address available")


def request(url, *, payload=None, headers=None, timeout=30, limit=MAX_BYTES * 2, public=False):
    location = urlsplit(url)
    require(location.hostname and not location.username and not location.password and not location.fragment,
            "Invalid endpoint URL")
    loopback = location.hostname in ("localhost", "127.0.0.1", "::1")
    require(location.scheme == "https" or (not public and location.scheme == "http" and loopback),
            "External endpoints require HTTPS")
    if public:
        require(location.port in (None, 443), "Research requires HTTPS on port 443")
    cls = http.client.HTTPSConnection if location.scheme == "https" else http.client.HTTPConnection
    deadline = time.monotonic() + timeout
    conn = cls(location.hostname, location.port, timeout=timeout)
    if public:
        conn._create_connection = public_socket  # Use the validated IP with TLS verification for the hostname.
    body = None if payload is None else json.dumps(payload).encode()
    target = (location.path or "/") + ("?" + location.query if location.query else "")
    try:
        conn.connect()
        conn.request("GET" if body is None else "POST", target, body=body,
                     headers={"Accept-Encoding": "identity", "User-Agent": "PitCrewHarness/0.1",
                              **({"Content-Type": "application/json"} if body is not None else {}),
                              **(headers or {})})
        sock = conn.sock
        response = conn.getresponse()
        if not 200 <= response.status < 300:
            if (response.status == 429 and location.hostname.endswith((".services.ai.azure.com", ".openai.azure.com"))
                    and response.getheader("x-ms-budget-cause") == "budget_exceeded"):
                raise MeteredModelError("Microsoft Foundry blocked the request at its vendor budget. Review the Foundry cost control before resuming.",
                                        Usage((0, 0), 0, "vendor_blocked"), budget_reached=True)
            raise RuntimeError(f"{location.hostname} returned HTTP {response.status}")
        chunks = bytearray()
        while len(chunks) <= limit:
            if response.isclosed():
                return bytes(chunks), response.getheader("Content-Type", "")
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"{location.hostname} response timed out")
            sock.settimeout(left)
            part = response.read1(min(64_000, limit - len(chunks) + 1))
            if not part:
                return bytes(chunks), response.getheader("Content-Type", "")
            chunks.extend(part)
        raise ValueError(f"{location.hostname} response exceeded size limit")
    finally:
        conn.close()


def provider_settings(raw, *, require_model=True):
    require(isinstance(raw, dict), "Provider settings must be an object")
    kind = raw.get("kind")
    require(kind in ("demo", "foundry", "azure-gateway", "compatible", "local", *AGENT_CLIS), "Choose a supported provider")
    settings = {"kind": kind}
    if kind in AGENT_CLIS:
        model = raw.get("model") or ""
        require(isinstance(model, str) and len(model.strip()) <= 160 and not any(ord(char) < 32 for char in model),
                "Invalid model name")
        settings["model"] = model.strip()
    elif kind != "demo":
        url, model = raw.get("endpoint"), raw.get("model") or ""
        require(isinstance(url, str) and 0 < len(url) <= 500, "Provider endpoint is required")
        require(isinstance(model, str) and len(model.strip()) <= 160, "Invalid deployment or model name")
        parsed = urlsplit(url)
        if kind == "local":
            require(parsed.hostname in ("localhost", "127.0.0.1", "::1"),
                    "A local model must use a loopback URL on the workspace host")
        require(parsed.hostname and not any((parsed.username, parsed.password, parsed.query, parsed.fragment))
                and (parsed.scheme == "https" or (parsed.scheme == "http" and parsed.hostname in
                     ("localhost", "127.0.0.1", "::1"))), "Use a HTTPS base URL or loopback HTTP")
        require(not re.search(r"[\s?#\\]", url), "Endpoint URL must omit whitespace, query strings, and fragments")
        base_url = url.rstrip("/")
        if kind == "azure-gateway":
            require(parsed.scheme == "https" and parsed.path.strip("/"), "Enter the HTTPS Azure AI Gateway endpoint")
            base_url = base_url.removesuffix("/chat/completions")
            require(urlsplit(base_url).path.strip("/"), "Enter the gateway model route shown in Azure AI Gateway")
        else:
            require(not base_url.endswith("/chat/completions"), "Enter the provider base URL")
        if kind == "foundry":
            require(parsed.hostname.endswith((".services.ai.azure.com", ".openai.azure.com")),
                    "Use a Microsoft Foundry resource or project hostname")
            path = parsed.path.rstrip("/")
            if not path or re.fullmatch(r"/api/projects/[A-Za-z0-9_-]+", path):
                base_url += "/openai/v1"
            require(base_url.endswith("/openai/v1") and parsed.scheme == "https", "Use a Foundry resource or project endpoint")
        settings.update(endpoint=base_url, model=model.strip())
        require(settings["model"] or not require_model or foundry_project_endpoint(settings),
                "Deployment or model is required")
    for key in ("input_rate", "output_rate", "compute_rate"):
        value = raw.get(key, 0)
        require(type(value) in (int, float) and 0 <= value <= 10_000, f"Invalid {key}")
        settings[key] = value
    return settings


def provider_headers(settings, key):
    if settings["kind"] == "azure-gateway":
        require(key, "Set AI_GATEWAY_API_KEY or enter an Azure AI Gateway runtime key in Provider settings")
    if not key:
        return {}
    if settings["kind"] == "foundry" and "/api/projects/" not in settings["endpoint"]:
        return {"api-key": key}
    return {"Authorization": f"Bearer {key}"}


def foundry_project_endpoint(settings):
    if settings["kind"] == "foundry":
        project = settings["endpoint"].removesuffix("/openai/v1")
        if re.fullmatch(r"/api/projects/[A-Za-z0-9_-]+", urlsplit(project).path):
            return project
    return None


def foundry_chat_support(deployment):
    capabilities = deployment.get("capabilities")
    if not isinstance(capabilities, dict):
        return None
    flags = {re.sub(r"[^a-z]", "", name.lower()): str(value).lower() for name, value in capabilities.items()}
    chat = flags.get("chatcompletion")
    if chat in ("true", "false"):
        return chat == "true"
    if any(flags.get(name) == "true" for name in ("embeddings", "imagegeneration", "texttospeech", "speechrecognition")):
        return False
    return None


def foundry_deployments(settings, key):
    project = foundry_project_endpoint(settings)
    require(project,
            "Paste the Foundry project endpoint from your project's Home page. It ends in /api/projects/<project>.")
    require(key, "Enter a Foundry project API key or Microsoft Entra access token to load deployments")
    # Entra access tokens use compact JWT format. Foundry project API keys use the api-key header.
    headers = {"Authorization": f"Bearer {key}"} if key.startswith("eyJ") and key.count(".") in (2, 4) else {"api-key": key}
    route = project + "/deployments"
    url = route + "?api-version=v1&deploymentType=ModelDeployment"
    target = urlsplit(route)
    deadline = time.monotonic() + 20
    seen = set()
    deployments = {}
    while True:
        require(urlsplit(url)._replace(query="") == target and url not in seen and len(seen) < 50,
                "Foundry returned an invalid deployment continuation")
        seen.add(url)
        remaining = deadline - time.monotonic()
        require(remaining > 0, "Foundry deployment discovery timed out")
        raw, _ = request(url, timeout=remaining, headers=headers)
        response = json.loads(raw)
        require(isinstance(response, dict) and isinstance(response.get("value"), list),
                "Foundry returned an invalid deployment list")
        for deployment in response["value"]:
            if not isinstance(deployment, dict) or deployment.get("type") != "ModelDeployment":
                continue
            name = deployment.get("name")
            if isinstance(name, str) and 0 < len(name) <= 160 and name.isprintable() and name.strip() == name:
                publisher = deployment.get("modelPublisher")
                deployments[name] = {"id": name, "owned_by": publisher[:160] if isinstance(publisher, str) else "",
                                     "chat": foundry_chat_support(deployment)}
        next_link = response.get("nextLink")
        if next_link is None:
            return [deployments[name] for name in sorted(deployments, key=str.casefold)]
        require(isinstance(next_link, str) and 0 < len(next_link) <= 8192 and not re.search(r"[\s\\]", next_link),
                "Foundry returned an invalid deployment continuation")
        url = urljoin(url, next_link)


def list_provider_models(settings, key):
    require(settings["kind"] != "demo", "Choose a model provider before loading models")
    require(settings["kind"] not in AGENT_CLIS, "Use the selected CLI's model picker to see available models")
    if settings["kind"] == "foundry":
        return {"kind": "foundry", "models": foundry_deployments(settings, key)}
    result, _ = request(settings["endpoint"] + "/models", timeout=20, headers=provider_headers(settings, key))
    response = json.loads(result)
    require(isinstance(response, dict) and isinstance(response.get("data"), list), "Provider returned an invalid model list")
    models = {}
    for item in response["data"]:
        if not isinstance(item, dict):
            continue
        identifier = item.get("id")
        if isinstance(identifier, str) and 0 < len(identifier) <= 160 and not any(ord(char) < 32 for char in identifier):
            owner = item.get("owned_by")
            models[identifier] = {"id": identifier, "owned_by": owner[:160] if isinstance(owner, str) else ""}
    return {"kind": settings["kind"], "models": [models[identifier] for identifier in sorted(models, key=str.casefold)]}


def reports_charge(settings):
    endpoint = urlsplit(settings.get("endpoint", ""))
    return endpoint.scheme == "https" and endpoint.hostname in ("openrouter.ai", "eu.openrouter.ai", "us.openrouter.ai")


def complete(settings, key, persona, data, timeout):
    if settings["kind"] in AGENT_CLIS:
        content, tokens = complete_with_cli(settings, persona, data, timeout)
    else:
        headers = provider_headers(settings, key)
        payload = {"model": settings["model"], "messages": [
            {"role": "system", "content": persona},
            {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
        ]}
        openrouter = reports_charge(settings)
        response, _ = request(settings["endpoint"] + "/chat/completions", timeout=timeout, headers=headers, payload=payload)
        obj = json.loads(response)
        usage = obj.get("usage") or {}
        amount, cost_source = usage.get("cost") if openrouter else None, "vendor_charge"
        if openrouter and usage.get("is_byok") is True:
            details = usage.get("cost_details") or {}
            upstream = usd_amount(details.get("upstream_inference_cost"))
            fee = usd_amount(amount)
            amount = upstream + fee if upstream is not None and fee is not None else None
            cost_source = "vendor_estimate"
        tokens = Usage((usage.get("prompt_tokens"), usage.get("completion_tokens")), amount, cost_source, obj.get("id"))
        try:
            choice = obj["choices"][0]
            if choice.get("finish_reason") not in (None, "stop"):
                raise MeteredModelError(f"Provider stopped with {choice.get('finish_reason')}", tokens)
            content = choice["message"]["content"]
        except (IndexError, KeyError, TypeError) as exc:
            raise MeteredModelError("Provider returned no completion", tokens) from exc
    if not isinstance(content, str):
        raise MeteredModelError("Provider returned no text", tokens)
    cleaned = content.strip()
    if cleaned.startswith("```") and cleaned.endswith("```"):
        cleaned = cleaned.split("\n", 1)[-1][:-3].strip()
    return cleaned, tokens


class PageText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "template", "svg"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "template", "svg"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if self.hidden == 0 and not INJECT.search(data):
            self.parts.append(data.strip())


def fetch_source(url, hosts=DEFAULT_HOSTS, timeout=12, topic=""):
    location = urlsplit(url)
    require(url.startswith("https://") and location.hostname in hosts, "Research host is outside its allowlist")
    data, media = request(url, timeout=timeout, limit=1_500_000, public=True)
    require("html" in media or "text/" in media or "json" in media, "Unsupported research content type")
    source = data.decode("utf-8", "replace")
    if "html" in media:
        parser = PageText()
        parser.feed(source)
        source = " ".join(parser.parts)
    cleaned = " ".join(source.split())
    require(not INJECT.search(cleaned), "Suspicious source content was rejected")
    terms = set(re.findall(r"[a-zA-Z]{5,}", topic.lower())) - {"build", "create", "research", "current", "should"}
    chunks = [cleaned[i:i + 850] for i in range(0, len(cleaned), 850)]
    ranked = sorted(enumerate(chunks), key=lambda item: (-len(terms & set(re.findall(r"[a-z]{5,}", item[1].lower()))),
                                                        item[0]))
    selected = sorted(ranked[:5])
    excerpt = " … ".join(chunk for _, chunk in selected).strip()
    require(len(excerpt) >= 80, "Source has insufficient usable content")
    return {"url": url, "excerpt": excerpt}


def packages(files):
    deps = set()
    python = files.get("requirements.txt")
    if python is not None:
        for line in python.splitlines():
            line = line.partition("#")[0].strip()
            if line:
                pin = PYTHON_PIN.fullmatch(line)
                require(pin, "Python dependencies require exact name==version pins")
                deps.add(("PyPI", *pin.groups()))
    package = files.get("package.json")
    lock = files.get("package-lock.json")
    if package is not None:
        meta = json.loads(package)
        require(isinstance(meta, dict), "Invalid package.json")
        require(not meta.get("workspaces"), "npm workspaces are unsupported in generated apps")
        require(not meta.get("optionalDependencies"), "Root optional dependencies are unsupported")
        require(not meta.get("overrides"), "npm dependency overrides are unsupported")
        if any(meta.get(key) for key in ("dependencies", "devDependencies", "peerDependencies")):
            require(lock is not None, "npm dependencies require package-lock.json")
    if lock is not None:
        require(package is not None, "package-lock.json requires package.json")
        data = json.loads(lock)
        require(isinstance(data, dict) and data.get("lockfileVersion") in (2, 3)
                and isinstance(data.get("packages"), dict) and "" in data["packages"],
                "Expected an npm v2/v3 package lock")
        for path, item in data["packages"].items():
            if not path:
                continue
            name = path.rsplit("node_modules/", 1)[-1]
            version = item.get("version", "") if isinstance(item, dict) else ""
            require("node_modules/" in path and NAME.fullmatch(name) and VERSION.fullmatch(version)
                    and not item.get("link") and not item.get("inBundle"),
                    "npm lock contains an unsupported dependency")
            require(item.get("resolved", "").startswith("https://registry.npmjs.org/")
                    and item.get("integrity", "").startswith(("sha512-", "sha256-")),
                    "npm dependencies require registry URLs and content integrity")
            deps.add(("npm", name, version))
    require(len(deps) <= 400, "Too many dependencies to audit")
    return sorted(deps)


def advisories(deps, timeout):
    if not deps:
        return []
    queries = []
    for ecosystem, name, version in deps:
        require(ecosystem in ("npm", "PyPI") and NAME.fullmatch(name) and VERSION.fullmatch(version),
                "Invalid dependency coordinates")
        queries.append({"package": {"ecosystem": ecosystem, "name": name}, "version": version})
    raw, _ = request(OSV, payload={"queries": queries}, timeout=timeout, public=True)
    results = json.loads(raw)["results"]
    require(isinstance(results, list) and len(results) == len(deps), "Incomplete advisory response")
    found = []
    for dep, result in zip(deps, results):
        for item in result.get("vulns", []):
            found.append({"package": dep[1], "version": dep[2], "id": item["id"]})
        require(not result.get("next_page_token") or result.get("vulns"), "Unverified advisory page")
    return found


def environment_provider():
    return environment("PITCREW_PROVIDER") or ("azure-gateway" if os.environ.get("AI_GATEWAY_CHAT_URL") else None)


def provider_key(kind):
    if kind in AGENT_CLIS:
        return ""
    if kind == "azure-gateway" and (key := os.environ.get("AI_GATEWAY_API_KEY")):
        return key
    return environment("PITCREW_API_KEY", "")


def initial_provider(*, require_model=True):
    kind = environment_provider() or "demo"
    endpoint, model = environment("PITCREW_ENDPOINT"), environment("PITCREW_MODEL")
    if kind == "azure-gateway":
        endpoint = endpoint or os.environ.get("AI_GATEWAY_CHAT_URL")
        model = model or os.environ.get("AI_GATEWAY_MODEL")
    return provider_settings({"kind": kind, "endpoint": endpoint, "model": model}, require_model=require_model)
