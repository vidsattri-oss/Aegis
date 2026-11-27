"""Browser authorization for public OAuth clients on HTTPS MCP servers."""
import base64
import hashlib
import http.client
import ipaddress
import json
import re
import secrets
import socket
import threading
import time
from contextlib import closing
from urllib.parse import parse_qsl, urlencode, urlsplit

from .contracts import require
from .integrations import public_socket
from .mcp import VERSION


def https_url(value):
    require(isinstance(value, str) and len(value) <= 2000 and not re.search(r"[\s\\]", value), "Invalid MCP OAuth URL")
    location = urlsplit(value)
    require(location.scheme == "https" and location.hostname and location.port in (None, 443)
            and not (location.username or location.password or location.fragment), "MCP OAuth endpoints must use public HTTPS")
    return location


def exchange(url, *, data=None, form=False, allowed=(200,)):
    location = https_url(url)
    path = location.path or "/"
    if location.query:
        path += "?" + location.query
    headers = {"Accept": "application/json, text/event-stream"}
    body = None
    if data is not None:
        body = (urlencode(data) if form else json.dumps(data)).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
    with closing(http.client.HTTPSConnection(location.hostname, timeout=10)) as client:
        client._create_connection = public_socket
        client.request("POST" if body is not None else "GET", path, body=body, headers=headers)
        with client.getresponse() as response:
            require(response.status in allowed, f"MCP OAuth endpoint returned HTTP {response.status}")
            if response.status == 401:
                return response.getheader("WWW-Authenticate", "")
            if response.status in (202, 204, 404) or 401 in allowed:
                return None
            raw = response.read(64_001)
            require(len(raw) <= 64_000, "MCP OAuth response exceeded its size limit")
            value = json.loads(raw)
            require(isinstance(value, dict), "Invalid MCP OAuth response")
            return value


def discover(resource):
    location = https_url(resource)
    origin = f"https://{location.netloc}"
    challenge = exchange(resource, data={"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": {
        "protocolVersion": VERSION, "capabilities": {}, "clientInfo": {"name": "Aegis", "version": "0.1.0"}}},
        allowed=(200, 202, 204, 401))
    advertised = re.search(r'\bresource_metadata="([^"\r\n]+)"', challenge or "")
    if advertised:
        protected = exchange(advertised[1])
    else:
        route = "/.well-known/oauth-protected-resource"
        protected = exchange(origin + route + location.path.rstrip("/"), allowed=(200, 404))
        if protected is None and location.path.rstrip("/"):
            protected = exchange(origin + route, allowed=(200, 404))
    require(protected and isinstance(protected.get("resource"), str)
            and protected["resource"].rstrip("/") == resource.rstrip("/"),
            "The MCP server must advertise OAuth metadata for its resource URL")
    issuers = protected.get("authorization_servers")
    require(isinstance(issuers, list) and issuers, "MCP OAuth metadata is missing an authorization server")
    issuer = issuers[0]
    location = https_url(issuer)
    require(not location.query, "Invalid OAuth issuer")
    origin = f"https://{location.netloc}"
    suffix = location.path.rstrip("/")
    candidates = (origin + "/.well-known/oauth-authorization-server" + suffix,
                  origin + "/.well-known/openid-configuration" + suffix,
                  origin + suffix + "/.well-known/openid-configuration")
    metadata = None
    for url in dict.fromkeys(candidates):
        metadata = exchange(url, allowed=(200, 404))
        if metadata:
            break
    require(metadata and metadata.get("issuer") == issuer, "OAuth authorization server issuer does not match its metadata")
    methods = metadata.get("code_challenge_methods_supported")
    require(isinstance(methods, list) and "S256" in methods, "The OAuth server must advertise PKCE S256")
    return metadata, challenge or ""


class MCPOAuth:
    def __init__(self, redact):
        self.redact = redact
        self.lock = threading.RLock()
        self.pending = {}
        self.tokens = {}

    @staticmethod
    def identity(server):
        settings = server["oauth"]
        return server["name"], server["url"], settings["client_id"], tuple(settings["scopes"])

    def begin(self, server, redirect):
        key = self.identity(server)
        _, resource, client_id, scopes = key
        metadata, challenge = discover(resource)
        authorize = https_url(metadata.get("authorization_endpoint"))
        addresses = socket.getaddrinfo(authorize.hostname, 443, type=socket.SOCK_STREAM)
        require(addresses and all(ipaddress.ip_address(address[4][0]).is_global for address in addresses),
                "MCP authorization must open a public HTTPS site")
        token_url = https_url(metadata.get("token_endpoint")).geturl()
        if not client_id:
            registration = metadata.get("registration_endpoint")
            require(registration, "Set oauth.client_id to a registered public client ID for this server")
            registered = exchange(registration, data={"client_name": "Aegis", "application_type": "native",
                "redirect_uris": [redirect], "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"], "token_endpoint_auth_method": "none"}, allowed=(200, 201))
            client_id = registered.get("client_id")
            require(isinstance(client_id, str) and client_id and len(client_id) <= 1000
                    and registered.get("token_endpoint_auth_method", "none") == "none", "MCP registration must support a public client")
        if not scopes and (hint := re.search(r'\bscope="([^"\r\n]+)"', challenge)):
            scopes = hint[1].split()
        verifier = secrets.token_urlsafe(64)
        state = secrets.token_urlsafe(32)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        self.redact.add(verifier)
        with self.lock:
            now = time.monotonic()
            self.pending = {id: item for id, item in self.pending.items() if item["expires"] > now and item["key"] != key}
            require(len(self.pending) < 16, "Too many MCP sign-ins are in progress")
            self.pending[state] = {"key": key, "expires": now + 300, "redirect": redirect, "client_id": client_id,
                                   "token_url": token_url, "verifier": verifier, "issuer": metadata["issuer"],
                                   "verify_issuer": metadata.get("authorization_response_iss_parameter_supported") is True}
        query = dict(parse_qsl(authorize.query)) | {"response_type": "code", "client_id": client_id,
            "redirect_uri": redirect, "resource": resource, "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256"}
        if scopes:
            query["scope"] = " ".join(scopes)
        return authorize._replace(query=urlencode(query)).geturl()

    def finish(self, query):
        state = query.get("state", [""])
        require(len(state) == 1, "Invalid MCP OAuth callback state")
        with self.lock:
            flow = self.pending.pop(state[0], None)
        require(flow and flow["expires"] > time.monotonic(), "MCP sign-in expired. Start again in Extensions.")
        issuer = query.get("iss")
        require((not issuer and not flow["verify_issuer"]) or issuer == [flow["issuer"]], "Unexpected MCP OAuth issuer")
        codes = query.get("code", [])
        require("error" not in query and len(codes) == 1 and 0 < len(codes[0]) <= 8192,
                "MCP authorization was cancelled or did not return a valid code")
        self.redact.add(codes[0])
        data = {"grant_type": "authorization_code", "code": codes[0], "redirect_uri": flow["redirect"],
                "client_id": flow["client_id"], "code_verifier": flow["verifier"], "resource": flow["key"][1]}
        token = self.token_response(flow, data)
        with self.lock:
            self.tokens[flow["key"]] = token

    def token_response(self, flow, data):
        response = exchange(flow["token_url"], data=data, form=True)
        access = response.get("access_token")
        refresh = response.get("refresh_token", flow.get("refresh"))
        require(str(response.get("token_type", "")).lower() == "bearer" and isinstance(access, str)
                and re.fullmatch(r"[!-~]{1,8192}", access), "The MCP OAuth server did not return a valid bearer token")
        require(refresh is None or isinstance(refresh, str) and 0 < len(refresh) <= 8192,
                "Invalid MCP OAuth refresh credential")
        self.redact.add(access)
        self.redact.add(refresh)
        lifetime = response.get("expires_in", 3600)
        require(type(lifetime) is int and lifetime > 0, "Invalid MCP OAuth token expiry")
        return {"access": access, "refresh": refresh, "expires": time.monotonic() + lifetime,
                "client_id": flow["client_id"], "token_url": flow["token_url"]}

    def connected(self, server):
        with self.lock:
            token = self.tokens.get(self.identity(server))
            return bool(token and (token["expires"] > time.monotonic() or token["refresh"]))

    def access_token(self, server):
        key = self.identity(server)
        with self.lock:
            token = self.tokens.get(key)
            require(token, "Connect this MCP server in Extensions")
            if token["expires"] <= time.monotonic() + 10 and token["refresh"]:
                try:
                    token = self.token_response(token, {"grant_type": "refresh_token", "refresh_token": token["refresh"],
                                                         "client_id": token["client_id"], "resource": key[1]})
                except (ValueError, RuntimeError, OSError, http.client.HTTPException):
                    self.tokens.pop(key, None)
                    raise ValueError("MCP sign-in could not be refreshed. Reconnect it in Extensions.") from None
                self.tokens[key] = token
            require(token["expires"] > time.monotonic(), "MCP sign-in expired. Reconnect it in Extensions.")
            return token["access"]

    def disconnect(self, server):
        key = self.identity(server)
        with self.lock:
            self.tokens.pop(key, None)
            self.pending = {id: item for id, item in self.pending.items() if item["key"] != key}
