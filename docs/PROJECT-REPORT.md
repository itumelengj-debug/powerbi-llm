# Building a Power BI Specialist LLM on a Single 24 GB GPU

**A technical project report — three approaches, two instructive failures, and one unified local system**

- **Author:** Itumeleng Nthite
- **Hardware:** NVIDIA RTX 3090 (24 GB VRAM) · Windows 11 + WSL2 (Ubuntu) · Python 3.12
- **Period:** August – September 2026
- **Status:** Resolved

---

## Abstract

This report documents a two-month effort to build a domain-specialist language model for
Microsoft Power BI that runs **entirely locally** on a single 24 GB consumer GPU — with no
cloud API and no data egress, a hard constraint for regulated financial-services desk work.

Three approaches were attempted:

1. **Instruction fine-tuning** of a 12B model via QLoRA — collapsed at scale.
2. A **no-fine-tune three-layer system** (retrieval + behavioural prompt + visual-spec
   generator) — succeeded.
3. A **pre-built model over a hybrid (attention + SSM) base** — blocked by an Ollama
   runtime regression, then resolved by porting its behavioural rulebook onto the MoE base
   that already powered the successful RAG layer.

The result is a native `ollama run powerbi` model scoring **12/12** on a 12-task benchmark
at **156 tokens/s** — a 26× throughput improvement over the standalone hybrid — unified with
the retrieval index and gated execution tools behind a single callable interface.

---

## 1. Objective

A model that answers DAX, Power Query M, semantic-modelling, and report-building questions
with consultant-grade depth, grounded in Microsoft's current documentation, running entirely
on local hardware. Two non-negotiable constraints: **full locality** (data never leaves the
machine) and **grounded accuracy** (no invented DAX functions, JSON keys, or schema names).

## 2. Environment

| Component | Specification |
|---|---|
| GPU | NVIDIA RTX 3090, 24 GB GDDR6X, driver 610.74 |
| Host OS | Windows 11 · WSL2 (Ubuntu) for training |
| Python / CUDA | Python 3.12 · CUDA 12.4 · torch 2.12.1+cu130 |
| Inference stack | Ollama 0.34.4 · standalone llama.cpp b11147 (CUDA 12.4) |
| Training stack | Unsloth 2026.9.5 · TRL 0.24.0 · PEFT 0.21.0 · bitsandbytes 0.50.2 |
| Knowledge index | 1,326-document Power BI / Fabric corpus (SQLite + FTS5 + embeddings) |

### 2.1 Model candidates

| Base model | Size (Q4_K_M) | Architecture | Outcome |
|---|---|---|---|
| `gemma4:12b` | 7.6 GB | Dense (fine-tune target) | Collapsed at 4.6k examples |
| `gemma4:26b` | 18 GB | Mixture-of-Experts (MoE) | **Chosen base — reliable, fast** |
| `qwen3.8:27b` | 17 GB | Hybrid attention + SSM | Ollama blocked; 6 tok/s standalone |

---

## 3. Method — three approaches

### 3.1 Attempt 1 — Instruction fine-tuning (Gemma 4 12B, QLoRA)

Bake Power BI knowledge *into the weights* by fine-tuning Gemma 4 12B on a hand-built
dataset of consultant-voiced examples. A first pass of 208 correctness-checked examples
across 8 modules (DAX, modelling, Power Query M, RLS, visuals, performance, SemPy, custom
visuals) converged cleanly — loss 2.322 → 0.836 over 78 steps (~9 min), 65.5M trainable
parameters (0.55%, the correct LoRA ratio). On held-out probes it showed a genuine
consultant voice, though one probe hallucinated a nonexistent DAX function.

Scaling the dataset via a semi-automated generator to **4,634 examples** (95.5%
RAG-grounded) **collapsed the model** — it degraded into non-English garbage and repetition
loops, worse than the base model, across four cycles.

> **Finding 1.** On a 24 GB card, fine-tuning a 12B model for instruction-following
> teaches *style, not knowledge*, and is fragile to scale. **Status: FAILED** — model deleted.

### 3.2 Attempt 2 — Three-layer system (no fine-tune)

Abandon fine-tuning; compose the capability from parts that are each individually reliable:

| Layer | Component | Role |
|---|---|---|
| 1 · Knowledge | `rag_pbi.py` + base `gemma4:26b` | Answers only from retrieved context, cites sources, refuses when absent |
| 2 · Behaviour | `powerbi-consultant` Modelfile | Diagnosis-first, "why-first" consultant voice |
| 3 · Execution | `gen_visual.py` + 48-chart Deneb library | Emits valid visual specs on request |

The decisive result: the larger `gemma4:26b`, used raw with a disciplined grounding prompt,
followed the "answer only from context, cite sources" instruction *perfectly* — refusing when
context lacked the answer — where the fine-tuned 12B had collapsed. **Status: SUCCESS.**

### 3.3 Attempt 3 — "powerbi" model (Qwen3.8 27B hybrid, external build)

A pre-built "Power BI-only" model over Qwen3.8 27B — a newer hybrid attention + Mamba (SSM)
architecture. The model pulled fine (17 GB) but **would not start under Ollama**: tensors
offloaded 66/66 layers to GPU, then `llama-server` never reported "ready", and Ollama's
hard-coded 5-minute startup timeout killed the load.

Root cause was a runtime regression, not the model: Ollama's `load-mode none` staging
(issue #18123), multi-token-prediction draft loading, and a vision projector — all loading
at once on a 24 GB card. The identical GGUF served through standalone llama.cpp b11147
(no timeout) loaded in ~1–2 min and generated a correct DAX measure — but at ~6 tokens/s.

---

## 4. Results & resolution

### 4.1 The throughput problem

A dense 27B fully offloaded should yield ~25–35 tokens/s on a 3090; the hybrid achieved 6.
Suspects: quantised KV cache without flash attention, sysmem fallback, and the hybrid's
draft/vision overhead.

### 4.2 The resolution — port the brain, not the base

The "brain" is a **32 KB system prompt**, not weights. Rebuilding on `gemma4:26b` — which
loads natively, is MoE-fast, and is the base the RAG layer already trusts — unified
everything:

```
build.ps1 -Base gemma4:26b -Name powerbi -MaxContext 32768
```

produced a native `ollama run powerbi` at 32768 context, 100% GPU, answering at
**156 tokens/s** — a **26×** speedup.

| Metric | Qwen3.8 27B (standalone) | powerbi on gemma4:26b (native) |
|---|---|---|
| Startup | 1–2 min (no timeout ceiling) | Native Ollama load, no timeout |
| Throughput | ~6 tok/s | **156 tok/s (26×)** |
| Context | 32768 | 32768, 100% GPU |
| test.ps1 score | n/a (untested at speed) | **12 / 12** |

### 4.3 Evaluation — 12-task benchmark

`test.ps1` auto-scores DAX (YoY, share-of-total, UDF), Power Query M, TMDL scripting, PBIR
page/visual construction, theming, RLS, refresh API, safety, and scope refusal. Two initial
failures were fixed:

- **02 dax-share** — used `REMOVEFILTERS` where "share of visible total" requires
  `ALLSELECTED`. Fixed with an explicit rule in the brain.
- **06 pbir-page** — the safety gate correctly paused to ask before writing files; the
  checker wanted the artifact. It passed on re-run, and the checker regex was widened.

Final score: **12/12** at 135–173 tokens/s.

---

## 5. Final architecture

The two working attempts were unified into one system:

| Layer | Component | Discipline |
|---|---|---|
| Knowledge | `agent.rag.DocumentStore` (1,326-doc index) | Facts from retrieval, cited [S1]…[Sn], never from memory |
| Behaviour | `powerbi` brain (32 KB rulebook) | Scope lock, DAX / M / PBIR standards, safety gates |
| Execution | `jo_powerbi.PowerBIAgent` tools | `read_project`, `write_report_files`, `stage_tmdl`, `run_powershell`, `lookup_docs` — fail-closed on approval, with artifact verification |

```python
from powerbi_agent import run_powerbi

res = run_powerbi(question, k=6, allowed_roots=[r"C:\PBI"])
res.text        # the answer (PII-scanned before release)
res.issues      # non-empty = verifier flagged something
res.needs_user  # steps only the user can do (apply a staged TMDL script)
```

---

## 6. Transferable lessons

1. **Fine-tuning a 12B model for instruction-following on a 24 GB card is a dead end** —
   it teaches style, not knowledge, and collapses at scale. Use RAG + system prompts.
2. **A bigger base model used raw beats a fine-tuned smaller one** for grounded,
   non-hallucinating retrieval.
3. **Domain knowledge belongs in retrieval, not weights** — unless you have the data and
   hardware to fine-tune at scale (which 24 GB is not).
4. **New hybrid architectures can outrun your inference stack's startup ceiling.** Always
   check whether the blocker is the *model* or the *runtime wrapper* — the difference here
   was a 5-minute timeout, not a bad model.
5. **Antivirus false-positives on local inference binaries are real** — llama.cpp launchers
   are quarantined by Avast by default.

## 7. Conclusion

The final system is a native `ollama run powerbi` model scoring **12/12** at
**156 tokens/s**, unified with the retrieval index and gated execution tools behind a single
call. What began as three separate attempts resolved into one system: the fine-tune taught
the limits of the hardware, the RAG layer taught the discipline of grounding, and the hybrid
model contributed the behavioural rulebook — each failure informing the eventual success.
