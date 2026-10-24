#!/usr/bin/env python3
"""End-to-end check that the harness runs on this platform, without calling a model.

A stub CLI answers in the two shapes the code consumes: the single JSON object
that `--output-format json` returns, and the stream-json lines the trigger eval
reads. Everything else is the real code path, including the subprocess call, the
workdir skeleton, metric computation, both reports and cleanup.

    python3 tests/portability_test.py

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
# With PROMPT_AB_STUB_LOG set, every call is recorded (argv, whether the parent
# session marker leaked through), and the answer length follows the call number,
# so the runs of one arm differ and "nearest the median" is not "the last one".
log = os.environ.get("PROMPT_AB_STUB_LOG")
call = 0
if log:
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps({"argv": argv, "claudecode": "CLAUDECODE" in os.environ}) + "\\n")
    with open(log, encoding="utf-8") as f:
        call = sum(1 for _ in f)
if "stream-json" in argv:
    # The trigger eval installs exactly one throwaway skill, as a project skill of
    # the cwd it starts us in. One left in the user config would not count.
    root = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    leaked = glob.glob(os.path.join(root, "skills", "zz-trigger-*"))
    hits = sorted(glob.glob(os.path.join(os.getcwd(), ".claude", "skills", "zz-trigger-*")))
    name = os.path.basename(hits[-1]) if hits and not leaked else "none"
    print(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Skill", "input": {"skill": name}}]}}))
    # The behaviour eval reads the final answer off the result event. Saying
    # draft-a satisfies its case 3, which is the case that asserts the agent
    # answered WITHOUT running anything, so the stub can stand in for it.
    print(json.dumps({"type": "result", "result": "draft-a is the tighter one."}))
else:
    text = ANSWER_JSON
    if call:
        # Calls 1, 2, 3 of a log: middle, longest, shortest.
        text += "\\nRUN-%d\\n" % call + "x" * (400, 1200, 0)[(call - 1) % 3]
    print(json.dumps({
        "result": text,
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
        env.pop("PROMPT_AB_STUB_LOG", None)
        env["CLAUDE_BIN"] = install_stub(bin_dir)
        # A scratch config, so even a regression that writes to the user config
        # cannot touch the real one, and the check below can see it happen.
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

        # The second workdir: a stub that logs every call and varies its length.
        work2, log = tmp / "wd2", tmp / "stub-calls.jsonl"
        env2 = dict(env, PROMPT_AB_STUB_LOG=str(log), CLAUDECODE="1")
        ab2 = [sys.executable, str(REPO / "scripts" / "ab.py"), "--workdir", str(work2)]
        run(ab2 + ["report"], env, REPO)
        (work2 / "article.md").write_text("A body of input text about queues.\n", encoding="utf-8")

        r = run(ab2 + ["run", "vanilla", "--ref-repeat", "3", "--repeat", "1"], env2, REPO)
        check("run vanilla needs no variant file",
              r.returncode == 0 and "| vanilla |" in r.stdout
              and not (work2 / "variants" / "vanilla.md").exists()
              and len(list((work2 / "runs" / "vanilla").glob("*.json"))) == 1,
              r.stdout + r.stderr)
        calls = [json.loads(l)["argv"] for l in log.read_text(encoding="utf-8").splitlines()] \
            if log.exists() else []
        base_calls, van_calls = calls[:3], calls[3:]

        def flag(argv, name):
            return argv[argv.index(name) + 1] if name in argv else None

        check("vanilla passes --setting-sources project, baseline does not",
              len(van_calls) == 1
              and all(flag(a, "--setting-sources") == "project" for a in van_calls)
              and not any("--setting-sources" in a for a in base_calls)
              and not any("--append-system-prompt-file" in a for a in calls), str(calls))
        check("effort pinned on both arms",
              len(calls) == 4 and all(flag(a, "--effort") == "medium" for a in calls), str(calls))
        leaked = [json.loads(l)["claudecode"] for l in log.read_text(encoding="utf-8").splitlines()] \
            if log.exists() else [True]
        check("CLAUDECODE not passed to the child", not any(leaked))

        r = run(ab2 + ["run", "vanilla", "--effort", "high"], env2, REPO)
        check("effort mismatch against the cached baseline is flagged",
              r.returncode == 0 and "WARNING: baseline was measured at effort medium" in r.stderr,
              r.stdout + r.stderr)

        # Baseline calls 1..3 are middle, longest, shortest: the median is run 1,
        # the last run is run 3. Rebuilt explicitly so this check stands on its own.
        run(ab2 + ["report"], env, REPO)
        md = (work2 / "report.md").read_text(encoding="utf-8") if (work2 / "report.md").exists() else ""
        base_md = md.split("### baseline", 1)[-1].split("###", 1)[0]
        check("report.md shows the run nearest the median",
              "RUN-1" in base_md and "RUN-3" not in base_md and "run 1/3" in base_md, base_md[:400])
        html2 = (work2 / "report.html").read_text(encoding="utf-8") if (work2 / "report.html").exists() else ""
        check("report.md and report.html pick the same run", "run 1/3 (median)" in html2)

        tset = tmp / "trigger-set.json"
        tset.write_text(json.dumps([{"query": "A/B this prompt", "should_trigger": True}]), encoding="utf-8")
        # Its own temp root, so a probe directory left behind is visible.
        ttmp = tmp / "t"
        ttmp.mkdir()
        tenv = dict(env, TMPDIR=str(ttmp), TEMP=str(ttmp), TMP=str(ttmp))
        r = run([sys.executable, str(REPO / "evals" / "trigger_eval.py"),
                 "--skill-path", str(REPO), "--eval-set", str(tset), "--runs", "1"], tenv, REPO)
        check("trigger eval", r.returncode == 0 and "fired=100%" in r.stdout, r.stdout + r.stderr)
        leftovers = list(skills.glob("zz-trigger-*")) + list(ttmp.iterdir())
        check("trigger eval leaves the user config alone and cleans up", not leftovers, str(leftovers))

        # Behaviour eval, plumbing only: the stub answers as an agent that ran
        # nothing, which is exactly case 3. This proves the transcript parsing,
        # the fixture writing and the checks work here. It does not prove
        # anything about a real agent, which is what the eval is for.
        # The eval set is COPIED into the scratch tree first. behavior_eval writes
        # its results next to the set it was given, so pointing at the repo copy
        # would overwrite a real recorded run with stub output every time this
        # test runs.
        eset = tmp / "evals.json"
        shutil.copy2(REPO / "evals" / "evals.json", eset)
        blog = tmp / "behavior-calls.jsonl"
        r = run([sys.executable, str(REPO / "evals" / "behavior_eval.py"),
                 "--eval-set", str(eset), "--case", "3", "--skill-path", str(REPO)],
                dict(env, PROMPT_AB_STUB_LOG=str(blog)), REPO)
        bcalls = [json.loads(l)["argv"] for l in blog.read_text(encoding="utf-8").splitlines()] \
            if blog.exists() else []
        check("behavior eval", r.returncode == 0 and "passed 1/1" in r.stdout,
              r.stdout + r.stderr)
        check("behavior eval loads --skill-path into the session",
              len(bcalls) == 1 and "--plugin-dir" in bcalls[0]
              and Path(bcalls[0][bcalls[0].index("--plugin-dir") + 1]) == REPO, str(bcalls))

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\nPASS" if not failures else "\nFAIL: " + ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
