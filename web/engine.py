"""LoopToToneConvertor engine: ChordMini client with rate limiting, worker queue
and conversion of beat/chord/key results into Tone.js JSON files.

Pure Python stdlib (no third-party dependencies).
"""

from __future__ import annotations

import faulthandler
import json
import math
import os
import re
import ssl
import threading
import time
import urllib.request
import urllib.error
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state.json"

FIREBASE_KEY = "AIzaSyA9K2ksUbHcf9S01O4hOYERokw8mhIzZRw"
FIREBASE_PROJECT = "chordmini-d29f9"
FIREBASE_APP_ID = "1:191567167632:web:113d549db841800daa5815"
FIREBASE_STORAGE = "https://firebasestorage.googleapis.com/v0/b/chordmini-d29f9.firebasestorage.app"
APPCHECK_EXCHANGE = ("https://content-firebaseappcheck.googleapis.com/v1/projects/"
                     "chordmini-d29f9/apps/1:191567167632:web:113d549db841800daa5815"
                     ":exchangeRecaptchaV3Token")

DEFAULT_CONFIG = {
    "source": r"C:\Users\Vitali\Downloads\AIMusicTools\TrimmedAudio",
    "raw_dir": r"C:\Users\Vitali\Downloads\AIMusicTools\LoopConvertorRawSON",
    "output": r"C:\Users\Vitali\Downloads\AIMusicTools\LoopConvertorToneJSON",
    "api_base": "https://chordmini.me",
    "beat_model": "madmom",
    "chord_model": "chord-cnn-lstm",
    "detect_key": True,
    "call_interval_sec": 33,
    "autostart": False,
    "snap_grid": True,
}
TOKEN_FILE = ROOT / "token.json"

AUDIO_EXTS = {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac", ".opus", ".wma"}

PT_CLASSES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

DURATION_OPTIONS = [
    (2, "8n"), (3, "8n."), (4, "4n"), (6, "4n."), (8, "2n"), (12, "2n."),
    (16, "1m"), (24, "1m."), (32, "2m"), (48, "3m"), (64, "4m"),
    (96, "6m"), (128, "8m"), (256, "16m"),
]

DUR_TICKS = {
    "8n": 240, "8n.": 360, "4n": 480, "4n.": 720, "2n": 960, "2n.": 1440,
    "1m": 1920, "1m.": 2880, "2m": 3840, "3m": 5760, "4m": 7680,
    "6m": 11520, "8m": 15360, "16m": 30720,
}

MIDI_TPQ = 480

QUALITY_OFFSETS = {
    "": (0, 4, 7), "maj": (0, 4, 7), "M": (0, 4, 7), "major": (0, 4, 7),
    "m": (0, 3, 7), "min": (0, 3, 7), "minor": (0, 3, 7),
    "dim": (0, 3, 6), "aug": (0, 4, 8),
    "sus2": (0, 2, 7), "sus4": (0, 5, 7), "5": (0, 7, 12),
    "6": (0, 4, 7, 9), "m6": (0, 3, 7, 9), "7": (0, 4, 7, 10),
    "maj7": (0, 4, 7, 11), "M7": (0, 4, 7, 11), "m7": (0, 3, 7, 10),
    "mmaj7": (0, 3, 7, 11), "mM7": (0, 3, 7, 11), "dim7": (0, 3, 6, 9),
    "m7b5": (0, 3, 6, 10), "half-dim": (0, 3, 6, 10), "h": (0, 3, 6, 10),
    "9": (0, 4, 7, 10, 14), "maj9": (0, 4, 7, 11, 14), "m9": (0, 3, 7, 10, 14),
    "7sus4": (0, 5, 7, 10), "7sus2": (0, 2, 7, 10),
}


def _log_console(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class Engine:
    def __init__(self, config_path: Path = CONFIG_PATH, state_path: Path = STATE_PATH):
        self.config_path = config_path
        self.state_path = state_path
        self.lock = threading.Lock()
        self.config = self._load_config()
        self.state = self._load_state()
        self._worker = None
        self._last_call_ts = 0.0
        self._log_seq = self.state.get("log_seq", 0)
        self._api_last_check = 0.0
        self._api_ok = None
        self.lock = threading.RLock()
        threading.Thread(target=self._watchdog, daemon=True).start()

    # ------------------------------------------------------------------ watchdog

    def _watchdog(self) -> None:
        """If the engine lock is held uninterruptibly this process must die
        on its own: otherwise the panel would hang forever (buttons dead).
        When that happens we dump thread stacks (debug-stacks.txt) so the
        culprit is visible on the next restart."""
        misses = 0
        while True:
            time.sleep(20)
            ok = self.lock.acquire(timeout=2)
            if ok:
                self.lock.release()
                misses = 0
                continue
            misses += 1
            self.log("error", f"watchdog: engine lock unresponsive ({misses}/3)")
            if misses < 3:
                continue
            try:
                with open(str(ROOT / "debug-stacks.txt"), "w", encoding="utf-8") as fh:
                    faulthandler.dump_traceback(file=fh)
            except Exception:
                pass
            os._exit(1)

    # ------------------------------------------------------------------ io

    def _load_config(self) -> dict:
        if self.config_path.is_file():
            try:
                data = json.loads(self.config_path.read_text(encoding="utf-8"))
                return {**DEFAULT_CONFIG, **data}
            except Exception:
                pass
        return dict(DEFAULT_CONFIG)

    def save_config(self) -> None:
        self.config_path.write_text(
            json.dumps(self.config, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _load_state(self) -> dict:
        if self.state_path.is_file():
            try:
                return json.loads(self.state_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"files": {}, "order": [], "running": False, "paused": False,
                "started_at": None, "finished_at": None, "log_seq": 0}

    # --------------------------------------------------------------- appcheck

    @staticmethod
    def _jwt_claims(token: str) -> dict | None:
        try:
            import base64
            seg = token.split(".")[1]
            seg += "=" * (-len(seg) % 4)
            return json.loads(base64.urlsafe_b64decode(seg))
        except Exception:
            return None

    def set_appcheck_token(self, token: str, source: str = "extension") -> None:
        claims = self._jwt_claims(token)
        exp = float(claims.get("exp") or 0) if claims else 0
        entry = {
            "token": token,
            "source": source,
            "received_at": time.time(),
            "expires_at": exp,
        }
        tmp = TOKEN_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(entry), encoding="utf-8")
        os.replace(tmp, TOKEN_FILE)
        if exp:
            self.log("info", f"App Check token updated (expires {time.strftime('%Y-%m-%d %H:%M', time.gmtime(exp))})")
        else:
            self.log("info", "App Check token updated (expiry unknown)")

    def _load_token(self) -> dict | None:
        try:
            return json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
        except Exception:
            return None

    def appcheck_status(self) -> dict:
        entry = self._load_token()
        if not entry or not entry.get("token"):
            return {"present": False, "error": "no token"}
        exp = float(entry.get("expires_at") or 0)
        return {
            "present": True,
            "source": entry.get("source"),
            "expires_at": exp,
            "expires_in_sec": max(0, exp - time.time()) if exp else None,
            "updated_at": entry.get("received_at"),
        }

    def appcheck_header(self, required: bool = True) -> str:
        entry = self._load_token()
        token = (entry or {}).get("token") or ""
        if token and float(entry.get("expires_at") or 0) and \
                float(entry.get("expires_at") or 0) < time.time() + 600:
            self.log("warn", "App Check token expired or expiring soon; open chordmini.me "
                             "with the browser extension to refresh")
        if not token and required:
            raise RuntimeError(
                "No App Check token. Open https://chordmini.me with the LoopToTone "
                "extension installed, then retry."
            )
        return token

    def save_state(self) -> None:
        with self.lock:
            payload = {
                "files": self.state["files"],
                "order": self.state["order"],
                "running": self.state["running"],
                "paused": self.state["paused"],
                "started_at": self.state["started_at"],
                "finished_at": self.state["finished_at"],
                "log_seq": self._log_seq,
            }
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def log(self, level: str, msg: str) -> None:
        self._log_seq += 1
        entry = {"n": self._log_seq, "t": time.time(), "level": level, "msg": msg}
        with self.lock:
            self.state.setdefault("log", []).append(entry)
            log = self.state["log"]
            if len(log) > 2000:
                del log[: len(log) - 2000]
        _log_console(msg)

    # ------------------------------------------------------------------ scan

    def scan_source(self) -> None:
        src = Path(self.config["source"])
        self.log("info", f"Scanning {src}")
        if not src.is_dir():
            self.log("error", f"Source directory does not exist: {src}")
            return
        names = []
        for p in sorted(src.iterdir()):
            if p.is_file() and p.suffix.lower() in AUDIO_EXTS:
                names.append(p.name)
        with self.lock:
            files = self.state["files"]
            order = self.state["order"]
            known = set(order)
            added = 0
            for name in names:
                if name not in known:
                    files[name] = {"status": "pending", "step": "", "error": None,
                                   "size": (src / name).stat().st_size,
                                   "result": None, "json_file": None,
                                   "attempts": 0, "done_at": None}
                    order.append(name)
                    added += 1
            self.state["order"] = order
        if added:
            self.log("info", f"Added {added} new file(s), total {len(names)}")

    def pending_names(self) -> list[str]:
        with self.lock:
            files = self.state["files"]
            return [n for n in self.state["order"] if files.get(n, {}).get("status") == "pending"]

    def requeue_failed(self) -> None:
        with self.lock:
            for name, f in self.state["files"].items():
                if f["status"] == "failed":
                    f["status"] = "pending"
                    f["error"] = None
                    f["attempts"] = 0
        self.log("info", "Failed files requeued")

    def reset_state(self) -> None:
        with self.lock:
            self.state["files"] = {}
            self.state["order"] = []
            self.state["running"] = False
            self.state["paused"] = True
            self.state["started_at"] = None
            self.state["finished_at"] = None
            self.state["log"] = []
        self.log("info", "State cleared, rescanning source")
        self.scan_source()

    # ------------------------------------------------------------------ api

    def _api_interval_ok(self) -> bool:
        return (time.monotonic() - self._last_call_ts) >= (self.config["call_interval_sec"] or 0)

    def _wait_api_slot(self, tag: str) -> None:
        while self._api_interval_ok() is False:
            wait = self.config["call_interval_sec"] - (time.monotonic() - self._last_call_ts)
            self.log("info", f"API rate limit: waiting {wait:.0f}s before {tag}")
            if not self._sleep_interruptible(wait):
                raise InterruptedError("stopped by user")
        self._last_call_ts = time.monotonic()

    def _sleep_interruptible(self, secs: float) -> bool:
        end = time.monotonic() + secs
        while time.monotonic() < end:
            if self._should_stop():
                return False
            time.sleep(min(0.25, max(0.05, end - time.monotonic())))
        return True

    def _should_stop(self) -> bool:
        with self.lock:
            return not self.state["running"] or self.state["paused"]

    def _multipart(self, file_bytes: bytes, filename: str, fields: dict) -> tuple[bytes, str]:
        boundary = "----LoopTone" + uuid.uuid4().hex
        parts = []
        for key, val in fields.items():
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{val}\r\n".encode()
            )
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n".encode()
        )
        parts.append(file_bytes)
        parts.append(f"\r\n--{boundary}--\r\n".encode())
        body = b"".join(parts)
        ctype = f"multipart/form-data; boundary={boundary}"
        return body, ctype

    @staticmethod
    def _multipart_fields(fields: dict) -> tuple[bytes, str]:
        boundary = "----LoopTone" + uuid.uuid4().hex
        parts = []
        for key, val in fields.items():
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{val}\r\n".encode()
            )
        parts.append(f"--{boundary}--\r\n".encode())
        return b"".join(parts), f"multipart/form-data; boundary={boundary}"

    def _http_json(self, method: str, url: str, body: bytes | None = None,
                   headers: dict | None = None, tag: str = "http", timeout: int = 240,
                   raw: bool = False):
        req = urllib.request.Request(url, data=body, method=method)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        req.add_header("User-Agent", "LoopToToneConvertor/1.0")
        try:
            with self._open_checked(req, tag, timeout) as resp:
                data = resp.read()
            if raw:
                return resp.status, data
            return resp.status, json.loads(data.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            err = exc.read().decode("utf-8", "replace")
            if raw:
                return exc.code, err.encode()
            try:
                return exc.code, json.loads(err)
            except Exception:
                return exc.code, err

    def _fb_anon_token(self) -> str:
        st, data = self._http_json(
            "POST",
            f"https://identitytoolkit.googleapis.com/v1/accounts:signUp?key={FIREBASE_KEY}",
            body=json.dumps({"returnSecureToken": True}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            tag="firebase-auth", timeout=30,
        )
        if st != 200 or not isinstance(data, dict) or not data.get("idToken"):
            raise RuntimeError(f"firebase anonymous auth failed: {st}")
        return data["idToken"]

    def _fb_upload(self, id_token: str, data: bytes, fname: str) -> str:
        appcheck = self.appcheck_header()
        obj = f"temp/{int(time.time() * 1000)}-{fname}"
        url = f"{FIREBASE_STORAGE}/o?name={urllib.parse.quote(obj, safe='')}"
        meta = json.dumps({
            "name": obj,
            "contentType": "audio/mpeg",
            "metadata": {
                "offload": "true",
                "cleanup": "auto",
                "uploadedAt": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
            },
        }).encode("utf-8")
        st, upload_hdrs, body = self._http_full(
            "POST", url, meta,
            {
                "Authorization": f"Firebase {id_token}",
                "Content-Type": "application/json",
                "x-goog-upload-command": "start",
                "x-goog-upload-protocol": "resumable",
                "x-firebase-appcheck": appcheck,
            },
            tag="storage-start", timeout=30,
        )
        upload_url = upload_hdrs.get("X-Goog-Upload-URL")
        if st != 200 or not upload_url:
            raise RuntimeError(f"storage upload start failed: {st} {body[:200]}")
        CH = 256 * 1024
        off = 0
        while off < len(data):
            chunk = data[off:off + CH]
            final = off + len(chunk) >= len(data)
            st2, _, body2 = self._http_full(
                "PUT", upload_url, chunk,
                {
                    "Content-Type": "application/octet-stream",
                    "x-goog-upload-command": "upload, finalize" if final else "upload",
                    "x-goog-upload-offset": str(off),
                },
                tag="storage-chunk", timeout=60,
            )
            if st2 not in (200, 201):
                raise RuntimeError(f"storage chunk failed: {st2} {body2[:200]}")
            off += len(chunk)
        st3, hdrs3, body3 = self._http_full(
            "GET",
            f"{FIREBASE_STORAGE}/o/{urllib.parse.quote(obj, safe='')}?alt=media",
            None,
            {"Authorization": f"Firebase {id_token}", "x-firebase-appcheck": appcheck},
            tag="storage-media", timeout=60, raw=True,
        )
        if st3 != 200:
            raise RuntimeError(f"storage media lookup failed: {st3} {body3[:200]}")
        token = hdrs3.get("X-Goog-Download-Token")
        url = f"{FIREBASE_STORAGE}/o/{urllib.parse.quote(obj, safe='')}?alt=media"
        if token:
            url += f"&token={urllib.parse.quote(token, safe='')}"
        return url

    def _http_full(self, method: str, url: str, body: bytes | None,
                   headers: dict | None, tag: str, timeout: int, raw: bool = False):
        req = urllib.request.Request(url, data=body, method=method)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        req.add_header("User-Agent", "LoopToToneConvertor/1.0")
        last_err = None
        for attempt in range(1, 4):
            try:
                with self._open_checked(req, tag, timeout) as resp:
                    return resp.status, dict(resp.headers), resp.read()
            except urllib.error.HTTPError as exc:
                err = exc.read()
                if exc.code >= 500:
                    last_err = f"HTTP {exc.code}: {err[:200]}"
                    self.log("warn", f"{tag}: {last_err} (attempt {attempt}/3)")
                    if not self._sleep_interruptible(15 * attempt):
                        raise InterruptedError("stopped by user")
                    continue
                return exc.code, dict(exc.headers), err
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_err = str(exc)
                self.log("warn", f"{tag}: {last_err} (attempt {attempt}/3)")
                if not self._sleep_interruptible(15 * attempt):
                    raise InterruptedError("stopped by user")
        raise RuntimeError(f"{tag}: failed after retries: {last_err}")

    def _offload(self, path: str, fields: dict, tag: str, timeout: int = 600) -> dict:
        appcheck = self.appcheck_header()
        body, ctype = self._multipart_fields(fields)
        st, data = self._http_json(
            "POST", self.config["api_base"].rstrip("/") + path, body,
            {
                "Content-Type": ctype,
                "Content-Length": str(len(body)),
                "x-firebase-appcheck": appcheck,
            },
            tag=tag, timeout=timeout,
        )
        if st != 200:
            raise RuntimeError(f"{tag}: HTTP {st}: {str(data)[:300]}")
        self.log("info", f"{tag}: ok")
        return data

    def _api_call(self, path: str, body: bytes, ctype: str, tag: str, timeout: int = 240) -> dict:
        url = self.config["api_base"].rstrip("/") + path
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", ctype)
        req.add_header("User-Agent", "LoopToToneConvertor/1.0")
        last_err = None
        ssl_warned = False
        for attempt in range(1, 5):
            try:
                with self._open_checked(req, tag, timeout) as resp:
                    raw = resp.read()
                data = json.loads(raw.decode("utf-8"))
                self.log("info", f"{tag}: ok ({len(raw)} bytes)")
                return data
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", "replace")
                body_text = raw[:300]
                if exc.code == 429:
                    retry = exc.headers.get("Retry-After")
                    wait = float(retry) if retry and retry.isdigit() else 65
                    self.log("warn", f"{tag}: 429 rate limited, waiting {wait:.0f}s")
                    if not self._sleep_interruptible(wait):
                        raise InterruptedError("stopped by user")
                elif exc.code >= 500:
                    last_err = f"HTTP {exc.code}: {body_text}"
                    self.log("warn", f"{tag}: {last_err} (attempt {attempt}/4)")
                    if not self._sleep_interruptible(20 * attempt):
                        raise InterruptedError("stopped by user")
                else:
                    raise RuntimeError(f"{tag}: HTTP {exc.code}: {body_text}")
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_err = str(exc)
                self.log("warn", f"{tag}: {last_err} (attempt {attempt}/4)")
                if not self._sleep_interruptible(15 * attempt):
                    raise InterruptedError("stopped by user")
        raise RuntimeError(f"{tag}: failed after retries: {last_err}")

    def _open_checked(self, req, tag: str, timeout: int):
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.URLError as exc:
            if not isinstance(exc.reason, ssl.SSLCertVerificationError):
                raise
            if not getattr(self, "_ssl_warned", False):
                self._ssl_warned = True
                self.log("warn",
                         f"{tag}: TLS verification failed ({exc.reason}); chordmini sends a chain "
                         f"with an expired intermediate - retrying without verification")
            return urllib.request.urlopen(
                req, timeout=timeout, context=ssl._create_unverified_context()
            )

    def check_api(self, force: bool = False) -> bool:
        now = time.monotonic()
        if not force and now - self._api_last_check < 60:
            return bool(self._api_ok)
        self._api_last_check = now
        try:
            url = self.config["api_base"].rstrip("/") + "/api/model-info"
            with self._open_checked(
                urllib.request.Request(url, method="GET"), "check-api", 20
            ) as resp:
                ok = resp.status == 200
                if ok:
                    json.loads(resp.read().decode("utf-8"))
        except Exception:
            ok = False
        self._api_ok = ok
        return ok

    # ------------------------------------------------------------------ analysis helpers

    @staticmethod
    def _beat_time(beat) -> float:
        if isinstance(beat, dict):
            return float(beat.get("time", beat.get("beat", 0)))
        return float(beat)

    def _analyze(self, file_path: Path) -> dict:
        data = file_path.read_bytes()
        fname = file_path.name

        self._wait_api_slot("upload")
        id_token = self._fb_anon_token()
        self.log("info", f"[{fname}] uploading to Firebase Storage ({len(data) / 1e6:.2f} MB)")
        offload_url = self._fb_upload(id_token, data, fname)

        duration = 0.0
        try:
            import wave
            with wave.open(str(file_path), "rb") as w:
                duration = w.getnframes() / float(w.getframerate())
        except Exception:
            pass

        self._wait_api_slot("detect-beats")
        beats_resp = self._offload(
            "/api/detect-beats-offload",
            {
                "offload_url": offload_url,
                "detector": self.config["beat_model"],
                "delete_offload": "0",
                "audio_duration": str(duration) if duration else "",
            },
            "detect-beats-offload",
        )
        beats = [self._beat_time(b) for b in beats_resp.get("beats") or []]
        bpm = float(beats_resp.get("bpm") or 0)
        ts_raw = str(beats_resp.get("time_signature") or "4")
        try:
            time_sig = int(ts_raw.split("/")[0].strip()) or 4
        except (ValueError, IndexError):
            time_sig = 4
        duration = float(beats_resp.get("duration") or duration or 0)

        self._wait_api_slot("recognize-chords")
        chord_resp = self._offload(
            "/api/recognize-chords-offload",
            {
                "offload_url": offload_url,
                "model": self.config["chord_model"],
                "detector": self.config["chord_model"],
                "chord_dict": "full",
                "delete_offload": "0",
            },
            "recognize-chords-offload",
        )
        chords = []
        for c in chord_resp.get("chords") or []:
            chords.append({
                "start": float(c.get("start", 0)),
                "end": float(c.get("end", c.get("start", 0))),
                "chord": str(c.get("chord", "N")),
                "confidence": float(c.get("confidence") or 0),
            })
        if not chords and duration > 0:
            self.log("warn", f"{fname}: no chords returned, trying heuristic")
            chords = [{"start": 0.0, "end": duration, "chord": "N", "confidence": 0.0}]

        self._wait_api_slot("delete-offload")
        try:
            self._http_json(
                "POST", self.config["api_base"].rstrip("/") + "/api/offload/delete",
                json.dumps({"url": offload_url}).encode("utf-8"),
                {"Content-Type": "application/json", "x-firebase-appcheck": self.appcheck_header()},
                tag="offload-delete", timeout=60,
            )
        except Exception as exc:
            self.log("warn", f"{fname}: offload cleanup skipped ({exc})")

        key, scale = "", ""
        if self.config.get("detect_key") and chords:
            try:
                payload = json.dumps(
                    {"chords": [{"time": c["start"], "chord": c["chord"]} for c in chords]}
                ).encode("utf-8")
                self._wait_api_slot("detect-key")
                key_resp = self._api_call(
                    "/api/detect-key", payload, "application/json", "detect-key", timeout=300
                )
                key, scale = self._split_key(str(key_resp.get("primaryKey") or ""))
            except Exception as exc:
                self.log("warn", f"{fname}: key detection skipped ({exc})")

        return {
            "bpm": round(bpm) if bpm else 0,
            "time_signature": time_sig,
            "duration": duration,
            "beats": beats,
            "chords": chords,
            "key": key,
            "scale": scale,
        }

    @staticmethod
    def _split_key(primary: str) -> tuple[str, str]:
        m = re.match(r"^\s*([A-Ga-g](?:#|♯|b|♭)?)\s*(major|minor|m)?", primary)
        if not m:
            return "", ""
        key_raw = m.group(1).replace("♯", "#").replace("♭", "b")
        key = key_raw[0].upper() + key_raw[1:]
        scale_raw = m.group(2) or ""
        scale = {"major": "major", "minor": "minor", "m": "minor"}.get(scale_raw, scale_raw.lower())
        return key, scale

    def _save_analysis(self, name: str, analysis: dict) -> None:
        path = self._raw_dir() / f"{Path(name).stem}.analysis.json"
        path.write_text(json.dumps({
            "source": "chordmini API",
            "bpm": analysis["bpm"],
            "time_signature": analysis["time_signature"],
            "duration": analysis["duration"],
            "key": analysis["key"],
            "scale": analysis["scale"],
            "beats": analysis["beats"],
            "chords": analysis["chords"],
        }, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        self.log("info", f"[{name}] saved raw analysis -> {path}")

    # ------------------------------------------------------------------ conversion

    @staticmethod
    def _parse_chord(symbol: str) -> tuple[int, int, list[int], int | None] | None:
        s = symbol.strip().replace("♯", "#").replace("♭", "b")
        if not s or s in ("N", "N.C.", "NC", "None", "-"):
            return None
        m = re.match(r"^([A-Ga-g])([#b]?)(.*)$", s)
        if not m:
            return None
        pc = (PT_CLASSES.index(m.group(1).upper()) + (1 if m.group(2) == "#" else -1 if m.group(2) == "b" else 0)) % 12
        rest = m.group(3)
        slash = None
        sm = re.match(r"^(.*?)/([A-Ga-g][#b]?)$", rest)
        if sm:
            rest = sm.group(1)
            bass = sm.group(2)
            slash = (PT_CLASSES.index(bass[0].upper()) + (1 if len(bass) > 1 and bass[1] == "#" else -1 if len(bass) > 1 and bass[1] == "b" else 0)) % 12

        # 1) try exact (case-sensitive) match first — QUALITY_OFFSETS distinguishes
        #    "M7" (major 7th) from "m7" (minor 7th), so lowercasing before lookup
        #    would silently collapse them into the wrong entry.
        offsets = QUALITY_OFFSETS.get(rest)

        quality = rest.lower()
        if offsets is None:
            # 2) fall back to case-insensitive match
            offsets = QUALITY_OFFSETS.get(quality)

        if offsets is None:
            q = re.sub(r"[^a-z0-9]", "", quality)
            if q.startswith("maj7") or q.startswith("m7"):
                offsets = (0, 3, 7) if q.startswith("m7") and not q.startswith("maj7") else (0, 4, 7)
            elif q.startswith("maj") or q.startswith("major"):
                offsets = (0, 4, 7)
            elif q and q.startswith("m") and not q.startswith("maj"):
                offsets = (0, 3, 7)
            elif q and "sus" in q:
                offsets = (0, 5, 7)
            elif q and ("7" in q or "11" in q or "13" in q):
                offsets = (0, 4, 7, 10)
            else:
                offsets = (0, 4, 7)

        tones = sorted({(pc + off) % 12 for off in (offsets or (0, 4, 7))})
        return pc, pc, tones, slash

    @staticmethod
    def _duration_steps(steps: int) -> tuple[int, str]:
        steps = max(2, int(round(steps)))
        best = min(DURATION_OPTIONS, key=lambda d: abs(d[0] - steps))
        return best

    @staticmethod
    def _note_name(midi: int) -> str:
        pc = midi % 12
        octave = midi // 12 - 1
        return f"{PT_CLASSES[pc]}{octave}"

    @staticmethod
    def _note_midi(name: str) -> int:
        m = re.match(r"^([A-Ga-g][#b]?)(-?\d+)$", name)
        if not m:
            return 60
        pc = PT_CLASSES.index(m.group(1).upper())
        return (int(m.group(2)) + 1) * 12 + pc

    @staticmethod
    def _vlq(n: int) -> bytes:
        out = [n & 0x7F]
        n >>= 7
        while n:
            out.insert(0, 0x80 | (n & 0x7F))
            n >>= 7
        return bytes(out)

    def _meta(self, delta: int, mtype: int, payload: bytes) -> bytes:
        return (
            self._vlq(delta)
            + b"\xff" + bytes([mtype])
            + self._vlq(len(payload)) + payload
        )

    def _write_midi(self, tone: dict, path: Path) -> None:
        import struct

        bpm = int(tone["bpm"]) or 120
        tempo = int(60000000 / bpm)

        meta = (
            self._meta(0, 0x03, b"LoopToTone")
            + self._meta(0, 0x51, tempo.to_bytes(3, "big"))
            + self._meta(0, 0x58, bytes([4, 2, 24, 8]))
            + self._meta(0, 0x2F, b"")
        )

        events = []
        for n in tone["notes"]:
            t_on = int(n["step"]) * (MIDI_TPQ // 4)
            t_off = t_on + DUR_TICKS.get(n["duration"], 120)
            mid = self._note_midi(n["note"])
            vel = max(1, min(127, int(round(float(n["velocity"]) * 127))))
            events.append((t_on, 0x90, mid, vel))
            events.append((t_off, 0x80, mid, 0))
        events.sort(key=lambda e: (e[0], 0 if e[1] == 0x80 else 1))

        notes_track = self._vlq(0) + bytes([0xC0, 0])
        prev = 0
        for tick, status, mid, vel in events:
            notes_track += self._vlq(tick - prev) + bytes([status, mid, vel])
            prev = tick
        notes_track += self._meta(0, 0x2F, b"")

        header = b"MThd" + struct.pack(">IHHH", 6, 1, 2, MIDI_TPQ)
        chunks = header
        for track in (meta, notes_track):
            chunks += b"MTrk" + struct.pack(">I", len(track)) + track
        path.write_bytes(chunks)

    def _voicing(self, analysis: dict) -> dict:
        bpm = analysis["bpm"] or 120
        sps = bpm / 60.0 * 4.0
        chords = analysis["chords"]
        notes: list[dict] = []
        merged = []

        def snap(x: int) -> int:
            return int(round(x / 4)) * 4

        for c in chords:
            raw_start = round(c["start"] * sps)
            raw_end = max(raw_start + 1, round(c["end"] * sps))
            if self.config.get("snap_grid", True):
                start = snap(raw_start)
                end = max(start + 4, snap(raw_end))
            else:
                start = raw_start
                end = raw_end
            if merged and merged[-1]["chord"] == c["chord"] and merged[-1]["end"] == start:
                merged[-1]["end"] = end
            else:
                merged.append({"chord": c["chord"], "start": start, "end": end})

        arp_pattern = [0, 1, 2, 1]

        def fit_duration(avail_steps: int) -> str:
            """Longest musical duration that fits into avail_steps
            (fallback: shortest possible), so notes never spill into the
            next segment."""
            for steps_, name in DURATION_OPTIONS:
                if steps_ <= avail_steps:
                    return name
            return "8n"

        for i, seg in enumerate(merged):
            parsed = self._parse_chord(seg["chord"])
            if parsed is None:
                continue
            root_pc, _, tones, slash_bass = parsed
            start, end = seg["start"], seg["end"]
            next_start = merged[i + 1]["start"] if i + 1 < len(merged) else end
            bass_end = min(end, next_start)
            if bass_end <= start:
                bass_end = start + 4

            bass_pc = slash_bass if slash_bass is not None else root_pc

            bass_midi = bass_pc + 12 * 4
            bass_avail = bass_end - start
            bass_dur = fit_duration(bass_avail)
            notes.append({
                "step": start,
                "note": self._note_name(bass_midi),
                "duration": bass_dur,
                "velocity": round(0.42 + 0.05 * (bass_avail / 64.0), 2),
                "chance": None,
            })

            block_vels = [0.25, 0.22, 0.2]
            k = 0
            step = start
            while step < bass_end:
                top_dur = "4n"
                if step + 4 > bass_end:
                    top_dur = fit_duration(bass_end - step)
                if k == 0:
                    for i, tone in enumerate(tones[:3]):
                        notes.append({
                            "step": step,
                            "note": self._note_name(tone + 12 * 5),
                            "duration": top_dur,
                            "velocity": block_vels[i] if i < 3 else 0.2,
                            "chance": None,
                        })
                else:
                    tone = tones[arp_pattern[k % 4]]
                    notes.append({
                        "step": step,
                        "note": self._note_name(tone + 12 * 5),
                        "duration": top_dur,
                        "velocity": 0.28,
                        "chance": None,
                    })
                step += 4
                k += 1

        steps_total = 0
        for n in notes:
            d_steps, _ = self._duration_steps(
                {"8n": 2, "8n.": 3, "4n": 4, "4n.": 6, "2n": 8, "2n.": 12,
                 "1m": 16, "1m.": 24, "2m": 32, "3m": 48, "4m": 64,
                 "6m": 96, "8m": 128, "16m": 256}[n["duration"]]
            )
            steps_total = max(steps_total, n["step"] + d_steps)

        progression = []
        for seg in merged:
            if seg["chord"] not in progression and self._parse_chord(seg["chord"]):
                progression.append(seg["chord"])

        return {
            "bpm": bpm,
            "time_signature": analysis["time_signature"],
            "duration": analysis["duration"],
            "steps": steps_total,
            "notes": notes,
            "progression": progression,
            "chord_count": len(merged),
            "key": analysis["key"],
            "scale": analysis["scale"],
        }

    # ------------------------------------------------------------------ worker

    def _process(self, name: str) -> None:
        src = Path(self.config["source"])
        fpath = src / name
        if not fpath.is_file():
            raise FileNotFoundError(f"missing source file: {fpath}")
        self.log("info", f"[{name}] starting ({fpath.stat().st_size / 1e6:.2f} MB)")
        with self.lock:
            self.state["files"][name]["step"] = "beats"
        analysis = self._analyze(fpath)
        self._save_analysis(name, analysis)
        if not analysis["beats"]:
            self.log("warn", f"[{name}] no beats returned")
        chords_count = analysis.get("chord_count")
        if chords_count is None:
            chords_count = len(analysis.get("chords") or [])
        self.log("info", f"[{name}] bpm={analysis.get('bpm')} sig={analysis.get('time_signature')}/4 "
                         f"chords={chords_count}")
        with self.lock:
            self.state["files"][name]["step"] = "convert"
        tone = self._voicing(analysis)
        tone["name"] = Path(name).stem
        tone["instrument"] = "piano"
        tone["swing"] = 0.0
        tone["key"] = tone.get("key") or analysis.get("key") or ""
        tone["scale"] = tone.get("scale") or analysis.get("scale") or ""

        out = self._output_dir()
        json_name = f"{tone['name']}.json"
        out_path = out / json_name
        payload = [{key: tone[key] for key in (
            "name", "bpm", "instrument", "steps", "key", "scale", "swing", "notes"
        )}]
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        self.log("info", f"[{name}] wrote {out_path} (steps={tone['steps']}, "
                         f"notes={len(tone['notes'])}, prog={' '.join(tone['progression'])})")
        with self.lock:
            self.state["files"][name].update({
                "status": "done",
                "step": "",
                "error": None,
                "json_file": json_name,
                "done_at": time.time(),
                "result": {
                    "bpm": tone["bpm"],
                    "time_signature": tone["time_signature"],
                    "key": tone["key"],
                    "scale": tone["scale"],
                    "steps": tone["steps"],
                    "notes": len(tone["notes"]),
                    "chords": tone["chord_count"],
                    "progression": tone["progression"],
                    "duration": round(tone["duration"], 2),
                },
            })

    def _output_dir(self) -> Path:
        out = Path(self.config["output"])
        out.mkdir(parents=True, exist_ok=True)
        return out

    def _raw_dir(self) -> Path:
        raw = self.config.get("raw_dir") or str(Path(self.config["output"]) / "raw")
        d = Path(raw)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def run_worker(self) -> None:
        while True:
            if self._should_stop():
                self.log("info", "Worker paused/stopped")
                time.sleep(0.5)
                continue
            try:
                self.check_api()
            except Exception:
                self.log("warn", "API health check failed")
            names = self.pending_names()
            if not names:
                with self.lock:
                    total = len(self.state["files"])
                    busy = any(
                        f["status"] in ("pending", "working")
                        for f in self.state["files"].values()
                    )
                if total and not busy:
                    with self.lock:
                        self.state["running"] = False
                        self.state["paused"] = True
                        self.state["finished_at"] = time.time()
                    self.log("info", "Queue finished, engine stopped")
                    return
                self.log("info", "Queue empty")
                time.sleep(1)
                continue
            name = names[0]
            with self.lock:
                self.state["files"][name]["status"] = "working"
                self.state["files"][name]["attempts"] = self.state["files"][name].get("attempts", 0) + 1
            try:
                self._process(name)
            except InterruptedError:
                with self.lock:
                    self.state["files"][name]["status"] = "pending"
                return
            except Exception as exc:
                tb = traceback.format_exc()
                with self.lock:
                    f = self.state["files"][name]
                    f["step"] = ""
                    f["status"] = "failed"
                    f["error"] = str(exc)
                self.log("error", f"[{name}] FAILED: {exc}\n{tb}")
            self.save_state()

    # ------------------------------------------------------------------ control

    def start(self) -> None:
        with self.lock:
            if self.state["running"] and not self.state["paused"]:
                return
            total = len(self.state["files"])
            busy = any(
                f["status"] in ("pending", "working")
                for f in self.state["files"].values()
            )
            if total and not busy:
                self.log("info", "Queue completed, starting a clean rerun")
                self.state["files"] = {}
                self.state["order"] = []
                self.state["started_at"] = None
                self.state["finished_at"] = None
            self.state["running"] = True
            self.state["paused"] = False
            if self.state["started_at"] is None:
                self.state["started_at"] = time.time()
            self.state["finished_at"] = None
        self.scan_source()
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self.run_worker, daemon=True)
            self._worker.start()
        self.log("info", "Started")

    def pause(self) -> None:
        with self.lock:
            self.state["paused"] = True
        self.log("info", "Paused")

    def resume(self) -> None:
        with self.lock:
            self.state["paused"] = False
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self.run_worker, daemon=True)
            self._worker.start()
        self.log("info", "Resumed")

    def stop(self) -> None:
        with self.lock:
            self.state["running"] = False
            self.state["paused"] = True
            if not self.pending_names():
                self.state["finished_at"] = time.time()
        self.log("info", "Stopped")

    # ------------------------------------------------------------------ state view

    def view(self) -> dict:
        with self.lock:
            files = []
            done_times = []
            for name in self.state["order"]:
                f = self.state["files"].get(name)
                if f is None:
                    continue
                files.append({
                    "name": name,
                    "status": f["status"],
                    "step": f["step"],
                    "error": f["error"],
                    "size": f.get("size", 0),
                    "json_file": f.get("json_file"),
                    "result": f.get("result"),
                    "attempts": f.get("attempts", 0),
                    "done_at": f.get("done_at"),
                })
                if f["status"] == "done" and f.get("done_at"):
                    done_times.append(f["done_at"])
            totals = {}
            for st in ("pending", "working", "done", "failed"):
                totals[st] = sum(1 for f in files if f["status"] == st)
            totals["total"] = len(files)
            eta = None
            if len(done_times) > 1 and totals["total"] > totals["done"]:
                avg = (done_times[-1] - done_times[0]) / max(1, len(done_times) - 1)
                remaining = totals["pending"] + totals["working"] + totals["failed"]
                if remaining > 0:
                    eta = avg * remaining
            log = {"log": self.state.get("log", [])[-300:], "log_seq": self._log_seq}
        return {
            "config": self.config,
            "running": self.state["running"],
            "paused": self.state["paused"],
            "started_at": self.state["started_at"],
            "finished_at": self.state["finished_at"],
            "api_ok": self._api_ok,
            "appcheck": self.appcheck_status(),
            "totals": totals,
            "eta": eta,
            "files": files,
            **log,
        }