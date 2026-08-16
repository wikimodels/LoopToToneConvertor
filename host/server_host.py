"""Native messaging host for LoopToToneConvertor.

Chrome connects in stdio mode; the extension sends {} and this host makes sure
the engine (web/server.py) is running on http://127.0.0.1:8002, starting it
detached if needed, then replies with {"ok": true, "started": bool, "pid": ...}.

Registered per-user via install_host.cmd (HKCU, no admin required).
"""

from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HOST_NAME = "com.looptotone.server"
ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "web" / "server.py"
LOG = ROOT / "server.log"
STATE_URL = "http://127.0.0.1:8002/api/state"
READ = sys.stdin.buffer
WRITE = sys.stdout.buffer


def _recv() -> dict:
    raw = READ.read(4)
    if len(raw) != 4:
        return {}
    (length,) = struct.unpack("<I", raw)
    payload = READ.read(length)
    try:
        return json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}


def _send(obj: dict):
    payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    WRITE.write(struct.pack("<I", len(payload)) + payload)
    WRITE.flush()


def engine_alive() -> bool:
    try:
        with urllib.request.urlopen(STATE_URL, timeout=1.5) as r:
            return r.status == 200
    except OSError:
        return False


def start_engine() -> int | None:
    if sys.version_info >= (3, 8):
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        raise RuntimeError("Python 3.8+ required")
    python = sys.executable or "python"
    logf = open(LOG, "ab")
    proc = subprocess.Popen(
        [python, str(SERVER)],
        cwd=str(ROOT),
        stdin=subprocess.DEVNULL,
        stdout=logf,
        stderr=subprocess.STDOUT,
        close_fds=True,
        creationflags=creationflags,
    )
    return proc.pid if proc else None


def wait_ready(timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if engine_alive():
            return True
        time.sleep(0.5)
    return engine_alive()


def main():
    _recv()
    if engine_alive():
        _send({"ok": True, "started": False})
        return
    pid = None
    try:
        pid = start_engine()
    except OSError as exc:
        _send({"ok": False, "error": f"cannot start engine: {exc}"})
        return
    if wait_ready():
        _send({"ok": True, "started": True, "pid": pid})
    else:
        _send({"ok": False, "error": "engine did not start in time, see server.log", "pid": pid})


if __name__ == "__main__":
    if os.name == "nt":
        import msvcrt
        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    main()