#!/usr/bin/env python3
"""CopyCheck orchestrator: copy-fidelity benchmark over LM Studio models.

v2:
- models discovered live from LM Studio (auto-include every LLM)
- per-model tuning (max_par, repeats) via bench_models.json + size buckets
- heavy models run first (sorted on size_bytes, descending)
- OOM preflight via `lms load --estimate-only` (mirrors the real load flags)
- reasoning_content captured; generous max_tokens for thinking models
- system-prompt variant grid (always): baseline/echo/no-thinking/codeblock/few-shot
- empty-content early-abort per cell
- per-cell concurrency ladder (L=1, M=max_par/2, S=max_par) so the shared KV
  pool is never oversubscribed; auto-halves max_par if a stream is truncated
- save point after every rep + `s` key for a manual checkpoint; resume continues
  a cell at the next missing rep instead of redoing it
- atomic resumable rep-file writes (fsync'd)
"""

import argparse
import datetime
import json
import os
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait as futures_wait, FIRST_COMPLETED

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import api
import corpus
import metrics
import render
import tui

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
    """Return {'max_par': int, 'repeats': int, ...} for a model key."""
    over = cfg["models"].get(key, {})
    est_gb = size_bytes / 1e9 if size_bytes else None
    if "max_par" in over:
        max_par = int(over["max_par"])
    elif "slots" in over:  # legacy key
        max_par = int(over["slots"])
    elif est_gb is not None and est_gb > 8.0:
        max_par = 4
    else:
        max_par = 8
    repeats = int(over.get("repeats", REPEATS_DEFAULT))
    return {"max_par": max_par, "repeats": repeats, "est_gb": est_gb,
            "ctx": over.get("ctx"), "gpu": over.get("gpu")}


def _write_atomic(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _progress_default():
    return {"start_ts": None, "run_id": None, "commit_global": 0,
            "models": {}, "models_cfg": {}, "skipped": [], "concurrency": {}}


class State:
    def __init__(self):
        self.records = []
        self.progress = _progress_default()
        self.failures = []


def load_records():
    p = os.path.join(OUT, "requests.jsonl")
    if not os.path.exists(p):
        return []
    recs = {}
    with open(p, encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if ln:
                try:
                    r = json.loads(ln)
                except Exception:
                    continue
                key = (r.get("model"), r.get("cell"), r.get("variant"), r.get("rep"))
                recs[key] = r
    return list(recs.values())


def load_progress():
    p = os.path.join(OUT, "progress.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as fh:
            prog = json.load(fh)
        for k, v in _progress_default().items():
            prog.setdefault(k, v)
        return prog
    return _progress_default()


def save_progress(state):
    p = os.path.join(OUT, "progress.json")
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state.progress, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, p)


def append_jsonl(rec):
    p = os.path.join(OUT, "requests.jsonl")
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


class StopSignal:
    def __init__(self):
        self.event = threading.Event()


class SaveSignal:
    def __init__(self):
        self.event = threading.Event()


def start_key_listener(sig, save_sig):
    """Daemon thread: stdin (cbreak) -> SPACE = safe-stop, S = save checkpoint.

    The thread only sets events; all writing happens on the main thread.
    """
    try:
        import select
        import termios
        import tty
    except Exception as e:
        log(f"key listener disabled: {e}")
        return None
    try:
        fd = sys.stdin.fileno()
    except Exception:
        return None
    if not os.isatty(fd):
        log("key listener disabled: stdin is not a terminal (SPACE/S unavailable, "
            "Ctrl-C only)")
        return None
    log("keys: SPACE = safe-stop (keeps finished reps) · S = save checkpoint now")

    def _listen():
        old = None
        try:
            old = termios.tcgetattr(fd)
            tty.setcbreak(fd)
            while not sig.event.is_set():
                r, _, _ = select.select([sys.stdin], [], [], 0.25)
                if r:
                    try:
                        chunk = os.read(fd, 64)
                    except Exception:
                        break
                    low = chunk.lower()
                    if b"s" in low:
                        log("SAVE: S pressed (checkpoint after current rep)")
                        save_sig.event.set()
                    if b" " in chunk:
                        log("SAFE-STOP: SPACE pressed")
                        sig.event.set()
                        return
        finally:
            if old is not None:
                try:
                    termios.tcsetattr(fd, termios.TCSADRAIN, old)
                except Exception:
                    pass

    th = threading.Thread(target=_listen, daemon=True)
    th.start()
    return th


def cell_workers(cell, max_par):
    """Per-cell concurrency ladder.

    The KV pool is shared across concurrent sequences, so a cell's aggregate
    demand must fit inside ctx. L cells peak at ~6.7k tokens/req, M at ~2.2k,
    S at ~0.9k; inside a 16k pool that means 1 / ctx-slots-ish / full width.
    """
    size = cell.rsplit("-", 1)[-1]
    if size == "L":
        return 1
    if size == "M":
        return max(1, max_par // 2)
    return max(1, max_par)


def _done_reps(state, model_key, cell, variant="baseline"):
    """Reps already recorded for this model/cell/variant.

    Union of the progress bookkeeping and whatever is actually on disk, so a
    stale or lost progress.json still resumes correctly.
    """
    done = set()
    prog = state.progress.get("models", {}).get(model_key, {})
    for cell_done in prog.get("reps_done", {}).get(cell, []):
        if cell_done.get("variant", "baseline") == variant:
            done.add(int(cell_done.get("rep")))
    for r in state.records:
        if (r.get("model") == model_key and r.get("cell") == cell
                and (r.get("variant") or "baseline") == variant and r.get("ok")):
            done.add(int(r["rep"]))
    return done


def _mark_rep_done(state, model_key, cell, rep, variant="baseline", total=None):
    prog = state.progress.setdefault("models", {}).setdefault(model_key, {})
    reps = prog.setdefault("reps_done", {}).setdefault(cell, [])
    entry = {"rep": int(rep), "variant": variant}
    if not any(e.get("rep") == entry["rep"] and e.get("variant", "baseline") == variant
               for e in reps):
        reps.append(entry)
        reps.sort(key=lambda e: (e.get("variant", "baseline"), e["rep"]))
    if total is not None:
        prog.setdefault("reps_total", {})[cell] = int(total)


def _clear_rep_done(state, model_key, cell, rep, variant="baseline"):
    prog = state.progress.get("models", {}).get(model_key, {})
    reps = prog.get("reps_done", {}).get(cell)
    if not reps:
        return
    prog["reps_done"][cell] = [e for e in reps
                              if not (e.get("rep") == int(rep)
                                      and e.get("variant", "baseline") == variant)]


def _clear_cell_done(state, model_key, cell):
    prog = state.progress.get("models", {}).get(model_key, {})
    prog.get("reps_done", {}).pop(cell, None)


def _drop_rep_files(model_key, cell, rep):
    """Remove just one rep's artifacts so a re-run cannot leave stale files."""
    d = os.path.join(OUT, "outputs", render.dir_name(model_key), cell)
    for suffix in (".raw.txt", ".extracted.txt", ".meta.json", ".diff.txt"):
        p = os.path.join(d, f"rep-{rep:04d}{suffix}")
        try:
            os.remove(p)
        except OSError:
            pass


def checkpoint(state, model_key, cell, rep, repeats):
    """Manual save point (`s` key). Flush progress + a human-readable marker."""
    save_progress(state)
    done = sorted(_done_reps(state, model_key, cell))
    payload = {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "run_id": state.progress.get("run_id"),
        "model": model_key,
        "cell": cell,
        "reps_done": len(done),
        "reps_total": repeats,
        "records_total": len([r for r in state.records if r.get("ok")]),
        "concurrency": state.progress.get("concurrency", {}).get(model_key),
        "git_head": _git_head(),
    }
    _write_atomic(os.path.join(OUT, "CHECKPOINT.json"),
                  json.dumps(payload, indent=2) + "\n")
    log(f"CHECKPOINT saved: {model_key}/{cell} rep {rep} "
        f"({payload['reps_done']}/{repeats} reps, {payload['records_total']} records)")
    return payload


def _service_keys(state, save_sig, stop_sig, model_key, cell, next_rep, repeats):
    """Honour pending keypresses from the main loop (never from the listener)."""
    if save_sig.event.is_set():
        save_sig.event.clear()
        checkpoint(state, model_key, cell, next_rep if next_rep else 0, repeats)
    return stop_sig.event.is_set()


def _git_head():
    try:
        import subprocess as _sp
        p = _sp.run(["git", "-C", REPO_ROOT, "rev-parse", "--short", "HEAD"],
                    capture_output=True, text=True, timeout=20)
        return p.stdout.strip() or None
    except Exception:
        return None


def _step_down(state, model_key, reason):
    """Halve this model's concurrency cap after a truncated stream."""
    conc = state.progress.setdefault("concurrency", {})
    cur = conc.get(model_key)
    if cur is None:
        return None
    new = max(1, int(cur) // 2)
    if new == cur:
        return cur
    conc[model_key] = new
    log(f"step-down {cur}->{new} for {model_key}: {reason}")
    save_progress(state)
    return new


def stop_key_listener(thread):
    if thread is not None:
        thread.join(timeout=2.0)


def _purge_cell(model_key, cell, state, reason="discard"):
    """Drop a cell's recorded reps entirely.

    Only used when the data itself is worthless (garbage early-abort, model
    abort) or when the user asks for a clean redo. Safe-stop and crashes keep
    every finished rep -- see _discard_unfinished.
    """
    d_out = os.path.join(OUT, "outputs", render.dir_name(model_key), cell)
    d_prompts = os.path.join(OUT, "prompts", render.dir_name(model_key), cell)
    for d in (d_out, d_prompts):
        if os.path.isdir(d):
            shutil.rmtree(d)
    state.records[:] = [r for r in state.records
                        if not (r.get("model") == model_key and r.get("cell") == cell)]
    state.failures[:] = [f for f in state.failures
                         if not (f.get("model") == model_key and f.get("cell") == cell)]
    _clear_cell_done(state, model_key, cell)
    p = os.path.join(OUT, "requests.jsonl")
    if os.path.exists(p):
        keep = None
        try:
            with open(p, encoding="utf-8") as fh:
                keep = []
                for ln in fh:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        r = json.loads(ln)
                    except Exception:
                        continue
                    if r.get("model") == model_key and r.get("cell") == cell:
                        continue
                    keep.append(ln)
        except Exception as e:
            log(f"purge: could not scan requests.jsonl ({e}); resume will dedup")
            keep = None
        if keep is not None:
            with open(p, "w", encoding="utf-8") as fh:
                if keep:
                    fh.write("\n".join(keep) + "\n")
    log(f"purged {model_key}/{cell} ({reason})")


def _write_stopped(state, reason, partial=None):
    payload = {"ts": datetime.datetime.now().isoformat(timespec="seconds"),
               "reason": reason,
               "commit_global": state.progress.get("commit_global", 0)}
    if partial:
        payload["partial"] = partial
    _write_atomic(os.path.join(OUT, "STOPPED.json"),
                  json.dumps(payload, indent=2) + "\n")


def _rep_tps(rec):
    usage = rec.get("usage")
    ct = (usage.get("completion_tokens") or 0) if isinstance(usage, dict) else 0
    wall = rec.get("wall_total_s") or 0
    return ct / wall if wall > 0 else None


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
            if "finish_reason" in last_err:
                rec["truncated"] = True
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

    subjects.sort(key=lambda m: (m["size"] or 0, m["key"]), reverse=True)

    tunings = {}
    for m in subjects:
        t = model_tuning(m["key"], m.get("size", 0), cfg)
        if opts.slots:
            t["max_par"] = opts.slots
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
        est = api.lms_load_estimate(m["key"], parallel=t["max_par"],
                                    ctx=t.get("ctx"), gpu=t.get("gpu"))
        t = tunings[m["key"]]
        if est.get("total_gb") is not None:
            t["est_total_gb"] = est["total_gb"]
        if est.get("gpu_gb") is not None:
            t["est_gpu_gb"] = est["gpu_gb"]
        log(f"model {m['key']}: max_par={t['max_par']} repeats={t['repeats']} "
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

    stop_sig = StopSignal()
    save_sig = SaveSignal()
    sig_thread = start_key_listener(stop_sig, save_sig)
    tui.header(f"{len(active)} engines · 5-prompt grid · temp 0 · resume-safe · mission → out/")

    try:
        in_flight = None
        stop_reason = None
        for model_key in active:
            if stop_sig.event.is_set():
                log("SAFE-STOP at model boundary")
                break
            if not api._server_up(BASE):
                log(f"server down before {model_key}; skipping")
                save_progress(state)
                continue
            t = tunings[model_key]
            tui.model_card(model_key, t)
            log(f"== MODEL {model_key} == max_par={t['max_par']} reps={t['repeats']} ==")
            api.lms_unload_all()
            api.lms_load(model_key, parallel=t["max_par"], ctx=t.get("ctx"),
                          gpu=t.get("gpu"))
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
                if stop_sig.event.is_set():
                    log("SAFE-STOP at cell boundary")
                    break
                if cell in done_cells:
                    log(f"  skip (done) {cell}")
                    continue
                src = cells[cell]
                repeats = t["repeats"]
                in_flight = (model_key, cell)
                log(f"  cell {cell} ({len(src)} chars) x{repeats}")

                # Resume: only the reps that are not already recorded get run.
                done_reps = _done_reps(state, model_key, cell, "baseline")
                todo = [r for r in range(1, repeats + 1) if r not in done_reps]
                if done_reps:
                    log(f"    resuming {cell}: {len(done_reps)} rep(s) already saved, "
                        f"{len(todo)} to go")

                # Seed the garbage streak from reps recorded earlier so the
                # early-abort rule behaves the same on a resumed cell.
                empty_streak = 0
                for r in sorted(done_reps):
                    rec = next((x for x in state.records
                                if x.get("model") == model_key and x.get("cell") == cell
                                and x.get("rep") == r), None)
                    if not rec:
                        continue
                    m = rec.get("metrics", {})
                    bad = (m.get("cer", 0) >= 0.99 or rec.get("reasoning_only")
                           or not rec.get("raw_output"))
                    empty_streak = empty_streak + 1 if bad else 0

                aborted = False
                stopped = False
                # Clear only the reps we are about to (re)run, never the whole cell.
                for rep in todo:
                    _drop_rep_files(model_key, cell, rep)

                conc = state.progress.setdefault("concurrency", {})
                conc.setdefault(model_key, t["max_par"])
                workers = min(cell_workers(cell, conc[model_key]), len(todo) or 1)
                log(f"    workers={workers} (max_par={conc[model_key]}, "
                    f"ladder L=1 M={max(1, conc[model_key] // 2)} S={conc[model_key]})")

                ex = ThreadPoolExecutor(max_workers=workers)
                futs = {ex.submit(run_sample, state, model_key, cell, src,
                                  rep, endpoint, "baseline"): rep for rep in todo}
                submitted_at = {rep: time.time() for rep in todo}
                pending = set(futs)
                results = {}
                nxt = todo[0] if todo else None
                try:
                    while pending:
                        if stop_sig.event.is_set() or save_sig.event.is_set():
                            _service_keys(state, save_sig, stop_sig, model_key, cell,
                                          nxt, repeats)
                        done_futs, pending = futures_wait(
                            pending, timeout=0.5, return_when=FIRST_COMPLETED)
                        for f in done_futs:
                            results[futs[f]] = (f.result(), time.time())
                        # Consume in rep order so the streak logic stays stable.
                        while nxt is not None and nxt in results:
                            rec, t_end = results.pop(nxt)
                            rep = nxt
                            rec["wall_total_s"] = round(
                                max(0.0, t_end - submitted_at.get(rep, t_end)), 2)
                            nxt += 1
                            if not rec["ok"]:
                                state.failures.append(
                                    {"model": model_key, "cell": cell, "rep": rep,
                                     "variant": "baseline", "error": rec.get("error")})
                                consec_fail += 1
                                if rec.get("truncated"):
                                    _step_down(state, model_key,
                                               "stream ended without finish_reason")
                                    conc = state.progress["concurrency"]
                                if model_abort is None:
                                    model_abort = rec.get("error", "")[:200]
                                log(f"    rep {rep}: FAIL ({rec.get('error', '')[:120]}); "
                                    f"consec_fail={consec_fail}")
                                if consec_fail >= 3:
                                    log(f"    MODEL ABORT {model_key}: {consec_fail} "
                                        "consecutive request failures")
                                    for f in pending:
                                        f.cancel()
                                    aborted = True
                                    pending = set()
                                    break
                            else:
                                consec_fail = 0
                                save_sample_files(rec)
                                append_jsonl(rec)
                                state.records.append(rec)
                                _mark_rep_done(state, model_key, cell, rep,
                                               "baseline", total=repeats)
                                # save point after every rep
                                save_progress(state)
                                m = rec.get("metrics", {})
                                bad = (m.get("cer", 0) >= 0.99 or rec.get("reasoning_only")
                                       or not rec.get("raw_output"))
                                empty_streak = empty_streak + 1 if bad else 0
                                log(f"    rep {rep}: ok={rec['ok']} "
                                    f"cer={m.get('cer', 0):.4f} "
                                    f"exact={m.get('exact')} "
                                    f"wall={rec.get('wall_total_s')}s "
                                    f"[saved {len(_done_reps(state, model_key, cell))}"
                                    f"/{repeats}]")
                                tui.cell_scan(rep, repeats, m.get("cer", 0),
                                              _rep_tps(rec))
                                if empty_streak >= EMPTY_ABORT and len(done_reps) + (
                                        rep - len(done_reps)) < repeats:
                                    log(f"    EARLY ABORT: {EMPTY_ABORT} consecutive "
                                        "empty/reasoning-only/garbage reps")
                                    aborted = True
                                    for f in pending:
                                        f.cancel()
                                    pending = set()
                                    break
                            if stop_sig.event.is_set():
                                log("SAFE-STOP requested (SPACE); keeping "
                                    f"{len(_done_reps(state, model_key, cell))} "
                                    "finished rep(s) for resume")
                                stopped = True
                                stop_reason = (f"{model_key} / {cell} "
                                              f"(partial: {len(_done_reps(state, model_key, cell))}"
                                              f"/{repeats} reps kept)")
                                for f in pending:
                                    f.cancel()
                                pending = set()
                                break
                        if aborted or stopped:
                            break
                    if save_sig.event.is_set():
                        _service_keys(state, save_sig, stop_sig, model_key, cell,
                                      nxt, repeats)
                except KeyboardInterrupt:
                    stopped = True
                    raise
                finally:
                    ex.shutdown(wait=(not stopped), cancel_futures=True)
                tui.scan_done()

                if stopped:
                    in_flight = None
                    save_progress(state)
                    break

                # A cell is only "done" once every rep is on disk.
                if len(_done_reps(state, model_key, cell, "baseline")) >= repeats:
                    done_cells.add(cell)

                if model_abort:
                    state.progress["skipped"].append(
                        {"model": model_key, "reason": "load_failed",
                         "detail": model_abort})
                    _purge_cell(model_key, cell, state, reason="model abort")
                    save_progress(state)
                    break

                if aborted:
                    # Garbage output is not a measurement: drop the cell so the
                    # next run retries it from scratch instead of resuming on.
                    state.progress["models"].setdefault(model_key, {})
                    state.progress["models"][model_key]["no_content"] = \
                        state.progress["models"][model_key].get("no_content", []) + [cell]
                    _purge_cell(model_key, cell, state,
                                reason="garbage early-abort (redo from scratch)")

                if not opts.no_grid and not aborted:
                    for variant in VARIANTS:
                        if variant == "baseline":
                            continue
                        if _done_reps(state, model_key, cell, variant):
                            continue
                        t0 = time.time()
                        rec = run_sample(state, model_key, cell, src, 1, endpoint, variant)
                        rec["wall_total_s"] = round(time.time() - t0, 2)
                        if rec["ok"]:
                            save_sample_files(rec)
                        append_jsonl(rec)
                        state.records.append(rec)
                        if rec["ok"]:
                            _mark_rep_done(state, model_key, cell, 1, variant)
                        save_progress(state)
                        m = rec.get("metrics", {})
                        log(f"    variant {variant}: cer={m.get('cer', 0):.4f} "
                            f"exact={m.get('exact')}")
                        if stop_sig.event.is_set():
                            stopped = True
                            stop_reason = f"{model_key} / {cell} (variant grid interrupted)"
                            break

                if stopped:
                    in_flight = None
                    save_progress(state)
                    break

                in_flight = None
                # Cell finished cleanly (baseline + variants): its per-rep
                # bookkeeping is no longer needed, done_cells carries the state.
                _clear_cell_done(state, model_key, cell)
                state.progress["models"].setdefault(model_key, {})
                state.progress["models"][model_key]["done_cells"] = sorted(done_cells)
                save_progress(state)
                msg = commit_cell(state, model_key)
                render_readme(state, model_key, do_commit=True, message=msg)
                tui.event("CELL", f"{len(done_cells)}/{len(cells_iter)} · {cell}")

            api.lms_unload(model_key)
            if stop_sig.event.is_set():
                break

        if stop_sig.event.is_set():
            api.lms_unload_all()
            stop_key_listener(sig_thread)
            _write_stopped(state, stop_reason or "safe-stop at boundary")
            log("SAFE-STOP: models unloaded; progress saved; nothing committed")
            tui.closing(stop_reason or "safe-stop at boundary")
            sys.exit(0)

        render_readme(state, None, do_commit=True)
        log("==== RUN COMPLETE ====")
        log(f"failures: {len(state.failures)}")
        with open(os.path.join(OUT, "FAILED.txt"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps(state.failures, indent=2))
        save_progress(state)
        stop_key_listener(sig_thread)
    except KeyboardInterrupt:
        log("interrupt received; saving progress without commit")
        partial = None
        if in_flight is not None:
            mk, cl = in_flight
            partial = {"model": mk, "cell": cl,
                       "reps_kept": len(_done_reps(state, mk, cl)),
                       "reps_total": state.progress.get("models", {})
                                    .get(mk, {}).get("reps_total", {}).get(cl)}
            log(f"partial cell kept: {mk}/{cl} "
                f"({partial['reps_kept']} reps on disk, resume continues here)")
        api.lms_unload_all()
        stop_key_listener(sig_thread)
        save_progress(state)
        _write_stopped(state, "keyboard-interrupt", partial=partial)
        log("progress saved to out/progress.json; nothing committed; models unloaded")
        sys.exit(130)


if __name__ == "__main__":
    run(parse_args())