"""Tidy a run session's blocks before anything shows or runs them.

Two things the athlete does not want and the coach model sometimes writes:

- a warm-up or cool-down at exactly the pace of the block beside it. That is
  one continuous run, so the two are joined into a single block;
- walk recoveries between reps. Recoveries in a run are an easy jog.

Applied where plan rows are read, so the session card, the watch workout and
the coach context all see the same text. Stored rows are not rewritten.
"""
from __future__ import annotations

import re
from typing import Any

_EMPTY = {"", "—", "–", "-", "none", "n/a"}
_TIMED = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*min(?:utes?|s)?\b[\s,]*(.*?)\s*$", re.I | re.S)
_NOT_PLAIN = re.compile(r"\d\s*[×x]\s*\d|\bstrides?\b|\bdrills?\b|\bbuild|\bprogress|\bpick-?ups?\b", re.I)
_CEILING = re.compile(r",?\s*no faster than\s+(\d+:\d{2})\s*/\s*km", re.I)
_EASY = {"", "easy", "easy jog", "easy run", "easy running", "jog", "z1", "z2", "easy z1", "easy z2"}
_SEP = re.compile(r";\s*|,?\s+then\s+", re.I)
_WALK_OR_JOG = re.compile(
    r"\bwalk(?:ing)?\s*(?:/|or)\s*(?:easy\s+)?jog(?:ging)?\b"
    r"|\b(?:easy\s+)?jog(?:ging)?\s*(?:/|or)\s*walk(?:ing)?\b", re.I)
_WALK = re.compile(r"\b(?:easy\s+|brisk\s+)?walk(?:ing)?\b", re.I)


def _blank(value: Any) -> bool:
    return not isinstance(value, str) or value.strip().lower() in _EMPTY


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip(" ,.;").lower()


def _timed(clause: str) -> tuple[float, str] | None:
    """(minutes, the rest of the text) for a plain steady block, else None."""
    m = _TIMED.match(clause)
    if not m or _NOT_PLAIN.search(clause):
        return None
    return float(m.group(1)), m.group(2).strip(" ,.;")


def _same_pace(a: str, b: str) -> str | None:
    """The text to keep when two blocks are the same effort, else None."""
    if _norm(a) == _norm(b):
        return a
    ceilings = set(_CEILING.findall(a)) | set(_CEILING.findall(b))
    if len(ceilings) > 1:
        return None
    ka, kb = _norm(_CEILING.sub("", a)), _norm(_CEILING.sub("", b))
    if ka == kb and ka in _EASY:
        return a if len(a) >= len(b) else b
    return None


def jog_recoveries(text: str) -> str:
    """Walk recoveries become an easy jog."""
    return _WALK.sub("easy jog", _WALK_OR_JOG.sub("easy jog", text))


def tidy(structure: Any, discipline: Any) -> Any:
    if not isinstance(structure, dict) or str(discipline or "").lower() != "run":
        return structure
    out = dict(structure)
    for key in ("warmup", "main"):
        if isinstance(out.get(key), str):
            out[key] = jog_recoveries(out[key])
    main = out.get("main")
    if _blank(main):
        return out

    warm = out.get("warmup")
    if not _blank(warm):
        sep = _SEP.search(main)
        first, rest = (main[:sep.start()], main[sep.start():]) if sep else (main, "")
        w, f = _timed(warm), _timed(first)
        keep = _same_pace(w[1], f[1]) if w and f else None
        if keep is not None:
            main = f"{w[0] + f[0]:g} min {keep}".rstrip() + rest
            out["warmup"] = "—"

    cool = out.get("cooldown")
    if not _blank(cool):
        seps = list(_SEP.finditer(main))
        head, last = (main[:seps[-1].end()], main[seps[-1].end():]) if seps else ("", main)
        l, c = _timed(last), _timed(cool)
        keep = _same_pace(l[1], c[1]) if l and c else None
        if keep is not None:
            main = head + f"{l[0] + c[0]:g} min {keep}".rstrip()
            out["cooldown"] = "—"

    out["main"] = main
    return out
