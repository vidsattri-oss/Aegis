"""Bounded release guidance. Dependency and reviewer checks run in the Lead."""
import http.client
from urllib.parse import urlsplit

from .contracts import INJECT

SOURCES = (
    "https://genai.owasp.org/top-10-for-agentic-applications-2026/",
    "https://docs.docker.com/engine/security/",
)
FOCUS = (
    "Trace trust boundaries from browser and preview to the control plane and between containers.",
    "Check handling of untrusted inputs, authentication, authorization, secrets, and dependencies where relevant.",
    "Check the actual release source and identify any material risk still requiring an operator decision.",
)


def release_guidance(fetch, remaining, *, sample):
    guidance = []
    for url in SOURCES:
        entry = {"url": url, "status": "reference" if sample else "unavailable"}
        if not sample:
            timeout = min(5, remaining())
            try:
                excerpt = fetch(url, (urlsplit(url).hostname,), timeout, "sandbox isolation privilege agent tool risks").get("excerpt")
                if isinstance(excerpt, str) and excerpt.strip() and not INJECT.search(excerpt):
                    entry.update(status="fetched", excerpt=excerpt[:1600])
            except (OSError, ValueError, RuntimeError, http.client.HTTPException):
                pass
        guidance.append(entry)
    return guidance
