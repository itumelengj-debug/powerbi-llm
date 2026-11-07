r"""
mcp_server.py - expose the grounded Power BI agent as an MCP server (stdio, standard library only).

Any MCP client (VS Code, Claude Desktop, Cursor, ...) can then use the local powerbi stack.
Everything still runs on this machine: the client talks to this process, this process talks to
Ollama on localhost.

Tools:
  powerbi_ask           ask the agent (retrieval + brain + gated tools + verifier)
  powerbi_search_docs   return the retrieved [S1]..[Sn] passages for a query
  powerbi_read_project  inventory a PBIP project (tables, measures, pages, visuals)
  powerbi_verify        run the artifact verifier on text (headers, JSON, PBIR rules, TMDL, citations)
  powerbi_design_dashboard  compile a page from a spec (Dashboard Studio): staged, or written with --expose-writes
  powerbi_make_theme    accessible report theme from a preset or brand colour
  powerbi_lint_design   design score (0-100) and fixes for an existing report
  powerbi_svg_measure   stage a DAX SVG measure (sparkline, progress, bullet, delta badge, colour rule)
  powerbi_run_dax       read-only DAX query against the open Desktop model   (--dax)
  powerbi_write_report_files  write PBIR pages/visuals                      (--expose-writes)

Safety: inside powerbi_ask every write and script is DENIED (the server cannot show you an approval
prompt), so the agent returns the artifacts and staged TMDL for you to apply. --expose-writes adds
powerbi_write_report_files as its own MCP tool, so that your MCP client's tool-approval prompt is the
gate; only enable it with a client that asks before each tool call.

Client config example (VS Code .vscode/mcp.json / Claude Desktop claude_desktop_config.json):
    {"servers": {"powerbi": {"command": "python",
        "args": ["C:\\Tools\\powerbi-llm\\mcp_server.py", "--root", "C:\\PBI", "--dax"]}}}
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback

import powerbi_agent as pa
from jo_powerbi import DESIGN_TOOLS, HAVE_DESIGN, PowerBIAgent, PowerBIConfig, ToolError, verify_artifacts

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "powerbi-llm", "version": "1.1.0"}


def _log(*a):
    print(*a, file=sys.stderr, flush=True)


class Server:
    def __init__(self, roots: list, model: str = pa.DEFAULT_MODEL, dax: bool = False,
                 expose_writes: bool = False, brain_mode: str = "full", k: int = pa.DEFAULT_K,
                 theme: str = "boardroom", brand_color: str = ""):
        self.roots, self.model, self.k, self.brain_mode = list(roots), model, k, brain_mode
        self.theme, self.brand_color = theme, brand_color
        self.dax_runner = pa.make_dax_runner() if dax else None
        self.expose_writes = expose_writes
        self.schema = pa.make_schema_validator(False)

    # ---- tool table
    def tools(self) -> list:
        t = [
            {"name": "powerbi_ask",
             "description": "Ask the local Power BI specialist (DAX, Power Query M, modelling, PBIR/TMDL, admin). "
                            "Answers are grounded in Microsoft Learn and the user's project, cite [S1]-style "
                            "sources and are verified. Writes and scripts are never executed from here.",
             "inputSchema": {"type": "object", "properties": {
                 "question": {"type": "string"},
                 "think": {"type": "boolean", "description": "force thinking on/off (default: automatic)"}},
                 "required": ["question"]}},
            {"name": "powerbi_search_docs",
             "description": "Retrieve the top documentation and project passages for a Power BI question.",
             "inputSchema": {"type": "object", "properties": {
                 "query": {"type": "string"}, "k": {"type": "integer", "minimum": 1, "maximum": 20}},
                 "required": ["query"]}},
            {"name": "powerbi_read_project",
             "description": "Inventory a PBIP project folder: tables, columns, measures, relationships, pages, "
                            "visuals. Only folders under the server's --root are allowed.",
             "inputSchema": {"type": "object", "properties": {"project_root": {"type": "string"}},
                             "required": ["project_root"]}},
            {"name": "powerbi_verify",
             "description": "Check Power BI artifacts in a markdown answer: '### CHANNEL' headers, JSON validity, "
                            "PBIR visual/page rules, TMDL indentation and table replacement, secrets, placeholders.",
             "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
        ]
        if HAVE_DESIGN:
            for d in DESIGN_TOOLS:
                fn = d["function"]
                desc = fn["description"]
                if fn["name"] == "design_dashboard":
                    desc += (" Pages are written only when this server runs with --expose-writes; otherwise "
                             "they are staged with a PowerShell writer script.")
                t.append({"name": "powerbi_" + fn["name"], "description": desc, "inputSchema": fn["parameters"]})
        if self.dax_runner:
            t.append({"name": "powerbi_run_dax",
                      "description": "Run a read-only DAX query (DEFINE/EVALUATE) against the model open in "
                                     "Power BI Desktop and return the first rows.",
                      "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}},
                                      "required": ["query"]}})
        if self.expose_writes:
            t.append({"name": "powerbi_write_report_files",
                      "description": "Write PBIR page.json / visual.json files into a report under --root. Validates "
                                     "JSON, names and schema, refuses while Desktop is open, backs up first, "
                                     "registers new pages. Cannot delete.",
                      "inputSchema": {"type": "object", "properties": {
                          "report_path": {"type": "string"},
                          "files": {"type": "array", "items": {"type": "object", "properties": {
                              "path": {"type": "string"}, "content": {"type": "string"}},
                              "required": ["path", "content"]}}},
                          "required": ["report_path", "files"]}})
        return t

    def _agent(self, approve=None) -> PowerBIAgent:
        cfg = PowerBIConfig(model=self.model, ollama_url=pa.OLLAMA_HOST, allowed_roots=self.roots,
                            brain_mode=self.brain_mode, theme=self.theme, brand_color=self.brand_color)
        return PowerBIAgent(cfg, approve=approve, dax_runner=self.dax_runner, schema_validator=self.schema)

    def call_tool(self, name: str, args: dict) -> str:
        if name == "powerbi_ask":
            res = pa.run_powerbi(args["question"], k=self.k, allowed_roots=self.roots, model=self.model,
                                 think=args.get("think"), dax_runner=self.dax_runner,
                                 schema_validator=self.schema, brain_mode=self.brain_mode)
            out = res.text.strip()
            if res.issues:
                out += "\n\n[VERIFY] issues:\n" + "\n".join(f"- {i}" for i in res.issues)
            if res.needs_user:
                out += "\n\n[NEEDS USER]\n" + "\n".join(f"- {n}" for n in res.needs_user)
            return out
        if name == "powerbi_search_docs":
            k = max(1, min(int(args.get("k") or self.k), 20))
            hits = pa.retrieve_context(args["query"], k=k, project_roots=self.roots)
            return json.dumps(hits, ensure_ascii=False, indent=1) if hits else "No passages found."
        if name == "powerbi_read_project":
            return self._agent().tool_read_project(args["project_root"])
        if name == "powerbi_verify":
            issues = verify_artifacts(args["text"], schema_check=self.schema)
            return "PASS: no issues found." if not issues else "ISSUES:\n" + "\n".join(f"- {i}" for i in issues)
        if name == "powerbi_design_dashboard" and HAVE_DESIGN:
            # The MCP client's per-call approval is the gate when writes are exposed.
            agent = self._agent(approve=(lambda action: True) if self.expose_writes else None)
            return agent.tool_design_dashboard(args["spec"], args.get("report_path", ""), write=self.expose_writes)
        if name == "powerbi_make_theme" and HAVE_DESIGN:
            return self._agent().tool_make_theme(**{k: v for k, v in args.items()
                                                    if k in ("preset", "brand_color", "mode", "font", "name")})
        if name == "powerbi_lint_design" and HAVE_DESIGN:
            return self._agent().tool_lint_design(args["report_path"], args.get("page", ""))
        if name == "powerbi_svg_measure" and HAVE_DESIGN:
            keys = ("kind", "measure", "axis", "target", "comparison", "table", "name", "colour")
            return self._agent().tool_svg_measure(**{k: v for k, v in args.items() if k in keys})
        if name == "powerbi_run_dax" and self.dax_runner:
            return self._agent().tool_run_dax_query(args["query"])
        if name == "powerbi_write_report_files" and self.expose_writes:
            # The MCP client's per-call approval is the gate for this tool.
            return self._agent(approve=lambda action: True).tool_write_report_files(args["report_path"],
                                                                                    args["files"])
        raise KeyError(name)

    # ---- JSON-RPC
    def handle(self, msg: dict):
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if mid is None:          # notification (initialized, cancelled, ...): no reply
            return None
        try:
            if method == "initialize":
                asked = params.get("protocolVersion")
                version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
                result = {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": SERVER_INFO,
                          "instructions": "Local Power BI specialist. Use powerbi_ask for Power BI questions and "
                                          "builds; powerbi_verify to check artifacts before applying them."}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": self.tools()}
            elif method == "tools/call":
                name = params.get("name", "")
                if name not in {t["name"] for t in self.tools()}:
                    return _error(mid, -32602, f"unknown tool: {name}")
                try:
                    text, is_error = self.call_tool(name, params.get("arguments") or {}), False
                except KeyError as e:
                    text, is_error = f"ERROR: missing argument {e}", True
                except ToolError as e:
                    text, is_error = f"ERROR: {e}", True
                except Exception as e:
                    _log(traceback.format_exc())
                    text, is_error = f"ERROR: {type(e).__name__}: {e}", True
                if text.startswith(("ERROR", "DENIED")):
                    is_error = True
                result = {"content": [{"type": "text", "text": text}], "isError": is_error}
            elif method in ("resources/list", "prompts/list"):
                result = {method.split("/")[0]: []}
            else:
                return _error(mid, -32601, f"method not found: {method}")
        except Exception as e:
            _log(traceback.format_exc())
            return _error(mid, -32603, f"{type(e).__name__}: {e}")
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def serve(self, stdin=sys.stdin, stdout=sys.stdout):
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                reply = _error(None, -32700, "parse error")
            else:
                if isinstance(msg, list):      # JSON-RPC batch (2025-03-26)
                    reply = [r for r in (self.handle(m) for m in msg) if r] or None
                else:
                    reply = self.handle(msg)
            if reply:
                stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
                stdout.flush()


def _error(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def main(argv=None):
    ap = argparse.ArgumentParser(description="MCP server for the local powerbi stack (stdio)")
    ap.add_argument("--root", action="append", default=[], help="folder the tools may read/write (repeatable)")
    ap.add_argument("--model", default=pa.DEFAULT_MODEL)
    ap.add_argument("--k", type=int, default=pa.DEFAULT_K)
    ap.add_argument("--dax", action="store_true", help="add powerbi_run_dax (needs Desktop + ADOMD.NET)")
    ap.add_argument("--expose-writes", action="store_true",
                    help="add powerbi_write_report_files, gated by the MCP client's approval prompt")
    ap.add_argument("--brain", choices=["full", "modular"], default="full")
    ap.add_argument("--theme", default="boardroom", help="Dashboard Studio preset for previews and linting")
    ap.add_argument("--brand-color", default="", help="derive the Dashboard Studio look from a brand colour")
    a = ap.parse_args(argv)
    for stream in (sys.stdin, sys.stdout):   # Windows pipes default to cp1252; MCP is UTF-8
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    _log(f"powerbi MCP server: model={a.model} roots={a.root or 'none'} dax={a.dax} writes={a.expose_writes}")
    Server(a.root, a.model, a.dax, a.expose_writes, a.brain, a.k, a.theme, a.brand_color).serve()


if __name__ == "__main__":
    main()
