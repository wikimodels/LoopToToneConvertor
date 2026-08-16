"""LoopToToneConvertor engine: ChordMini client with rate limiting, worker queue
and conversion of beat/chord/key results into Tone.js JSON files.

Pure Python stdlib (no third-party dependencies).
"""

from __future__ import annotations

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

DEFAULT_CONFIG = {
    "source": r"C:\Users\Vitali\Downloads\TrimmedAudio",
    "output": r"D:\Music\ToneJs",
    "api_base": "https://chordmini.me",
    "beat_model": "madmom",
    "chord_model": "chord-cnn-lstm",
    "detect_key": True,
    "call_interval_sec": 33,
    "autostart": False,
}

AUDIO_EXTS = {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac", ".opus", ".wma"}

PT_CLASSES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

DURATION_OPTIONS = [
    (2, "8n"), (3, "8n."), (4, "4n"), (6, "4n."), (8, "2n"), (12, "2n."),
    (16, "1m"), (24, "1m."), (32, "2m"), (48, "3m"), (64, "4m"),
    (96, "6m"), (128, "8m"), (256, "16m"),
]

DUR_TICKS = {
    "8n": 60, "8n.": 90, "4n": 120, "4n.": 180, "2n": 240, "2n.": 360,
    "1m": 480, "1m.": 720, "2m": 960, "3m": 1440, "4m": 1920,
    "6m": 2880, "8m": 3840, "16m": 7680,
}

MIDI_TPQ = 480

QUALITY_OFFSETS = {
    "": (0, 4, 7),
    "m": (0, 3, 7),
    "dim": (0, 3, 6),
    "aug": (0, 4, 8),
    "sus2": (0, 2, 7),
    "sus4": (0, 5, 7),
    "5": (0, 7, 12),
    "6": (0, 4, 7, 9),
    "m6": (0, 3, 7, 9),
    "7": (0, 4, 7, 10),
    "maj7": (0, 4, 7, 11),
    "M7": (0, 4, 7, 11),
    "m7": (0, 3, 7, 10),
    "mmaj7": (0, 3, 7, 11),
    "mM7": (0, 3, 7, 11),
    "dim7": (0, 3, 6, 9),
    "m7b5": (0, 3, 6, 10),
    "half-dim": (0, 3, 6, 10),
    "h": (0, 3, 6, 10),
    "9": (0, 4, 7, 10, 14),
    "maj9": (0, 4, 7, 11, 14),
    "m9": (0, 3, 7, 10, 14),
    "7sus4": (0, 5, 7, 10),
    "7sus2": (0, 2, 7, 10),
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

        self._wait_api_slot("detect-beats")
        beats_resp = self._api_call(
            "/api/detect-beats",
            *self._multipart(data, fname, {"detector": self.config["beat_model"]}),
            "detect-beats",
        )
        beats = [self._beat_time(b) for b in beats_resp.get("beats") or []]
        bpm = float(beats_resp.get("bpm") or 0)
        time_sig = int(beats_resp.get("time_signature") or 4) or 4
        duration = float(beats_resp.get("duration") or 0)

        self._wait_api_slot("recognize-chords")
        chord_resp = self._api_call(
            "/api/recognize-chords",
            *self._multipart(data, fname, {"detector": self.config["chord_model"]}),
            "recognize-chords",
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
        m = re.match(r"^\s*([A-Ga-g][#♯b♭]?)\s*(major|minor|m)?", primary)
        if not m:
            return "", ""
        key = m.group(1).replace("♯", "#").replace("♭", "b").upper()
        scale_raw = m.group(2) or ""
        scale = {"major": "major", "minor": "minor", "m": "minor"}.get(scale_raw, scale_raw.lower())
        return key, scale

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
        quality = rest.lower()
        offsets = QUALITY_OFFSETS.get(quality)
        if offsets is None:
            q = re.sub(r"[^a-z0-9]", "", quality)
            if q.startswith("maj7") or q.startswith("m7"):
                pass
            elif q and q.startswith("m"):
                offsets = (0, 3, 7)
            elif q and "sus" in q:
                offsets = (0, 5, 7)
            elif q and ("7" in q or "11" in q or "13" in q):
                offsets = (0, 4, 7, 10)
            else:
                offsets = (0, 4, 7)
        tones = sorted({(pc + off) % 12 for off in offsets})
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
        for c in chords:
            start = round(c["start"] * sps)
            end = max(start + 1, round(c["end"] * sps))
            if merged and merged[-1]["chord"] == c["chord"] and merged[-1]["end"] == start:
                merged[-1]["end"] = end
            else:
                merged.append({"chord": c["chord"], "start": start, "end": end})

        arp_pattern = [0, 1, 2, 1]
        for seg in merged:
            parsed = self._parse_chord(seg["chord"])
            if parsed is None:
                continue
            _, _, tones, slash_bass = parsed
            start, end = seg["start"], seg["end"]
            bass_pc = slash_bass if slash_bass is not None else tones[0]

            bass_midi = bass_pc + 12 * 4
            bass_steps, bass_dur = self._duration_steps(end - start)
            notes.append({
                "step": start,
                "note": self._note_name(bass_midi),
                "duration": bass_dur,
                "velocity": round(0.42 + 0.05 * ((end - start) / 64.0), 2),
                "chance": None,
            })

            block_vels = [0.25, 0.22, 0.2]
            k = 0
            step = start
            while step < end:
                if k == 0:
                    for i, tone in enumerate(tones[:3]):
                        notes.append({
                            "step": step,
                            "note": self._note_name(tone + 12 * 5),
                            "duration": "4n",
                            "velocity": block_vels[i] if i < 3 else 0.2,
                            "chance": None,
                        })
                else:
                    tone = tones[arp_pattern[k % 4]]
                    notes.append({
                        "step": step,
                        "note": self._note_name(tone + 12 * 5),
                        "duration": "4n",
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
        if not analysis["beats"]:
            self.log("warn", f"[{name}] no beats returned")
        self.log("info", f"[{name}] bpm={analysis['bpm']} sig={analysis['time_signature']}/4 "
                         f"chords={analysis['chord_count'] if 'chord_count' not in analysis else len(analysis['chords'])}")
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

        midi_dir = out / "midi"
        midi_dir.mkdir(parents=True, exist_ok=True)
        midi_name = f"{tone['name']}.mid"
        midi_path = midi_dir / midi_name
        self._write_midi(tone, midi_path)

        self.log("info", f"[{name}] wrote {out_path} (steps={tone['steps']}, "
                         f"notes={len(tone['notes'])}, prog={' '.join(tone['progression'])})")
        self.log("info", f"[{name}] wrote {midi_path}")
        with self.lock:
            self.state["files"][name].update({
                "status": "done",
                "step": "",
                "error": None,
                "json_file": json_name,
                "midi_file": midi_name,
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
                with self.lock:
                    f = self.state["files"][name]
                    f["step"] = ""
                    f["status"] = "failed"
                    f["error"] = str(exc)
                self.log("error", f"[{name}] FAILED: {exc}")
            self.save_state()

    # ------------------------------------------------------------------ control

    def start(self) -> None:
        with self.lock:
            if self.state["running"] and not self.state["paused"]:
                return
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
                    "midi_file": f.get("midi_file"),
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
            "totals": totals,
            "eta": eta,
            "files": files,
            **log,
        }