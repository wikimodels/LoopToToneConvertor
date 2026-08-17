import re

PT_CLASSES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

QUALITY_OFFSETS = {
    "": (0, 4, 7), "maj": (0, 4, 7), "M": (0, 4, 7), "major": (0, 4, 7),
    "m": (0, 3, 7), "min": (0, 3, 7), "minor": (0, 3, 7),
    "dim": (0, 3, 6), "aug": (0, 4, 8),
    "sus2": (0, 2, 7), "sus4": (0, 5, 7), "5": (0, 7, 12),
    "6": (0, 4, 7, 9), "m6": (0, 3, 7, 9), "7": (0, 4, 7, 10),
    "maj7": (0, 4, 7, 11), "M7": (0, 4, 7, 11), "m7": (0, 3, 7, 10),
    "min7": (0, 3, 7, 10), "min6": (0, 3, 7, 9), "maj6": (0, 4, 7, 9),
    "mmaj7": (0, 3, 7, 11), "mM7": (0, 3, 7, 11), "dim7": (0, 3, 6, 9),
    "m7b5": (0, 3, 6, 10), "half-dim": (0, 3, 6, 10), "h": (0, 3, 6, 10),
    "9": (0, 4, 7, 10, 14), "maj9": (0, 4, 7, 11, 14), "m9": (0, 3, 7, 10, 14),
    "7sus4": (0, 5, 7, 10), "7sus2": (0, 2, 7, 10),
}

# Номер ступени аккорда (как её пишет chordmini в числовом slash, напр.
# "A:maj7/5") -> индекс в кортеже offsets данного аккорда (root=0, 3rd=1,
# 5th=2, 7th=3, 9th=4, 11th=5, 13th=6). Это НЕ фиксированный интервал в
# полутонах — квинта у dim/m7b5 уменьшённая, а не чистая, поэтому берём
# offset именно из уже посчитанной для конкретного аккорда таблицы, а не
# считаем "root + 7 полутонов" в лоб.
DEGREE_TO_OFFSET_INDEX = {1: 0, 3: 1, 5: 2, 7: 3, 9: 4, 11: 5, 13: 6}


def pc_from_note(note: str) -> int:
    """C/C#/Db/... -> pitch class 0..11 (0 = C)."""
    m = re.match(r"^([A-Ga-g])([#b]?)$", note.strip())
    if not m:
        return -1
    base = PT_CLASSES.index(m.group(1).upper())
    acc = m.group(2)
    return (base + (1 if acc == "#" else -1 if acc == "b" else 0)) % 12


def parse_chord(symbol: str) -> tuple[int, int, list[int], int | None] | None:
    """Chord symbol -> (root pc, root pc, tone pcs in root/3rd/5th/7th order, slash bass pc | None).

    Returns None for rests/no-chord symbols. Tones keep the musical order of
    the chord formula so voicing can distinguish roles (root/3rd/5th/7th).

    Slash bass can be given either as a note name ("F#:maj/A#") or as a
    scale-degree number relative to the chord's own root ("A:maj7/5", as
    chordmini emits it). Both forms resolve to a concrete pitch class.
    """
    s = symbol.strip().replace("♯", "#").replace("♭", "b")
    if not s or s in ("N", "N.C.", "NC", "None", "-"):
        return None
    m = re.match(r"^([A-Ga-g])([#b]?)(.*)$", s)
    if not m:
        return None
    pc = pc_from_note(m.group(1) + m.group(2))
    rest = m.group(3)

    slash = None          # resolved pitch class of slash bass, if any
    slash_degree = None   # numeric scale-degree slash, resolved after offsets are known

    sm = re.match(r"^(.*?)/([A-Ga-g][#b]?|\d{1,2})$", rest)
    if sm:
        rest = sm.group(1)
        bass = sm.group(2)
        if bass[0].isalpha():
            slash = pc_from_note(bass)
        else:
            slash_degree = int(bass)

    rest = rest.lstrip(":").lstrip(".")

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
        if q.startswith("min7") or q.startswith("m7"):
            offsets = (0, 3, 7, 10)
        elif q.startswith("maj7"):
            offsets = (0, 4, 7, 11)
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

    offsets = offsets or (0, 4, 7)

    # Resolve numeric slash-degree now that we know this chord's own offsets.
    # E.g. "A:maj7/5" -> degree 5 -> offsets[2] -> root + that interval.
    if slash is None and slash_degree is not None:
        idx = DEGREE_TO_OFFSET_INDEX.get(slash_degree)
        if idx is not None and idx < len(offsets):
            slash = (pc + offsets[idx]) % 12
        # else: unknown/unsupported degree (e.g. a 15th) — leave slash as
        # None and let the caller fall back to the chord root, same as
        # before this fix, rather than guessing.

    tones = []
    seen = set()
    for off in offsets:
        pc_tone = (pc + off) % 12
        if pc_tone not in seen:
            seen.add(pc_tone)
            tones.append(pc_tone)
    return pc, pc, tones, slash


def note_name(midi: int) -> str:
    pc = midi % 12
    octave = midi // 12 - 1
    return f"{PT_CLASSES[pc]}{octave}"


def note_midi(name: str) -> int:
    m = re.match(r"^([A-Ga-g][#b]?)(-?\d+)$", name.strip())
    if not m:
        return -1
    pc = pc_from_note(m.group(1))
    if pc < 0:
        return -1
    octave = int(m.group(2))
    return pc + 12 * (octave + 1)