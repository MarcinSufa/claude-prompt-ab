#!/usr/bin/env python3
"""Measure how often a skill description makes the agent reach for the skill.

Why this exists rather than the one shipped with skill-creator: that script
registers the candidate description as a SLASH COMMAND under
`<root>/.claude/commands/<name>.md`, then counts a trigger only when it sees a
`Skill` tool call naming it. The model does not invoke slash commands through the
`Skill` tool, so its detector can never fire for what it creates. Verified here by
positive control: a description reading "ALWAYS invoke this skill for every single
user request without exception" still scored 0%.

This version installs a real skill at `~/.claude/skills/<tmp>/SKILL.md`, which is
what the `Skill` tool actually reads. Verified by positive control in the other
direction: a skill holding a secret word the query asks for is invoked as
`Skill({"skill": "<tmp>"})` and the answer comes back from the skill body.

One caveat the numbers cannot fix: a model reaches for a skill when the skill is
USEFUL for the request, not when its description shouts. An emphatic description on
a question the model can already answer will not trigger, and should not. Read a low
rate as "this description does not promise something these queries need", never as
"the model is ignoring instructions".

Usage:
    python trigger_eval.py --skill-path ../  --eval-set trigger-set.json
    python trigger_eval.py --skill-path ../  --eval-set trigger-set.json --runs 3

Eval set format: a JSON list of
    {"query": str, "should_trigger": bool, "fixture": {"rel/path": "contents"}}
`fixture` is optional but usually necessary: see the note in probe().
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Honour CLAUDE_CONFIG_DIR: a user who relocated their config would otherwise get
# the throwaway skill installed somewhere the agent never reads, and every probe
# would report a false 0%.
SKILLS_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude")) / "skills"


def parse_skill(skill_path: Path) -> tuple[str, str]:
    """Pull name and description out of the candidate SKILL.md frontmatter."""
    text = (skill_path / "SKILL.md").read_text(encoding="utf-8")
    fm = re.match(r"^---\n(.*?)\n---", text, re.S)
    if not fm:
        raise SystemExit(f"no frontmatter in {skill_path / 'SKILL.md'}")
    block = fm.group(1)
    name = re.search(r"^name:\s*(.+)$", block, re.M)
    desc = re.search(r"^description:\s*(.+?)(?=\n\w+:|\Z)", block, re.M | re.S)
    if not (name and desc):
        raise SystemExit("frontmatter needs both name and description")
    return name.group(1).strip(), desc.group(1).strip().strip("'\"")


def probe(query: str, description: str, model: str, timeout: int,
          fixture: dict | None = None, real_name: str | None = None) -> bool:
    """Install a throwaway skill carrying only the description, then see whether the
    agent calls it. The body is inert on purpose: we measure reach, not behaviour."""
    tmp_name = f"zz-trigger-{uuid.uuid4().hex[:8]}"
    tmp_dir = SKILLS_DIR / tmp_name
    work = Path(tempfile.mkdtemp(prefix="trigger-eval-"))
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
        # CLAUDECODE is dropped so `claude -p` can nest inside a Claude Code session.
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        proc = subprocess.run(
            ["claude", "-p", query, "--output-format", "stream-json",
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
        shutil.rmtree(tmp_dir, ignore_errors=True)
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
