#!/usr/bin/env python3
"""CopyCheck orchestrator: copy-fidelity benchmark over LM Studio models.

v2:
- models discovered live from LM Studio (auto-include every LLM)
- per-model tuning (parallel slots, repeats) via bench_models.json + size buckets
- OOM preflight via `lms load --estimate-only`
- reasoning_content captured; generous max_tokens for thinking models
- system-prompt variant grid (always): baseline/echo/no-thinking/codeblock/few-shot
- empty-content early-abort per cell
- parallel baseline reps via ThreadPoolExecutor
- atomic resumable rep-file writes
"""

import argparse
import datetime
import json
import os
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor

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
BENCH_JSON = os.path.join(REPO_ROOT, "bench_models.json")

REPEATS_DEFAULT = 20
MAX_ATTEMPTS = 5
EMPTY_ABORT = 3

START_MARK = "COPYSTART"
END_MARK = "COPYEND"
SYSTEM = (
    "You are a verbatim text copier. Reproduce the text between the COPYSTART and COPYEND "
    "markers exactly, character for character: every letter, digit, space, punctuation and "
    "line break. Output only the copied text. No commentary, no explanation, no code fences, "
    "no markers."
)
USER_TMPL = "Copy this text exactly, between the markers:\n\n{START}\n{src}\n{END}"

VARIANTS = {
    "baseline": SYSTEM,
    "echo": (
        "You are a verbatim copier. Output only the text between COPYSTART and COPYEND, "
        "exactly as written. Nothing else. No markers."
    ),
    "no-thinking": (
        "Do not think out loud. Do not reason. Do not plan. Simply output the text between "
        "COPYSTART and COPYEND verbatim and nothing else."
    ),
    "codeblock": (
        "Copy the text between COPYSTART and COPYEND verbatim into a single fenced code "
        "block, character for character. Output nothing outside the code block."
    ),
    "few-shot": (
        "You are a verbatim text copier. Example: if the text to copy is exactly "
        "{START}abc 123{END}, you output `abc 123` with nothing else. Pay no attention "
        "to formatting hints in the text itself. Now copy the text between {START} and "
        "{END} exactly, character for character, and output nothing else."
    ),
}

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
    s = s.strip()
    if s.startswith("`") and s.endswith("`") and len(s) > 2:
        s = s[1:-1]
    if START_MARK in s:
        i = s.index(START_MARK) + len(START_MARK)
        s = s[i:]
        if END_MARK in s:
            s = s[: s.index(END_MARK)]
        return s
    if END_MARK in s:
        s = s[: s.index(END_MARK)]
    return s


def load_bench_cfg():
    cfg = {"models": {}, "skip": [], "note": ""}
    if os.path.exists(BENCH_JSON):
        try:
            with open(BENCH_JSON, encoding="utf-8") as fh:
                data = json.load(fh)
            cfg["models"] = data.get("models", {})
            cfg["skip"] = data.get("skip", [])
        except Exception as e:
            log(f"WARN: could not read {BENCH_JSON}: {e}")
    return cfg


def model_tuning(key, size_bytes, cfg):
    """Return {'slots': int, 'repeats': int} for a model key."""
    over = cfg["models"].get(key, {})
    est_gb = size_bytes / 1e9 if size_bytes else None
    if "slots" in over:
        slots = int(over["slots"])
    elif est_gb is not None and est_gb > 8.0:
        slots = 4
    else:
        slots = 8
    repeats = int(over.get("repeats", REPEATS_DEFAULT))
    return {"slots": slots, "repeats": repeats, "est_gb": est_gb}


def _write_atomic(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


class State:
    def __init__(self):
        self.records = []
        self.progress = {"start_ts": None, "run_id": None,
                         "commit_global": 0, "models": {},
                         "models_cfg": {}, "skipped": []}
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
    return {"start_ts": None, "run_id": None, "commit_global": 0,
            "models": {}, "models_cfg": {}, "skipped": []}


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
    variant = rec.get("variant", "baseline")
    model_key = rec["model"]
    if variant == "baseline":
        mdir = os.path.join(OUT, "outputs", render.dir_name(model_key), cell)
    else:
        mdir = os.path.join(OUT, "prompts", render.dir_name(model_key), cell, variant)
    os.makedirs(mdir, exist_ok=True)
    if variant == "baseline":
        stem = f"rep-{rec['rep']:04d}"
    else:
        stem = variant
    _write_atomic(os.path.join(mdir, f"{stem}.raw.txt"), rec.get("raw_output", ""))
    _write_atomic(os.path.join(mdir, f"{stem}.extracted.txt"), rec.get("extracted", ""))
    _write_atomic(os.path.join(mdir, f"{stem}.meta.json"),
                  json.dumps(rec, ensure_ascii=False, indent=2, default=str))
    _write_atomic(os.path.join(mdir, f"{stem}.diff.txt"), diff_text(rec))
    indir = os.path.join(OUT, "inputs", cell)
    os.makedirs(indir, exist_ok=True)
    _write_atomic(os.path.join(indir, "source.txt"), rec["src"])


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
                     "deletions", "substitutions", "lcp", "lcs_len", "pct_conserved",
                     "first_divergence", "exact_pct", "len_ratio")}, indent=2, default=str),
        "### FIRST DIVERGENCE",
        f"  src: {src[fd:fd+60]!r}",
        f"  out: {ext[fd:fd+60]!r}",
    ])


def run_sample(state, model_key, cell, src, rep, endpoint, variant="baseline"):
    cell_type, size = cell.split("-")
    system_prompt = VARIANTS.get(variant, SYSTEM)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": USER_TMPL.format(START=START_MARK, END=END_MARK, src=src)},
    ]
    max_tokens = min(32768, int(len(src) * 1.25) + 512 + 2048)
    rec = {
        "run_id": state.progress.get("run_id"),
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "model": model_key,
        "cell": cell, "type": cell_type, "size": size, "rep": rep,
        "variant": variant,
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
                    "reasoning": "", "reasoning_only": False,
                    "extracted": "", "metrics": metrics.score(src, ""),
                    "finish_reason": None, "usage": None, "created": None, "id": None,
                    "stats": None, "timings": {}, "model_returned": None})
        return rec

    raw = result.get("content_raw", "")
    reasoning = result.get("reasoning_raw", "")
    extracted = extract(raw)
    score = metrics.score(src, extracted)
    rec.update({
        "ok": True,
        "raw_output": raw,
        "reasoning": reasoning,
        "reasoning_only": bool(reasoning) and not raw,
        "extracted": extracted,
        "metrics": score,
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


def _row(r, d):
    m = r.get("metrics")
    if not (r.get("ok") and m):
        return None
    d.setdefault("cer_sum", 0.0)
    d["cer_sum"] += m["cer"]
    d["exact"] = d.get("exact", 0) + (1 if m["exact"] else 0)
    d["n"] = d.get("n", 0) + 1
    d["ok"] = d.get("ok", 0) + 1
    if not r.get("raw_output"):
        d["empty"] = d.get("empty", 0) + 1
    if r.get("reasoning_only"):
        d["reasoning_only"] = d.get("reasoning_only", 0) + 1
    d["wall_sum"] = d.get("wall_sum", 0.0) + float(r.get("wall_total_s", 0.0) or 0.0)
    return d


def aggregate(records):
    per_model = {}
    per_type = {}
    per_cell = {}
    per_variant = {}

    for r in records:
        m = r.get("metrics")
        if r.get("ok") and m and r.get("variant") == "baseline":
            _row(r, per_model.setdefault(r["model"], {}))
            _row(r, per_type.setdefault(r["type"], {}))
            _row(r, per_cell.setdefault(r["model"], {}).setdefault(r["cell"], {}))
        if r.get("ok") and m and r.get("variant") != "baseline":
            pv = per_variant.setdefault(r["model"], {}).setdefault(r["variant"], {})
            pv["cer_sum"] = pv.get("cer_sum", 0.0) + m["cer"]
            pv["exact"] = pv.get("exact", 0) + (1 if m["exact"] else 0)
            pv["n"] = pv.get("n", 0) + 1

    def finalize(d):
        return {k: {
            "cer": v["cer_sum"] / max(1, v["n"]),
            "exact_pct": round(100.0 * v.get("exact", 0) / max(1, v["n"]), 2),
            "samples": v["n"],
            "ok": v.get("ok", 0),
            "empty": v.get("empty", 0),
            "reasoning_only": v.get("reasoning_only", 0),
            "len_ratio": round(1.0, 4),
            "wall_avg_s": round(v.get("wall_sum", 0.0) / max(1, v["n"]), 2),
        } for k, v in d.items()}

    def per_cell_finalize(group):
        out = {}
        for cell, v in group.items():
            out[cell] = {
                "cer": v["cer_sum"] / max(1, v["n"]),
                "exact_pct": round(100.0 * v.get("exact", 0) / max(1, v["n"]), 2),
                "samples": v["n"],
                "empty": v.get("empty", 0),
                "reasoning_only": v.get("reasoning_only", 0),
                "wall_avg_s": round(v.get("wall_sum", 0.0) / max(1, v["n"]), 2),
            }
        return out

    def variants_finalize(pv):
        return {
            m: {v: {"cer": d["cer_sum"] / max(1, d["n"]),
                    "exact_pct": round(100.0 * d.get("exact", 0) / max(1, d["n"]), 2),
                    "samples": d["n"]}
                for v, d in vmap.items()}
            for m, vmap in pv.items()
        }

    return (finalize(per_model), finalize(per_type),
            {k: per_cell_finalize(v) for k, v in per_cell.items()},
            variants_finalize(per_variant))


def conclusion(per_model):
    ranked = sorted(
        [(k, d["cer"]) for k, d in per_model.items() if d["samples"] > 0],
        key=lambda x: x[1],
    )
    if not ranked:
        return "No baseline results yet — the run is just getting started."
    best_k, best_cer = ranked[0]
    worst_k, worst_cer = ranked[-1]
    best_pct = per_model[best_k]["exact_pct"]
    extra = ""
    if len(ranked) == 1:
        if best_cer <= 0.02:
            extra = " That model is a dependable verbatim copier for editing tasks."
        elif best_cer <= 0.10:
            extra = " Usable for most edits, but verify long or punctuation-dense output."
        else:
            extra = " Not yet dependable for verbatim work — proofread before applying edits."
    else:
        extra = (" The prompt-variant grid below shows whether a different system prompt "
                 "rescues the weaker copiers.")
    return (
        f"Best copier so far: {render.short_name(best_k)} @ {best_cer*100:.2f}% CER "
        f"({best_pct:.1f}% exact). Worst: {render.short_name(worst_k)} @ {worst_cer*100:.2f}%."
        f"{extra}"
    )


def build_state(state, loaded_model):
    per_model, per_type, per_cell, per_variant = aggregate(state.records)
    cfg = state.progress.get("models_cfg", {})
    skipped_keys = {s["model"] for s in state.progress.get("skipped", [])}
    total = 0
    for k, t in cfg.items():
        if k in skipped_keys:
            continue
        total += (int(t["repeats"]) + len(VARIANTS) - 1) * len(corpus.CELL_KEYS())
    return {
        "name": "CopyCheck",
        "aim": AIM,
        "why": WHY,
        "loaded_model": loaded_model,
        "models_order": list(cfg.keys()),
        "samples_done": len([r for r in state.records if r.get("ok")]),
        "total_samples": total,
        "elapsed_sec": (time.time() - state.progress["start_ts"]) if state.progress.get("start_ts") else 0.0,
        "last_update": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "commit_num": state.progress.get("commit_global", 0),
        "run_id": state.progress.get("run_id"),
        "per_model": per_model,
        "per_type": per_type,
        "per_cell": per_cell,
        "per_variant": per_variant,
        "skipped": state.progress.get("skipped", []),
        "conclusion": conclusion(per_model),
        "refresh": "every cell commit",
    }


def render_readme(state, loaded_model, do_commit=True, message=None):
    s = build_state(state, loaded_model)
    md = render.build_readme(s)
    _write_atomic(os.path.join(REPO_ROOT, "README.md"), md)
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


def parse_args():
    p = argparse.ArgumentParser(description="CopyCheck benchmark runner (v2)")
    p.add_argument("--fresh", action="store_true", help="wipe out/ and freeze a fresh corpus")
    p.add_argument("--models", default=None, help="comma-separated model keys to run")
    p.add_argument("--limit", type=int, default=None, help="run only the first N cells per model")
    p.add_argument("--reps", type=int, default=None, help="override repeats per cell")
    p.add_argument("--slots", type=int, default=None, help="override parallel slots for every model")
    p.add_argument("--no-grid", action="store_true", help="disable the prompt-variant grid")
    return p.parse_args()


def _probe_model(model_key, endpoint):
    """One tiny request to confirm the model actually answers."""
    try:
        api.chat(BASE, model_key,
                 [{"role": "user", "content": "ping"}],
                 endpoint=endpoint, max_tokens=1, temperature=0.0, stream=True, timeout=30)
        return None
    except Exception as e:
        return str(e)


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

    cfg = load_bench_cfg()

    state = State()
    state.records = load_records()
    state.progress = load_progress()
    if not state.progress.get("start_ts"):
        state.progress["start_ts"] = time.time()
    if not state.progress.get("run_id"):
        state.progress["run_id"] = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")

    endpoint = api.probe_endpoint(BASE)
    log(f"endpoint selected: {endpoint}")

    all_llms = api.list_llms(BASE)
    if not all_llms:
        log("FATAL: no LLMs discovered at " + BASE)
        sys.exit(1)

    if opts.models:
        want = {m.strip() for m in opts.models.split(",") if m.strip()}
        subjects = [m for m in all_llms if m["key"] in want]
    else:
        subjects = [m for m in all_llms if m["key"] not in cfg["skip"]]

    subjects.sort(key=lambda m: (m["size"] or 0, m["key"]))

    tunings = {}
    for m in subjects:
        t = model_tuning(m["key"], m.get("size", 0), cfg)
        if opts.slots:
            t["slots"] = opts.slots
        if opts.reps:
            t["repeats"] = opts.reps
        t["size"] = m.get("size", 0)
        tunings[m["key"]] = t

    skipped = []
    free_gb = api.gpu_mem_free_gb()
    vram_total_gb = api.gpu_mem_total_gb()
    log(f"GPU: {vram_total_gb if vram_total_gb is not None else 'unknown'} GB total, "
        f"{free_gb if free_gb is not None else 'unknown'} GB free")
    for m in subjects:
        est = api.lms_load_estimate(m["key"])
        t = tunings[m["key"]]
        if est.get("total_gb") is not None:
            t["est_total_gb"] = est["total_gb"]
        if est.get("gpu_gb") is not None:
            t["est_gpu_gb"] = est["gpu_gb"]
        log(f"model {m['key']}: slots={t['slots']} repeats={t['repeats']} "
            f"est_gpu={est['gpu_gb']}GB est_total={est['total_gb']}GB est_only(rc={est['rc']})")

    if skipped:
        log(f"skipped {len(skipped)} model(s) before any requests")
    state.progress["models_cfg"] = tunings
    state.progress["skipped"] = skipped
    save_progress(state)

    if not api._server_up(BASE):
        log("FATAL: server unreachable despite revive attempts")
        sys.exit(1)

    active = [m["key"] for m in subjects if not any(s["model"] == m["key"] for s in skipped)]
    log(f"subjects: {active}")

    try:
        for model_key in active:
            if not api._server_up(BASE):
                log(f"server down before {model_key}; skipping")
                save_progress(state)
                continue
            t = tunings[model_key]
            log(f"== MODEL {model_key} == slots={t['slots']} reps={t['repeats']} ==")
            api.lms_unload_all()
            api.lms_load(model_key, parallel=t["slots"])
            if not api._server_up(BASE, tries=6, wait=4):
                log(f"  load failed for {model_key}; skipping")
                state.progress["skipped"].append({"model": model_key, "reason": "load_failed",
                                                  "detail": "server did not come back after lms load"})
                api.lms_unload(model_key)
                continue
            probe_err = _probe_model(model_key, endpoint)
            if probe_err:
                log(f"  model {model_key} did not answer (abort): {probe_err[:140]}")
                state.progress["skipped"].append({"model": model_key, "reason": "load_failed",
                                                  "detail": str(probe_err)[:200]})
                api.lms_unload(model_key)
                continue
            done_cells = set(state.progress["models"].get(model_key, {}).get("done_cells", []))

            cells_iter = list(cells)
            if opts.limit:
                cells_iter = cells_iter[: opts.limit]
            consec_fail = 0
            model_abort = None
            for cell in cells_iter:
                if cell in done_cells:
                    log(f"  skip (done) {cell}")
                    continue
                src = cells[cell]
                repeats = t["repeats"]
                log(f"  cell {cell} ({len(src)} chars) x{repeats}")

                p_out = os.path.join(OUT, "outputs", render.dir_name(model_key), cell)
                p_prompts = os.path.join(OUT, "prompts", render.dir_name(model_key), cell)
                for p in (p_out, p_prompts):
                    if os.path.isdir(p):
                        shutil.rmtree(p)

                empty_streak = 0
                aborted = False
                ex = ThreadPoolExecutor(max_workers=t["slots"])
                futs = [ex.submit(run_sample, state, model_key, cell, src,
                                  rep, endpoint, "baseline")
                        for rep in range(1, repeats + 1)]
                try:
                    for rep, fut in enumerate(futs, start=1):
                        t0 = time.time()
                        rec = fut.result()
                        rec["wall_total_s"] = round(time.time() - t0, 2)
                        if not rec["ok"]:
                            state.failures.append({"model": model_key, "cell": cell, "rep": rep,
                                                   "variant": "baseline",
                                                   "error": rec.get("error")})
                            consec_fail += 1
                            if model_abort is None:
                                model_abort = rec.get("error", "")[:200]
                            log(f"    rep {rep}: FAIL ({rec.get('error', '')[:120]}); "
                                f"consec_fail={consec_fail}")
                            if consec_fail >= 3:
                                log(f"    MODEL ABORT {model_key}: {consec_fail} consecutive "
                                    "request failures")
                                for f in futs[rep:]:
                                    f.cancel()
                                aborted = True
                                break
                        else:
                            consec_fail = 0
                            save_sample_files(rec)
                            append_jsonl(rec)
                            state.records.append(rec)
                            m = rec.get("metrics", {})
                            bad = m.get("cer", 0) >= 0.99 or rec.get("reasoning_only") or not rec.get("raw_output")
                            empty_streak = empty_streak + 1 if bad else 0
                            log(f"    rep {rep}: ok={rec['ok']} cer={m.get('cer', 0):.4f} "
                                f"exact={m.get('exact')} wall={rec.get('wall_total_s')}s")
                            if empty_streak >= EMPTY_ABORT and rep < repeats:
                                log(f"    EARLY ABORT: {EMPTY_ABORT} consecutive "
                                    "empty/reasoning-only/garbage reps")
                                aborted = True
                                for f in futs[rep:]:
                                    f.cancel()
                                break
                finally:
                    ex.shutdown(wait=True, cancel_futures=True)

                if model_abort:
                    state.progress["skipped"].append(
                        {"model": model_key, "reason": "load_failed",
                         "detail": model_abort})
                    save_progress(state)
                    break

                if aborted:
                    state.progress["models"].setdefault(model_key, {})
                    state.progress["models"][model_key]["no_content"] = \
                        state.progress["models"][model_key].get("no_content", []) + [cell]

                if not opts.no_grid:
                    for variant in VARIANTS:
                        if variant == "baseline":
                            continue
                        t0 = time.time()
                        rec = run_sample(state, model_key, cell, src, 1, endpoint, variant)
                        rec["wall_total_s"] = round(time.time() - t0, 2)
                        if rec["ok"]:
                            save_sample_files(rec)
                        append_jsonl(rec)
                        state.records.append(rec)
                        m = rec.get("metrics", {})
                        log(f"    variant {variant}: cer={m.get('cer', 0):.4f} "
                            f"exact={m.get('exact')}")

                done_cells.add(cell)
                state.progress["models"].setdefault(model_key, {})
                state.progress["models"][model_key]["done_cells"] = sorted(done_cells)
                save_progress(state)
                msg = commit_cell(state, model_key)
                render_readme(state, model_key, do_commit=True, message=msg)

            api.lms_unload(model_key)

        render_readme(state, None, do_commit=True)
        log("==== RUN COMPLETE ====")
        log(f"failures: {len(state.failures)}")
        with open(os.path.join(OUT, "FAILED.txt"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps(state.failures, indent=2))
        save_progress(state)
    except KeyboardInterrupt:
        log("interrupted; progress saved")
        save_progress(state)
        render_readme(state, None, do_commit=True)
        sys.exit(130)


if __name__ == "__main__":
    run(parse_args())