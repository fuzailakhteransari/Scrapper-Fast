from __future__ import annotations

import json
import hashlib
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from tkinter import messagebox


ROOT = Path(__file__).resolve().parent
DEFAULT_PORT = 8765
PORT_SCAN_LIMIT = 20
APP_ID = "lightning-contact-scraper"
BUILD_FILES = (
    "contact_scraper/web_app.py",
    "contact_scraper/templates/scraper.html",
    "contact_scraper/static/scraper.css",
    "contact_scraper/static/scraper.js",
)
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008


def build_signature() -> str:
    digest = hashlib.sha256()
    for relative_path in BUILD_FILES:
        path = ROOT / relative_path
        digest.update(relative_path.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def health_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/api/health"


def app_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def is_scraper_server(port: int) -> bool:
    try:
        with urllib.request.urlopen(health_url(port), timeout=0.6) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return (
            payload.get("ok") is True
            and payload.get("app") == APP_ID
            and payload.get("build") == build_signature()
        )
    except (
        OSError,
        ValueError,
        urllib.error.URLError,
        json.JSONDecodeError,
    ):
        return False


def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def select_port() -> tuple[int, bool]:
    for port in range(DEFAULT_PORT, DEFAULT_PORT + PORT_SCAN_LIMIT):
        if is_scraper_server(port):
            return port, True
        if port_is_free(port):
            return port, False
    raise RuntimeError(
        f"No free port found between {DEFAULT_PORT} and "
        f"{DEFAULT_PORT + PORT_SCAN_LIMIT - 1}."
    )


def browser_executable() -> Path | None:
    candidates = (
        Path(os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe")),
        Path(os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe")),
        Path(os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe")),
        Path(os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe")),
    )
    return next((path for path in candidates if path.is_file()), None)


def open_app(port: int) -> None:
    if os.getenv("SCRAPER_LAUNCHER_NO_BROWSER") == "1":
        return
    url = app_url(port)
    browser = browser_executable()
    if browser:
        subprocess.Popen(
            [str(browser), f"--app={url}"],
            cwd=ROOT,
            creationflags=CREATE_NO_WINDOW,
        )
    else:
        webbrowser.open(url)


def start_server(port: int) -> subprocess.Popen:
    pythonw = ROOT / ".venv" / "Scripts" / "pythonw.exe"
    python = ROOT / ".venv" / "Scripts" / "python.exe"
    executable = pythonw if pythonw.is_file() else python
    if not executable.is_file():
        raise RuntimeError(
            "The project environment is missing.\n\n"
            "Run setup.ps1 once, then double-click the launcher again."
        )

    env = os.environ.copy()
    env["SCRAPER_UI_PORT"] = str(port)
    env["PYTHONUNBUFFERED"] = "1"
    log_dir = ROOT / "ui_data"
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout = (log_dir / "launcher-server.log").open("a", encoding="utf-8")
    stderr = (log_dir / "launcher-server-error.log").open("a", encoding="utf-8")
    try:
        process = subprocess.Popen(
            [str(executable), "-m", "contact_scraper.web_app"],
            cwd=ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS,
            close_fds=True,
        )
    finally:
        stdout.close()
        stderr.close()
    return process


def wait_until_ready(port: int, process: subprocess.Popen, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_scraper_server(port):
            return
        if process.poll() is not None:
            raise RuntimeError(
                "The scraper server stopped during startup.\n\n"
                "Check ui_data\\launcher-server-error.log for details."
            )
        time.sleep(0.2)
    process.terminate()
    raise RuntimeError(
        "The scraper server did not become ready in time.\n\n"
        "Check ui_data\\launcher-server-error.log for details."
    )


def main() -> None:
    try:
        os.chdir(ROOT)
        port, already_running = select_port()
        if not already_running:
            process = start_server(port)
            wait_until_ready(port, process)
        open_app(port)
    except Exception as exc:
        messagebox.showerror("Lightning Contact Scraper", str(exc))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
