r"""
bench.py - Power BI benchmark for the local model *and* the full system.

  model mode  raw /api/chat to the `powerbi` model (what test.ps1 measures)
  agent mode  the real stack: retrieval -> brain -> gated tools -> verifier, against a sandbox copy of
              bench/fixture/FX (writes land in the sandbox; PowerShell is always denied)

Each task runs --repeats times (sampling is random, so one pass proves little); the summary reports
per-task pass rate, per-category and per-level scores, flaky tasks, verifier findings and speed.

    python bench\bench.py                               # model mode, 3 repeats, all tasks
    python bench\bench.py --mode agent --repeats 5
    python bench\bench.py --brain modular --only dax    # does the modular brain hold up on DAX?
    python bench\bench.py --dax                         # also execute generated measures in Desktop
    python bench\bench.py --schema fetch                # also validate PBIR JSON against $schema
    python bench\bench.py --compare runs\A\summary.json runs\B\summary.json
    python bench\bench.py --list

Results go to bench/runs/<model>-<mode>-<stamp>/: one transcript per task and repeat, results.csv,
summary.json and summary.md.
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
import tempfile
import time
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path[:0] = [str(HERE), str(ROOT), str(ROOT / "agent-jo")]

import tasks as T  # noqa: E402
from checks import RunResult, design_lint, first_measure, jsons  # noqa: E402
from jo_powerbi import PowerBIAgent, PowerBIConfig, select_brain, verify_artifacts  # noqa: E402

FIXTURE = HERE / "fixture" / "FX"
MODEL_MODE_PROJECT = r"C:\PBI\FX"


# --------------------------------------------------------------------------- transport (model mode)

def _post(host: str, path: str, body: dict, timeout: int = 1800) -> dict:
    req = urllib.request.Request(host.rstrip("/") + path, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


class ModelRunner:
    def __init__(self, host: str, model: str, think: bool, brain: str):
        self.host, self.model, self.think, self.brain_mode = host, model, think, brain
        info = _post(host, "/api/show", {"model": model}, timeout=60)
        self.caps = set(info.get("capabilities") or [])
        self.brain = info.get("system") or ""

    def run(self, prompt: str) -> tuple:
        messages = []
        if self.brain_mode == "modular" and self.brain:
            messages.append({"role": "system", "content": select_brain(self.brain, prompt)[0]})
        messages.append({"role": "user", "content": prompt})
        body = {"model": self.model, "stream": False, "messages": messages}
        if "thinking" in self.caps:
            body["think"] = self.think
        r = _post(self.host, "/api/chat", body)
        msg = r.get("message") or {}
        tps = r["eval_count"] / (r["eval_duration"] / 1e9) if r.get("eval_duration") else 0.0
        return RunResult(text=msg.get("content") or "", thinking=msg.get("thinking") or ""), tps


# --------------------------------------------------------------------------- agent mode

def _snapshot(folder: Path) -> dict:
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in folder.rglob("*") if p.is_file()}


class AgentRunner:
    def __init__(self, host: str, model: str, think, brain: str, k: int, retrieval: bool,
                 dax_runner=None, schema=None):
        self.host, self.model, self.think, self.brain, self.k = host, model, think, brain, k
        self.retrieval, self.dax_runner, self.schema = retrieval, dax_runner, schema

    def run(self, prompt_template: str) -> tuple:
        with tempfile.TemporaryDirectory(prefix="pbi-bench-") as td:
            sandbox = Path(td) / "FX"
            shutil.copytree(FIXTURE, sandbox)
            before = _snapshot(sandbox)
            prompt = prompt_template.format(project=sandbox, report=sandbox / "FX.Report")
            context = []
            if self.retrieval:
                import powerbi_agent as pa
                context = pa.retrieve_context(prompt, k=self.k, project_roots=[str(sandbox)])
            cfg = PowerBIConfig(model=self.model, ollama_url=self.host, allowed_roots=[str(sandbox)],
                                backup_dir=str(Path(td) / "backups"), staging_dir=str(Path(td) / "staging"),
                                brain_mode=self.brain)
            agent = PowerBIAgent(cfg, approve=lambda a: a.get("tool") == "write_report_files",  # never PowerShell
                                 audit=lambda e: None, dax_runner=self.dax_runner, schema_validator=self.schema)
            res = agent.run(prompt, context=context, think=self.think)
            after = _snapshot(sandbox)
            written = {}
            for rel, data in after.items():
                if before.get(rel) != data:
                    try:
                        written[rel] = json.loads(data.decode("utf-8"))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        written[rel] = data.decode("utf-8", "replace")
            return RunResult(text=res.text, mode="agent", actions=res.actions, written=written, context=context,
                             issues=res.issues, thinking=res.thinking), 0.0


# --------------------------------------------------------------------------- scoring

def _normalise(out) -> tuple:
    if isinstance(out, tuple):
        return bool(out[0]), str(out[1]) if len(out) > 1 else ""
    return bool(out), ""


def score(task: T.Task, r: RunResult, dax_runner=None, schema=None) -> dict:
    try:
        ok, reason = _normalise(task.check(r))
    except Exception as e:
        ok, reason = False, f"check crashed: {type(e).__name__}: {e}"
    extra = {"verifier_issues": verify_artifacts(r.text, context=r.context or None)}
    try:
        lint = design_lint(r)
    except Exception:
        lint = None
    if lint is not None:
        extra["design_score"] = lint["score"]

    if ok and schema is not None and task.category == "pbir":
        problems = [p for d in jsons(r) if isinstance(d, dict) for p in schema(d, task.id)]
        extra["schema_issues"] = problems
        if problems:
            ok, reason = False, "schema: " + problems[0]

    if ok and dax_runner is not None and task.dax:
        m = first_measure(r.text)
        if not m:
            ok, reason = False, "dax exec: no '### DAX' measure found"
        else:
            q = f"DEFINE MEASURE {task.dax['table']}[__bench] = {m[1]}\n{task.dax['query']}"
            try:
                out = dax_runner(q, 5)
                extra["dax_exec"] = "ok"
                vals = [v for s in out.get("results") or [] for row in s.get("rows") or [] for v in row[1:]]
                if vals and all(v is None for v in vals):
                    ok, reason = False, "dax exec: every value blank"
            except Exception as e:
                ok, reason = False, f"dax exec: {str(e)[:300]}"
                extra["dax_exec"] = "error"
    return {"pass": ok, "reason": reason, **extra}


# --------------------------------------------------------------------------- summary

def summarise(rows: list, meta: dict) -> dict:
    by_task = defaultdict(list)
    for r in rows:
        by_task[r["id"]].append(r)
    tasks = {}
    for tid, rs in by_task.items():
        n = len(rs)
        p = sum(r["pass"] for r in rs)
        tasks[tid] = {"category": rs[0]["category"], "level": rs[0]["level"], "runs": n, "passes": p,
                      "rate": round(p / n, 3), "flaky": 0 < p < n,
                      "avg_seconds": round(sum(r["seconds"] for r in rs) / n, 1)}

    def agg(key):
        g = defaultdict(list)
        for t in tasks.values():
            g[t[key]].append(t["rate"])
        return {k: round(sum(v) / len(v), 3) for k, v in sorted(g.items())}

    rates = [t["rate"] for t in tasks.values()]
    tps = [r["tok_s"] for r in rows if r["tok_s"]]
    return {
        "meta": meta,
        "score": round(sum(rates) / len(rates), 3) if rates else 0.0,
        "all_runs_passed": sum(1 for t in tasks.values() if t["passes"] == t["runs"]),
        "tasks_total": len(tasks),
        "by_category": agg("category"),
        "by_level": agg("level"),
        "flaky": sorted(t for t, v in tasks.items() if v["flaky"]),
        "failing": sorted(t for t, v in tasks.items() if v["passes"] == 0),
        "verifier_issue_rate": round(sum(bool(r["verifier_issues"]) for r in rows) / len(rows), 3) if rows else 0,
        "median_tok_s": sorted(tps)[len(tps) // 2] if tps else None,
        "design_score": (round(sum(ds) / len(ds), 1) if (ds := [r["design_score"] for r in rows
                                                                  if isinstance(r.get("design_score"), (int, float))])
                         else None),
        "tasks": tasks,
    }


def markdown(s: dict) -> str:
    m = s["meta"]
    lines = [f"# Bench: {m['model']} ({m['mode']}, brain {m['brain']}, think {m['think']})", "",
             f"- Started {m['started']}, {m['repeats']} repeat(s)",
             f"- **Score {s['score']:.1%}** (mean per-task pass rate); {s['all_runs_passed']}/{s['tasks_total']} "
             "tasks passed every run",
             f"- Verifier flagged {s['verifier_issue_rate']:.0%} of answers"
             + (f"; median {s['median_tok_s']:.0f} tok/s" if s.get("median_tok_s") else "")
             + (f"; mean design score {s['design_score']}/100" if s.get("design_score") is not None else ""), "",
             "| Category | Score |", "|---|---|"]
    lines += [f"| {k} | {v:.0%} |" for k, v in s["by_category"].items()]
    lines += ["", "| Level | Score |", "|---|---|"] + [f"| {k} | {v:.0%} |" for k, v in s["by_level"].items()]
    if s["flaky"]:
        lines += ["", "**Flaky:** " + ", ".join(s["flaky"])]
    if s["failing"]:
        lines += ["", "**Never passed:** " + ", ".join(s["failing"])]
    return "\n".join(lines) + "\n"


def compare(a_path: str, b_path: str) -> str:
    a, b = (json.loads(Path(p).read_text(encoding="utf-8")) for p in (a_path, b_path))
    lines = [f"A: {a['meta']['model']} {a['meta']['mode']} brain={a['meta']['brain']}  score {a['score']:.1%}",
             f"B: {b['meta']['model']} {b['meta']['mode']} brain={b['meta']['brain']}  score {b['score']:.1%}",
             f"Delta: {b['score'] - a['score']:+.1%}", "", "Category      A      B"]
    for c in sorted(set(a["by_category"]) | set(b["by_category"])):
        av, bv = a["by_category"].get(c), b["by_category"].get(c)
        lines.append(f"{c:<12} {av if av is None else f'{av:.0%}':>6} {bv if bv is None else f'{bv:.0%}':>6}")
    moved = []
    for t in sorted(set(a["tasks"]) & set(b["tasks"])):
        d = b["tasks"][t]["rate"] - a["tasks"][t]["rate"]
        if abs(d) >= 0.34:
            moved.append(f"  {'+' if d > 0 else '-'} {t}: {a['tasks'][t]['rate']:.0%} -> {b['tasks'][t]['rate']:.0%}")
    if moved:
        lines += ["", "Tasks that moved:"] + moved
    return "\n".join(lines)


# --------------------------------------------------------------------------- main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Power BI benchmark (model or full agent)")
    ap.add_argument("--mode", choices=["model", "agent"], default="model")
    ap.add_argument("--model", default="powerbi")
    ap.add_argument("--host", default="http://127.0.0.1:11434")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--only", help="run tasks whose id contains this text")
    ap.add_argument("--category")
    ap.add_argument("--level", choices=["core", "hard"])
    ap.add_argument("--think", action="store_true")
    ap.add_argument("--brain", choices=["full", "modular"], default="full")
    ap.add_argument("--k", type=int, default=6)
    ap.add_argument("--no-retrieval", action="store_true", help="agent mode without retrieved passages")
    ap.add_argument("--dax", action="store_true", help="execute generated measures against the open Desktop model")
    ap.add_argument("--schema", choices=["off", "cache", "fetch"], default="off")
    ap.add_argument("--out", default=str(HERE / "runs"))
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"))
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args(argv)

    if a.compare:
        print(compare(*a.compare))
        return 0
    selected = T.get(mode=a.mode, only=a.only, category=a.category, level=a.level)
    if a.list:
        for t in selected:
            print(f"{t.id:<26} {t.category:<10} {t.level:<5} {t.prompt[:70]}")
        print(f"{len(selected)} task(s)")
        return 0
    if not selected:
        print("no tasks selected")
        return 2

    dax_runner = None
    if a.dax:
        from pbi_dax import DesktopDaxRunner
        dax_runner = DesktopDaxRunner()
    schema = None
    if a.schema != "off":
        from pbir_schema import SchemaValidator
        schema = SchemaValidator(allow_fetch=a.schema == "fetch")
        if not schema.available:
            print("--schema needs: pip install jsonschema")
            return 2

    if a.mode == "model":
        runner = ModelRunner(a.host, a.model, a.think, a.brain)
    else:
        runner = AgentRunner(a.host, a.model, a.think if a.think else None, a.brain, a.k,
                             not a.no_retrieval, dax_runner, schema)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = Path(a.out) / f"{a.model.replace(':', '_')}-{a.mode}-{a.brain}-{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    meta = {"model": a.model, "mode": a.mode, "brain": a.brain, "think": a.think, "repeats": a.repeats,
            "started": datetime.now().isoformat(timespec="seconds"), "dax_exec": a.dax, "schema": a.schema,
            "retrieval": a.mode == "agent" and not a.no_retrieval}
    print(f"{len(selected)} task(s) x {a.repeats} | {a.model} | {a.mode} | brain {a.brain} | -> {out}\n")

    rows = []
    for t in selected:
        prompt = t.prompt if a.mode == "agent" else t.prompt.format(project=MODEL_MODE_PROJECT,
                                                                    report=MODEL_MODE_PROJECT + r"\FX.Report")
        marks = []
        for rep in range(1, a.repeats + 1):
            t0 = time.time()
            try:
                r, tps = runner.run(prompt)
                res = score(t, r, dax_runner, schema)
            except Exception as e:
                r, tps = RunResult(text=f"ERROR: {type(e).__name__}: {e}", mode=a.mode), 0.0
                res = {"pass": False, "reason": "run error", "verifier_issues": []}
            secs = time.time() - t0
            marks.append("." if res["pass"] else "F")
            row = {"id": t.id, "category": t.category, "level": t.level, "repeat": rep, "pass": res["pass"],
                   "reason": res["reason"], "seconds": round(secs, 1), "tok_s": round(tps, 1),
                   "verifier_issues": res.get("verifier_issues", []), "dax_exec": res.get("dax_exec", ""),
                   "design_score": res.get("design_score", "")}
            rows.append(row)
            md = [f"# {t.id} (repeat {rep}) - {'PASS' if res['pass'] else 'FAIL'}", "", "## Prompt", prompt, ""]
            if res["reason"]:
                md += ["## Reason", res["reason"], ""]
            if r.context:
                md += ["## Context", *[f"- [{c['id']}] {c['source']}" for c in r.context], ""]
            if r.actions:
                md += ["## Tool calls", *[f"- {x['tool']}: {x['status']}" for x in r.actions], ""]
            if row["verifier_issues"]:
                md += ["## Verifier", *[f"- {i}" for i in row["verifier_issues"]], ""]
            if r.thinking:
                md += ["## Thinking", r.thinking, ""]
            md += ["## Answer", r.text]
            (out / f"{t.id}-r{rep}.md").write_text("\n".join(md) + "\n", encoding="utf-8")
        print(f"{t.id:<26} {''.join(marks):<6} {sum(m == '.' for m in marks)}/{len(marks)}")

    with (out / "results.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        for r in rows:
            w.writerow({**r, "verifier_issues": " | ".join(r["verifier_issues"])})
    s = summarise(rows, meta)
    (out / "summary.json").write_text(json.dumps(s, indent=2), encoding="utf-8")
    (out / "summary.md").write_text(markdown(s), encoding="utf-8")
    print("\n" + markdown(s))
    return 0


if __name__ == "__main__":
    sys.exit(main())
