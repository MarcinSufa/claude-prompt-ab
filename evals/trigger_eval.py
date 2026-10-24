#!/usr/bin/env python3
"""Measure how often a skill description makes the agent reach for the skill.

Why this exists rather than the one shipped with skill-creator: that script
registers the candidate description as a SLASH COMMAND under
`<root>/.claude/commands/<name>.md`, then counts a trigger only when it sees a
`Skill` tool call naming it. The model does not invoke slash commands through the
`Skill` tool, so its detector can never fire for what it creates. Verified here by
positive control: a description reading "ALWAYS invoke this skill for every single
user request without exception" still scored 0%.

This version installs a real skill, which is what the `Skill` tool actually
reads. Verified by positive control in the other direction (with the skill under
`~/.claude/skills/<tmp>/`): a skill holding a secret word the query asks for is
invoked as `Skill({"skill": "<tmp>"})` and the answer comes back from the skill body.

Where it installs: as a PROJECT skill, `<probe cwd>/.claude/skills/<tmp>/SKILL.md`,
inside the throwaway directory each probe already runs in. Nothing is written to
the user's `~/.claude/skills`, so a killed run cannot leave a stray skill behind
that every later session would load. A temporary HOME or CLAUDE_CONFIG_DIR is not
an option: the CLI honours both, but then it finds no login (`claude auth status`
reports loggedIn false), every probe fails and the eval reports a false 0%.

One caveat the numbers cannot fix: a model reaches for a skill when the skill is
USEFUL for the request, not when its description shouts. An emphatic description on
a question the model can already answer will not trigger, and should not. Read a low
rate as "this description does not promise something these queries need", never as
"the model is ignoring instructions".

Usage:
    python3 trigger_eval.py --skill-path ../  --eval-set trigger-set.json
    python3 trigger_eval.py --skill-path ../  --eval-set trigger-set.json --runs 3

Eval set format: a JSON list of
    {"query": str, "should_trigger": bool, "fixture": {"rel/path": "contents"}}
`fixture` is optional but usually necessary: see the note in probe().
"""

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Set by a parent Claude Code session; dropped so `claude -p` can nest inside one.
NESTED_SESSION_VARS = ("CLAUDECODE",)


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


def parse_skill(skill_path: Path) -> tuple[str, str]:
    """Pull name and description out of the candidate SKILL.md frontmatter."""
    text = (skill_path / "SKILL.md").read_text(encoding="utf-8")
    fm = re.match(r"^---\n(.*?)\n---", text, re.S)
    if not fm:
        raise SystemExit(f"no frontmatter in {skill_path / 'SKILL.md'}")
    block = fm.group(1)
    name = re.search(r"^name:\s*(.+)$", block, re.M)
    desc = re.search(r"^description:\s*(.+?)(?=\n[\w-]+:|\Z)", block, re.M | re.S)
    if not (name and desc):
        raise SystemExit("frontmatter needs both name and description")
    return name.group(1).strip(), desc.group(1).strip().strip("'\"")


def probe(query: str, description: str, model: str, timeout: int,
          fixture: dict | None = None, real_name: str | None = None) -> bool:
    """Install a throwaway skill carrying only the description, then see whether the
    agent calls it. The body is inert on purpose: we measure reach, not behaviour."""
    tmp_name = f"zz-trigger-{uuid.uuid4().hex[:8]}"
    work = Path(tempfile.mkdtemp(prefix="trigger-eval-"))
    # A project skill of the probe's own cwd: loaded by this session only, and
    # removed with the directory. See the module docstring for why not ~/.claude.
    tmp_dir = work / ".claude" / "skills" / tmp_name
    try:
        # Files the query talks about. A measurement skill asked to compare two
        # prompts in an EMPTY directory has nothing to measure, and declining to
        # invoke it is correct behaviour, so a fixture-free probe scores the
        # scenario rather than the description. Verified: the same query scored 0%
        # bare and invoked the skill as its first tool call with files present.
        for rel, body in (fixture or {}).items():
            f = work / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(body, encoding="utf-8")
        tmp_dir.mkdir(parents=True, exist_ok=True)
        # A block scalar, not a bare value. Real descriptions carry quotes, colons
        # and commas, all of which break plain YAML; the frontmatter then fails to
        # parse and the skill silently falls back to its own name as the
        # description, so every probe measures the wrong text and reports 0%.
        indented = "\n  ".join(description.split("\n"))
        (tmp_dir / "SKILL.md").write_text(
            f"---\nname: {tmp_name}\ndescription: |\n  {indented}\n---\n\n"
            f"# {tmp_name}\n\nAcknowledge that this skill was selected, then stop.\n",
            encoding="utf-8",
        )
        env = {k: v for k, v in os.environ.items() if k not in NESTED_SESSION_VARS}
        proc = subprocess.run(
            [*claude_argv(), "-p", query,
             "--output-format", "stream-json",
             "--verbose", "--model", model, "--effort", "low"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(work), env=env, timeout=timeout,
        )
        for line in proc.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "assistant":
                continue
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "tool_use" and block.get("name") == "Skill":
                    # Count the REAL skill name too. When the candidate is already
                    # installed, the model prefers it over the throwaway copy: same
                    # description, better name, real body. Counting only the UUID
                    # name reports 0% while the agent is in fact reaching for the
                    # skill under test. This is the exact defect that makes
                    # skill-creator's own run_eval.py unusable.
                    picked = block.get("input", {}).get("skill")
                    if picked == tmp_name or (real_name and picked == real_name):
                        return True
        return False
    except subprocess.TimeoutExpired:
        return False
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="Trigger-rate eval for a skill description")
    ap.add_argument("--skill-path", type=Path, required=True)
    ap.add_argument("--eval-set", type=Path, required=True)
    ap.add_argument("--runs", type=int, default=1, help="probes per query")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--model", default="opus")
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--description", help="override the description under test")
    args = ap.parse_args()

    name, description = parse_skill(args.skill_path)
    description = args.description or description
    cases = json.loads(args.eval_set.read_text(encoding="utf-8"))

    print(f"skill: {name}")
    print(f"description: {len(description)} chars")
    print(f"cases: {len(cases)} x {args.runs} run(s)\n", flush=True)

    jobs = [(c, i) for c in cases for i in range(args.runs)]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        fired = list(pool.map(
            lambda j: probe(j[0]["query"], description, args.model, args.timeout,
                            j[0].get("fixture"), name), jobs))

    results, passed = [], 0
    for idx, case in enumerate(cases):
        hits = sum(fired[idx * args.runs:(idx + 1) * args.runs])
        rate = hits / args.runs
        ok = (rate >= 0.5) == bool(case["should_trigger"])
        passed += ok
        results.append({**case, "trigger_rate": rate, "pass": ok})
        want = "yes" if case["should_trigger"] else "no "
        print(f"{'OK  ' if ok else 'FAIL'} want={want} fired={rate:.0%}  {case['query'][:62]}")

    pos = [r for r in results if r["should_trigger"]]
    neg = [r for r in results if not r["should_trigger"]]
    print(f"\npassed {passed}/{len(cases)}")
    if pos:
        print(f"  trigger rate on positives:   {sum(r['trigger_rate'] for r in pos)/len(pos):.0%}")
    if neg:
        print(f"  false-fire rate on negatives: {sum(r['trigger_rate'] for r in neg)/len(neg):.0%}")

    out = args.eval_set.with_name("trigger-results.json")
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"written: {out}")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    sys.exit(main())
