#!/usr/bin/env python3
"""End-to-end check that the harness runs on this platform, without calling a model.

A stub CLI answers in the two shapes the code consumes: the single JSON object
that `--output-format json` returns, and the stream-json lines the trigger eval
reads. Everything else is the real code path, including the subprocess call, the
workdir skeleton, metric computation, both reports and cleanup.

    python tests/portability_test.py

Exit code 0 means the harness works here. This exists because "portable by
construction" is a claim, and a claim is not a measurement.
"""

import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ANSWER = (
    "## Findings\n\nF1 The pool saturates under burst load.\n"
    "F2 Retries are unbounded, so a failing job amplifies.\n\n- one\n- two\n"
)

STUB = """#!/usr/bin/env python3
import glob, json, os, sys
argv = sys.argv[1:]
if "stream-json" in argv:
    # The trigger eval installs exactly one throwaway skill before calling us.
    root = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    hits = sorted(glob.glob(os.path.join(root, "skills", "zz-trigger-*")))
    name = os.path.basename(hits[-1]) if hits else "none"
    print(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Skill", "input": {"skill": name}}]}}))
else:
    print(json.dumps({
        "result": ANSWER_JSON,
        "duration_ms": 1234, "total_cost_usd": 0.01,
        "usage": {"input_tokens": 2, "output_tokens": 180,
                  "cache_read_input_tokens": 900, "cache_creation_input_tokens": 0}}))
""".replace("ANSWER_JSON", json.dumps(ANSWER))


def install_stub(bin_dir: Path) -> str:
    """Write the fake CLI and return the CLAUDE_BIN command that starts it.

    The command names the interpreter explicitly, so no shim, no executable bit
    and no PATHEXT entry is involved and one line works on every platform. It has
    to be CLAUDE_BIN rather than a PATH prefix: on Windows subprocess resolves a
    bare name through the parent process environment, so prepending a directory
    to the child PATH redirects nothing.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    target = bin_dir / "claude_stub.py"
    target.write_text(STUB, encoding="utf-8")
    return subprocess.list2cmdline([sys.executable, str(target)])


def run(cmd, env, cwd) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, env=env, cwd=str(cwd), capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=300)


def main() -> int:
    # A Windows console defaults to a legacy code page and raises on a byte it
    # cannot map. Captured subprocess output is arbitrary, so make the terminal
    # safe before printing any of it.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    print(f"platform: {platform.system()} {platform.release()} | python {platform.python_version()}")
    tmp = Path(tempfile.mkdtemp(prefix="prompt-ab-portability-"))
    failures = []

    def check(label, ok, detail=""):
        print(f"  {'OK  ' if ok else 'FAIL'} {label}")
        if not ok:
            failures.append(label)
            if detail:
                print("       " + detail.strip().replace("\n", "\n       ")[:600])

    try:
        bin_dir, work, home = tmp / "bin", tmp / "wd", tmp / "home"
        skills = home / ".claude" / "skills"
        skills.mkdir(parents=True, exist_ok=True)

        env = dict(os.environ)
        env["CLAUDE_BIN"] = install_stub(bin_dir)
        # Send the throwaway skill to a scratch config so a test run can never
        # touch the real one.
        env["CLAUDE_CONFIG_DIR"] = str(home / ".claude")

        ab = [sys.executable, str(REPO / "scripts" / "ab.py"), "--workdir", str(work)]

        r = run(ab + ["report"], env, REPO)
        check("workdir skeleton", (work / "task.md").exists() and (work / "variants").is_dir(), r.stderr)

        (work / "article.md").write_text("A body of input text about distributed systems.\n", encoding="utf-8")
        (work / "variants" / "v1.md").write_text("# Terse\n\n- Answer in under 200 words.\n", encoding="utf-8")

        r = run(ab + ["run", "v1", "--ref-repeat", "2", "--repeat", "1"], env, REPO)
        check("run through subprocess", r.returncode == 0 and "| v1 |" in r.stdout, r.stdout + r.stderr)
        check("runs persisted", len(list((work / "runs" / "baseline").glob("*.json"))) == 2)

        html = (work / "report.html").read_text(encoding="utf-8") if (work / "report.html").exists() else ""
        for label, ok in [
            ("html: table", "<thead><tr>" in html),
            ("html: cards", html.count('class="card" data-arm=') >= 2),
            ("html: sticky header", html.count('class="hd"') >= 2),
            ("html: task panel", "Task and input" in html),
            ("html: prompt diff", "System prompt diff" in html),
            ("html: arm filter", "armfilter" in html and "chip act" in html),
            ("html: utf-8", "charset=utf-8" in html),
        ]:
            check(label, ok)

        r = run(ab + ["diff", "baseline", "v1"], env, REPO)
        check("diff table", r.returncode == 0 and "| baseline |" in r.stdout, r.stdout + r.stderr)

        tset = tmp / "trigger-set.json"
        tset.write_text(json.dumps([{"query": "A/B this prompt", "should_trigger": True}]), encoding="utf-8")
        r = run([sys.executable, str(REPO / "evals" / "trigger_eval.py"),
                 "--skill-path", str(REPO), "--eval-set", str(tset), "--runs", "1"], env, REPO)
        check("trigger eval", r.returncode == 0 and "fired=100%" in r.stdout, r.stdout + r.stderr)
        leftovers = list(skills.glob("zz-trigger-*"))
        check("trigger eval cleans up after itself", not leftovers, str(leftovers))

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\nPASS" if not failures else "\nFAIL: " + ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
