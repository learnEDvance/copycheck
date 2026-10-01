"""Command-center themed live console output for CopyCheck (safe-stop aware)."""

import shutil
import sys


def _tty():
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


def _c(s, code):
    return f"\x1b[{code}m{s}\x1b[0m" if _tty() else s


def cyan(s): return _c(s, "96")
def green(s): return _c(s, "92")
def yellow(s): return _c(s, "93")
def magenta(s): return _c(s, "95")
def red(s): return _c(s, "91")
def dim(s): return _c(s, "90")
def bold(s): return _c(s, "1")


def w():
    try:
        return max(80, shutil.get_terminal_size((100, 24)).columns - 2)
    except Exception:
        return 100


def header(summary):
    h = w()
    title = "◢◤ COPYCHECK ▸ VERBATIM COMMAND CENTER ◥◣"
    print(magenta("┏" + "━" * h + "┓"))
    pad = max(0, (h - len(title)) // 2)
    print(magenta("┃") + " " * pad + bold(cyan(title))
          + " " * (h - pad - len(title)) + magenta("┃"))
    print(magenta("┃") + " " + dim(summary)
          + " " * max(0, h - len(summary) - 1) + magenta("┃"))
    print(magenta("┃") + yellow(" SPACE = SAFE-STOP ") + cyan("·")
          + yellow(" CTRL-C = EMERGENCY ") + cyan("·") + yellow(" RESUME-SAFE ")
          + cyan("·") + yellow(" AUTO-COMMIT PER CELL ") + magenta("┃"))
    print(magenta("┗" + "━" * h + "┛"), flush=True)


def model_card(model_key, t):
    h = w() - 1
    slots = t.get("max_par", t.get("slots", 1))
    reps = t.get("repeats", 20)
    ctx = t.get("ctx") or "-"
    gpu = t.get("gpu")
    gpu = gpu if gpu is not None else "-"
    line = f" DOORS OPEN >> {model_key}  [{slots}x parallel · ctx {ctx} · gpu {gpu} · {reps} reps ]"
    print("", flush=True)
    print(magenta("┏" + "━" * (h + 1) + "┓"))
    print(magenta("┃") + " " + cyan(line)
          + " " * max(0, h - len(line)) + magenta("┃"))
    print(magenta("┗" + "━" * (h + 1) + "┛"), flush=True)


def cell_scan(n, total, cer, tps):
    if not _tty():
        return
    pct = n / max(1, total)
    bw = 24
    filled = int(pct * bw)
    bar = magenta("│") + green("█" * filled) + dim("░" * (bw - filled)) + magenta("│")
    tt = f"{tps:6.1f}" if isinstance(tps, (int, float)) else "   n/a"
    cers = "n/a" if cer is None else f"{cer:.4f}"
    txt = f" baseline {n:>4}/{total:<4}  {bar}  cer={cers}  {tt} tok/s"
    print("\r  " + txt + " " * max(0, w() - len(txt) - 2), end="", flush=True)


def scan_done():
    if _tty():
        print("", flush=True)


def event(tag, text, color=None):
    color = color or dim
    print(f"{green('▸')} {cyan(tag):<9} {color(text)}", flush=True)


def closing(reason):
    h = w()
    print("", flush=True)
    print(magenta("┏" + "━" * h + "┓"))
    print(magenta("┃") + green(" ✓ SAFE-STOP ENGAGED ")
          + dim("· models unloaded · progress saved · no commit") + magenta("┃"))
    print(magenta("┃") + dim(f"   {reason}") + magenta("┃"))
    print(magenta("┃") + dim("   resume anytime:  python3 run.py   (in-flight cell redone)")
          + magenta("┃"))
    print(magenta("┗" + "━" * h + "┛"), flush=True)