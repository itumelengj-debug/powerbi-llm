# Runtime notes - getting `powerbi` fast and native

## 1. The Ollama load timeout is a known regression, not your setup

`load_mode` changed from `mmap` to `none` in Ollama 0.32.10: instead of mapping the file, the whole model is staged through CPU buffers first, so loads that took seconds now blow past the 5-minute startup timeout. It is still present in 0.33.x (issue #18123). The documented workaround is pinning Ollama to 0.32.9.

That leaves a version trap for this build: **Qwen3.8 support only arrived in 0.32.12**, which is already past the regression. So on Ollama you get either the regression or no Qwen3.8 - not both. Three ways out, in order of effort:

| Option | Command | Result |
|---|---|---|
| Rebuild the brain on a base your stack already loads | `.\build.ps1 -Base gemma4:26b -Name powerbi` | Native `ollama run powerbi`, MoE speed, and it's the base your RAG layer already trusts |
| Same, on a fast coder MoE | `.\build.ps1 -Base qwen3-coder:30b -Name powerbi-fast` | Fastest option for file and JSON generation |
| Stay on llama.cpp with Qwen3.8 | see section 2 | Keeps the newest base, no Ollama dependency |

The brain is a system layer, not weights - moving it to another base costs one command and a `test.ps1` run. Compare scores before deciding.

## 2. 6 tok/s is not what this hardware does - find out why

A 27B Q4_K_M with 15.3 GB of weights fully offloaded should run roughly 25-35 tok/s on a 3090. Ranked suspects, all testable:

1. **No flash attention.** Your command had `-ctk q8_0 -ctv q8_0` without `-fa on`. Quantised KV cache without flash attention takes a slow path.
2. **Sysmem fallback.** If the KV cache at 32K pushes total allocation past 24 GB, the NVIDIA driver silently spills into shared system memory instead of failing, which costs an order of magnitude. Set NVIDIA Control Panel > Manage 3D settings > Program settings > llama-server.exe > *CUDA - Sysmem Fallback Policy* = **Prefer No Sysmem Fallback**, then watch it fail loudly instead.
3. **MTP draft head plus vision projector** loading alongside the model - both cost VRAM, and a mismatched draft can slow generation rather than speed it up.
4. **Architecture kernels.** If nothing above moves the number, llama.cpp's kernels for this hybrid (attention + SSM) are the ceiling. That is a runtime limit, not something to tune around: switch base per section 1.

Run the sweep:

```powershell
.\bench-llamacpp.ps1 -Model C:\models\qwen3.8-27b-Q4_K_M.gguf -Server C:\llama\llama-server.exe -SystemFile .\brain.txt
```

It starts llama-server once per configuration (baseline, +flash, f16 KV, 8K context, batch tuning), waits for `/health`, measures tokens/s on a real DAX prompt, samples VRAM, writes `bench-results.csv` and server logs, then tells you which class of problem you have. Add `-Configs nodraft` if your build accepts `--draft-max 0`.

Also: `--jinja` is required for tool calling on llama-server, so keep it in whichever command you settle on.

## 3. Connect Jo to whichever runtime wins

`agent-jo/jo_powerbi.py` now speaks both:

```python
# llama.cpp (OpenAI endpoint) - no /api/show, so point it at the extracted brain
cfg = PowerBIConfig(backend="llamacpp", llamacpp_url="http://127.0.0.1:8088",
                    system_prompt_path="brain.txt", allowed_roots=[r"C:\PBI"])
```

```
python jo_powerbi.py --extract-brain ..\Modelfile > brain.txt
```

Everything else is unchanged: same tools, same approval gate, same verifier. Images become OpenAI image parts, tool results carry `tool_call_id`, and no `think` flag is sent.

## 4. Merge this with the layer that already works

Your three-layer system and this model are solving different halves, so run them together rather than choosing:

- **Knowledge** stays in retrieval - your indexed Microsoft documentation, the part a fine-tune could never hold.
- **Behaviour** comes from the brain - delivery format, PBIR construction rules, safety gates.
- **Execution** comes from Jo's tools.

Pass retrieved passages straight into the run:

```python
hits = rag_pbi.search(question, k=6)          # your existing retriever
res = pbi.run(question, jo_system_prompt=JO_RULES,
              context=[{"id": f"S{i}", "source": h.source, "text": h.text}
                       for i, h in enumerate(hits, 1)])
```

The context block tells the model those passages are the source of truth for product facts, to cite `[S1]`-style tags, and to answer `Unverified:` rather than fill gaps from memory - the same discipline that made `gemma4:26b` reliable, now in front of the Power BI rules and the tools.

## 5. Don't re-attempt the fine-tune

Your conclusion holds. At this hardware tier a fine-tune buys voice, not knowledge, and 4.6k synthetic RAG-derived examples is exactly the recipe for the collapse you saw. If you ever revisit it, the only defensible target is output format on a few hundred hand-checked examples, one epoch, low learning rate, judged against `test.ps1` - and the brain already gets that for free.
