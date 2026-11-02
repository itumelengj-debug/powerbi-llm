"""
jo_powerbi.py - Power BI route for Agent Jo.

Routes Power BI requests to the local `powerbi` Ollama model while keeping its
built-in rules, exposes five gated tools, and verifies every artifact before
Jo's release step. Standard library only.

Wiring inside Jo:

    from jo_powerbi import PowerBIAgent, PowerBIConfig, is_powerbi_request

    pbi = PowerBIAgent(
        PowerBIConfig(allowed_roots=[r"C:\\PBI"]),
        approve=jo_policy_gate,      # (action: dict) -> bool   - your approval UI / policy
        audit=jo_audit_log,          # (event: dict) -> None    - your audit log
        web_search=jo_web_search,    # (query: str) -> [{"title","url","snippet"}]  (optional)
    )

    if is_powerbi_request(text):                       # or Jo's own router
        res = pbi.run(text, jo_system_prompt=JO_RULES, images=[png_bytes])
        # res.text   -> Jo's PII scan + release
        # res.issues -> non-empty = verification failed after the fix round: block or flag
        # res.needs_user -> steps only the user can do (e.g. Apply a staged TMDL script)

Optional extras (all off unless you pass them):
    dax_runner=DesktopDaxRunner()      # read-only run_dax_query tool against the open Desktop model
    schema_validator=SchemaValidator() # PBIR $schema validation (pbir_schema.py, needs jsonschema)
    on_token=lambda kind, text: ...    # stream tokens: kind is "content", "thinking" or "status"
    PowerBIConfig(brain_mode="modular")  # send only the brain sections the request needs

Fail-closed defaults: no approve callback = every write and script is denied.
"""
from __future__ import annotations

import base64
import hashlib
import json
import platform
import re
import shutil
import subprocess
import tempfile
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

# --------------------------------------------------------------------------- config


@dataclass
class PowerBIConfig:
    backend: str = "ollama"            # "ollama" | "llamacpp" (llama-server OpenAI endpoint)
    ollama_url: str = "http://127.0.0.1:11434"
    llamacpp_url: str = "http://127.0.0.1:8088"
    system_prompt_path: str = ""       # llamacpp: file holding the brain (see brain_from_modelfile)
    model: str = "powerbi"
    allowed_roots: list = field(default_factory=list)      # folders the agent may read/write
    backup_dir: str = str(Path.home() / "PBI-Backups")
    staging_dir: str = str(Path.home() / "PBI-Staging")
    think: Optional[bool] = None       # None = auto: on for reasoning-heavy asks, off for builds
    max_tool_rounds: int = 8
    max_fix_rounds: int = 1            # automatic re-asks when verification fails
    approve_writes: bool = True        # False = report-file writes inside allowed_roots skip approval
    request_timeout: int = 1800
    doc_domains: tuple = ("learn.microsoft.com",)
    brain_mode: str = "full"           # "full" | "modular": core rules + only the sections the request needs
    dax_max_rows: int = 50             # rows per result set returned to the model by run_dax_query


@dataclass
class PowerBIResult:
    text: str
    issues: list
    actions: list
    needs_user: list
    thinking: str = ""


class ToolError(Exception):
    pass


# --------------------------------------------------------------------------- routing

_PBI_TERMS = re.compile(
    r"(?i)\b(power\s*bi|pbi[xpr]?|dax|tmdl|power\s*query|semantic\s+model|measures?|visuals?|slicers?|"
    r"report\s+page|dataflows?|direct\s*lake|rls|calculation\s+group|field\s+parameter|paginated|xmla|"
    r"matrix\s+visual|drillthrough|bookmarks?)\b"
)
_REASONING = re.compile(
    r"(?i)\b(why|slow|optimi[sz]e|debug|wrong|incorrect|error|design|modell?ing|relationship|context|"
    r"calculation\s+group|rls|security|performance|explain|compare)\b"
)


def is_powerbi_request(text: str) -> bool:
    return bool(_PBI_TERMS.search(text or ""))


# --------------------------------------------------------------------------- tool schemas

TOOLS = [
    {"type": "function", "function": {
        "name": "read_project",
        "description": "Inventory a Power BI project (PBIP) folder: tables, columns, measures, relationships, "
                       "report pages and visuals with positions. Call before building against a model you have not seen.",
        "parameters": {"type": "object", "properties": {
            "project_root": {"type": "string", "description": "Folder that contains the .pbip file"}},
            "required": ["project_root"]}}},
    {"type": "function", "function": {
        "name": "write_report_files",
        "description": "Create or replace PBIR page/visual files. Validates JSON and names, refuses while Power BI "
                       "Desktop is open, backs up the report, writes files and registers new pages in pages.json. "
                       "Requires user approval. Cannot delete anything.",
        "parameters": {"type": "object", "properties": {
            "report_path": {"type": "string", "description": "Path to the <Name>.Report folder"},
            "files": {"type": "array", "items": {"type": "object", "properties": {
                "path": {"type": "string",
                         "description": "Relative to <Name>.Report/definition, e.g. pages/<page>/visuals/<visual>/visual.json"},
                "content": {"type": "string", "description": "Complete JSON file content"}},
                "required": ["path", "content"]}}},
            "required": ["report_path", "files"]}}},
    {"type": "function", "function": {
        "name": "stage_tmdl",
        "description": "Stage a createOrReplace TMDL script (tabs for indentation, 'ref table' for existing tables) "
                       "for the user to paste into TMDL view and Apply. Use for measures, columns, calculation "
                       "groups, functions and relationships.",
        "parameters": {"type": "object", "properties": {
            "script": {"type": "string"},
            "label": {"type": "string", "description": "Short description, e.g. 'YTD measures'"}},
            "required": ["script", "label"]}}},
    {"type": "function", "function": {
        "name": "run_powershell",
        "description": "Run a PowerShell script on the user's Windows PC. Always requires user approval. Use only "
                       "when no dedicated tool fits (REST API calls, refresh, inventory).",
        "parameters": {"type": "object", "properties": {
            "script": {"type": "string"},
            "purpose": {"type": "string", "description": "One line: what it does and what it changes"}},
            "required": ["script", "purpose"]}}},
    {"type": "function", "function": {
        "name": "lookup_docs",
        "description": "Search Microsoft Learn for current Power BI documentation. The query must be generic: never "
                       "include table, column or measure names, data values, emails or IDs.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
]

TOOLS_NOTE = """## TOOLS (this session)
You have tools: read_project, write_report_files, stage_tmdl, run_powershell, lookup_docs.
- Use read_project before building against an unfamiliar model.
- Use write_report_files for PBIR pages and visuals (not PowerShell). New page: include its page.json; pages.json registration is automatic.
- Use stage_tmdl for semantic-model changes; the user applies it in TMDL view, so build visuals that depend on new measures only after saying so.
- Use lookup_docs when something is Unverified; keep queries generic.
- A tool result starting with DENIED or ERROR means nothing changed: explain and stop or correct.
- Your final message summarises what was done and lists anything the user must still do."""

RUN_DAX_TOOL = {"type": "function", "function": {
    "name": "run_dax_query",
    "description": "Run a read-only DAX query (DEFINE ... EVALUATE ...) against the semantic model open in Power BI "
                   "Desktop and return the first rows. Use it to test a new measure before delivering it, e.g. "
                   "DEFINE MEASURE _Measures[Test] = <expr> EVALUATE SUMMARIZECOLUMNS('Date'[Year], \"v\", [Test]). "
                   "Cannot change the model.",
    "parameters": {"type": "object", "properties": {
        "query": {"type": "string", "description": "A DAX query starting with DEFINE or EVALUATE"}},
        "required": ["query"]}}}

TOOLS_NOTE_DAX = ("- Use run_dax_query to test every new or changed measure against the open model before you "
                  "deliver it (DEFINE MEASURE ... EVALUATE ...). If it errors or returns only blanks, fix it first. "
                  "Never claim a measure was tested unless a run_dax_query result shows it.")


def tools_note(has_dax: bool = False) -> str:
    if not has_dax:
        return TOOLS_NOTE
    return TOOLS_NOTE.replace("lookup_docs.\n", "lookup_docs, run_dax_query.\n", 1) + "\n" + TOOLS_NOTE_DAX


# --------------------------------------------------------------------------- verifier

_BLOCK = re.compile(r"```([\w#+-]*)[^\n]*\n(.*?)```", re.S)
_HEX20 = re.compile(r"^[0-9a-f]{20}$")
_NAME = re.compile(r"^[\w-]{1,50}$")
_SECRET = re.compile(r"(?i)(password|passwd|client_secret|secret|apikey|api_key)\s*[=:]\s*['\"][^'\"$]{4,}['\"]")
_PLACEHOLDER = re.compile(r"(?i)(^|\s)\.\.\.(\s|$)|rest unchanged|same as before")
_FIELD_KINDS = {"Measure", "Column", "Aggregation", "HierarchyLevel"}


def _check_visual(j: dict, where: str) -> list:
    issues = []
    name = j.get("name", "")
    if not _NAME.match(str(name)):
        issues.append(f"{where}: visual name '{name}' is not a legal PBIR name")
    if "$schema" not in j:
        issues.append(f"{where}: missing $schema")
    vis = j.get("visual")
    if vis is None and "visualGroup" not in j:
        issues.append(f"{where}: missing 'visual' object")
        return issues
    if vis is None:
        return issues
    if not vis.get("visualType"):
        issues.append(f"{where}: missing visual.visualType")
    q = vis.get("query") or {}
    if "drillFilterOtherVisuals" in q:
        issues.append(f"{where}: drillFilterOtherVisuals belongs under visual, not visual.query")
    for role, spec in (q.get("queryState") or {}).items():
        refs = []
        for p in (spec or {}).get("projections", []):
            kinds = set((p.get("field") or {}).keys()) & _FIELD_KINDS
            if len(kinds) != 1:
                issues.append(f"{where}: {role} projection needs exactly one of Measure/Column/Aggregation")
            if not p.get("queryRef") or not p.get("nativeQueryRef"):
                issues.append(f"{where}: {role} projection missing queryRef or nativeQueryRef")
            refs.append(p.get("nativeQueryRef"))
        if len(refs) != len(set(refs)):
            issues.append(f"{where}: duplicate nativeQueryRef in {role}")
    if vis.get("visualContainerObjects") and "padding" not in vis["visualContainerObjects"]:
        issues.append(f"{where}: visualContainerObjects set without explicit padding")
    return issues


def _check_page(j: dict, where: str) -> list:
    issues = []
    if not _NAME.match(str(j.get("name", ""))):
        issues.append(f"{where}: page name is not a legal PBIR name")
    if "visualContainers" in j:
        issues.append(f"{where}: page.json must not contain visualContainers")
    if j.get("displayOption") not in (None, "FitToPage", "FitToWidth", "ActualSize"):
        issues.append(f"{where}: displayOption must be FitToPage, FitToWidth or ActualSize")
    return issues


def _check_json_doc(j, where: str) -> list:
    if not isinstance(j, dict):
        return []
    if "position" in j or "visual" in j or "visualGroup" in j:
        return _check_visual(j, where)
    if "displayName" in j and "height" in j and "width" in j:
        return _check_page(j, where)
    return []


_CITE = re.compile(r"\[(S\d+)\]")


def context_ids(context: Optional[list]) -> list:
    return [str(c.get("id") or f"S{i}") for i, c in enumerate(context or [], 1)]


def check_citations(text: str, context: Optional[list]) -> list:
    """Flag [Sn] tags that do not match a passage the model was actually given."""
    if not context:
        return []
    known = set(context_ids(context))
    bad = sorted({t for t in _CITE.findall(text or "") if t not in known}, key=lambda t: int(t[1:]))
    if not bad:
        return []
    return [f"cites {', '.join('[' + t + ']' for t in bad)} but only {', '.join(sorted(known, key=lambda t: (len(t), t)))} "
            "were provided - cite a real passage or mark the point Unverified"]


def verify_artifacts(text: str, context: Optional[list] = None,
                     schema_check: Optional[Callable[[dict, str], list]] = None) -> list:
    """Return a list of problems found in the model's final answer (empty = pass).
    context: the retrieved passages the answer was grounded in (enables the citation check).
    schema_check: optional (doc, where) -> [problems], e.g. pbir_schema.SchemaValidator()."""
    issues = check_citations(text, context)
    for n, m in enumerate(_BLOCK.finditer(text or ""), 1):
        lang, body = m.group(1).lower(), m.group(2)
        where = f"block {n} ({lang or 'untagged'})"
        before = [ln for ln in text[: m.start()].splitlines() if ln.strip()]
        if not before or not before[-1].lstrip().startswith("###"):
            issues.append(f"{where}: missing '### <CHANNEL>' header line")
        if lang in ("dax", "m", "powershell", "tmdl", "json") and _PLACEHOLDER.search(body):
            issues.append(f"{where}: contains placeholder text ('...' or similar)")
        if _SECRET.search(body):
            issues.append(f"{where}: hard-coded secret")
        if lang == "json":
            try:
                doc = json.loads(body)
                issues += _check_json_doc(doc, where)
                if schema_check and isinstance(doc, dict):
                    issues += schema_check(doc, where)
            except json.JSONDecodeError as e:
                issues.append(f"{where}: invalid JSON ({e.msg}, line {e.lineno})")
        elif lang == "tmdl":
            lines = body.splitlines()
            if any(re.match(r"^ {2,}\S", ln) for ln in lines):
                issues.append(f"{where}: TMDL must be indented with tabs, not spaces")
            if lines and lines[0].strip() == "createOrReplace" and any(re.match(r"^\ttable\s", ln) for ln in lines):
                issues.append(f"{where}: 'table' without 'ref' replaces the whole table - use 'ref table'")
        elif lang == "powershell":
            if re.search(r'@"\s*\n[\s\S]*?\$schema', body):
                issues.append(f"{where}: double-quoted here-string expands $schema - use @' '@")
            for k, hs in enumerate(re.findall(r"@'\r?\n(.*?)\r?\n'@", body, re.S), 1):
                if hs.lstrip().startswith(("{", "[")):
                    try:
                        doc = json.loads(hs)
                        issues += _check_json_doc(doc, f"{where} here-string {k}")
                        if schema_check and isinstance(doc, dict):
                            issues += schema_check(doc, f"{where} here-string {k}")
                    except json.JSONDecodeError as e:
                        issues.append(f"{where} here-string {k}: invalid JSON ({e.msg})")
            writes_report = re.search(r"(?i)definition", body) and re.search(r"(?i)WriteAllText|Set-Content|Out-File", body)
            if writes_report:
                if "PBIDesktop" not in body:
                    issues.append(f"{where}: writes report files without checking Power BI Desktop is closed")
                if not re.search(r"(?i)Copy-Item|git\s+commit|backup", body):
                    issues.append(f"{where}: writes report files without a backup step")
    return issues


# --------------------------------------------------------------------------- helpers

_REL_OK = re.compile(r"^pages/([\w-]+)/(page\.json|visuals/([\w-]+)/(visual|mobile)\.json)$")
_TMDL_OBJ = re.compile(r"^\t(measure|column|calculationGroup)\s+('(?:[^']|'')+'|[^\s=]+)")
_COMMON = {"date", "year", "month", "value", "amount", "total", "count", "name", "table", "sales", "measure"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def desktop_running() -> bool:
    if platform.system() != "Windows":
        return False
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq PBIDesktop.exe", "/NH"],
                         capture_output=True, text=True).stdout
    return "PBIDesktop.exe" in out


def _b64(img) -> str:
    if isinstance(img, (bytes, bytearray)):
        return base64.b64encode(img).decode()
    p = Path(str(img))
    if p.exists():
        return base64.b64encode(p.read_bytes()).decode()
    return str(img)  # assume already base64


def _grounding_block(context: list) -> str:
    """Render Jo's retrieved passages with grounding rules (attempt-2 RAG layer, attempt-3 behaviour)."""
    lines = ["## RETRIEVED CONTEXT (Agent Jo)",
             "These passages are the source of truth for product facts, function names, properties and limits.",
             "Cite the tag ([S1], [S2]) after any claim that comes from them. Only cite tags listed below. If the",
             "context does not cover a point, say 'Unverified:' and give the check - never fill the gap from memory.",
             "When passages disagree, prefer the most recently updated one. Passages whose source starts with",
             "'project:' come from the user's own model and report: use them for real table, column and measure names.",
             ""]
    for tag, c in zip(context_ids(context), context):
        src = str(c.get("source") or c.get("url") or "")
        date = str(c.get("date") or "").strip()
        text = str(c.get("text") or c.get("content") or "").strip()
        lines.append(f"[{tag}] {src}" + (f" (updated {date})" if date else "") + f"\n{text}\n")
    return "\n".join(lines)


def brain_from_modelfile(path: str) -> str:
    """Extract the SYSTEM block from a Modelfile (for runtimes without /api/show)."""
    t = Path(path).read_text(encoding="utf-8")
    m = re.search(r'SYSTEM\s+"""(.*?)"""', t, re.S)
    if not m:
        raise ValueError(f"no SYSTEM \"\"\" block in {path}")
    return m.group(1).strip()


# --------------------------------------------------------------------------- modular brain

_SECTION = re.compile(r"(?m)^## (\d+)\. ")
_CORE_SECTIONS = {1, 2, 3, 11}                   # scope, operating rules, delivery channels, safety
_SECTION_TRIGGERS = {
    4: r"page|visual|report|build|add|create|my model|my report|pbip|project|table|column",
    5: r"dax|measure|calculat|ytd|mtd|qtd|yoy|year.over.year|time intelligence|filter context|context transition|"
       r"udf|user.defined function|visual calculation|field parameter|rls|row.level|\bsum\b|divide|evaluate|"
       r"summarize|iterator|variable|\bvar\b|slow measure|blank",
    6: r"power query|m code|m query|query folding|fold|csv|excel file|connector|dataflow|incremental refresh|"
       r"privacy level|transform|advanced editor|parameter|source|sharepoint|odbc|sql server|web\.contents",
    7: r"model|relationship|star schema|storage mode|import mode|directquery|direct lake|dual|composite|aggregation|"
       r"hybrid table|rls|ols|row.level|object.level|perspective|translation|tmdl|calculation group|cardinality|"
       r"bidirectional|many.to.many|role|dimension|fact table|date table",
    8: r"page|visual|pbir|pbip|report file|visual\.json|page\.json|layout|bookmark|drillthrough|tooltip page|"
       r"slicer|textbox|card|chart|matrix|table visual|navigation|button|mobile layout|report folder",
    9: r"design|layout|theme|colou?r|font|accessib|mobile|dashboard|look|style|ux|page|visual|chart",
    10: r"service|workspace|deploy|pipeline|gateway|refresh|rest api|\bapi\b|capacity|licen|tenant|admin|git|fabric|"
        r"powershell|xmla|endorse|sensitivity|app\b|publish|semantic.link|notebook|schedule|premium|ppu",
}


def split_brain(brain: str) -> tuple:
    """Split the rulebook into (preamble, [(number, text), ...]) on '## N. TITLE' headings."""
    starts = [(m.start(), int(m.group(1))) for m in _SECTION.finditer(brain)]
    if not starts:
        return brain, []
    sections = []
    for i, (pos, num) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(brain)
        sections.append((num, brain[pos:end].rstrip()))
    return brain[: starts[0][0]].rstrip(), sections


def select_brain(brain: str, request: str, has_images: bool = False) -> tuple:
    """Return (prompt, included_section_numbers). Core sections are always kept; topic sections only when the
    request mentions their topic. Falls back to the full brain when nothing matches or the brain has no sections."""
    pre, sections = split_brain(brain)
    if not sections:
        return brain, []
    low = (request or "").lower()
    picked = {n for n, rx in _SECTION_TRIGGERS.items() if re.search(rx, low)}
    if has_images:
        picked |= {8, 9}
    if not picked:
        return brain, [n for n, _ in sections]
    if 4 in picked or 8 in picked:
        picked.add(4)
    keep = _CORE_SECTIONS | picked
    included = [n for n, _ in sections if n in keep]
    omitted = [n for n, _ in sections if n not in keep]
    parts = [pre] + [t for n, t in sections if n in keep]
    if omitted:
        parts.append(f"(Sections {', '.join(map(str, omitted))} of the rulebook were omitted because this request "
                     "does not appear to need them. If it does, follow general Power BI best practice and say so.)")
    return "\n\n".join(x for x in parts if x), included


# --------------------------------------------------------------------------- DAX queries

_DAX_START = re.compile(r"(?is)^\s*(?:(?://|--)[^\n]*\n\s*|/\*.*?\*/\s*)*(DEFINE|EVALUATE)\b")


def dax_query_violations(query: str) -> list:
    """run_dax_query accepts DAX queries only (DEFINE/EVALUATE). XMLA, DMVs and MDX are refused."""
    v = []
    q = query or ""
    if not q.strip():
        return ["empty query"]
    if len(q) > 20000:
        v.append("query longer than 20,000 characters")
    if not _DAX_START.match(q):
        v.append("must start with DEFINE or EVALUATE")
    if re.search(r"(?i)\$system\.|<\s*(Batch|Alter|Create|Delete|Refresh|Execute|Statement)\b", q):
        v.append("XMLA commands and DMV queries are not allowed")
    return v


def _default_audit_factory(staging_dir: str):
    path = Path(staging_dir) / "audit.jsonl"

    def audit(event: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    return audit


# --------------------------------------------------------------------------- agent


class PowerBIAgent:
    def __init__(self, cfg: PowerBIConfig,
                 approve: Optional[Callable[[dict], bool]] = None,
                 audit: Optional[Callable[[dict], None]] = None,
                 web_search: Optional[Callable[[str], list]] = None,
                 dax_runner: Optional[Callable[[str, int], dict]] = None,
                 schema_validator: Optional[Callable[[dict, str], list]] = None,
                 on_token: Optional[Callable[[str, str], None]] = None):
        """dax_runner: (query, max_rows) -> {"results": [{"columns": [...], "rows": [[...]], "total": n}]};
        raise to report an error. Enables the read-only run_dax_query tool (see pbi_dax.DesktopDaxRunner).
        schema_validator: (doc, where) -> [problems]; checks PBIR files against their $schema.
        on_token: (kind, text) -> None; streams "content" and "thinking" tokens and "status" notes."""
        self.cfg = cfg
        self.approve = approve or (lambda action: False)          # fail closed
        self.audit = audit or _default_audit_factory(cfg.staging_dir)
        self.web_search = web_search
        self.dax_runner = dax_runner
        self.schema_validator = schema_validator
        self.on_token = on_token
        self._brain: Optional[str] = None
        self._caps: set = set()
        self._schema_names: set = set()

    # ---- transport (Ollama /api/chat or llama-server /v1/chat/completions)
    @property
    def _is_llamacpp(self) -> bool:
        return self.cfg.backend == "llamacpp"

    def _post(self, path: str, body: dict, timeout: Optional[int] = None) -> dict:
        base = self.cfg.llamacpp_url if self._is_llamacpp else self.cfg.ollama_url
        req = urllib.request.Request(base.rstrip("/") + path,
                                     data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout or self.cfg.request_timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def _post_stream(self, path: str, body: dict):
        """Yield JSON events from a streaming response: Ollama NDJSON lines or llama-server SSE 'data:' lines."""
        base = self.cfg.llamacpp_url if self._is_llamacpp else self.cfg.ollama_url
        req = urllib.request.Request(base.rstrip("/") + path,
                                     data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.cfg.request_timeout) as r:
            for raw in r:
                line = raw.decode("utf-8").strip()
                if not line:
                    continue
                if line.startswith("data:"):
                    line = line[5:].strip()
                    if line == "[DONE]":
                        return
                elif line.startswith((":", "event:")):
                    continue
                yield json.loads(line)

    def _emit(self, kind: str, text: str) -> None:
        if self.on_token and text:
            try:
                self.on_token(kind, text)
            except Exception:  # a broken UI callback must not kill the request
                pass

    def tools(self) -> list:
        return TOOLS + ([RUN_DAX_TOOL] if self.dax_runner else [])

    def brain(self) -> str:
        if self._brain is None:
            if self._is_llamacpp:
                p = Path(self.cfg.system_prompt_path or "")
                if not p.exists():
                    raise RuntimeError("backend 'llamacpp' needs system_prompt_path (extract it with: "
                                       "python jo_powerbi.py --extract-brain Modelfile > brain.txt)")
                self._brain = p.read_text(encoding="utf-8").strip()
                self._caps = {"tools"}
            else:
                info = self._post("/api/show", {"model": self.cfg.model}, timeout=30)
                self._brain = info.get("system") or ""
                self._caps = set(info.get("capabilities") or [])
            if not self._brain:
                raise RuntimeError(f"Model '{self.cfg.model}' has no system prompt - build it with build.ps1.")
        return self._brain

    def system_prompt(self, jo_system_prompt: str = "", context: Optional[list] = None,
                      request: str = "", has_images: bool = False) -> str:
        brain = self.brain()
        if self.cfg.brain_mode == "modular":
            brain, included = select_brain(brain, request, has_images)
            self.audit({"ts": _now(), "event": "pbi_brain", "mode": "modular", "sections": included,
                        "chars": len(brain)})
        parts = [brain]
        if jo_system_prompt.strip():
            parts.append("## HOST APPLICATION RULES (Agent Jo)\n" + jo_system_prompt.strip())
        parts.append(tools_note(bool(self.dax_runner)))
        if context:
            parts.append(_grounding_block(context))
        return "\n\n".join(parts)

    def _chat(self, messages: list, think: bool) -> dict:
        """Returns an Ollama-shaped message dict: role, content, optional thinking and tool_calls.
        Streams tokens to on_token when one is set."""
        stream = self.on_token is not None
        if self._is_llamacpp:
            body = {"model": self.cfg.model, "messages": messages, "stream": stream,
                    "tools": self.tools(), "tool_choice": "auto"}
            if stream:
                return self._llamacpp_stream(body)
            data = self._post("/v1/chat/completions", body)
            m = (data.get("choices") or [{}])[0].get("message") or {}
            return self._llamacpp_message(m.get("content"), m.get("reasoning_content"), m.get("tool_calls") or [])
        body = {"model": self.cfg.model, "messages": messages, "stream": stream, "tools": self.tools()}
        if "thinking" in self._caps:
            body["think"] = think
        # No options.num_ctx: keep the context the runtime was started with.
        if not stream:
            return self._post("/api/chat", body)["message"]
        content, thinking, calls = [], [], []
        for ev in self._post_stream("/api/chat", body):
            if ev.get("error"):
                raise RuntimeError(f"Ollama error: {ev['error']}")
            m = ev.get("message") or {}
            if m.get("thinking"):
                thinking.append(m["thinking"])
                self._emit("thinking", m["thinking"])
            if m.get("content"):
                content.append(m["content"])
                self._emit("content", m["content"])
            calls += m.get("tool_calls") or []
        out = {"role": "assistant", "content": "".join(content)}
        if thinking:
            out["thinking"] = "".join(thinking)
        if calls:
            out["tool_calls"] = calls
        return out

    @staticmethod
    def _llamacpp_message(content, reasoning, tool_calls) -> dict:
        calls = []
        for c in tool_calls:
            fn = c.get("function") or {}
            calls.append({"id": c.get("id"), "type": "function",
                          "function": {"name": fn.get("name", ""), "arguments": fn.get("arguments") or {}}})
        out = {"role": "assistant", "content": content or ""}
        if reasoning:
            out["thinking"] = reasoning
        if calls:
            out["tool_calls"] = calls
        return out

    def _llamacpp_stream(self, body: dict) -> dict:
        content, reasoning, calls = [], [], {}
        for ev in self._post_stream("/v1/chat/completions", body):
            delta = ((ev.get("choices") or [{}])[0]).get("delta") or {}
            if delta.get("reasoning_content"):
                reasoning.append(delta["reasoning_content"])
                self._emit("thinking", delta["reasoning_content"])
            if delta.get("content"):
                content.append(delta["content"])
                self._emit("content", delta["content"])
            for tc in delta.get("tool_calls") or []:   # OpenAI streams tool calls as fragments keyed by index
                slot = calls.setdefault(tc.get("index", len(calls)),
                                        {"id": None, "function": {"name": "", "arguments": ""}})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                slot["function"]["name"] += fn.get("name") or ""
                slot["function"]["arguments"] += fn.get("arguments") or ""
        return self._llamacpp_message("".join(content), "".join(reasoning), [calls[i] for i in sorted(calls)])

    def _user_message(self, text: str, images: Optional[list]) -> dict:
        if not images:
            return {"role": "user", "content": text}
        if self._is_llamacpp:
            parts = [{"type": "text", "text": text}]
            for i in images:
                parts.append({"type": "image_url",
                              "image_url": {"url": "data:image/png;base64," + _b64(i)}})
            return {"role": "user", "content": parts}
        return {"role": "user", "content": text, "images": [_b64(i) for i in images]}

    def _tool_message(self, name: str, call: dict, content: str) -> dict:
        if self._is_llamacpp:
            return {"role": "tool", "tool_call_id": call.get("id") or name, "name": name, "content": content}
        return {"role": "tool", "tool_name": name, "content": content}

    # ---- main loop
    def run(self, user_text: str, jo_system_prompt: str = "", images: Optional[list] = None,
            history: Optional[list] = None, think: Optional[bool] = None,
            context: Optional[list] = None) -> PowerBIResult:
        """context: retrieved passages from Jo's RAG layer, e.g.
        [{"id": "S1", "source": "learn.microsoft.com/...", "text": "..."}]"""
        messages = [{"role": "system", "content": self.system_prompt(jo_system_prompt, context,
                                                                     request=user_text, has_images=bool(images))}]
        messages += list(history or [])
        messages.append(self._user_message(user_text, images))

        if think is None:
            think = self.cfg.think if self.cfg.think is not None else bool(_REASONING.search(user_text))
        actions, needs_user, thinking = [], [], []
        tool_rounds = fixes = 0
        self.audit({"ts": _now(), "event": "pbi_request", "think": think, "chars": len(user_text)})

        while True:
            msg = self._chat(messages, think)
            if msg.get("thinking"):
                thinking.append(msg["thinking"])
            messages.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")})
            calls = msg.get("tool_calls") or []

            if calls:
                tool_rounds += 1
                if tool_rounds > self.cfg.max_tool_rounds:
                    text = "Stopped: tool-call limit reached. Actions so far are listed in the audit log."
                    return PowerBIResult(text, ["tool-call limit reached"], actions, needs_user, "\n".join(thinking))
                for call in calls:
                    fn = call.get("function") or {}
                    name, args = fn.get("name", ""), fn.get("arguments") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {}
                    self._emit("status", f"[tool] {name}\n")
                    result = self._dispatch(name, args, actions, needs_user)
                    messages.append(self._tool_message(name, call, result))
                continue

            text = msg.get("content") or ""
            issues = verify_artifacts(text, context=context, schema_check=self.schema_validator)
            if issues and fixes < self.cfg.max_fix_rounds:
                fixes += 1
                self._emit("status", f"\n[verify] {len(issues)} issue(s) found - asking the model to fix them\n")
                self.audit({"ts": _now(), "event": "pbi_verify_fail", "issues": issues, "round": fixes})
                messages.append({"role": "user", "content":
                                 "Automated verification failed. Fix only these issues and resend the complete "
                                 "corrected artifacts:\n- " + "\n- ".join(issues)})
                continue
            self.audit({"ts": _now(), "event": "pbi_result", "issues": issues,
                        "actions": [{"tool": a["tool"], "status": a["status"]} for a in actions]})
            return PowerBIResult(text, issues, actions, needs_user, "\n".join(thinking))

    # ---- tool dispatch
    def _dispatch(self, name: str, args: dict, actions: list, needs_user: list) -> str:
        handlers = {
            "read_project": self.tool_read_project,
            "write_report_files": self.tool_write_report_files,
            "stage_tmdl": self.tool_stage_tmdl,
            "run_powershell": self.tool_run_powershell,
            "lookup_docs": self.tool_lookup_docs,
        }
        if self.dax_runner:
            handlers["run_dax_query"] = self.tool_run_dax_query
        entry = {"ts": _now(), "event": "pbi_tool", "tool": name}
        try:
            if name not in handlers:
                raise ToolError(f"unknown tool '{name}'")
            out = handlers[name](**args)
            status = "denied" if out.startswith("DENIED") else "ok"
            if name == "stage_tmdl" and status == "ok":
                needs_user.append(out)
        except TypeError as e:
            out, status = f"ERROR: bad arguments for {name}: {e}", "error"
        except ToolError as e:
            out, status = f"ERROR: {e}", "error"
        except Exception as e:  # tool failure must not crash Jo
            out, status = f"ERROR: {type(e).__name__}: {e}", "error"
        entry.update(status=status, detail=out[:500])
        self.audit(entry)
        actions.append({"tool": name, "status": status, "detail": out[:500]})
        return out

    def _confine(self, p: str) -> Path:
        rp = Path(p).expanduser().resolve()
        for root in self.cfg.allowed_roots:
            r = Path(root).expanduser().resolve()
            if rp == r or r in rp.parents:
                return rp
        raise ToolError(f"{p} is outside the folders Agent Jo may touch ({', '.join(self.cfg.allowed_roots) or 'none configured'})")

    # ---- tools
    def tool_read_project(self, project_root: str) -> str:
        root = self._confine(project_root)
        out = {"semantic_models": [], "reports": []}
        for sm in sorted(root.glob("*.SemanticModel")):
            model = {"name": sm.name, "tables": {}, "relationships": ""}
            for f in sorted((sm / "definition" / "tables").glob("*.tmdl")):
                lines = f.read_text(encoding="utf-8").splitlines()
                tname = f.stem
                if lines and lines[0].startswith("table "):
                    tname = lines[0][6:].strip().strip("'")
                objs = []
                for ln in lines:
                    m = _TMDL_OBJ.match(ln)
                    if m:
                        oname = m.group(2).strip("'").replace("''", "'")
                        objs.append(f"{m.group(1)} {oname}")
                        self._schema_names.add(oname)
                self._schema_names.add(tname)
                model["tables"][tname] = objs
            rel = sm / "definition" / "relationships.tmdl"
            if rel.exists():
                model["relationships"] = rel.read_text(encoding="utf-8")[:4000]
            out["semantic_models"].append(model)
        for rp in sorted(root.glob("*.Report")):
            rep = {"name": rp.name, "path": str(rp), "format": "PBIR" if (rp / "definition").exists() else "PBIR-Legacy",
                   "pages": []}
            pages_dir = rp / "definition" / "pages"
            order = []
            if (pages_dir / "pages.json").exists():
                order = json.loads((pages_dir / "pages.json").read_text(encoding="utf-8")).get("pageOrder", [])
            for pdir in sorted(pages_dir.glob("*/"), key=lambda d: order.index(d.name) if d.name in order else 999):
                pj = pdir / "page.json"
                if not pj.exists():
                    continue
                page = json.loads(pj.read_text(encoding="utf-8"))
                visuals = []
                for vj in sorted(pdir.glob("visuals/*/visual.json")):
                    v = json.loads(vj.read_text(encoding="utf-8"))
                    pos = v.get("position", {})
                    visuals.append({"name": v.get("name"), "type": (v.get("visual") or {}).get("visualType"),
                                    "x": pos.get("x"), "y": pos.get("y"), "w": pos.get("width"), "h": pos.get("height")})
                rep["pages"].append({"name": page.get("name"), "displayName": page.get("displayName"),
                                     "size": f"{page.get('width')}x{page.get('height')}", "visuals": visuals})
            out["reports"].append(rep)
        return json.dumps(out, ensure_ascii=False)[:20000]

    def tool_write_report_files(self, report_path: str, files: list) -> str:
        report = self._confine(report_path)
        definition = report / "definition"
        pages_json = definition / "pages" / "pages.json"
        if report.suffix != ".Report" or not pages_json.exists():
            raise ToolError(f"{report} is not a PBIR report folder (missing definition/pages/pages.json)")
        if not files:
            raise ToolError("no files given")

        staged, new_pages = [], []
        for f in files:
            rel = str(f.get("path", "")).replace("\\", "/").strip("/")
            if rel.startswith("definition/"):
                rel = rel[len("definition/"):]
            m = _REL_OK.match(rel)
            if not m:
                raise ToolError(f"path '{rel}' not allowed - only pages/<page>/page.json and "
                                "pages/<page>/visuals/<visual>/visual.json")
            try:
                doc = json.loads(f.get("content", ""))
            except json.JSONDecodeError as e:
                raise ToolError(f"{rel}: invalid JSON ({e.msg}, line {e.lineno})")
            problems = _check_json_doc(doc, rel)
            if self.schema_validator:
                problems += self.schema_validator(doc, rel)
            if m.group(2) == "page.json":
                if doc.get("name") != m.group(1):
                    problems.append(f"{rel}: name must equal its folder name '{m.group(1)}'")
                new_pages.append(m.group(1))
            elif m.group(4) == "visual" and doc.get("name") != m.group(3):
                problems.append(f"{rel}: name must equal its folder name '{m.group(3)}'")
            if problems:
                raise ToolError("; ".join(problems))
            staged.append((rel, json.dumps(doc, indent=2, ensure_ascii=False) + "\n"))

        for page in {s[0].split("/")[1] for s in staged}:
            if page not in new_pages and not (definition / "pages" / page / "page.json").exists():
                raise ToolError(f"page '{page}' does not exist - include its page.json")
        if desktop_running():
            raise ToolError("Power BI Desktop is open - close it, then retry")

        summary = f"Write {len(staged)} file(s) to {report.name}: " + ", ".join(r for r, _ in staged)
        if self.cfg.approve_writes and not self.approve({"tool": "write_report_files", "summary": summary,
                                                         "report": str(report), "files": [r for r, _ in staged]}):
            return "DENIED: the user did not approve the write. Nothing was changed."

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = Path(self.cfg.backup_dir) / f"{report.name}-{stamp}"
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(report, backup)

        hashes = {}
        for rel, content in staged:
            target = definition / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            data = content.encode("utf-8")
            target.write_bytes(data)
            hashes[rel] = _sha(data)

        pj = json.loads(pages_json.read_text(encoding="utf-8"))
        order = list(pj.get("pageOrder", []))
        added = [p for p in new_pages if p not in order]
        if added:
            pj["pageOrder"] = order + added
            pages_json.write_bytes((json.dumps(pj, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))

        self.audit({"ts": _now(), "event": "pbi_write", "report": str(report), "backup": str(backup),
                    "files": hashes, "pages_registered": added})
        return (f"OK: wrote {len(staged)} file(s); registered pages {added or 'none'}; backup at {backup}. "
                "User must reopen the .pbip in Power BI Desktop.")

    def tool_stage_tmdl(self, script: str, label: str) -> str:
        lines = script.strip("\n").splitlines()
        if not lines or lines[0].strip() != "createOrReplace":
            raise ToolError("script must start with 'createOrReplace'")
        if any(re.match(r"^ {2,}\S", ln) for ln in lines):
            raise ToolError("TMDL must be indented with tabs, not spaces")
        if any(re.match(r"^\ttable\s", ln) for ln in lines):
            raise ToolError("'table' without 'ref' would replace an entire table - use 'ref table'")
        staging = Path(self.cfg.staging_dir)
        staging.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^\w-]+", "-", label).strip("-")[:40] or "script"
        path = staging / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{safe}.tmdl"
        path.write_text(script.strip("\n") + "\n", encoding="utf-8")
        self.audit({"ts": _now(), "event": "pbi_tmdl_staged", "file": str(path), "sha": _sha(path.read_bytes())})
        return (f"OK: staged '{label}' at {path}. The user must open TMDL view in Power BI Desktop, paste it, "
                "click Apply, then Save.")

    def tool_run_powershell(self, script: str, purpose: str) -> str:
        if not self.approve({"tool": "run_powershell", "summary": purpose, "script": script}):
            return "DENIED: the user did not approve this script. Nothing was run."
        exe = "powershell.exe" if platform.system() == "Windows" else shutil.which("pwsh")
        if not exe:
            raise ToolError("PowerShell not available on this machine")
        with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8-sig") as tf:
            tf.write(script)
            tmp = tf.name
        try:
            p = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", tmp],
                               capture_output=True, text=True, timeout=600)
        finally:
            Path(tmp).unlink(missing_ok=True)
        self.audit({"ts": _now(), "event": "pbi_powershell", "purpose": purpose, "exit": p.returncode,
                    "sha": _sha(script.encode("utf-8"))})
        return f"EXIT {p.returncode}\nSTDOUT:\n{p.stdout[-6000:]}\nSTDERR:\n{p.stderr[-2000:]}"

    def tool_run_dax_query(self, query: str) -> str:
        problems = dax_query_violations(query)
        if problems:
            raise ToolError("query refused (" + "; ".join(problems) + ")")
        sha = _sha(query.encode("utf-8"))
        try:
            out = self.dax_runner(query, self.cfg.dax_max_rows) or {}
        except Exception as e:
            self.audit({"ts": _now(), "event": "pbi_dax", "ok": False, "sha": sha})
            return f"ERROR: the query failed: {str(e)[:2000]}"
        sets = out.get("results") or []
        # Values go back to the local model only; the audit log records shape, never data.
        self.audit({"ts": _now(), "event": "pbi_dax", "ok": True, "sha": sha,
                    "rows": [s.get("total", len(s.get("rows") or [])) for s in sets]})
        lines = []
        for n, rs in enumerate(sets, 1):
            cols, rows = rs.get("columns") or [], rs.get("rows") or []
            total = rs.get("total", len(rows))
            lines.append(f"Result {n}: {total} row(s)" + (f", first {len(rows)} shown" if total > len(rows) else ""))
            lines.append(" | ".join(map(str, cols)))
            for r in rows:
                lines.append(" | ".join("(blank)" if v is None else str(v) for v in r))
            values = [v for r in rows for v in (r[1:] if len(r) > 1 else r)]
            if values and all(v is None for v in values):
                lines.append("WARNING: every value is blank - check filters, relationships and the date table.")
        return ("OK\n" + "\n".join(lines))[:12000] if lines else "OK: the query returned no result sets."

    def outbound_violations(self, query: str) -> list:
        v = []
        if len(query) > 200:
            v.append("query longer than 200 characters")
        if re.search(r"\d{6,}", query):
            v.append("long number (possible ID or value)")
        if re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", query):
            v.append("email address")
        if re.search(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-", query):
            v.append("GUID")
        low = query.lower()
        for n in self._schema_names:
            if len(n) >= 5 and n.lower() not in _COMMON and n.lower() in low:
                v.append(f"model object name '{n}'")
        return v

    def tool_lookup_docs(self, query: str) -> str:
        if not self.web_search:
            return "ERROR: web lookup is not enabled in Agent Jo."
        problems = self.outbound_violations(query)
        if problems:
            self.audit({"ts": _now(), "event": "pbi_outbound_blocked", "reasons": problems})
            return "ERROR: query blocked (" + "; ".join(problems) + "). Rephrase generically."
        hits = self.web_search(query) or []
        keep = [h for h in hits if any(d in str(h.get("url", "")) for d in self.cfg.doc_domains)][:5]
        self.audit({"ts": _now(), "event": "pbi_lookup", "query": query, "results": len(keep)})
        if not keep:
            return "No Microsoft Learn results. Mark the point Unverified and give the user a check."
        return json.dumps(keep, ensure_ascii=False)[:8000]


if __name__ == "__main__":  # python jo_powerbi.py --extract-brain ..\Modelfile > brain.txt
    import sys
    if len(sys.argv) == 3 and sys.argv[1] == "--extract-brain":
        sys.stdout.write(brain_from_modelfile(sys.argv[2]))
    else:
        sys.stderr.write(__doc__ + "\nUsage: python jo_powerbi.py --extract-brain <Modelfile>\n")
        sys.exit(2)
