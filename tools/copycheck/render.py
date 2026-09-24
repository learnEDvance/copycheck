import datetime
import json
import os
import subprocess

_RAINBOW = ["#ff0044", "#ff6a00", "#ffb300", "#00cc66", "#00aaff", "#7a5cff", "#d500f9"]
_DIM = "#3a3a3a"
_GRAY = "#8a8a8a"
_TYPES = ["gibberish", "prose", "code", "list", "table", "data"]


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
    return "\n".join(m)


def build_readme(state: dict) -> str:
    h1 = '<h1 align="center">' + "".join(
        f'<span style="color:{_RAINBOW[i % len(_RAINBOW)]}">{ch}</span>'
        for i, ch in enumerate(state["name"])
    ) + "</h1>"

    parts = [
        h1,
        "",
        f"**Aim:** {state['aim']}",
        "",
        f"**Why:** {state['why']}",
        "",
        "## Live Status",
        "",
        _box(state),
        "",
        "## Status (machine-readable)",
        "",
        "```",
        _machine(state),
        "```",
        "",
        "## Conclusion",
        "",
        state["conclusion"],
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
        "models_order": [
            "google/gemma-4-e4b",
            "anthropic/claude-hybrid",
            "meta-llama/llama-4b",
            "xai/grok-mini",
        ],
        "samples_done": 500,
        "total_samples": 600,
        "elapsed_sec": 6543.21,
        "last_update": now,
        "commit_num": 152,
        "per_model": {
            "google/gemma-4-e4b": {"cer": 0.0042, "exact_pct": 0.8, "samples": 180},
            "anthropic/claude-hybrid": {"cer": 0.0615, "exact_pct": 5.6, "samples": 165},
            "meta-llama/llama-4b": {"cer": 0.34, "exact_pct": 34.5, "samples": 155},
        },
        "per_type": {
            "gibberish": {"cer": 0.012, "exact_pct": 1.1, "samples": 80},
            "prose": {"cer": 0.02, "exact_pct": 1.9, "samples": 120},
            "code": {"cer": 0.11, "exact_pct": 11.8, "samples": 100},
            "list": {"cer": 0.045, "exact_pct": 4.4, "samples": 70},
            "table": {"cer": 0.07, "exact_pct": 7.2, "samples": 70},
            "data": {"cer": 0.025, "exact_pct": 2.6, "samples": 60},
        },
        "conclusion": (
            "gemma-4-e4b is copying input text verbatim with essentially perfect fidelity across all six"
            " content types; claude-hybrid remains solid but drops punctuation and reorders list items"
            " occasionally; llama-4b degrades sharply once source text exceeds a few hundred characters and"
            " is not yet viable for verbatim tasks. Next up: finish the remaining 100 samples, then promote"
            " the checklist table type to a stress test."
        ),
        "refresh": "every commit (~72 commits / run)",
    }

    readme = build_readme(state)
    dest = os.path.join("/tmp", "render_demo.md")
    with open(dest, "w", encoding="utf-8") as fh:
        fh.write(readme)

    for ln in readme.splitlines()[:40]:
        print(ln)

    pre_body = readme.split("<pre>", 1)[1].split("</pre>", 1)[0]
    box_lines = pre_body.splitlines()
    widths = [len(_strip_tags(l)) for l in box_lines]
    print("[demo] wrote:", dest)
    print("[demo] pre box lines:", len(box_lines), "max visible width:", max(widths))
    print("[demo] models:", json.dumps([short_name(m) for m in state["models_order"]]))
    print("[demo] exports: short_name, dir_name, cer_color, er_color, build_readme, commit_and_push")