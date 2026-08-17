"""LoopToToneConvertor web server: serves the dashboard, engine API routes,
download of finished Tone.js JSON files and folder opening. Pure stdlib."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web.engine import Engine  # noqa: E402

STATIC = ROOT / "web"
PORT = 8002
ENGINE = Engine()


class Handler(BaseHTTPRequestHandler):
    server_version = "LoopToToneWeb/1.0"

    def log_message(self, fmt, *args):
        pass

    # ---- helpers ---------------------------------------------------------

    def _send_bytes(self, data: bytes, ctype: str, status: int = 200, disposition: str | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if disposition:
            self.send_header("Content-Disposition", disposition)
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, obj, status: int = 200):
        self._send_bytes(json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json", status)

    def _read_json(self, size: int = 64 * 1024) -> dict:
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        return json.loads(raw.decode("utf-8")) if raw else {}

    def _send_static(self, name: str):
        p = STATIC / name
        if not p.is_file():
            return self._send_bytes(b"not found", "text/plain", 404)
        self._send_bytes(p.read_bytes(), mimetypes.guess_type(name)[0] or "application/octet-stream")

    def _source_dir(self) -> Path:
        return Path(ENGINE.config["source"])

    def _output_dir(self) -> Path:
        return Path(ENGINE.config["output"])

    # ---- routes -----------------------------------------------------------

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            return self._send_static("index.html")
        if path in ("/style.css", "/app.js", "/favicon.ico"):
            return self._send_static(path[1:])
        if path == "/api/state":
            return self._send_json(ENGINE.view())
        if path == "/api/appcheck-token":
            return self._handle_appcheck_token()
        if path == "/api/files":
            return self._send_json(self._list_files())
        if path == "/api/open-source":
            return self._open_dir(self._source_dir())
        if path == "/api/open-output":
            return self._open_dir(self._output_dir())
        if path == "/api/open-midi":
            return self._open_dir(self._output_dir() / "midi")
        if path == "/api/open-file":
            name = self.path.split("?", 1)[1] if "?" in self.path else ""
            return self._open_file(name)
        if path.startswith("/api/file/"):
            return self._download(path[len("/api/file/"):])
        return self._send_bytes(b"not found", "text/plain", 404)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/control":
            body = self._read_json()
            action = body.get("action")
            if action == "start":
                ENGINE.start()
                return self._send_json({"ok": True})
            if action == "pause":
                ENGINE.pause()
                return self._send_json({"ok": True})
            if action == "resume":
                ENGINE.resume()
                return self._send_json({"ok": True})
            if action == "stop":
                ENGINE.stop()
                return self._send_json({"ok": True})
            if action == "rescan":
                ENGINE.scan_source()
                ENGINE.save_state()
                return self._send_json({"ok": True})
            if action == "retry-failed":
                ENGINE.requeue_failed()
                ENGINE.save_state()
                return self._send_json({"ok": True})
            if action == "reset":
                ENGINE.reset_state()
                ENGINE.save_state()
                return self._send_json({"ok": True})
            if action == "check-api":
                return self._send_json({"ok": ENGINE.check_api(force=True)})
            return self._send_json({"ok": False, "error": "unknown action"}, 400)
        if path == "/api/appcheck-token":
            return self._handle_appcheck_token()
        if path == "/api/settings":
            return self._handle_settings()
        return self._send_bytes(b"not found", "text/plain", 404)

    # ---- handlers -----------------------------------------------------------

    def _handle_appcheck_token(self):
        if self.command == "GET":
            return self._send_json(ENGINE.appcheck_status())
        body = self._read_json(size=256 * 1024)
        token = body.get("token")
        if not token or not isinstance(token, str) or len(token) < 80:
            return self._send_json({"ok": False, "error": "invalid token"}, 400)
        ENGINE.set_appcheck_token(token, source=body.get("delivered_from", "extension"))
        return self._send_json({"ok": True, "status": ENGINE.appcheck_status()})

    def _handle_settings(self):
        body = self._read_json()
        cfg = ENGINE.config
        for key, val in body.items():
            if key not in cfg:
                continue
            if key in ("source", "output"):
                val = str(val).strip()
                if not val:
                    continue
                p = Path(val)
                try:
                    p.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    return self._send_json({"ok": False, "error": str(exc)}, 400)
                cfg[key] = str(p)
            elif key == "call_interval_sec":
                try:
                    cfg[key] = max(5, min(600, int(val)))
                except (TypeError, ValueError):
                    pass
            elif key in ("detect_key",):
                cfg[key] = bool(val)
            elif key in ("beat_model", "chord_model", "api_base"):
                cfg[key] = str(val).strip()
        ENGINE.save_config()
        return self._send_json({"ok": True, **cfg})

    def _list_files(self) -> dict:
        out = self._output_dir()
        items = []
        if out.is_dir():
            for p in sorted(out.glob("*.json")):
                items.append({
                    "name": p.name,
                    "size": p.stat().st_size,
                    "modified": p.stat().st_mtime,
                })
        return {"files": items}

    def _resolve_out(self, name: str) -> Path | None:
        for base in (self._output_dir(), self._output_dir() / "midi"):
            p = base / Path(name).name
            if p.is_file():
                return p
        return None

    def _download(self, name: str):
        p = self._resolve_out(name)
        if p is None:
            return self._send_json({"ok": False, "error": "file not found"}, 404)
        self._send_bytes(
            p.read_bytes(),
            "audio/midi" if p.suffix.lower() == ".mid" else "application/json",
            disposition=f'attachment; filename="{p.name}"',
        )

    def _open_dir(self, p: Path):
        try:
            os.startfile(str(p))
            return self._send_json({"ok": True, "path": str(p)})
        except OSError as exc:
            return self._send_json({"ok": False, "error": str(exc)}, 500)

    def _open_file(self, raw: str | None):
        if not raw:
            return self._send_json({"ok": False, "error": "missing file param"}, 400)
        name = raw.split("=", 1)[1] if "=" in raw else raw
        p = self._resolve_out(name)
        if p is None:
            return self._send_json({"ok": False, "error": "file not found"}, 404)
        try:
            os.startfile(str(p))
            return self._send_json({"ok": True, "path": str(p)})
        except OSError as exc:
            return self._send_json({"ok": False, "error": str(exc)}, 500)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--browser", action="store_true", help="open dashboard tab (UI moved to the extension side panel)")
    args = ap.parse_args()
    ENGINE.log("info", f"LoopToToneConvertor starting on http://localhost:{args.port}")
    ENGINE.log("info", f"source={ENGINE.config['source']} output={ENGINE.config['output']}")
    ENGINE.log("info", "clean start: dropping previous run state")
    ENGINE.reset_state()
    ENGINE.save_state()
    if ENGINE.config.get("autostart"):
        ENGINE.start()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://localhost:{args.port}"
    if args.browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        ENGINE.save_state()


if __name__ == "__main__":
    main()