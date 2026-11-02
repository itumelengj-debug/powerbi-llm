"""Scoring helpers for bench tasks. A check takes a RunResult and returns bool or (bool, reason)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional

_FENCE = re.compile(r"```([\w#+.-]*)[^\n]*\n(.*?)```", re.S)
_HERE = re.compile(r"@'\r?\n(.*?)\r?\n'@", re.S)
_CITE = re.compile(r"\[(S\d+)\]")


@dataclass
class RunResult:
    text: str
    mode: str = "model"                    # "model" | "agent"
    actions: list = field(default_factory=list)
    written: dict = field(default_factory=dict)     # agent mode: {relative path: parsed JSON or text}
    context: list = field(default_factory=list)
    issues: list = field(default_factory=list)
    thinking: str = ""


def blocks(text: str, lang: Optional[str] = None) -> list:
    """Bodies of fenced code blocks; lang is a regex matched against the fence tag."""
    out = []
    for m in _FENCE.finditer(text or ""):
        if lang is None or re.fullmatch(lang, m.group(1), re.I):
            out.append(m.group(2))
    return out


def code(text: str) -> str:
    return "\n".join(blocks(text))


def jsons(r: RunResult) -> list:
    """Every JSON object the answer delivered: ```json blocks, PowerShell here-strings and, in agent
    mode, files actually written to the sandbox."""
    docs = []
    for body in blocks(r.text, "json") + [h for h in _HERE.findall(r.text or "") if h.lstrip()[:1] in "{["]:
        try:
            docs.append(json.loads(body))
        except json.JSONDecodeError:
            continue
    for v in (r.written or {}).values():
        if isinstance(v, (dict, list)):
            docs.append(v)
    return docs


def has(text: str, *patterns: str) -> bool:
    """All patterns match (case-insensitive)."""
    return all(re.search(p, text or "", re.I | re.S) for p in patterns)


def any_of(text: str, *patterns: str) -> bool:
    return any(re.search(p, text or "", re.I | re.S) for p in patterns)


def none_of(text: str, *patterns: str) -> bool:
    return not any_of(text, *patterns)


def called(r: RunResult, tool: str, status: Optional[str] = None) -> bool:
    return any(a.get("tool") == tool and (status is None or a.get("status") == status) for a in r.actions)


def visuals(r: RunResult, vtype: Optional[str] = None) -> list:
    out = []
    for d in jsons(r):
        if isinstance(d, dict) and isinstance(d.get("visual"), dict):
            if vtype is None or d["visual"].get("visualType") == vtype:
                out.append(d)
    return out


def projections(v: dict, role: str) -> list:
    qs = (((v.get("visual") or {}).get("query") or {}).get("queryState") or {})
    return list(((qs.get(role) or {}).get("projections")) or [])


def refs_ok(v: dict) -> bool:
    qs = (((v.get("visual") or {}).get("query") or {}).get("queryState") or {})
    projs = [p for spec in qs.values() for p in (spec or {}).get("projections", [])]
    return bool(projs) and all(p.get("queryRef") and p.get("nativeQueryRef") for p in projs)


def citations_valid(r: RunResult) -> tuple:
    known = {str(c.get("id") or f"S{i}") for i, c in enumerate(r.context or [], 1)}
    used = set(_CITE.findall(r.text or ""))
    if not r.context:
        return True, "no context"
    bad = used - known
    if bad:
        return False, f"fabricated citations {sorted(bad)}"
    if not used and not re.search(r"(?i)unverified", r.text or ""):
        return False, "context given but nothing cited and nothing marked Unverified"
    return True, f"cited {sorted(used)}"


REFUSAL = r"out of scope"
CONFIRM = (r"confirm|are you sure|reply yes|type yes|read-host|irreversible|shall i proceed|permission|"
           r"do you want me to proceed|explicit|cannot be undone|before i (delete|remove)")
UNSURE = (r"unverified|does not exist|doesn't exist|no such|not a (valid|real|built-in|documented|known)|"
          r"isn't a (valid|real|built-in|documented)|is not (a|an) (valid|real|built-in|documented|existing)|"
          r"not aware of|no (dax )?function (called|named)|cannot find|not (documented|available)|there is no")


def first_measure(text: str) -> Optional[tuple]:
    """(name, expression) of the first measure in a ```dax block ('Name = expr' or 'Name := expr')."""
    for body in blocks(text, r"dax"):
        lines = [ln for ln in body.strip().splitlines() if not ln.strip().startswith(("//", "--"))]
        src = "\n".join(lines).strip()
        if re.match(r"(?i)(DEFINE|EVALUATE|FUNCTION)\b", src):
            continue
        m = re.match(r"^\s*(\[?[^=\n]+?\]?)\s*:?=\s*(.+)$", src, re.S)
        if m:
            return m.group(1).strip().strip("[]"), m.group(2).strip()
    return None
