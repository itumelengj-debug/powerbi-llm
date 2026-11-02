# Agent Jo - Power BI route

`jo_powerbi.py` lets Agent Jo send Power BI requests to the local `powerbi` model. It uses only the Python standard library. Its own tests are in `test_jo_powerbi.py` (28); the newer features are covered in `../tests/`.

## Wire it in

```python
from jo_powerbi import PowerBIAgent, PowerBIConfig, is_powerbi_request
from pbi_dax import DesktopDaxRunner          # optional
from pbir_schema import SchemaValidator        # optional

pbi = PowerBIAgent(
    PowerBIConfig(allowed_roots=[r"C:\PBI"]),   # the only folders it may read or write
    approve=jo_policy_gate,                     # (action: dict) -> bool
    audit=jo_audit_log,                         # (event: dict) -> None
    web_search=jo_web_search,                   # (query) -> [{"title", "url", "snippet"}], optional
    dax_runner=DesktopDaxRunner(),              # optional: read-only run_dax_query tool (pbi_dax.py)
    schema_validator=SchemaValidator(),         # optional: PBIR $schema checks (pbir_schema.py)
    on_token=stream_to_ui,                      # optional: (kind, text) -> None, streams the answer
)

if is_powerbi_request(text):                    # or Jo's own router
    res = pbi.run(text, jo_system_prompt=JO_RULES, images=attachments,
                  context=passages)             # retrieved [S1]..[Sn] passages (see ../pbi_retrieval.py)
    release(res.text) if not res.issues else flag(res.issues)
    show(res.needs_user)                        # e.g. "paste staged TMDL into TMDL view, Apply"
```

## What happens on each request

1. **Prompt merge.** The model's built-in Power BI rules come first, then Jo's rules, then the tool notes, then the retrieved passages (each with its source and update date). Nothing is lost when Jo sends its own system message.
   With `PowerBIConfig(brain_mode="modular")` only the core rule sections (scope, operating rules, delivery channels, safety) plus the sections the request needs (DAX, M, model, PBIR, design, admin) are sent. Anything ambiguous falls back to the full rulebook.
2. **Streaming.** With `on_token`, content and thinking tokens stream as they are generated (Ollama NDJSON and llama-server SSE, including tool calls sent in fragments), plus `status` notes for tool calls and fix rounds.
3. **Thinking.** Thinking is switched on automatically for reasoning-heavy asks (why, slow, optimise, RLS, modelling) and off for builds. You can override it with `think=` per call or in the config.
4. **Tool loop.** The model calls tools, capped at 8 rounds per request:
   - `read_project` reads your real names: tables, columns, measures, relationships, pages and visual positions.
   - `write_report_files` handles PBIR pages and visuals. Before asking for approval it:
     - checks each path (only `pages/.../page.json` or `.../visual.json` files),
     - checks the JSON and the report-file rules,
     - checks each file's name matches its folder,
     - checks Power BI Desktop is closed.

     After approval it backs up to `~/PBI-Backups`, writes the files and registers new pages. It cannot delete anything.
   - `stage_tmdl` saves a model script (new measures, columns and so on) to `~/PBI-Staging` for you to paste into TMDL view and Apply. It rejects any script that would replace a whole table, and scripts indented with spaces.
   - `run_powershell` always needs approval. Output is captured and the script's hash is audited.
   - `run_dax_query` (only with a `dax_runner`) runs a `DEFINE`/`EVALUATE` query against the open Desktop model so the model can test a measure before delivering it. XMLA and DMV queries are refused. The audit log records the query hash and row counts, never values.
   - `lookup_docs` goes through Jo's web search. Queries are blocked if they contain model object names, long numbers, emails or GUIDs, and only learn.microsoft.com results are returned.
5. **Verify.** The final answer is checked automatically. Every code block needs a `###` header. The checker also looks for:
   - invalid JSON,
   - visuals missing a query reference or display name,
   - report-file layout errors,
   - model scripts indented with spaces, or that would replace a whole table,
   - PowerShell text blocks that would swallow `$schema`,
   - scripts that write report files without a backup or a Desktop-closed check,
   - hard-coded secrets,
   - `...` placeholders,
   - citations that point at a passage that was never provided (e.g. `[S7]` when only S1–S6 were sent),
   - PBIR files that violate their `$schema` (with a `schema_validator`).

   If anything fails, the model gets one automatic fix round. Anything still failing comes back in `res.issues`.
6. **Release.** `res.text` goes through Jo's existing PII scan and release step. Every tool call, approval, write (with file hashes), blocked outbound query and verification result is sent to `audit`.

## Safe defaults

- **No `approve` callback:** every write and every script is denied.
- **Paths outside `allowed_roots`:** rejected.
- **No `web_search`:** docs lookup is off and the model marks points as Unverified instead.

## Test

```
python -m pytest -q test_jo_powerbi.py      # this folder
python -m pytest                            # from the repo root: everything (85 tests)
```

The tests run against a mock Ollama server, so no GPU is needed.
