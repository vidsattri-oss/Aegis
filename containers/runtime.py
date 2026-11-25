"""Trusted build, test and preview entry points for generated source trees."""
import base64
import http.client
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

SOURCE = Path("/input")
WORK = Path("/work/app")
PORT = 8080
VENV_PYTHON = Path("/work/venv/bin/python")
ORIGIN = f"http://127.0.0.1:{PORT}"
LIMIT = 8_000_000


def run(args, timeout=120):
    print("Running " + " ".join(str(arg) for arg in args), flush=True)
    subprocess.run(args, cwd=WORK, check=True, timeout=timeout, stdin=subprocess.DEVNULL)


def prepare():
    shutil.copytree(SOURCE, WORK)
    config = json.loads((WORK / "harness.json").read_text())
    if config["runtime"] == "python" and (WORK / "requirements.txt").is_file():
        if (WORK / "requirements.txt").read_text().strip():
            run([sys.executable, "-m", "venv", "/work/venv"])
            run([str(VENV_PYTHON), "-m", "pip", "install", "--no-index", "--only-binary=:all:",
                 "--no-deps", "--find-links=/wheels", "-r", "requirements.txt"])
            run([str(VENV_PYTHON), "-m", "pip", "check"])
    if config["runtime"] == "node" and (WORK / "package-lock.json").is_file():
        if Path("/npm-input").is_dir():
            shutil.copytree("/npm-input", "/work/npm")
        run(["npm", "ci", "--offline", "--ignore-scripts", "--no-audit", "--no-fund", "--cache=/work/npm",
             "--userconfig=/dev/null", "--globalconfig=/dev/null"])
    return config


def app_python():
    return str(VENV_PYTHON) if VENV_PYTHON.is_file() else sys.executable


def command(config):
    if config["runtime"] == "static":
        return [sys.executable, "-m", "http.server", str(PORT), "--bind", "127.0.0.1", "--directory", str(WORK)]
    return [app_python() if config["runtime"] == "python" else "node", config["entry"]]


def start_arguments():
    return {"cwd": WORK, "env": dict(os.environ, PORT=str(PORT), HOST="127.0.0.1"),
            "stdin": subprocess.DEVNULL}


def wait_app(app):
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        if app and app.poll() is not None:
            raise RuntimeError(f"Application exited ({app.returncode}) before becoming ready")
        client = http.client.HTTPConnection("127.0.0.1", PORT, timeout=1)
        try:
            client.request("GET", "/")
            if client.getresponse().status < 500:
                return
        except OSError:
            pass
        finally:
            client.close()
        time.sleep(0.25)
    raise RuntimeError("Application did not respond on port 8080")


def build(config):
    runtime = config["runtime"]
    if runtime == "python":
        run([sys.executable, "-m", "compileall", "-q", "."])
    if runtime in ("node", "static"):
        for path in sorted(WORK.rglob("*")):
            if "node_modules" not in path.parts and path.suffix in (".js", ".mjs", ".cjs"):
                run(["node", "--check", str(path.relative_to(WORK))], timeout=10)
    if config["mode"] == "artifact":
        run(command(config), timeout=20)
    else:
        with subprocess.Popen(command(config), **start_arguments()) as app:
            try:
                wait_app(app)
                client = http.client.HTTPConnection("127.0.0.1", PORT, timeout=3)
                try:
                    client.request("GET", "/")
                    result = client.getresponse()
                    if not 200 <= result.status < 400:
                        raise RuntimeError(f"Application root returned HTTP {result.status}")
                finally:
                    client.close()
            finally:
                app.terminate()


def browser_journeys(journeys):
    from playwright.sync_api import expect, sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(service_workers="block")
        context.route("**/*", lambda route: route.continue_()
                      if urlsplit(route.request.url).netloc == f"127.0.0.1:{PORT}" else route.abort())
        page = context.new_page()
        page.set_default_timeout(4500)
        for journey in journeys:
            print("Browser journey: " + journey["name"], flush=True)
            for step in journey["steps"]:
                action = step["action"]
                if action == "goto":
                    response = page.goto(ORIGIN + step["path"], wait_until="domcontentloaded", timeout=9000)
                    if not response or not response.ok:
                        raise RuntimeError("Browser navigation failed")
                elif action == "title":
                    expect(page).to_have_title(step["value"])
                else:
                    locator = page.locator(step["selector"])
                    if action == "visible":
                        expect(locator).to_be_visible()
                    elif action == "text":
                        expect(locator).to_contain_text(step["value"])
                    elif action == "click":
                        locator.click()
                    elif action == "fill":
                        locator.fill(step["value"])
        browser.close()


def qa(config):
    build(config)
    if config["runtime"] == "python":
        run([app_python(), "-c", "import sys, unittest; "
             "suite = unittest.defaultTestLoader.discover('.'); "
             "sys.exit(2) if not suite.countTestCases() else None; "
             "sys.exit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())"])
    if config["runtime"] == "node":
        run(["node", "--test"])
    if config["mode"] == "web":
        suites = json.loads(Path("/checks/journeys.json").read_text())
        with subprocess.Popen(command(config), **start_arguments()) as app:
            try:
                wait_app(app)
                browser_journeys(suites)
            finally:
                app.terminate()
    print("All execution checks passed.", flush=True)


def relay():
    request = json.load(sys.stdin)
    connection = http.client.HTTPConnection("127.0.0.1", PORT, timeout=8)
    try:
        connection.request(request["method"], request["path"], base64.b64decode(request["body"]),
                           headers=request["headers"])
        response = connection.getresponse()
        body = response.read(LIMIT + 1)
        if len(body) > LIMIT:
            raise RuntimeError("Preview response exceeded size limit")
        headers = [[key, value] for key, value in response.getheaders() if key.lower() in {
            "content-type", "cache-control", "location", "etag", "last-modified", "content-encoding",
        }]
        print(json.dumps({"status": response.status, "headers": headers, "body": base64.b64encode(body).decode()}))
    finally:
        connection.close()


if __name__ == "__main__":
    action = sys.argv[1]
    if action == "relay":
        relay()
    else:
        configuration = prepare()
        if action == "qa":
            qa(configuration)
        elif action == "build":
            build(configuration)
        elif action == "serve":
            arguments = start_arguments()
            os.chdir(WORK)
            executable, *argv = command(configuration)
            os.execvpe(executable, [executable, *argv], arguments["env"])
        else:
            raise ValueError("Unknown runtime action")
