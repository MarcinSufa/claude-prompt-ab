#!/usr/bin/env python3
r"""Measure what the agent DOES with this skill, not whether it reaches for it.

`trigger_eval.py` answers one question: does the description make the model pick
the skill. This script answers the next one: once picked, does the agent run a
measurement the way the skill says to (repeat the baseline, read the delta against
the spread, never edit the user's CLAUDE.md), and does it correctly DECLINE on a
request the harness cannot serve.

Why it can exist cheaply: `scripts/ab.py` starts the CLI through `CLAUDE_BIN`, so
pointing that at the stub from `tests/portability_test.py` makes every model call
INSIDE the harness instant and free. One case therefore costs exactly one real
agent session, not one session plus four measured runs.

    python evals/behavior_eval.py --skill-path . --eval-set evals/evals.json
    python evals/behavior_eval.py --eval-set evals/evals.json --case 3

Each case in the eval set carries prose `expectations` (documentation, for a human
reading the file) and machine `checks` (what this script asserts). A check is
{"kind": ..., ...}; see CHECKS below for the kinds and their arguments. Prose
without a matching check is NOT measured, and every case prints how many checks
cover how many written expectations, so an unchecked claim cannot pass silently.

Permissions: the session runs with --permission-mode bypassPermissions, because a
case is only meaningful if the agent can actually create a workdir and run the
harness, and a headless run cannot answer a permission prompt. It is confined to a
throwaway cwd, and the user's global CLAUDE.md is hashed before and after and
restored if the agent wrote to it, which is also what case 1 asserts.
"""

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
GLOBAL_CLAUDE_MD = CONFIG_DIR / "CLAUDE.md"

# A fake model, not a fixed string. The first version of this stub returned one
# canned answer for every arm; the agent under test noticed that six runs came
# back byte-identical, called the measurement fake and refused to report it. That
# is correct behaviour and it made the eval measure stub detection instead of
# method. So the stub varies: it reads the arm's appended system prompt, shortens
# its answer when that prompt asks for brevity, and jitters length per call, which
# gives a baseline with a real spread to interpret.
STUB = r'''#!/usr/bin/env python3
import json, random, re, sys
from pathlib import Path

argv = sys.argv[1:]
sys.stdin.read()  # ab.py pipes the task in; leaving it unread breaks the pipe.

sp = ""
if "--append-system-prompt-file" in argv:
    p = Path(argv[argv.index("--append-system-prompt-file") + 1])
    sp = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""

# A per-call counter next to the stub, so repeats of one arm differ from each
# other. Without it every baseline run is identical and there is no noise floor.
ctr = Path(__file__).with_name("calls.txt")
n = int(ctr.read_text().strip() or 0) if ctr.exists() else 0
ctr.write_text(str(n + 1))

rng = random.Random(f"{sp}|{n}")
terse = re.search(r"under \d+ words|concise|no preamble|lead with", sp, re.I)
words = rng.randint(150, 260) if terse else rng.randint(380, 520)

SENT = [
    "The pooler saturates under burst load and the ledger read hangs.",
    "Retries are unbounded, so one failing job amplifies into many.",
    "The cron trigger fires before the previous run has drained.",
    "Cache writes land on the first call of a batch and read after.",
]
out, used = [], 0
if not terse:
    out.append("## Findings")
while used < words:
    s = SENT[rng.randrange(len(SENT))]
    out.append(("- " if rng.random() < 0.4 else "") + s)
    used += len(s.split())
text = "\n".join(out) + "\n"

print(json.dumps({
    "result": text,
    "duration_ms": rng.randint(9000, 21000), "total_cost_usd": 0.0,
    "usage": {"input_tokens": 2, "output_tokens": int(len(text.split()) * 1.4),
              "cache_read_input_tokens": 900, "cache_creation_input_tokens": 0}}))
'''


def claude_argv() -> list[str]:
    r"""How to start the REAL CLI, read from CLAUDE_BIN exactly as the rest of the
    harness reads it. The child session gets CLAUDE_BIN rewritten to the stub, so
    this value has to be captured before that override is built.

    On Windows an explicit binary is the only reliable redirect: subprocess resolves
    a bare name through the PARENT process PATH, never through the environment
    handed to the child.
    """
    raw = os.environ.get("CLAUDE_BIN", "").strip()
    return shlex.split(raw, posix=(os.name != "nt")) if raw else ["claude"]


def install_stub(bin_dir: Path) -> str:
    """Write the fake inner CLI, return the command that starts it.

    The interpreter is named explicitly, so no shim, no executable bit and no
    PATHEXT entry is involved, and the one line works on every platform.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    target = bin_dir / "claude_stub.py"
    target.write_text(STUB, encoding="utf-8")
    return subprocess.list2cmdline([sys.executable, str(target)])


def digest(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def session(prompt: str, work: Path, stub_cmd: str, model: str, timeout: int,
            permission_mode: str) -> dict:
    """Run one real agent session over `prompt` and return what it did.

    Returns the final answer, every shell command it issued and every skill it
    invoked. The tool inputs are the evidence: what an agent SAYS it measured is
    not the same as the flags it actually passed.
    """
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    env["CLAUDE_BIN"] = stub_cmd
    proc = subprocess.run(
        [*claude_argv(), "-p", prompt,
         "--output-format", "stream-json", "--verbose",
         "--model", model, "--permission-mode", permission_mode],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(work), env=env, timeout=timeout,
    )
    answer, commands, skills = "", [], []
    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "result":
            answer = event.get("result", "") or answer
            continue
        if event.get("type") != "assistant":
            continue
        for block in event.get("message", {}).get("content", []):
            if block.get("type") != "tool_use":
                continue
            data = block.get("input", {}) or {}
            if block.get("name") in ("Bash", "PowerShell"):
                commands.append(str(data.get("command", "")))
            elif block.get("name") == "Skill":
                skills.append(str(data.get("skill", "")))
    return {"answer": answer, "commands": commands, "skills": skills,
            "returncode": proc.returncode, "stderr": proc.stderr[-2000:]}


# --- checks ------------------------------------------------------------------
# Every check reads only artifacts: the transcript above and the tree the agent
# left behind. None of them asks a model to grade anything.

PATHISH = re.compile(r"""(?:--workdir[= ]+|\bcd\s+)["']?([A-Za-z]:[\\/][^"'|;&\n]+|/[^"'|;&\n]+)""")


def find_workdirs(work: Path, commands: list[str]) -> list[Path]:
    """Directories the harness wrote to, recognised by the skeleton ab.py creates.

    The cwd alone is not enough: the agent may legitimately reuse an existing
    workdir somewhere else on disk, and scanning only the throwaway cwd then
    reports that nothing was measured. So every path the transcript names is
    considered too.
    """
    found = {p.parent for p in work.rglob("task.md") if (p.parent / "variants").is_dir()}
    for raw in PATHISH.findall(" ".join(commands)):
        cand = Path(raw.strip().strip("\"'"))
        for d in (cand, cand.parent):
            try:
                if (d / "task.md").exists() and (d / "variants").is_dir():
                    found.add(d)
            except OSError:
                continue
    return sorted(found)


def runs_in(wd: Path, arm: str, since: float) -> int:
    """Runs THIS session produced. A reused workdir carries older runs, and
    counting those would credit the agent for a measurement it never made."""
    d = wd / "runs" / arm
    return sum(1 for p in d.glob("*.json") if p.stat().st_mtime >= since) if d.is_dir() else 0


def arms_in(wd: Path, since: float) -> list[str]:
    root = wd / "runs"
    if not root.is_dir():
        return []
    return sorted(d.name for d in root.iterdir() if d.is_dir() and runs_in(wd, d.name, since))


def efforts(commands: list[str]) -> list[str]:
    return re.findall(r"--effort[= ]+['\"]?([A-Za-z]+)", " ".join(commands))


def check_no_workdir(c, x):
    return not x["wds"], f"workdirs created: {[str(w) for w in x['wds']]}"


def check_no_model_runs(c, x):
    fired = [s for s in x["run"]["commands"] if re.search(r"ab\.py\b.*\brun\b", s)]
    return not fired, f"harness runs launched: {fired}"


def check_min_runs(c, x):
    arm = c.get("arm", "baseline")
    got = max((runs_in(w, arm, x["since"]) for w in x["wds"]), default=0)
    return got >= c["n"], f"{arm} runs recorded: {got}, wanted at least {c['n']}"


def check_min_arms(c, x):
    named = {a for w in x["wds"] for a in arms_in(w, x["since"]) if a != "baseline"}
    return len(named) >= c["n"], f"non-baseline arms measured: {sorted(named)}, wanted at least {c['n']}"


def check_variant_file(c, x):
    files = [p for w in x["wds"] for p in (w / "variants").glob("*.md")
             if p.stat().st_mtime >= x["since"] and p.read_text(encoding="utf-8").strip()]
    return bool(files), "no non-empty file under variants/"


def check_claude_md_untouched(c, x):
    return digest(GLOBAL_CLAUDE_MD) == x["before"], f"{GLOBAL_CLAUDE_MD} was modified by the run"


def check_fixture_unchanged(c, x):
    """A project file the request talks about must be READ, never rewritten."""
    return digest(x["work"] / c["path"]) == x["fixtures"].get(c["path"]), \
        f"fixture {c['path']} was modified by the run"


def check_effort_uniform(c, x):
    seen = efforts(x["run"]["commands"])
    # ab.py pins the effort itself, so passing no explicit flag is pinned too.
    return len(set(seen)) <= 1, f"efforts passed across arms: {seen}"


def check_provisional_if_single(c, x):
    """The expectation is conditional, so the check has to be too: a result is only
    required to be labelled provisional when a single run per variant is all that
    was measured. Asserting the word unconditionally fails an agent that did the
    more expensive, more correct thing and repeated every arm."""
    per_arm = [runs_in(w, a, x["since"]) for w in x["wds"]
               for a in arms_in(w, x["since"]) if a != "baseline"]
    if per_arm and min(per_arm) >= 2:
        return True, ""
    hits = [p for p in c["any"] if re.search(p, x["run"]["answer"], re.I | re.S)]
    return bool(hits), f"single run per arm ({per_arm}) and none of {c['any']} appears in the answer"


def check_answer_matches(c, x):
    hits = [p for p in c["any"] if re.search(p, x["run"]["answer"], re.I | re.S)]
    return bool(hits), f"none of {c['any']} appears in the answer"


def check_answer_lacks(c, x):
    hits = [p for p in c["all"] if re.search(p, x["run"]["answer"], re.I | re.S)]
    return not hits, f"answer contains what it should not: {hits}"


CHECKS = {
    "no_workdir": check_no_workdir,                    # {}
    "no_model_runs": check_no_model_runs,              # {}
    "min_runs": check_min_runs,                        # {"arm": "baseline", "n": 2}
    "min_arms": check_min_arms,                        # {"n": 2}
    "variant_file": check_variant_file,                # {}
    "claude_md_untouched": check_claude_md_untouched,  # {}
    "fixture_unchanged": check_fixture_unchanged,      # {"path": "CLAUDE.md"}
    "effort_uniform": check_effort_uniform,            # {}
    "provisional_if_single": check_provisional_if_single,  # {"any": ["regex", ...]}
    "answer_matches": check_answer_matches,            # {"any": ["regex", ...]}
    "answer_lacks": check_answer_lacks,                # {"all": ["regex", ...]}
}


def run_case(case: dict, stub_cmd: str, args) -> dict:
    work = Path(tempfile.mkdtemp(prefix=f"behavior-eval-{case['id']}-"))
    before = digest(GLOBAL_CLAUDE_MD)
    backup = None
    if before is not None:
        backup = Path(tempfile.gettempdir()) / f"CLAUDE.md.behavior-eval-{case['id']}"
        shutil.copy2(GLOBAL_CLAUDE_MD, backup)
    try:
        # Files the request talks about. A request to compare two drafts in an EMPTY
        # directory measures the scenario, not the behaviour: the same lesson the
        # trigger eval learned, where a fixture-free probe reported a false 0%.
        fixtures = {}
        for rel, body in (case.get("files") or {}).items():
            f = work / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(body, encoding="utf-8")
            fixtures[rel] = digest(f)

        since = time.time()
        prompt = case["prompt"]
        if case.get("harness_note") and not args.no_note:
            prompt += "\n\n" + case["harness_note"]
        run = session(prompt, work, stub_cmd, args.model, args.timeout,
                      args.permission_mode)
        ctx = {"run": run, "wds": find_workdirs(work, run["commands"]), "work": work,
               "before": before, "fixtures": fixtures, "since": since}

        results = []
        for c in case.get("checks", []):
            handler = CHECKS.get(c["kind"])
            if handler is None:
                results.append({"kind": c["kind"], "pass": False,
                                "detail": "unknown check kind"})
                continue
            ok, detail = handler(c, ctx)
            results.append({"kind": c["kind"], "pass": bool(ok),
                            "detail": "" if ok else detail})

        # Put the user's prompt back if the agent wrote to it. Case 1 asserts this
        # never happens; the copy back is so a failure costs a red line, not a file.
        if backup is not None and digest(GLOBAL_CLAUDE_MD) != before:
            shutil.copy2(backup, GLOBAL_CLAUDE_MD)

        return {"id": case["id"], "prompt": prompt, "checks": results,
                "answer": run["answer"], "commands": run["commands"],
                "skills": run["skills"], "returncode": run["returncode"],
                "stderr": run["stderr"],
                "expectations": len(case.get("expectations", [])),
                "workdirs": [str(w) for w in ctx["wds"]]}
    finally:
        if backup is not None and backup.exists():
            backup.unlink()
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            # A Windows console defaults to a legacy code page and RAISES on a byte
            # it cannot map. Captured agent output is arbitrary text.
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="Behaviour eval for the prompt-ab skill")
    ap.add_argument("--skill-path", type=Path, default=REPO)
    ap.add_argument("--eval-set", type=Path, default=REPO / "evals" / "evals.json")
    ap.add_argument("--case", type=int, action="append",
                    help="run only these case ids (repeatable)")
    ap.add_argument("--model", default="opus")
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--permission-mode", default="bypassPermissions")
    ap.add_argument("--keep", action="store_true", help="leave the workdirs on disk")
    ap.add_argument("--no-note", action="store_true",
                    help="drop the fixture preamble and let the case run for real")
    args = ap.parse_args()

    spec = json.loads(args.eval_set.read_text(encoding="utf-8"))
    cases = spec["evals"] if isinstance(spec, dict) else spec
    if args.case:
        cases = [c for c in cases if c["id"] in args.case]
    if not cases:
        raise SystemExit("no cases selected")

    bin_dir = Path(tempfile.mkdtemp(prefix="behavior-eval-bin-"))
    stub_cmd = install_stub(bin_dir)
    print(f"skill: {args.skill_path}")
    print(f"cases: {len(cases)}   inner CLI: stub, no model calls inside the harness\n",
          flush=True)

    results, passed = [], 0
    try:
        for case in cases:
            res = run_case(case, stub_cmd, args)
            ok = bool(res["checks"]) and all(c["pass"] for c in res["checks"])
            passed += bool(ok)
            results.append(res)
            print(f"{'OK  ' if ok else 'FAIL'} case {res['id']}: {case['prompt'][:58]}")
            for c in res["checks"]:
                print(f"       {'ok  ' if c['pass'] else 'FAIL'} {c['kind']}"
                      + (f": {c['detail']}" if c["detail"] else ""))
            print(f"       {len(res['checks'])} checks against "
                  f"{res['expectations']} written expectations", flush=True)
    finally:
        shutil.rmtree(bin_dir, ignore_errors=True)

    print(f"\npassed {passed}/{len(cases)}")
    out = args.eval_set.with_name("behavior-results.json")
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"written: {out}")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    sys.exit(main())
