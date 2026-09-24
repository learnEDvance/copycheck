#!/usr/bin/env python3
"""CopyCheck orchestrator: copy-fidelity benchmark over LM Studio models."""

import argparse
import datetime
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import api
import corpus
import metrics
import render

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
OUT = os.path.join(HERE, "out")
REPO_ROOT = ROOT
BASE = api.BASE

MODEL_ORDER = [
    "gemma-3-270m-it",
    "google/gemma-4-e4b",
    "google/gemma-4-12b",
    "prism-ml/bonsai-27b",
]
REPEATS = 10
MAX_ATTEMPTS = 5

START_MARK = "COPYSTART"
END_MARK = "COPYEND"
SYSTEM = (
    "You are a verbatim text copier. Reproduce the text between the COPYSTART and COPYEND "
    "markers exactly, character for character: every letter, digit, space, punctuation and "
    "line break. Output only the copied text. No commentary, no explanation, no code fences, "
    "no markers."
)
USER_TMPL = "Copy this text exactly, between the markers:\n\n{START}\n{src}\n{END}"

AIM = "Measure how accurately local LLMs reproduce input text verbatim."
WHY = "Verbatim copy fidelity is the tightest probe of a model's raw reliability for text-editing tasks."


def log(*a):
    line = " ".join(str(x) for x in a)
    print(line, flush=True)
    with open(os.path.join(OUT, "log.txt"), "a", encoding="utf-8") as fh:
        fh.write(datetime.datetime.now().strftime("%H:%M:%S ") + line + "\n")


def extract(raw):
    if not raw:
        return ""
    s = raw.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
        s = s.rstrip()
        if s.endswith("```"):
            s = s[:-3].rstrip("\n")
    if START_MARK in s:
        i = s.index(START_MARK) + len(START_MARK)
        s = s[i:]
        if END_MARK in s:
            s = s[: s.index(END_MARK)]
        return s
    if END_MARK in s:
        s = s[: s.index(END_MARK)]
    return s


class State:
    def __init__(self):
        self.records = []
        self.progress = {"start_ts": None, "commit_global": 0, "models": {}}
        self.failures = []


def load_records():
    p = os.path.join(OUT, "requests.jsonl")
    if not os.path.exists(p):
        return []
    recs = []
    with open(p, encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if ln:
                try:
                    recs.append(json.loads(ln))
                except Exception:
                    continue
    return recs


def load_progress():
    p = os.path.join(OUT, "progress.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    return {"start_ts": None, "commit_global": 0, "models": {}}


def save_progress(state):
    p = os.path.join(OUT, "progress.json")
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state.progress, fh, indent=2)
    os.replace(tmp, p)


def append_jsonl(rec):
    p = os.path.join(OUT, "requests.jsonl")
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def save_sample_files(rec):
    cell = rec["cell"]
    mdir = os.path.join(OUT, "outputs", render.dir_name(rec["model"]), cell)
    os.makedirs(mdir, exist_ok=True)
    with open(os.path.join(mdir, f"rep-{rec['rep']:04d}.raw.txt"), "w", encoding="utf-8") as fh:
        fh.write(rec.get("raw_output", ""))
    with open(os.path.join(mdir, f"rep-{rec['rep']:04d}.extracted.txt"), "w", encoding="utf-8") as fh:
        fh.write(rec.get("extracted", ""))
    with open(os.path.join(mdir, f"rep-{rec['rep']:04d}.meta.json"), "w", encoding="utf-8") as fh:
        json.dump(rec, fh, ensure_ascii=False, indent=2, default=str)
    with open(os.path.join(mdir, f"rep-{rec['rep']:04d}.diff.txt"), "w", encoding="utf-8") as fh:
        fh.write(diff_text(rec))
    indir = os.path.join(OUT, "inputs", cell)
    os.makedirs(indir, exist_ok=True)
    with open(os.path.join(indir, "source.txt"), "w", encoding="utf-8") as fh:
        fh.write(rec["src"])


def diff_text(rec):
    m = rec.get("metrics", {})
    fd = m.get("first_divergence", 0)
    src = rec.get("src", "")
    ext = rec.get("extracted", "")
    return "\n".join([
        "### SOURCE",
        src,
        "### EXTRACTED OUTPUT",
        ext,
        "### METRICS",
        json.dumps({k: m.get(k) for k in
                    ("src_len", "out_len", "edit_distance", "cer", "exact", "insertions",
                     "deletions", "substitutions", "lcp", "lcs", "first_divergence",
                     "exact_pct", "len_ratio")}, indent=2, default=str),
        "### FIRST DIVERGENCE",
        f"  src: {src[fd:fd+60]!r}",
        f"  out: {ext[fd:fd+60]!r}",
    ])


def run_sample(state, model_key, cell, src, rep, endpoint):
    cell_type, size = cell.split("-")
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": USER_TMPL.format(START=START_MARK, END=END_MARK, src=src)},
    ]
    max_tokens = min(8192, len(src) * 2 + 512)
    rec = {
        "run_id": state.progress.get("run_id"),
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "model": model_key,
        "cell": cell, "type": cell_type, "size": size, "rep": rep,
        "src": src, "messages": messages,
        "params": {"temperature": 0.0, "max_tokens": max_tokens, "endpoint": endpoint},
        "attempts": [], "ok": False,
    }
    result = {"ok": False}
    last_err = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            result = api.chat(BASE, model_key, messages, endpoint=endpoint,
                              max_tokens=max_tokens, temperature=0.0, stream=True)
            result["ok"] = True
            rec["attempts"].append({"n": attempt, "error": None})
            break
        except Exception as e:
            last_err = str(e)
            rec["attempts"].append({"n": attempt, "error": last_err,
                                    "status": getattr(e, "status", None)})
            if attempt < MAX_ATTEMPTS:
                backoff = min(60, 3 * (2 ** (attempt - 1)))
                log(f"  attempt {attempt} failed ({last_err[:120]}); backoff {backoff}s")
                time.sleep(backoff)

    if not result.get("ok"):
        rec.update({"ok": False, "error": last_err, "raw_output": "",
                    "extracted": "", "metrics": metrics.score(src, ""),
                    "finish_reason": None, "usage": None, "created": None, "id": None,
                    "stats": None, "timings": {}, "model_returned": None})
        return rec

    raw = result.get("content_raw", "")
    extracted = extract(raw)
    rec.update({
        "ok": True,
        "raw_output": raw,
        "extracted": extracted,
        "metrics": metrics.score(src, extracted),
        "finish_reason": result.get("finish_reason"),
        "usage": result.get("usage"),
        "created": result.get("created"),
        "id": result.get("id"),
        "stats": result.get("stats"),
        "timings": {
            "first_byte_ms": result.get("first_byte_ms"),
            "ttft_ms": result.get("ttft_ms"),
            "wall_ms": result.get("wall_ms"),
            "delta_count": len(result.get("deltas", [])),
            "deltas": result.get("deltas"),
            "endpoint": result.get("endpoint"),
        },
        "model_returned": result.get("model"),
    })
    return rec


def aggregate(records):
    ok = [r for r in records if r.get("ok") and r.get("metrics")]
    per_model = {}
    per_type = {}
    for r in ok:
        m = r["metrics"]
        pm = per_model.setdefault(r["model"], {"cer_sum": 0.0, "exact": 0, "n": 0})
        pm["cer_sum"] += m["cer"]
        pm["exact"] += 1 if m["exact"] else 0
        pm["n"] += 1
        pt = per_type.setdefault(r["type"], {"cer_sum": 0.0, "exact": 0, "n": 0})
        pt["cer_sum"] += m["cer"]
        pt["exact"] += 1 if m["exact"] else 0
        pt["n"] += 1
    out_pm = {}
    for k, v in per_model.items():
        out_pm[k] = {"cer": v["cer_sum"] / v["n"],
                     "exact_pct": round(100.0 * v["exact"] / v["n"], 2),
                     "samples": v["n"]}
    out_pt = {}
    for k, v in per_type.items():
        out_pt[k] = {"cer": v["cer_sum"] / v["n"],
                     "exact_pct": round(100.0 * v["exact"] / v["n"], 2),
                     "samples": v["n"]}
    return out_pm, out_pt


def conclusion(state, per_model, models_order):
    ranked = sorted(
        [(k, d["cer"]) for k, d in per_model.items() if d["samples"] > 0],
        key=lambda x: x[1],
    )
    if not ranked:
        return "No model results yet — the run is just getting started."
    best_k, best_cer = ranked[0]
    worst_k, worst_cer = ranked[-1]
    best_pct = per_model[best_k]["exact_pct"]
    delta = best_cer / worst_cer if worst_cer > 0 else float("inf")
    extra = ""
    if len(ranked) == len([m for m in models_order if m in per_model]):
        if best_cer <= 0.02:
            extra = " That model is a dependable verbatim copier for editing tasks."
        elif best_cer <= 0.10:
            extra = " Usable for most edits, but verify long or punctuation-dense output."
        else:
            extra = " Not yet dependable for verbatim work — proofread before applying edits."
    return (
        f"Best copier so far: {render.short_name(best_k)} @ {best_cer*100:.2f}% CER "
        f"({best_pct:.1f}% exact). Worst: {render.short_name(worst_k)} @ {worst_cer*100:.2f}%."
        f"{extra}"
    )


def build_state(state, loaded_model):
    per_model, per_type = aggregate(state.records)
    models_order = list(MODEL_ORDER)
    present = {m["key"] for m in api.list_llms(BASE)} if loaded_model else {models_order[0]}
    order = [m for m in models_order if m in present] + [m for m in models_order if m not in present]
    return {
        "name": "CopyCheck",
        "aim": AIM,
        "why": WHY,
        "loaded_model": loaded_model,
        "models_order": order,
        "samples_done": len([r for r in state.records if r.get("ok")]),
        "total_samples": len(MODEL_ORDER) * len(corpus.CELL_KEYS()) * REPEATS,
        "elapsed_sec": (time.time() - state.progress["start_ts"]) if state.progress.get("start_ts") else 0.0,
        "last_update": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "commit_num": state.progress.get("commit_global", 0),
        "per_model": per_model,
        "per_type": per_type,
        "conclusion": conclusion(state, per_model, order),
        "refresh": "every cell commit (~72 pushes / run)",
    }


def render_readme(state, loaded_model, do_commit=True, message=None):
    s = build_state(state, loaded_model)
    md = render.build_readme(s)
    with open(os.path.join(REPO_ROOT, "README.md"), "w", encoding="utf-8") as fh:
        fh.write(md)
    if do_commit:
        ok, out = render.commit_and_push(REPO_ROOT, message or f"ccrun[render][{time.strftime('%Y%m%d-%H%M%S')}][{state.progress['commit_global']:04d}]")
        log(f"commit/push: {('OK' if ok else 'FAILED')} :: {str(out)[-200:]}")
        return ok
    return True


def commit_cell(state, model_key):
    state.progress["commit_global"] = int(state.progress.get("commit_global", 0)) + 1
    per_model_tally = state.progress["models"].setdefault(model_key, {"commits": 0})
    per_model_tally["commits"] = int(per_model_tally.get("commits", 0)) + 1
    short = render.short_name(model_key)
    ts = time.strftime("%Y%m%d-%H%M%S")
    n = per_model_tally["commits"]
    msg = f"ccrun[{short}][{ts}][{n:04d}]"
    return msg


def run(opts):
    os.makedirs(OUT, exist_ok=True)
    if opts.fresh:
        shutil.rmtree(OUT)
        os.makedirs(OUT)

    if not os.path.exists(os.path.join(OUT, "corpus.json")):
        cells = corpus.write_corpus(OUT)
        log("FROZE corpus -> out/corpus.json")
    else:
        with open(os.path.join(OUT, "corpus.json"), encoding="utf-8") as fh:
            cells = json.load(fh)["cells"]

    state = State()
    state.records = load_records()
    state.progress = load_progress()
    if not state.progress.get("start_ts"):
        state.progress["start_ts"] = time.time()
    if not state.progress.get("run_id"):
        state.progress["run_id"] = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")

    endpoint = api.probe_endpoint(BASE)
    log(f"endpoint selected: {endpoint}")

    present = {m["key"] for m in api.list_llms(BASE)}
    order = [m for m in MODEL_ORDER if m in present] + [m for m in MODEL_ORDER if m not in present]
    if opts.models:
        order = [m for m in opts.models.split(",") if m.strip()]
        order = [m.strip() for m in order if m.strip()]
        order = [m for m in order]  # trust user-specified keys

    if not api._server_up(BASE):
        log("FATAL: server unreachable despite revive attempts")
        sys.exit(1)

    try:
        for model_key in order:
            if not api._server_up(BASE):
                log(f"server down before {model_key}; skipping")
                save_progress(state)
                continue
            log(f"== MODEL {model_key} ==")
            api.lms_unload_all()
            api.lms_load(model_key, parallel=1)
            if not api._server_up(BASE, tries=6, wait=4):
                log(f"  load failed for {model_key}; skipping")
                api.lms_unload(model_key)
                continue
            done_cells = set(state.progress["models"].get(model_key, {}).get("done_cells", []))

            cells_iter = list(cells)
            if opts.limit:
                cells_iter = cells_iter[: opts.limit]
            for cell in cells_iter:
                if cell in done_cells:
                    log(f"  skip (done) {cell}")
                    continue
                src = cells[cell]
                repeats = opts.reps or REPEATS
                log(f"  cell {cell} ({len(src)} chars) x{repeats}")
                for rep in range(1, repeats + 1):
                    t0 = time.time()
                    rec = run_sample(state, model_key, cell, src, rep, endpoint)
                    rec["wall_total_s"] = round(time.time() - t0, 2)
                    if not rec["ok"]:
                        state.failures.append({"model": model_key, "cell": cell, "rep": rep,
                                               "error": rec.get("error")})
                    else:
                        save_sample_files(rec)
                    append_jsonl(rec)
                    state.records.append(rec)
                    m = rec.get("metrics", {})
                    log(f"    rep {rep}: ok={rec['ok']} cer={m.get('cer', 0):.4f} "
                        f"exact={m.get('exact')} wall={rec.get('wall_total_s')}s")
                done_cells.add(cell)
                state.progress["models"].setdefault(model_key, {})
                state.progress["models"][model_key]["done_cells"] = sorted(done_cells)
                save_progress(state)
                msg = commit_cell(state, model_key)
                render_readme(state, model_key, do_commit=True, message=msg)

            api.lms_unload(model_key)

        # final state after everything
        render_readme(state, None, do_commit=True)
        log("==== RUN COMPLETE ====")
        log(f"failures: {len(state.failures)}")
        with open(os.path.join(OUT, "FAILED.txt"), "w", encoding="utf-8") as fh:
            json.dump(state.failures, fh, indent=2)
        save_progress(state)
    except KeyboardInterrupt:
        log("interrupted; progress saved")
        save_progress(state)
        render_readme(state, None, do_commit=True)
        sys.exit(130)


def parse_args():
    p = argparse.ArgumentParser(description="CopyCheck benchmark runner")
    p.add_argument("--fresh", action="store_true", help="wipe out/ and freeze a fresh corpus")
    p.add_argument("--models", default=None, help="comma-separated model keys to run")
    p.add_argument("--limit", type=int, default=None, help="run only the first N cells per model")
    p.add_argument("--reps", type=int, default=None, help="repeats per cell (default %(default)s)")
    return p.parse_args()


if __name__ == "__main__":
    run(parse_args())