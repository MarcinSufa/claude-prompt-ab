#!/usr/bin/env python3
"""
A/B harness for system prompts.

One model call per iteration. The baseline is measured once and cached, because
it does not change while you edit a variant.

Usage:
    python ab.py run v1                 # run v1 against the cached baseline
    python ab.py run v1 --repeat 3      # 3 iterations, for a final verdict
    python ab.py run v1 --force-base    # discard the cached baseline, remeasure
    python ab.py report                 # rebuild report.md and report.html
    python ab.py diff v1 v2             # table for the named arms only

Workdir layout:
    task.md            user-prompt template, {ARTICLE} is replaced with the input
    article.md         the input text
    variants/v1.md     appended via --append-system-prompt-file
    runs/<arm>/        raw results: NNN.json + NNN.md
    report.md          table plus every answer as source
    report.html        the same, rendered side by side
"""

import argparse
import getpass
import os
import json
import re
import shlex
import statistics
import subprocess
import tempfile
import sys
import time
from pathlib import Path

# --workdir sets the working directory, defaulting to ./prompt-ab, so a single
# installed script serves every repository.
ROOT = Path.cwd() / "prompt-ab"
VARIANTS = ROOT / "variants"
RUNS = ROOT / "runs"
TEMPLATES = Path(__file__).resolve().parent.parent / "templates"


def set_workdir(path: Path) -> None:
    """Point at a workdir, creating the skeleton when it does not exist yet."""
    global ROOT, VARIANTS, RUNS
    ROOT = path.resolve()
    VARIANTS = ROOT / "variants"
    RUNS = ROOT / "runs"
    VARIANTS.mkdir(parents=True, exist_ok=True)
    RUNS.mkdir(parents=True, exist_ok=True)
    if not (ROOT / "task.md").exists() and (TEMPLATES / "task.md").exists():
        (ROOT / "task.md").write_text(
            read(TEMPLATES / "task.md"), encoding="utf-8")

# Reference arms:
#   vanilla  = bare agent, no CLAUDE.md at all
#   baseline = your CURRENT setup (global + project CLAUDE.md), nothing appended
#   <variant>= that same setup plus variants/<name>.md via --append-system-prompt-file
VANILLA = "vanilla"
BASELINE = "baseline"
# Only the baseline is measured automatically. Vanilla is a diagnostic rather than
# a candidate you would adopt, so run it explicitly with `run vanilla` when needed.
REFERENCE_ARMS = (BASELINE,)

# Runs execute outside the project tree so no CLAUDE.md sneaks in through the cwd.
# The directory is namespaced per user: on a shared Linux box a bare
# /tmp/prompt-ab-cwd owned by someone else fails with a permission error.
NEUTRAL_CWD = Path(tempfile.gettempdir()) / f"prompt-ab-cwd-{getpass.getuser()}"

# Tic phrases. A hit means the system prompt failed to suppress them.
TICKS = [
    "load-bearing",
    "loadbearing",
    "worth stating plainly",
    "here's the honest truth",
    "the real tension",
    "carry the argument",
    "you're absolutely right",
    "you are absolutely right",
    "great question",
]

# A quotation is not a tic. A model writing ABOUT a banned phrase, in backticks or
# in quotes, is not using it. Without this the counter measures the input's topic
# instead of the answer's style.
QUOTED = re.compile(r"`[^`]*`|\"[^\"]*\"|'[^']{3,}'", re.S)


def claude_argv() -> list[str]:
    r"""The command that starts the CLI, as argv.

    CLAUDE_BIN may hold a bare name, a path, or a full command with arguments
    ("/usr/bin/python3 /opt/stub.py", "npx claude"). It is split with shlex in
    Windows mode on Windows so a path like C:\tools\claude.exe survives, and in
    POSIX mode elsewhere. Pointing at an explicit binary is the only reliable way
    to redirect the CLI on Windows: subprocess resolves a bare name through the
    PARENT process PATH, not through the environment handed to the child, so
    prepending a directory to the child PATH changes nothing.
    """
    raw = os.environ.get("CLAUDE_BIN", "").strip()
    if not raw:
        return ["claude"]
    return shlex.split(raw, posix=(os.name != "nt"))


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def build_prompt() -> str:
    task = read(ROOT / "task.md")
    article = read(ROOT / "article.md")
    if "{ARTICLE}" not in task:
        raise SystemExit("task.md must contain the {ARTICLE} placeholder")
    return task.replace("{ARTICLE}", article)


def metrics(text: str) -> dict:
    unquoted = QUOTED.sub(" ", text).lower()
    return {
        "words": len(text.split()),
        "chars": len(text),
        # Rough token estimate for the VISIBLE text (~4 chars per token). The gap to
        # out_tok is reasoning, which the API does not report separately.
        "text_tok": round(len(text) / 4),
        "em_dash": text.count("—"),
        "ticks": sum(unquoted.count(t) for t in TICKS),
        "tick_list": sorted({t for t in TICKS if t in unquoted}),
        "headings": len(re.findall(r"^#{1,6} ", text, re.M)),
        "bullets": len(re.findall(r"^\s*[-*] ", text, re.M)),
        "refcodes": len(set(re.findall(r"\b[DOFRQA]\d{1,2}\b", text))),
    }


def invoke(prompt: str, sp_file: Path | None, model: str, timeout: int,
           clean: bool = False, effort: str = "medium", skills: bool = False) -> dict:
    cmd = [
        *claude_argv(), "-p",
        "--model", model,
        "--output-format", "json",
        # MCP is always off: otherwise you measure server instructions, not your prompt.
        "--strict-mcp-config",
        # Effort is pinned EXPLICITLY on every arm. Without it, an arm that drops user
        # settings runs at the default level while another inherits a higher one, and
        # most of the out_tok difference would be reasoning rather than style.
        "--effort", effort,
    ]
    if not skills:
        # Skills are OFF by default so the prompt is isolated. Verified by probe: with
        # this flag the agent answers "No" when asked whether a given skill is
        # available, and "Yes" without it. Pass --with-skills to measure the
        # conditions you actually work in, where prompt rules compete with skills.
        cmd.append("--disable-slash-commands")
    if clean:
        # Cuts both the global and the project CLAUDE.md. Verified by probe: without
        # this flag the agent sees both, with it, neither.
        cmd += ["--setting-sources", "project"]
    if sp_file is not None:
        cmd += ["--append-system-prompt-file", str(sp_file)]

    NEUTRAL_CWD.mkdir(parents=True, exist_ok=True)
    started = time.time()
    proc = subprocess.run(
        cmd, input=prompt, capture_output=True, cwd=str(NEUTRAL_CWD),
        text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )
    wall = time.time() - started

    if proc.returncode != 0:
        raise SystemExit(
            f"claude exited with code {proc.returncode}\n"
            f"stderr: {proc.stderr[:2000]}"
        )

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise SystemExit(f"stdout was not JSON:\n{proc.stdout[:2000]}")

    text = payload.get("result", "")
    usage = payload.get("usage", {}) or {}
    return {
        "text": text,
        "wall_s": round(wall, 1),
        "duration_s": round(payload.get("duration_ms", 0) / 1000, 1),
        "cost_usd": payload.get("total_cost_usd"),
        "in_tok": usage.get("input_tokens"),
        "out_tok": usage.get("output_tokens"),
        "cache_read": usage.get("cache_read_input_tokens"),
        # The first run of a batch WRITES the cache, later runs read it. Without this
        # field the input looked like 2 tokens, because the article went to cache.
        "cache_write": usage.get("cache_creation_input_tokens"),
        "metrics": metrics(text),
    }


def variant_dir(name: str) -> Path:
    d = RUNS / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_runs(name: str) -> list[dict]:
    """Metrics are recomputed from the stored text, never read back from the JSON,
    so redefining a metric reapplies to every historical run for free."""
    d = RUNS / name
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.json")):
        run = json.loads(read(p))
        run["metrics"] = metrics(run.get("text", ""))
        out.append(run)
    return out


def save_run(name: str, run: dict) -> Path:
    d = variant_dir(name)
    idx = len(list(d.glob("*.json"))) + 1
    (d / f"{idx:03d}.json").write_text(
        json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (d / f"{idx:03d}.md").write_text(run["text"], encoding="utf-8")
    return d / f"{idx:03d}.md"


def med(runs: list[dict], path: str):
    vals = []
    for r in runs:
        cur = r
        for key in path.split("."):
            cur = (cur or {}).get(key) if isinstance(cur, dict) else None
        if isinstance(cur, (int, float)):
            vals.append(cur)
    if not vals:
        return None
    return round(statistics.median(vals), 2)


def fmt(v, suffix=""):
    return "-" if v is None else f"{v:g}{suffix}"


def spread(runs: list[dict], path: str) -> str:
    """Min..max within one arm. This is the noise floor: any difference between
    variants smaller than this spread is indistinguishable from randomness."""
    vals = []
    for r in runs:
        cur = r
        for key in path.split("."):
            cur = (cur or {}).get(key) if isinstance(cur, dict) else None
        if isinstance(cur, (int, float)):
            vals.append(cur)
    if len(vals) < 2:
        return "n=1"
    return f"{min(vals):g}..{max(vals):g}"


def table(names: list[str]) -> str:
    # Delta is measured against the baseline, because the decision is whether an
    # appended prompt beats the setup you already have.
    ref_out = med(load_runs(BASELINE), "out_tok")

    ref_text = med(load_runs(BASELINE), "metrics.text_tok")

    head = (
        "| arm | n | text tok | spread | vs baseline | thinking | em dash | tics | ref codes | time |\n"
        "|---|---:|---:|:---:|---:|---:|---:|---:|---:|---:|\n"
    )
    rows = []
    for n in names:
        runs = load_runs(n)
        if not runs:
            continue
        out = med(runs, "out_tok")
        txt = med(runs, "metrics.text_tok")
        # Delta uses VISIBLE text. Against out_tok it would mix style with reasoning.
        if ref_text and txt and n != BASELINE:
            delta = f"{(txt - ref_text) / ref_text * 100:+.0f}%"
        else:
            delta = "-"
        think = fmt(round(out - txt)) if (out and txt) else "-"
        rows.append(
            f"| {n} | {len(runs)} | {fmt(txt)} | {spread(runs, 'metrics.text_tok')} | "
            f"{delta} | {think} | "
            f"{fmt(med(runs, 'metrics.em_dash'))} | {fmt(med(runs, 'metrics.ticks'))} | "
            f"{fmt(med(runs, 'metrics.refcodes'))} | {fmt(med(runs, 'duration_s'), 's')} |"
        )
    return head + "\n".join(rows) + "\n"


def all_variants() -> list[str]:
    if not RUNS.exists():
        return []
    names = [a for a in REFERENCE_ARMS if (RUNS / a).exists()]
    names += sorted(
        d.name for d in RUNS.iterdir()
        if d.is_dir() and d.name not in REFERENCE_ARMS
    )
    return names


def write_report(names: list[str] | None = None) -> Path:
    names = names or all_variants()
    out = ["# System prompt A/B\n", table(names), "\n## Representative answer per arm\n"]
    for n in names:
        runs = load_runs(n)
        if not runs:
            continue
        m = runs[-1]["metrics"]
        hit = ", ".join(m["tick_list"]) or "none"
        out.append(f"\n### {n}\n")
        out.append(f"_tics hit: {hit}_\n")
        out.append("\n```md\n" + runs[-1]["text"].strip() + "\n```\n")
    p = ROOT / "report.md"
    p.write_text("\n".join(out), encoding="utf-8")
    write_html(names)   # ta sama tresc, ale odpowiedzi wyrenderowane obok siebie
    return p


def one(name: str, prompt: str, sp: Path | None, args, clean: bool, i: int, total: int):
    r = invoke(prompt, sp, args.model, args.timeout, clean=clean,
               effort=args.effort, skills=args.with_skills)
    path = save_run(name, r)
    m = r["metrics"]
    print(f"  {name} {i+1}/{total}: text {m['text_tok']} tok, out {r['out_tok']} tok, "
          f"{r['duration_s']}s, em dash {m['em_dash']} -> {path.name}", flush=True)


def cmd_run(args):
    prompt = build_prompt()

    # Reference arms are measured once and cached, so editing a variant costs
    # exactly args.repeat model calls.
    for arm in REFERENCE_ARMS:
        if load_runs(arm) and not args.force_base:
            print(f"{arm}: cached ({len(load_runs(arm))} runs), skipping")
            continue
        if args.force_base and (RUNS / arm).exists():
            for f in (RUNS / arm).glob("*"):
                f.unlink()
        # The baseline gets more repeats than the variants: it sets the noise floor
        # that single-shot variant readings are judged against.
        n = args.ref_repeat
        print(f"{arm}: measuring {n}x (noise floor) ...", flush=True)
        for i in range(n):
            one(arm, prompt, None, args, clean=(arm == VANILLA), i=i, total=n)

    if args.variant in REFERENCE_ARMS:
        print()
        print(table(list(REFERENCE_ARMS)))
        print(f"full answers: {write_report()}")
        return

    sp = VARIANTS / f"{args.variant}.md"
    if not sp.exists():
        raise SystemExit(f"missing {sp}")

    print(f"{args.variant}: measuring {args.repeat}x ...", flush=True)
    for i in range(args.repeat):
        one(args.variant, prompt, sp, args, clean=False, i=i, total=args.repeat)

    print()
    print(table([*REFERENCE_ARMS, args.variant]))
    print(f"full answers: {write_report()}")


def cmd_report(args):
    print(f"written: {write_report(args.variants or None)}")


def cmd_diff(args):
    print(table(args.variants))


def main():
    ap = argparse.ArgumentParser(description="A/B harness for system prompts")
    ap.add_argument("--workdir", type=Path, default=None,
                    help="working directory (default ./prompt-ab)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run a variant against the cached baseline")
    r.add_argument("variant")
    r.add_argument("--repeat", type=int, default=1)
    r.add_argument("--ref-repeat", type=int, default=3,
                   help="baseline repeats; this sets the noise floor")
    r.add_argument("--model", default="opus")
    r.add_argument("--effort", default="medium",
                   choices=["low", "medium", "high", "xhigh", "max"],
                   help="same level on every arm; otherwise you measure reasoning, not the prompt")
    r.add_argument("--timeout", type=int, default=600)
    r.add_argument("--with-skills", action="store_true",
                   help="leave skills enabled; measures real working conditions, not the prompt alone")
    r.add_argument("--force-base", action="store_true")
    r.set_defaults(func=cmd_run)

    p = sub.add_parser("report", help="rebuild report.md and report.html")
    p.add_argument("variants", nargs="*")
    p.set_defaults(func=cmd_report)

    d = sub.add_parser("diff", help="table for the named arms only")
    d.add_argument("variants", nargs="+")
    d.set_defaults(func=cmd_diff)

    args = ap.parse_args()
    set_workdir(args.workdir or (Path.cwd() / 'prompt-ab'))
    args.func(args)




# --- HTML rendering: same data, answers shown as formatted text ---

_INLINE = [
    (re.compile(r"`([^`]+)`"), r"<code>\1</code>"),
    (re.compile(r"\*\*([^*]+)\*\*"), r"<strong>\1</strong>"),
    (re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)"), r"<em>\1</em>"),
]


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def md_to_html(md: str) -> str:
    """Minimal renderer: headings, lists, bold, italics, code, paragraphs. Enough for
    model answers, and it pulls in no dependency."""
    out, list_tag = [], None

    def close_list():
        nonlocal list_tag
        if list_tag:
            out.append(f"</{list_tag}>")
            list_tag = None

    for raw in md.splitlines():
        line = _esc(raw.rstrip())
        for pat, rep in _INLINE:
            line = pat.sub(rep, line)

        if not line.strip():
            close_list()
            continue

        h = re.match(r"^(#{1,6})\s+(.*)$", line)
        if h:
            close_list()
            lvl = len(h.group(1))
            out.append(f"<h{lvl}>{h.group(2)}</h{lvl}>")
            continue

        ul = re.match(r"^\s*[-*]\s+(.*)$", line)
        ol = re.match(r"^\s*\d+[.)]\s+(.*)$", line)
        if ul or ol:
            want = "ul" if ul else "ol"
            if list_tag != want:
                close_list()
                out.append(f"<{want}>")
                list_tag = want
            out.append(f"<li>{(ul or ol).group(1)}</li>")
            continue

        close_list()
        out.append(f"<p>{line}</p>")

    close_list()
    return "\n".join(out)


CSS = """
:root{--bg:#fff;--fg:#1a1a1a;--mut:#666;--line:#e0e0e0;--card:#fafafa;--acc:#0b62d0;
--pre:#f0f0f2}
@media (prefers-color-scheme:dark){:root{--bg:#16181c;--fg:#e6e6e6;--mut:#9aa0a6;
--line:#2c2f36;--card:#1d2026;--acc:#7fb2ff;--pre:#14161a}}
*{box-sizing:border-box}
body{margin:0;padding:24px;background:var(--bg);color:var(--fg);
font:15px/1.55 -apple-system,Segoe UI,Roboto,sans-serif}
h1{font-size:20px;margin:0 0 16px}
table{border-collapse:collapse;margin-bottom:20px;font-size:13px;width:100%}
th,td{border:1px solid var(--line);padding:6px 10px;text-align:right}
th:first-child,td:first-child{text-align:left}
th{background:var(--card);font-weight:600}
details{border:1px solid var(--line);border-radius:8px;background:var(--card);
margin-bottom:20px}
summary{cursor:pointer;padding:10px 14px;font-size:13px;color:var(--acc);
font-weight:600;user-select:none}
details>*:not(summary){margin:0 14px 14px}
details h3{font-size:12px;color:var(--mut);text-transform:uppercase;
letter-spacing:.04em;margin:14px 14px 6px}
pre{background:var(--pre);border:1px solid var(--line);border-radius:6px;padding:12px;
overflow:auto;max-height:320px;font:12px/1.5 ui-monospace,Consolas,monospace;
white-space:pre-wrap;word-break:break-word}
.filterbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:16px}
.filterbar .lbl{color:var(--mut);font-size:12px;margin-right:2px}
.chip{cursor:pointer;border:1px solid var(--line);border-radius:999px;padding:5px 13px;
font:12px inherit;background:var(--bg);color:var(--mut);user-select:none}
.chip.on{background:var(--acc);border-color:var(--acc);color:#fff;font-weight:600}
.chip.act{margin-left:auto;color:var(--acc)}
.grid{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));
align-items:start}
.card.off{display:none}
.card{border:1px solid var(--line);border-radius:8px;background:var(--card);
padding:0 16px 16px;overflow:auto;max-height:80vh}
/* The whole header sticks, not just the name: while a long answer scrolls, the
   numbers stay visible next to the arm they belong to. */
.hd{position:sticky;top:0;z-index:2;background:var(--card);margin:0 -16px 12px;
padding:12px 16px 10px;border-bottom:1px solid var(--line)}
.hd h2{margin:0;font-size:15px;color:var(--acc)}
.hd .meta{color:var(--mut);font-size:12px;margin-top:4px}
.card details{margin:0 0 14px;background:transparent}
.card details>*:not(summary){margin:0 0 12px}
.card summary{padding:6px 0;font-size:12px}
.card p{margin:8px 0}
.card h1,.card h3,.card h4{font-size:14px;margin:16px 0 6px}
.card ul,.card ol{margin:8px 0;padding-left:22px}
.card li{margin:3px 0}
code{background:rgba(127,127,127,.16);padding:1px 5px;border-radius:4px;font-size:.9em}
.diffbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:0 14px 12px}
.diffbar select{background:var(--bg);color:var(--fg);border:1px solid var(--line);
border-radius:6px;padding:6px 10px;font:13px inherit}
.diffbar .stat{color:var(--mut);font-size:12px;margin-left:auto}
table.diff{table-layout:fixed;font:12px/1.5 ui-monospace,Consolas,monospace;margin:0 14px 14px;
width:calc(100% - 28px)}
table.diff td{border:0;border-top:1px solid var(--line);padding:2px 8px;text-align:left;
white-space:pre-wrap;word-break:break-word;vertical-align:top;width:50%}
table.diff td.n{width:38px;color:var(--mut);text-align:right;user-select:none;
border-right:1px solid var(--line)}
tr.del td:not(.n){background:rgba(220,60,60,.14)}
tr.add td:not(.n){background:rgba(60,180,90,.16)}
tr.mod td:not(.n){background:rgba(220,160,40,.14)}
tr.same td:not(.n){color:var(--mut)}
"""


def _details(summary: str, blocks: list[tuple[str, str]], open_: bool = False) -> str:
    inner = "".join(
        (f"<h3>{_esc(h)}</h3>" if h else "") + f"<pre>{_esc(b)}</pre>"
        for h, b in blocks
    )
    return (f"<details{' open' if open_ else ''}>"
            f"<summary>{_esc(summary)}</summary>{inner}</details>")


def _task_block() -> str:
    """The input every arm shares. Without it the table is unreadable a week later:
    numbers mean nothing once the task behind them is gone."""
    blocks, label = [], "Task and input"
    task_p, art_p = ROOT / "task.md", ROOT / "article.md"
    if task_p.exists():
        blocks.append(("task.md (user-prompt template)", read(task_p).strip()))
    if art_p.exists():
        art = read(art_p)
        head = art[:1500].rstrip()
        more = f"\n\n[... truncated, full input is {len(art):,} chars / {len(art.split()):,} words]"
        blocks.append((f"article.md (substituted for {{ARTICLE}})", head + (more if len(art) > 1500 else "")))
        label = f"Task and input ({len(art.split()):,} words)"
    return _details(label, blocks) if blocks else ""


FILTER_JS = """
// Hiding an arm collapses its column; the grid is auto-fit, so the arms you kept
// widen to fill the row instead of leaving a gap.
const BAR = document.getElementById('armfilter');
const CARDS = () => Array.from(document.querySelectorAll('.card'));
function apply() {
  const on = new Set(Array.from(BAR.querySelectorAll('.chip.on')).map(c => c.dataset.arm));
  CARDS().forEach(c => c.classList.toggle('off', !on.has(c.dataset.arm)));
}
BAR.addEventListener('click', e => {
  const chip = e.target.closest('.chip');
  if (!chip) return;
  if (chip.classList.contains('act')) {
    const chips = Array.from(BAR.querySelectorAll('.chip[data-arm]'));
    const allOn = chips.every(c => c.classList.contains('on'));
    chips.forEach(c => c.classList.toggle('on', !allOn));
  } else {
    chip.classList.toggle('on');
  }
  apply();
});
apply();
"""


DIFF_JS = """
const P = JSON.parse(document.getElementById('sp-data').textContent);
const L = document.getElementById('d-left'), R = document.getElementById('d-right');
const OUT = document.getElementById('d-out'), ST = document.getElementById('d-stat');
for (const k of Object.keys(P)) {
  L.add(new Option(k, k)); R.add(new Option(k, k));
}
const keys = Object.keys(P);
if (keys.length > 1) { L.value = keys[keys.length - 2]; R.value = keys[keys.length - 1]; }

// Line-level LCS. Prompts run to ~100 lines, so quadratic is free here.
function lcs(a, b) {
  const m = a.length, n = b.length;
  const d = Array.from({length: m + 1}, () => new Uint32Array(n + 1));
  for (let i = m - 1; i >= 0; i--)
    for (let j = n - 1; j >= 0; j--)
      d[i][j] = a[i] === b[j] ? d[i + 1][j + 1] + 1 : Math.max(d[i + 1][j], d[i][j + 1]);
  const rows = [];
  let i = 0, j = 0;
  while (i < m && j < n) {
    if (a[i] === b[j]) rows.push(['same', a[i], b[j], ++i, ++j]);
    else if (d[i + 1][j] >= d[i][j + 1]) rows.push(['del', a[i], null, ++i, j]);
    else rows.push(['add', null, b[j], i, ++j]);
  }
  while (i < m) rows.push(['del', a[i], null, ++i, j]);
  while (j < n) rows.push(['add', null, b[j], i, ++j]);
  return rows;
}

const esc = s => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

function render() {
  const NL = String.fromCharCode(10);   // no escape literal, so the class of bugs
  const split = s => s ? s.split(NL) : [];  // where a file write mangles the newline is gone
  const a = split(P[L.value]), b = split(P[R.value]);  // an empty prompt is zero lines, not one
  const rows = lcs(a, b);
  // An adjacent del+add is one modified line, not two separate ones.
  const html = [], counts = {add: 0, del: 0, same: 0};
  for (let k = 0; k < rows.length; k++) {
    const [kind, la, rb, ln, rn] = rows[k];
    counts[kind]++;
    if (kind === 'del' && rows[k + 1] && rows[k + 1][0] === 'add') {
      const nx = rows[k + 1];
      counts.add++;
      html.push(`<tr class="mod"><td class="n">${ln}</td><td>${esc(la)}</td>` +
                `<td class="n">${nx[4]}</td><td>${esc(nx[2])}</td></tr>`);
      k++;
      continue;
    }
    const left = kind === 'add' ? '<td class="n"></td><td></td>'
                                : `<td class="n">${ln}</td><td>${esc(la)}</td>`;
    const right = kind === 'del' ? '<td class="n"></td><td></td>'
                                 : `<td class="n">${rn}</td><td>${esc(rb)}</td>`;
    html.push(`<tr class="${kind}">${left}${right}</tr>`);
  }
  OUT.innerHTML = html.join('');
  ST.textContent = `+${counts.add} / -${counts.del} lines, ${counts.same} unchanged`;
}
L.onchange = R.onchange = render;
render();
"""


def _diff_panel(names: list[str]) -> str:
    """Compares prompt text across variants. The table says something changed; this
    says WHAT changed in the prompt itself."""
    data = {}
    for n in names:
        sp = VARIANTS / f"{n}.md"
        if sp.exists():
            data[n] = read(sp).strip()
        elif n in REFERENCE_ARMS or n == VANILLA:
            # Reference arms have no file, but they MUST be listed: diffing an empty
            # append against a variant shows exactly what that variant adds to the
            # setup you already have, which is the usual question.
            data[f"{n} (nothing appended)"] = ""
    if len(data) < 2:
        return ""
    payload = json.dumps(data, ensure_ascii=False)
    opts = ('<div class="diffbar">'
            '<label>from <select id="d-left"></select></label>'
            '<label>to <select id="d-right"></select></label>'
            '<span class="stat" id="d-stat"></span></div>')
    return (f'<details><summary>System prompt diff ({len(data)} versions)</summary>'
            f'{opts}<table class="diff"><tbody id="d-out"></tbody></table></details>'
            f'<script type="application/json" id="sp-data">{_esc(payload)}</script>'
            f"<script>{DIFF_JS}</script>")


def write_html(names: list[str] | None = None) -> Path:
    names = names or all_variants()
    rows = table(names).strip().splitlines()
    cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
    thead = "".join(f"<th>{_esc(c)}</th>" for c in cells[0])
    tbody = "".join(
        "<tr>" + "".join(f"<td>{_esc(c)}</td>" for c in r) + "</tr>"
        for r in cells[2:]
    )

    cards = []
    for n in names:
        runs = load_runs(n)
        if not runs:
            continue
        # Show the REPRESENTATIVE run, the one nearest the median, not the last one.
        # The last run can be an outlier, and then the card contradicts its own
        # table: one variant had a median of 379 and a last run of 643.
        mid = med(runs, "metrics.text_tok") or 0
        last = min(runs, key=lambda r: abs(r["metrics"]["text_tok"] - mid))
        m = last["metrics"]
        pos = runs.index(last) + 1

        sp = VARIANTS / f"{n}.md"
        if sp.exists():
            body = read(sp).strip()
            prompt_block = _details(
                f"system prompt: variants/{n}.md ({len(body):,} chars, ~{len(body)//4} tok)",
                [("", body)])
        elif n == VANILLA:
            prompt_block = _details("system prompt: NONE, CLAUDE.md cut as well",
                                    [("", "--setting-sources project")])
        else:
            prompt_block = _details("system prompt: nothing appended, your CLAUDE.md only",
                                    [("", "(reference arm)")])

        cards.append(
            f'<div class="card" data-arm="{_esc(n)}">'
            f'<div class="hd"><h2>{_esc(n)}</h2><div class="meta">'
            f'{m["text_tok"]} text tok &middot; {m["words"]} words &middot; '
            f'{m["headings"]} headings &middot; {m["em_dash"]} em dash &middot; '
            f'{m["refcodes"]} codes'
            f'{f" &middot; run {pos}/{len(runs)} (median)" if len(runs) > 1 else ""}'
            f'</div></div>'
            f'{prompt_block}{md_to_html(last["text"])}</div>'
        )

    shown = [n for n in names if load_runs(n)]
    chips = "".join(
        f'<button class="chip on" data-arm="{_esc(n)}">{_esc(n)}</button>' for n in shown
    )
    bar = (f'<div class="filterbar" id="armfilter"><span class="lbl">show</span>{chips}'
           f'<button class="chip act">all / none</button></div>')

    doc = (f"<!doctype html><html lang=en><meta charset=utf-8>"
           f"<meta name=viewport content='width=device-width,initial-scale=1'>"
           f"<title>System prompt A/B</title><style>{CSS}</style>"
           f"<body><h1>System prompt A/B</h1>"
           f"<table><thead><tr>{thead}</tr></thead><tbody>{tbody}</tbody></table>"
           f"{_task_block()}{_diff_panel(names)}"
           f'{bar}<div class="grid">{"".join(cards)}</div>'
           f"<script>{FILTER_JS}</script></body></html>")

    p = ROOT / "report.html"
    p.write_text(doc, encoding="utf-8")
    return p


if __name__ == "__main__":
    sys.exit(main())
