"""Talk to the daemon over localhost, starting it in the background if needed."""
import http.client
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import launch
from .config import DATA_DIR, HOST, PORT


def _request(method, path, body=None, token=None, timeout=3.0):
    conn = http.client.HTTPConnection(HOST, PORT, timeout=timeout)
    try:
        headers = {"X-Divo": "1", "Content-Type": "application/json"}
        if token:
            headers["X-Divo-Token"] = token
        conn.request(method, path, json.dumps(body) if body is not None else None, headers)
        resp = conn.getresponse()
        return json.loads(resp.read().decode("utf-8") or "{}")
    finally:
        conn.close()


def start_daemon():
    flags = 0x00000008 | 0x00000200 | 0x08000000 if os.name == "nt" else 0  # detached, own group, no window
    subprocess.Popen(launch.service_command(), creationflags=flags, close_fds=True, start_new_session=(os.name != "nt"),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def ensure_daemon(wait=8.0):
    try:
        _request("GET", "/api/ping", timeout=0.6)
        return True
    except OSError:
        pass
    start_daemon()
    deadline = time.time() + wait
    while time.time() < deadline:
        time.sleep(0.3)
        try:
            _request("GET", "/api/ping", timeout=0.6)
            return True
        except OSError:
            pass
    return False


def send(req, source="mcp", token=None):
    """Returns the daemon's reply. Raises ConnectionError if it can't be reached or started.
    Pass `token` for sources the panel guards, like "gui"."""
    if not ensure_daemon():
        raise ConnectionError("could not start the divo-sys daemon")
    return _request("POST", "/api/cmd", {"req": req, "source": source}, token=token)


def get(path):
    if not ensure_daemon():
        raise ConnectionError("could not start the divo-sys daemon")
    return _request("GET", path)


def admin_token():
    try:
        return (DATA_DIR / ".token").read_text(encoding="utf-8").strip()
    except OSError:
        return None


def stop_daemon():
    token = admin_token()
    if not token:
        return False
    try:
        _request("POST", "/api/quit", {}, token=token)
        return True
    except OSError:
        return False
