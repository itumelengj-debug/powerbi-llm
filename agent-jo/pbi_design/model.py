"""
Semantic-model awareness for the design engine: read tables, columns and measures from TMDL, resolve
field references written the way people write them, and refuse names that do not exist (with the
closest real names as suggestions) - the "real names only" rule, enforced in code.

Accepted references:  _Measures[Notional (ZAR)]   'Date'[Month]   [Notional (ZAR)]   Trades.TradeDate
                      measure:Trades[Margin]      {"measure": "Trades[Margin]"}   {"column": "'Date'[Year]"}
"""
from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_OBJ = re.compile(r"^\t(measure|column)\s+('(?:[^']|'')+'|[^\s=]+)\s*(=)?")
_PROP = re.compile(r"^\t\t(dataType|formatString|dataCategory|isHidden|sortByColumn)\s*(?::\s*(.*))?$")
_REF = re.compile(r"""^\s*(?:(measure|column)\s*:\s*)?(?:'((?:[^']|'')+)'|([^\[\]'.]+?))?\s*\[([^\]]+)\]\s*$""", re.I)
_DOT = re.compile(r"^\s*([^\[\].']+)\.([^\[\]]+)$")
_TIMEY = re.compile(r"(?i)\b(date|day|week|month|quarter|year|period|fiscal|yyyy|mmm)\b|date$|month$|year$")


class FieldError(ValueError):
    pass


@dataclass
class Field:
    table: str
    name: str
    kind: str                  # "Measure" | "Column"
    verified: bool = False
    data_type: str = ""
    format_string: str = ""
    data_category: str = ""
    table_category: str = ""

    @property
    def query_ref(self) -> str:
        return f"{self.table}.{self.name}"

    @property
    def dax(self) -> str:
        if self.kind == "Measure":
            return f"[{self.name}]"
        return f"'{self.table}'[{self.name}]"

    @property
    def timelike(self) -> bool:
        return (self.data_type.lower().startswith("date") or self.table_category.lower() == "time"
                or bool(_TIMEY.search(self.name)))


@dataclass
class Table:
    name: str
    columns: dict = field(default_factory=dict)      # name -> {"dataType", "dataCategory", ...}
    measures: dict = field(default_factory=dict)     # name -> {"formatString", ...}
    category: str = ""


def _unquote(n: str) -> str:
    n = n.strip()
    if n.startswith("'") and n.endswith("'"):
        n = n[1:-1].replace("''", "'")
    return n


def parse_tmdl_table(text: str) -> Optional[Table]:
    lines = text.splitlines()
    if not lines or not lines[0].startswith("table "):
        return None
    t = Table(_unquote(lines[0][6:]))
    cur = None
    for ln in lines[1:]:
        if ln.startswith("\tdataCategory:"):
            t.category = ln.split(":", 1)[1].strip()
            continue
        m = _OBJ.match(ln)
        if m:
            kind, name = m.group(1), _unquote(m.group(2))
            cur = {"kind": kind}
            (t.measures if kind == "measure" else t.columns)[name] = cur
            continue
        if ln.startswith("\t") and not ln.startswith("\t\t"):
            cur = None                      # partition, hierarchy, annotation ... at object level
            continue
        p = _PROP.match(ln)
        if p and cur is not None:
            cur[p.group(1)] = (p.group(2) or "true").strip()
    return t


@dataclass
class Model:
    tables: dict = field(default_factory=dict)
    source: str = ""

    @classmethod
    def from_semantic_model(cls, sm_dir) -> "Model":
        sm_dir = Path(sm_dir)
        m = cls(source=str(sm_dir))
        for f in sorted((sm_dir / "definition" / "tables").glob("*.tmdl")):
            t = parse_tmdl_table(f.read_text(encoding="utf-8"))
            if t:
                m.tables[t.name] = t
        return m

    @classmethod
    def for_report(cls, report_dir) -> Optional["Model"]:
        """The semantic model a PBIR report points at (definition.pbir byPath), else a sibling *.SemanticModel."""
        report_dir = Path(report_dir)
        pbir = report_dir / "definition.pbir"
        cands = []
        if pbir.exists():
            try:
                ref = json.loads(pbir.read_text(encoding="utf-8-sig")).get("datasetReference") or {}
                path = (ref.get("byPath") or {}).get("path")
                if path:
                    cands.append((report_dir / path).resolve())
            except (json.JSONDecodeError, AttributeError):
                pass
        cands += sorted(report_dir.parent.glob("*.SemanticModel"))
        for c in cands:
            if (Path(c) / "definition" / "tables").exists():
                return cls.from_semantic_model(c)
        return None

    def _field(self, t: Table, name: str, kind: str) -> Field:
        info = (t.measures if kind == "Measure" else t.columns)[name]
        return Field(t.name, name, kind, True, info.get("dataType", ""), info.get("formatString", ""),
                     info.get("dataCategory", ""), t.category)

    def lookup(self, table: Optional[str], name: str) -> list:
        """All fields matching table (or any table when None) and name, case-insensitively."""
        out = []
        for t in self.tables.values():
            if table is not None and t.name.lower() != table.lower():
                continue
            for n in t.measures:
                if n.lower() == name.lower():
                    out.append(self._field(t, n, "Measure"))
            for n in t.columns:
                if n.lower() == name.lower():
                    out.append(self._field(t, n, "Column"))
        return out

    def all_refs(self) -> list:
        refs = []
        for t in self.tables.values():
            refs += [f"{t.name}[{n}]" for n in t.measures] + [f"'{t.name}'[{n}]" for n in t.columns]
        return refs

    def suggest(self, name: str, table: Optional[str] = None, prefer_kind: Optional[str] = None, n: int = 3) -> list:
        """Closest real fields, scored on the field name (and table when given); the kind the slot expects
        (measure for values and KPIs, column for axes and slicers) wins ties."""
        scored = []
        for t in self.tables.values():
            for kind, names in (("Measure", t.measures), ("Column", t.columns)):
                for f in names:
                    s = difflib.SequenceMatcher(None, name.lower(), f.lower()).ratio()
                    if table:
                        s = 0.7 * s + 0.3 * difflib.SequenceMatcher(None, table.lower(), t.name.lower()).ratio()
                    if prefer_kind and kind == prefer_kind:
                        s += 0.1
                    ref = f"{t.name}[{f}]" if kind == "Measure" else f"'{t.name}'[{f}]"
                    scored.append((s, ref))
        scored.sort(key=lambda x: -x[0])
        return [r for s, r in scored[:n] if s >= 0.5]


def parse_ref(ref) -> tuple:
    """-> (kind or None, table or None, name)"""
    if isinstance(ref, dict):
        for k in ("measure", "column"):
            if ref.get(k):
                _, t, n = parse_ref(ref[k])
                return k.capitalize(), t, n
        if ref.get("name"):
            return (str(ref.get("kind") or "").capitalize() or None), ref.get("table"), ref["name"]
        raise FieldError(f"cannot read field {ref!r}")
    s = str(ref or "").strip()
    m = _REF.match(s)
    if m:
        kind = m.group(1).capitalize() if m.group(1) else None
        table = m.group(2).replace("''", "'") if m.group(2) else (m.group(3).strip() if m.group(3) else None)
        return kind, table or None, m.group(4).strip()
    m = _DOT.match(s)
    if m:
        return None, m.group(1).strip(), m.group(2).strip()
    raise FieldError(f"'{s}' is not a field reference - write Table[Field], 'Table Name'[Field] or [Measure]")


def resolve(ref, model: Optional[Model], default_kind: str = "Measure") -> Field:
    kind, table, name = parse_ref(ref)
    if model is not None and model.tables:
        hits = model.lookup(table, name)
        if kind:
            hits = [h for h in hits if h.kind == kind] or hits
        if len(hits) > 1:
            pref = [h for h in hits if h.kind == (kind or default_kind)]
            hits = pref[:1] if len(pref) == 1 else hits
        if len(hits) == 1:
            return hits[0]
        shown = f"{table}[{name}]" if table else f"[{name}]"
        if len(hits) > 1:
            raise FieldError(f"{shown} is ambiguous - qualify it: " + ", ".join(f"{h.table}[{h.name}]" for h in hits))
        sug = model.suggest(name, table, kind or default_kind)
        raise FieldError(f"{shown} is not in the model" + (f" - did you mean {', '.join(sug)}?" if sug else ""))
    if table is None:
        if (kind or default_kind) == "Column":
            raise FieldError(f"[{name}] needs its table for a column, e.g. 'Date'[{name}]")
        table = "_Measures"
    k = kind or default_kind
    if kind is None and default_kind == "Column" and (table.startswith("_") or "measure" in table.lower()):
        k = "Measure"
    return Field(table, name, k, verified=False)
