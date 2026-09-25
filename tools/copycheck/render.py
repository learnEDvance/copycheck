import datetime
import json
import os
import subprocess

import corpus

_RAINBOW = ["#ff0044", "#ff6a00", "#ffb300", "#00cc66", "#00aaff", "#7a5cff", "#d500f9"]
_DIM = "#3a3a3a"
_GRAY = "#8a8a8a"
_TYPES = ["gibberish", "prose", "code", "list", "table", "data"]
_CELLS = corpus.CELL_KEYS()

_METRIC_GLOSSARY = """| Metric | Meaning |
|---|---|
| **CER** | Character Error Rate — the fraction of source characters that differ (insertions/deletions/substitutions) from the copied output. `0.00%` means a perfect, byte-for-byte copy; `100%` means nothing useful was produced. |
| **exact%** | The share of samples that matched the source character-for-character. |
| **samples** | Number of scored baseline samples for that cell/model. |
| **empty** | Samples where the model returned no text at all (HTTP 200 with zero content). |
| **reasoning-only** | Samples where the model only produced *reasoning* tokens (`reasoning_content`) and zero visible content — the transcript is saved, but the copy itself is a loss. |
| **len_ratio** | Length of extracted output ÷ length of source (1.00 = same length). |
| **OOM / skipped** | The model never ran because loading it would exceed free GPU memory (`lms load --estimate-only` preflight) or the load failed. Skipped models are listed in their own section. |"""

_METHODOLOGY = """1. **Models.** Every LLM discovered on the LM Studio server at run time is run automatically (`GET /api/v1/models`, embedding models excluded). Models are processed smallest-first. Parallel slots, repeats and skip rules come from `bench_models.json` (size-based defaults otherwise: ≤8 GB estimate → 8 slots, larger → 4, `gemma-3-270m-it` → 64 slots × 100 reps).
2. **Corpus.** A frozen, deterministic corpus of 18 cells (`out/corpus.json`): 6 content types (gibberish, prose, code, list, table, data) × 3 sizes (S ≈ 120, M ≈ 700, L ≈ 2000 chars).
3. **Baseline reps.** Each model produces `N` repetitions of each cell at temperature 0 with the single *baseline* system prompt. If 3 consecutive reps come back empty or reasoning-only, the remaining baseline reps for that cell are aborted (cell marked *no-content*).
4. **Prompt-variant grid.** For every cell, one extra temperature-0 sample is taken with each of 4 alternative system prompts: **echo-only**, **no-thinking**, **code-block**, **few-shot** — to see which prompt gets the best copy rate out of a given model.
5. **Scoring.** `metrics.py` computes Levenshtein CER, exact-match rate, LCS-conserved ratio and length ratio over the *extracted* output (markers/fences stripped).
6. **Artifacts.** Per-sample raw/extracted/diff/meta files live under `out/outputs/<model>/<cell>/` (baseline) and `out/prompts/<model>/<cell>/<variant>/` (variants); the full machine log is `out/requests.jsonl`; `out/progress.json` supports resume.
7. **Commits.** Every finished cell is committed and pushed automatically (`ccrun[<model>][<timestamp>][<n>]`), and this README is regenerated on each commit."""


def short_name(model_key: str) -> str:
    return model_key.rsplit("/", 1)[-1]


def dir_name(model_key: str) -> str:
    return model_key.replace("/", "__")


def cer_color(cer: float) -> str:
    if cer <= 0.02:
        return "#00cc66"
    if cer <= 0.10:
        return "#ffb300"
    return "#ff4d4d"


def er_color(pct: float) -> str:
    if pct <= 2.0:
        return "#00cc66"
    if pct <= 10.0:
        return "#ffb300"
    return "#ff4d4d"


def _span(text: str, color: str) -> str:
    return f'<span style="color:{color}">{text}</span>'


def _pad(text: str, vis: int, width: int) -> str:
    return text + " " * (width - vis)


def _fmt_duration(elapsed_sec: float) -> str:
    sec = max(0, int(round(elapsed_sec)))
    hh, rem = divmod(sec, 3600)
    mm, ss = divmod(rem, 60)
    return f"{hh:02d}:{mm:02d}:{ss:02d}"


def _wrap(text: str, width: int) -> list:
    out = []
    cur = ""
    for w in text.split(" "):
        nxt = w if not cur else cur + " " + w
        if len(nxt) <= width:
            cur = nxt
        else:
            if cur:
                out.append(cur)
            while len(w) > width:
                out.append(w[:width])
                w = w[width:]
            cur = w
    if cur:
        out.append(cur)
    return out


def _bar(cer: float) -> str:
    fill = int(round(14 * max(0.0, min(1.0, 1.0 - cer))))
    html = ""
    if fill > 0:
        html += _span("█" * fill, cer_color(cer))
    if 14 - fill > 0:
        html += _span("░" * (14 - fill), _DIM)
    return html


def _box(state: dict) -> str:
    name = state["name"]
    title = f"{name} live run · refresh: {state['refresh']}"
    w = max(72, len(title) + 4)
    c = w - 2
    label_w = 28
    value_w = c - label_w - 1
    model_w = 28
    cer_w = 10
    exact_w = 10
    bar_w = c - model_w - 1 - cer_w - 1 - exact_w - 1

    lines = []
    lines.append("┌─" + title + "─" * (c - len(title) - 1) + "┐")
    lines.append("├" + "─" * label_w + "┬" + "─" * value_w + "┤")

    def row2(label, val_text, vis):
        lab = " " + label
        if len(lab) > label_w:
            lab = lab[: label_w - 1] + "…"
        return "│" + lab.ljust(label_w) + "│" + _pad(val_text, vis, value_w) + "│"

    loaded = state["loaded_model"]
    lv = short_name(loaded) if loaded else "no model yet"
    if len(lv) > value_w - 1:
        lv = lv[: value_w - 2] + "…"
    lines.append(row2("loaded model", " " + lv, len(lv) + 1))

    sd = state["samples_done"]
    ts = state["total_samples"]
    ratio = (sd / ts) if ts else 0.0
    fill20 = int(round(20 * max(0.0, min(1.0, ratio))))
    prefix = f"{sd} / {ts}  "
    sbar = ""
    if fill20 > 0:
        sbar += _span("█" * fill20, _GRAY)
    if 20 - fill20 > 0:
        sbar += _span("░" * (20 - fill20), _DIM)
    lines.append(row2("samples done", " " + prefix + sbar, 1 + len(prefix) + 20))

    dur = _fmt_duration(state["elapsed_sec"])
    lines.append(row2("elapsed", " " + dur, 1 + len(dur)))

    upd = state["last_update"]
    if len(upd) > value_w - 1:
        upd = upd[: value_w - 2] + "…"
    lines.append(row2("last update", " " + upd, len(upd) + 1))

    cmt = f"#{state['commit_num']}"
    lines.append(row2("last commit", " " + cmt, len(cmt) + 1))

    ddiv = "├" + "─" * model_w + "┼" + "─" * cer_w + "┼" + "─" * exact_w + "┼" + "─" * bar_w + "┤"
    fdiv = "├" + "─" * c + "┤"
    lines.append(ddiv)

    def tbl_row(c0, c1, c2, bar_html, v0, v1, v2, v3):
        return (
            "│" + _pad(" " + c0, v0, model_w)
            + "│" + _pad(" " + c1, v1, cer_w)
            + "│" + _pad(" " + c2, v2, exact_w)
            + "│" + _pad(" " + bar_html, v3, bar_w)
            + "│"
        )

    lines.append(tbl_row("model", _span("CER", _DIM), _span("exact%", _DIM), _span("bar", _DIM), 6, 4, 7, 4))

    pm = state["per_model"]
    pending = []
    has_rows = False
    for key in state["models_order"]:
        d = pm.get(key)
        s = short_name(key)
        if d is None or d.get("samples", 0) == 0:
            pending.append(s)
            continue
        has_rows = True
        cer = d["cer"]
        pct = d["exact_pct"]
        cer_txt = f"{cer * 100:.2f}%"
        ex_txt = f"{pct:.2f}%"
        lines.append(tbl_row(
            s,
            _span(cer_txt, cer_color(cer)),
            _span(ex_txt, er_color(pct)),
            _bar(cer),
            len(s) + 1, len(cer_txt) + 1, len(ex_txt) + 1, 15,
        ))
    if not has_rows:
        note = "no model results yet"
        lines.append("│" + _pad(" " + _span(note, _DIM), len(note) + 1, c) + "│")
    if pending:
        note = "pending models: " + ", ".join(pending)
        lines.append("│" + _pad(" " + _span(note, _DIM), len(note) + 1, c) + "│")

    lines.append(fdiv)
    lines.append("│" + _pad(" " + _span("by type", _DIM), 9, c) + "│")
    lines.append(ddiv)
    lines.append(tbl_row("type", _span("CER", _DIM), _span("exact%", _DIM), _span("bar", _DIM), 6, 4, 7, 4))

    pt = state["per_type"]
    for t in _TYPES:
        d = pt.get(t)
        if d is not None and d.get("samples", 0) > 0:
            cer = d["cer"]
            pct = d["exact_pct"]
            cer_txt = f"{cer * 100:.2f}%"
            ex_txt = f"{pct:.2f}%"
            lines.append(tbl_row(
                t,
                _span(cer_txt, cer_color(cer)),
                _span(ex_txt, er_color(pct)),
                _bar(cer),
                len(t) + 1, len(cer_txt) + 1, len(ex_txt) + 1, 15,
            ))
        else:
            lines.append(tbl_row(
                t,
                _span("—", _DIM),
                _span("—", _DIM),
                _span("░" * 14, _DIM),
                len(t) + 1, 2, 2, 15,
            ))

    lines.append(fdiv)
    concl = state["conclusion"]
    if len(concl) > 150:
        concl = concl[:150] + "…"
    for ln in _wrap(concl, c - 2):
        lines.append("│" + _pad(" " + ln, len(ln) + 1, c) + "│")
    lines.append("└" + "─" * c + "┘")

    return "<pre>\n" + "\n".join(lines) + "\n</pre>"


def _machine(state: dict) -> str:
    m = []
    loaded = state["loaded_model"]
    m.append("run_id = " + str(state.get("run_id") or "n/a"))
    m.append("loaded_model = " + (short_name(loaded) if loaded else "None"))
    m.append(f"samples_done = {state['samples_done']}")
    m.append(f"total_samples = {state['total_samples']}")
    m.append(f"elapsed_sec = {state['elapsed_sec']}")
    m.append(f"last_update = {state['last_update']}")
    m.append(f"commit_num = {state['commit_num']}")
    for key in state["models_order"]:
        d = state["per_model"].get(key)
        if d is None:
            cer, pct, n = 0.0, 0.0, 0
        else:
            cer, pct, n = d["cer"], d["exact_pct"], d["samples"]
        s = short_name(key)
        m.append(f"model.{s}.cer = {cer}")
        m.append(f"model.{s}.exact_pct = {pct}")
        m.append(f"model.{s}.samples = {n}")
    for sk in state.get("skipped", []):
        m.append(f"skipped.{sk['model']} = {sk.get('reason')}: {sk.get('detail', '')}")
    return "\n".join(m)


def _md_header(state: dict) -> str:
    h1 = '<h1 align="center">' + "".join(
        f'<span style="color:{_RAINBOW[i % len(_RAINBOW)]}">{ch}</span>'
        for i, ch in enumerate(state["name"])
    ) + "</h1>"
    return h1


def _ranking_table(state: dict) -> str:
    rows = ["| Model | CER | exact% | samples | empty | reasoning-only | avg wall (s) |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for key in state["models_order"]:
        d = state["per_model"].get(key)
        if d is None or d.get("samples", 0) == 0:
            continue
        rows.append(
            f"| {short_name(key)} | "
            f"{_span(f'{d['cer']*100:.2f}%', cer_color(d['cer']))} | "
            f"{_span(f'{d['exact_pct']:.2f}%', er_color(d['exact_pct']))} | "
            f"{d['samples']} | {d.get('empty', 0)} | {d.get('reasoning_only', 0)} | "
            f"{d.get('wall_avg_s', 0)} |"
        )
    return "\n".join(rows) if len(rows) > 2 else "_No baseline samples yet._"


def _type_table(state: dict) -> str:
    rows = ["| Type | CER | exact% | samples |",
            "| --- | ---: | ---: | ---: |"]
    for t in _TYPES:
        d = state["per_type"].get(t)
        if d is not None and d.get("samples", 0) > 0:
            rows.append(
                f"| {t} | {_span(f'{d['cer']*100:.2f}%', cer_color(d['cer']))} | "
                f"{_span(f'{d['exact_pct']:.2f}%', er_color(d['exact_pct']))} | {d['samples']} |"
            )
        else:
            rows.append(f"| {t} | _pending_ | _pending_ | 0 |")
    return "\n".join(rows)


def _matrix_table(state: dict) -> str:
    """CER% per model (rows) x 18 cells (columns)."""
    hdr = (["| Model \\ Cell |"]
       + [f" {c.split('-')[0][:3]}·{c.split('-')[1]} " for c in _CELLS]
       + ["|"])
    sep = ["| --- |"] + [" ---: |"] * len(_CELLS)
    rows = [hdr, sep]
    for key in state["models_order"]:
        cells = state["per_cell"].get(key, {})
        line = [f"| **{short_name(key)}** |"]
        for c in _CELLS:
            d = cells.get(c)
            if d and d.get("samples", 0) > 0:
                line.append(f" {_span(f'{d['cer']*100:.0f}%', cer_color(d['cer']))} |")
            else:
                line.append(" · |")
        rows.append(line)
    return "\n".join("".join(r) for r in rows)


def _per_model_detail(state: dict) -> str:
    parts = ["## Per-model breakdown (18 cells)", ""]
    for key in state["models_order"]:
        cells = state["per_cell"].get(key, {})
        if not cells:
            continue
        parts.append(f"### {short_name(key)}")
        parts.append("")
        parts.append("| Cell | CER | exact% | samples | empty | reasoning-only | avg wall (s) |")
        parts.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
        for c in _CELLS:
            d = cells.get(c)
            if not d or d.get("samples", 0) == 0:
                parts.append(f"| {c} | _pending_ | _pending_ | 0 | 0 | 0 | 0 |")
                continue
            parts.append(
                f"| {c} | {_span(f'{d['cer']*100:.2f}%', cer_color(d['cer']))} | "
                f"{_span(f'{d['exact_pct']:.2f}%', er_color(d['exact_pct']))} | "
                f"{d['samples']} | {d.get('empty', 0)} | {d.get('reasoning_only', 0)} | "
                f"{d.get('wall_avg_s', 0)} |"
            )
        parts.append("")
    return "\n".join(parts)


def _variant_table(state: dict) -> str:
    rows = ["| Model | Variant | CER | exact% | samples |",
            "| --- | --- | ---: | ---: | ---: |"]
    for key in state["models_order"]:
        vm = state["per_variant"].get(key, {})
        if not vm:
            continue
        for variant in sorted(vm.keys()):
            d = vm[variant]
            rows.append(
                f"| {short_name(key)} | {variant} | "
                f"{_span(f'{d['cer']*100:.2f}%', cer_color(d['cer']))} | "
                f"{d['exact_pct']:.2f}% | {d['samples']} |"
            )
    return "\n".join(rows) if len(rows) > 2 else "_No variant samples yet._"


def _skipped_table(state: dict) -> str:
    sk = state.get("skipped", [])
    if not sk:
        return "_No models skipped._"
    rows = ["| Model | Reason | Detail |", "| --- | --- | --- |"]
    for s in sk:
        rows.append(f"| {s.get('model')} | {s.get('reason', '')} | {s.get('detail', '')} |")
    return "\n".join(rows)


def build_readme(state: dict) -> str:
    run_id = state.get("run_id")
    meta = f"**Run ID:** `{run_id}`  ·  **Last update:** {state['last_update']}  ·  **Elapsed:** {_fmt_duration(state['elapsed_sec'])}  ·  **Commits:** {state['commit_num']}"

    parts = [
        _md_header(state),
        "",
        "> **⚠️ AI-generated content.** This README and all of the data below are produced automatically by the "
        "CopyCheck harness, which runs **local LM Studio models** as the subjects. Any long-form analysis text you see here is "
        "generated by a model — treat prose as a summary, not ground truth.",
        "",
        "## What is this?",
        "",
        "**" + state["aim"] + "**",
        "",
        state["why"],
        "",
        "A *copy-fidelity* benchmark. Each model is shown 18 fixed texts — 6 content types "
        "(gibberish, prose, code, list, table, data) × 3 sizes (small ≈ 120 chars, medium ≈ 700, large ≈ 2000) — wrapped "
        "between `COPYSTART` / `COPYEND` markers, and asked to reproduce the text **exactly**. The output is extracted "
        "(markers and code fences stripped) and scored against the source. Lower CER = more faithful copying, which is "
        "the raw skill underneath reliable text editing.",
        "",
        "## Live status",
        "",
        _box(state),
        "",
        meta,
        "",
        "## How to read the results",
        "",
        _METRIC_GLOSSARY,
        "",
        "## Methodology",
        "",
        _METHODOLOGY,
        "",
        "## Ranking",
        "",
        _ranking_table(state),
        "",
        "## Content-type summary",
        "",
        _type_table(state),
        "",
        "## CER matrix (model × cell)",
        "",
        _matrix_table(state),
        "",
        _per_model_detail(state),
        "",
        "## Prompt-variant comparison",
        "",
        "One temperature-0 sample per variant per cell, to see which system prompt gets the best copy rate out of each model.",
        "",
        _variant_table(state),
        "",
        "## Skipped / failed models",
        "",
        _skipped_table(state),
        "",
        "## Conclusion",
        "",
        state["conclusion"],
        "",
        "<details>",
        "<summary><b>Machine-readable status</b></summary>",
        "",
        "```",
        _machine(state),
        "```",
        "",
        "</details>",
        "",
    ]
    return "\n".join(parts)


def commit_and_push(repo_root: str, message: str):
    def run(args):
        return subprocess.run(args, cwd=repo_root, capture_output=True, text=True)

    try:
        run(["git", "add", "-A"])
        st = run(["git", "status", "--porcelain"])
        if not st.stdout.strip():
            return (False, "no changes")
        cm = run(["git", "commit", "-m", message])
        if cm.returncode != 0:
            idc = run([
                "git", "-c", "user.name=copycheck-bot",
                "-c", "user.email=copycheck-bot@local",
                "commit", "-m", message,
            ])
            if idc.returncode == 0:
                cm = idc
        ps = run(["git", "push"])
        out = (cm.stdout or "") + (cm.stderr or "") + (ps.stdout or "") + (ps.stderr or "")
        ok = cm.returncode == 0 and ps.returncode == 0
        return (ok, out)
    except Exception as exc:
        return (False, str(exc))


def _strip_tags(s: str) -> str:
    out = []
    in_tag = False
    for ch in s:
        if ch == "<":
            in_tag = True
        elif ch == ">":
            in_tag = False
        elif not in_tag:
            out.append(ch)
    return "".join(out)


if __name__ == "__main__":
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state = {
        "name": "CopyCheck",
        "aim": "measure how accurately local LLMs copy input text verbatim",
        "why": "verbatim copying is the sharpest probe of a model's raw reliability",
        "loaded_model": "google/gemma-4-e4b",
        "run_id": "demo-20260925",
        "models_order": [
            "google/gemma-4-e4b",
            "prism-ml/bonsai-27b",
        ],
        "samples_done": 500,
        "total_samples": 600,
        "elapsed_sec": 6543.21,
        "last_update": now,
        "commit_num": 152,
        "per_model": {
            "google/gemma-4-e4b": {"cer": 0.0042, "exact_pct": 0.8, "samples": 180,
                                   "empty": 0, "reasoning_only": 0, "wall_avg_s": 1.2},
            "prism-ml/bonsai-27b": {"cer": 0.34, "exact_pct": 34.5, "samples": 155,
                                    "empty": 3, "reasoning_only": 2, "wall_avg_s": 8.9},
        },
        "per_type": {
            "gibberish": {"cer": 0.012, "exact_pct": 1.1, "samples": 80},
            "prose": {"cer": 0.02, "exact_pct": 1.9, "samples": 120},
            "code": {"cer": 0.11, "exact_pct": 11.8, "samples": 100},
            "list": {"cer": 0.045, "exact_pct": 4.4, "samples": 70},
            "table": {"cer": 0.07, "exact_pct": 7.2, "samples": 70},
            "data": {"cer": 0.025, "exact_pct": 2.6, "samples": 60},
        },
        "per_cell": {
            "google/gemma-4-e4b": {c: {"cer": 0.005, "exact_pct": 1.0, "samples": 10,
                                       "empty": 0, "reasoning_only": 0, "wall_avg_s": 1.0}
                                   for c in _CELLS},
            "prism-ml/bonsai-27b": {c: {"cer": 0.35, "exact_pct": 30.0, "samples": 9,
                                        "empty": 1, "reasoning_only": 1, "wall_avg_s": 9.0}
                                    for c in _CELLS},
        },
        "per_variant": {
            "google/gemma-4-e4b": {
                "echo": {"cer": 0.004, "exact_pct": 1.0, "samples": 18},
                "few-shot": {"cer": 0.003, "exact_pct": 1.5, "samples": 18},
                "no-thinking": {"cer": 0.004, "exact_pct": 1.0, "samples": 18},
                "codeblock": {"cer": 0.006, "exact_pct": 0.5, "samples": 18},
            },
            "prism-ml/bonsai-27b": {
                "echo": {"cer": 0.30, "exact_pct": 33.0, "samples": 18},
                "few-shot": {"cer": 0.29, "exact_pct": 34.0, "samples": 18},
                "no-thinking": {"cer": 0.35, "exact_pct": 30.0, "samples": 18},
                "codeblock": {"cer": 0.28, "exact_pct": 36.0, "samples": 18},
            },
        },
        "skipped": [
            {"model": "google/gemma-4-12b", "reason": "oom",
             "detail": "needs ~14.90GB, free ~8.10GB"},
        ],
        "conclusion": (
            "gemma-4-e4b is copying input text verbatim with essentially perfect fidelity across all six"
            " content types; bonsai-27b degrades sharply once source text exceeds a few hundred characters and"
            " is not yet viable for verbatim tasks. See the per-model breakdown and prompt-variant grid below."
        ),
        "refresh": "every cell commit",
    }

    readme = build_readme(state)
    dest = os.path.join("/tmp", "render_demo.md")
    with open(dest, "w", encoding="utf-8") as fh:
        fh.write(readme)

    print("wrote:", dest)
    print(readme[:1200])