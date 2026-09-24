# powerbi — a local Power BI specialist model for Ollama

A Power BI-only model you create in Ollama and run **entirely locally** on a single
24 GB GPU — no cloud API, no data egress. It answers DAX, Power Query M, semantic
modelling, and report-building questions with consultant-grade depth, grounded in
Microsoft's current documentation, and asks before doing anything destructive.

- **Base:** `gemma4:26b` — Mixture-of-Experts, 18 GB at Q4_K_M, native Ollama load.
- **Brain:** a ~32 KB rulebook baked in via a `SYSTEM` block (scope lock, DAX / M /
  PBIR standards, TMDL scripting, safety gates). It is a *system prompt, not weights*,
  so it moves freely between bases.
- **Measured:** **12/12** on a 12-task Power BI benchmark at **~156 tokens/s**.

> **Why not the original Qwen3.8 27B hybrid base?** A newer hybrid (attention + SSM)
> architecture that Ollama's current `llama.cpp` backend cannot start within its
> hard-coded 5-minute timeout (upstream regression #18123). The brain was ported onto
> `gemma4:26b` — which already loads natively and is the base the companion RAG layer
> trusts — producing a 26× throughput improvement. See `RUNTIME.md` for the full
> diagnosis.

---

## Quick start (Windows PowerShell)

```powershell
Set-ExecutionPolicy -Scope Process Bypass -Force
cd .\powerbi-llm
.\build.ps1        # pulls gemma4:26b (18 GB first run), fits context to VRAM, smoke-tests
.\test.ps1         # 12-task Power BI benchmark, auto-scored, transcripts saved
ollama run powerbi
```

`build.ps1`:
1. Checks Ollama and sets GPU-friendly env vars (flash attention, q8_0 KV cache,
   1 parallel slot, 30-min keep-alive) — restarting Ollama if any changed.
2. Pulls the base.
3. Creates `powerbi` at the largest context (up to 64K) that stays 100% in VRAM —
   it loads each size and steps down until nothing spills to system RAM.
4. Runs a smoke test and prints real generation speed.

### Build options

| Option | Use |
|---|---|
| `.\build.ps1 -Base qwen3-coder:30b -Name powerbi-fast` | Faster MoE base (no vision/thinking) |
| `.\build.ps1 -Base qwen3.6:27b-coding` | Alternative dense base |
| `.\build.ps1 -MaxContext 32768` | Cap the context window |
| `.\test.ps1 -Think` | Evaluate with thinking on |
| `.\test.ps1 -Model powerbi-fast` / `-Only pbir` | Compare another build, or a subset |

Manual build on any OS:

```powershell
ollama pull gemma4:26b
ollama create powerbi -f Modelfile
```

---

## Using it

- **Terminal:** `ollama run powerbi`. `/set nothink` for fast generation, `/set think`
  for hard DAX and modelling. Pass a screenshot path to have it read a report page or error.
- **API:** `POST http://localhost:11434/api/chat` with `{"model":"powerbi", ...}`.
  OpenAI-compatible endpoint: `http://localhost:11434/v1`.
- **Fixed output channels:** `### DAX`, `### DAX QUERY`, `### TMDL VIEW`, `### M`,
  `### FILE: <path>`, `### POWERSHELL`, `### THEME`, `### STEPS` — each followed by one
  fenced code block, so a harness can route artifacts automatically.

### Grounded answers (the full system)

The companion `powerbi_agent.py` unifies three layers behind one call:

1. **Knowledge** — the app's own retrieval index (`agent.rag.DocumentStore`), a
   1,326-document Power BI / Fabric corpus.
2. **Behaviour** — the `powerbi` brain.
3. **Execution** — gated tools (`read_project`, `write_report_files`, `stage_tmdl`,
   `run_powershell`, `lookup_docs`), all fail-closed on approval, with artifact
   verification.

```python
from powerbi_agent import run_powerbi

res = run_powerbi("Why does my YTD measure show blanks in December?", k=6)
print(res.text)        # the answer
print(res.issues)      # non-empty = verifier flagged something
print(res.needs_user)  # steps only the user can do (apply a staged TMDL script)
```

Retrieved passages are passed in as `[S1]`-tagged source-of-truth and cited; anything
not covered is marked `Unverified:` instead of guessed.

---

## Repository layout

```
Modelfile              the brain (SYSTEM block) + base + parameters
build.ps1              one-command build (base pull + GPU tuning + context fit + smoke test)
test.ps1               12-task Power BI benchmark, auto-scored
bench-llamacpp.ps1     llama-server flag sweep to diagnose slow generation
RUNTIME.md             the Ollama/Qwen3.8 diagnosis and why the base moved to gemma4:26b
agent-jo/              jo_powerbi.py adapter (Ollama + llama.cpp backends, 28 tests)
powerbi_agent.py       unified three-layer glue (RAG + brain + gated tools)
rag_pbi.py             retrieval harness (layer 1)
gen_visual.py          Deneb visual-spec generator (layer 3)
docs/PROJECT-REPORT.md the full project report
```

## Making it act on your PC

Ollama only generates. Something has to execute the scripts it produces:

1. **Manual:** run the `### POWERSHELL` blocks; paste `### TMDL VIEW` / `### DAX QUERY`
   blocks into Desktop.
2. **Your own harness:** send `tools` in `/api/chat` (the base supports tool calling and
   the brain switches to tools when present). If the harness sends its own system
   message, prepend `ollama show powerbi --system` so the brain is not replaced.
3. **Live semantic-model edits:** Microsoft's Power BI Modeling MCP server (preview)
   exposes Desktop / Fabric / PBIP models as MCP tools.

## Updating the brain

Edit the `SYSTEM` block in `Modelfile`, re-run `build.ps1` (seconds — the base is
cached), then re-run `test.ps1` and compare scores. When Power BI ships something the
model gets wrong, add one line.

## Limits

- **Knowledge:** base model + brain; it does not browse.
- **PBIR schemas change monthly.** It reuses the `$schema` versions already in your
  project, and asks for a Desktop-saved exemplar before writing an unfamiliar visual type.
- **Speed:** a MoE 26B on a 3090 generates ~150 tokens/s (`build.ps1` prints your real
  figure); thinking adds latency.

## License

PolyForm Noncommercial License 1.0.0 — see `LICENSE`.
