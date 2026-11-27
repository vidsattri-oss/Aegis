"""Diagnostics for the workspace, local Docker mounts, and provider access."""
import http.client

from .config import environment

CACHES = {"PyPI": ("AEGIS_WHEELHOUSE", "/wheels"), "npm": ("AEGIS_NPM_CACHE", "/npm-input")}


def check_workspace(docker, store, provider_check=None):
    results = []

    def check(name, operation):
        try:
            results.append({"name": name, "status": "passed", "detail": operation()})
        except (OSError, ValueError, RuntimeError, http.client.HTTPException) as exc:
            results.append({"name": name, "status": "failed", "detail": store.redact(str(exc))})

    def docker_ready():
        capacity = docker.doctor()
        return f"{capacity['cpu']} CPUs and {capacity['ram']} MiB. All three role images are available."

    check("Docker engine and role images", docker_ready)
    if results[-1]["status"] == "passed":
        check("Workspace bind mount", lambda: docker.probe_mounts(store.directory) or "Read a test file inside the isolated worker.")
        mount_status = results[-1]["status"]
    else:
        mount_status = "skipped"
        results.append({"name": "Workspace bind mount", "status": mount_status, "detail": "Docker and the role images must be ready."})
    for ecosystem, (variable, _) in CACHES.items():
        if environment(variable):
            detail = ("Readable inside the worker. Package availability is checked during the offline build."
                      if mount_status == "passed" else "Configured. Check the workspace bind mount result.")
        else:
            detail = f"Set {variable} for apps that install {ecosystem} dependencies."
        results.append({"name": f"{ecosystem} dependency cache", "status": "info", "detail": detail})
    if provider_check:
        check("Model provider", lambda: f"{provider_check()['model']} answered a connectivity request.")
    else:
        results.append({"name": "Model provider", "status": "skipped", "detail": "No model request sent."})
    return {"ready": all(item["status"] != "failed" for item in results), "checks": results}
