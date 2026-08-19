# prompt-ab

A Claude Code skill that measures whether an edit to your system prompt actually changed the output, or whether you are looking at randomness.

You read an article, watch a video, or have an idea. You add a rule to `CLAUDE.md`. The next answer looks better. Did the rule do that?

Usually you cannot tell. The same prompt on identical input can vary by 68% run to run. That is not a hypothetical: it is the measurement that caused this skill to exist. A single run made a variant look 55% better than the baseline. Three runs showed the variant was slightly worse.

`prompt-ab` runs the same task through several prompt variants, counts what changed, establishes how noisy the model is before reporting any difference, and renders every answer side by side so you can read them rather than trust a number.

## What it is not

It does not tell you which answer is **better**. Counters measure length, structure and banned phrases; they cannot rank quality. For that you need a judge, and Anthropic already ships two things in that space: the `skill-creator` plugin, whose loop grades skill-on against skill-off outputs with assertions, and `claude plugin eval`, which runs eval cases against a plugin with a no-plugin baseline arm.

This is the cheap, deterministic counterpart. No judge, no containers, no scoring model. It answers a narrower question: *did the text change, by how much, and is that more than the noise?*

## Install

```bash
git clone https://github.com/MarcinSufa/claude-prompt-ab ~/.claude/skills/prompt-ab
```

Claude Code picks up `~/.claude/skills/*/SKILL.md` automatically. Requirements: Python 3.10+ and the `claude` CLI on `PATH`. No third-party packages.

## Quick start

```bash
# 1. create the workdir skeleton
python ~/.claude/skills/prompt-ab/scripts/ab.py --workdir ./prompt-ab report

# 2. put your input in prompt-ab/article.md, and a task in prompt-ab/task.md
#    task.md holds the user prompt with {ARTICLE} where the input goes

# 3. write the prompt you want to test
cp ~/.claude/skills/prompt-ab/templates/example-variant.md ./prompt-ab/variants/v1.md

# 4. measure: baseline three times to set the noise floor, v1 once
python ~/.claude/skills/prompt-ab/scripts/ab.py --workdir ./prompt-ab run v1 --ref-repeat 3

# 5. open ./prompt-ab/report.html
```

Or just ask Claude Code: *"A/B this system prompt change"*, and the skill triggers on its own.

## What you get

```
| arm      | n | text tok | spread   | vs baseline | thinking | em dash | tics | ref codes | time  |
|----------|--:|---------:|:--------:|------------:|---------:|--------:|-----:|----------:|------:|
| baseline | 3 |      600 | 526..603 |           - |      340 |       0 |    0 |         0 | 19.6s |
| v1       | 3 |      658 | 614..820 |        +10% |      308 |       0 |    0 |         0 | 20.4s |
| v2       | 3 |      379 | 350..643 |        -37% |      186 |       0 |    0 |         5 | 13.8s |
```

Read the `spread` column first. It is the baseline min..max, the noise floor. A variant inside that band is indistinguishable from randomness, no matter how good the percentage looks.

`report.html` carries the same table plus every answer rendered side by side, in light and dark:

- Each column pins its name **and its metrics** while the answer scrolls, so a number is never read against the wrong arm.
- One panel holds the shared input, so the file still makes sense a week later.
- Each column holds the exact system prompt behind it.
- A diff viewer compares any two prompt versions, line by line, so you can see which edit produced which number.

## Arms

| Arm | What it is |
|---|---|
| `baseline` | Your current setup: global and project `CLAUDE.md`, nothing appended. Measured once, cached, reused across iterations. |
| `<variant>` | The same setup plus `variants/<name>.md`, appended with `--append-system-prompt-file`. Variants add; they never replace. |
| `vanilla` | A bare agent with no `CLAUDE.md` at all. A diagnostic, not a candidate. Run it explicitly. |

Because the baseline is cached, iterating on a prompt costs one model call per round, not a full sweep.

## Commands

| Command | Effect |
|---|---|
| `run <variant>` | Baseline (cached) then the variant; writes both reports |
| `run <variant> --repeat N` | N iterations of the variant |
| `run <variant> --ref-repeat N` | N iterations of the baseline; this sets the noise floor |
| `run <variant> --force-base` | Discard the cached baseline and remeasure |
| `run <variant> --with-skills` | Leave skills enabled: measures real conditions, not the prompt alone |
| `run vanilla` | The no-`CLAUDE.md` diagnostic arm |
| `diff <a> <b> ...` | Table for the named arms only |
| `report [names...]` | Rebuild `report.md` and `report.html` |
| `--workdir PATH` | Working directory, default `./prompt-ab` |
| `--effort LEVEL` | `low`/`medium`/`high`/`xhigh`/`max`, pinned on every arm |
| `--model NAME` | Default `opus` |

## Metrics

| Column | Meaning |
|---|---|
| `text tok` | Estimated tokens of visible answer text |
| `spread` | Baseline min..max: the noise floor. Read this before any delta. |
| `vs baseline` | Change in visible text against your current setup |
| `thinking` | Total output tokens minus visible text, so reasoning |
| `em dash`, `tics` | Style counters. Tics discount quoted and backticked spans, because a model writing *about* a banned phrase is not using it. |
| `ref codes` | Distinct `F1`/`D1`/`R1`-style codes the answer emitted |

## What this taught us

The skill exists because a session set out to adopt a well-argued prompt technique and measured the opposite of the claim. Every item below cost a real round of work.

1. **Pin `--effort` on every arm.** One setup produced 1600 tokens of reasoning at `xhigh` against 384 at the default, while emitting *less* visible text. If arms differ in effort, you are measuring reasoning and calling it style.
2. **Cutting `CLAUDE.md` also cuts `settings.json`.** `--setting-sources project` removes both, so a "vanilla" arm silently differs from "baseline" in two variables, not one. That comparison explains nothing.
3. **`output_tokens` is not answer length.** It bundles reasoning. A variant can cut visible text while raising the total.
4. **Recompute metrics from the stored text.** Never read them back from the stored JSON, and redefining a metric reapplies to every historical run for free.
5. **The input must not be about the thing you are measuring.** A tic counter run over an article discussing those tics counts the topic, not the style.
6. **Negative rules are weak.** A `NEVER use X` rule sat loaded in context and was violated five times inside one answer. Rewrite each prohibition as an instruction naming the substitute. Then check the file itself: an instruction file that demonstrates the behaviour it forbids is teaching by example, and example beats instruction.
7. **Tic lists do not transfer between models.** Harvest them from that model's own output rather than copying someone else's list.
8. **Disabling skills is not neutral.** It isolates the prompt, but the baseline you measure is then not the baseline you work in. `--with-skills` exists for exactly that reason: in one measurement a variant gained 37% with skills off and 22% with them on.
9. **Report the run nearest the median, never the last one.** A report card showing the last run contradicted its own table: median 379, last run 643.
10. **A rule that fires half the time is not a rule.** A reference-point instruction fired in one run of three under one wording and two of three under another. Sample before believing.

## Evals

`evals/trigger_eval.py` measures how often this skill's description makes an agent actually reach for it. Current result on the bundled 12-case set, one probe per case:

```
passed 9/12
  trigger rate on positives:    50%
  false-fire rate on negatives:  0%
```

It never fires on the wrong request, and fires on half the right ones. The three misses are the vaguest phrasings, the ones with no artifact in the working directory to compare.

```bash
python evals/trigger_eval.py --skill-path . --eval-set evals/trigger-set.json --runs 3
```

The script exists because the one shipped with `skill-creator` could not produce a non-zero number here. Three separate defects had to be cleared before any figure meant anything, and all three are the same class of mistake:

1. **Upstream:** it registers the candidate description as a slash command under `.claude/commands/`, then counts a trigger only on a `Skill` tool call. The model does not invoke slash commands that way, so the detector can never fire for what the script creates.
2. **Ours, first attempt:** the description was written into the throwaway skill as a bare YAML value. Real descriptions carry quotes and colons, the frontmatter failed to parse, and every probe silently measured the skill's own name instead.
3. **Ours, second attempt:** with the real skill installed alongside, the model preferred it over the throwaway copy, so counting only the throwaway name reported 0% while the agent was in fact reaching for the skill under test.

Each was caught by a positive control rather than by reading the code: give the harness something that cannot fail to trigger, and see whether the number moves. It did not, three times, and each stall named a different bug.

### Behaviour eval

`evals/evals.json` describes the behaviour a correct run should produce, and `evals/behavior_eval.py` executes it:

```bash
python evals/behavior_eval.py                  # all cases
python evals/behavior_eval.py --case 3         # one case
```

Each case runs one real agent session over the case prompt. The model calls made *inside* the harness go to a stub, so a case costs one session rather than a session plus a full measurement. Nothing is graded by a model: every check reads the transcript (which flags the agent passed, which skill it invoked) or the tree it left behind (how many runs landed under each arm, whether a variant file was written, whether any `CLAUDE.md` changed). Where a case's prose expectation has no matching check, it is documented but not measured, and the output says so by printing checks against expectations.

Result on Windows 11, opus, one run per case: **3/3**.

Two things had to be fixed before that number meant anything, and both were found by the agent behaving *better* than the eval assumed:

- The stub first returned one canned answer for every arm. The agent noticed six byte-identical runs, called the measurement fake and refused to report it, then overrode `CLAUDE_BIN` to reach the real binary. The stub now reads each arm's system prompt, shortens its answer when that prompt asks for brevity, and jitters length per call, so the baseline has a spread to interpret.
- The `provisional` expectation is conditional in prose (a single run per variant must be labelled provisional) and was coded unconditionally, which failed an agent that had repeated every arm five times. `provisional_if_single` now only demands the word when a single run is all there was.

A case may also carry a `harness_note`, appended to its prompt. It exists because two rules the agent correctly follows make a headless case unrunnable: it will not report numbers from a model it can tell is a fixture, and it stops at a cost gate waiting for a confirmation no headless run can give. Pass `--no-note` to drop it and pay for a real measurement instead.

## Platforms

Requires **Python 3.10+** (the code uses `X | None` annotations) and the `claude` CLI. No third-party packages.

`tests/portability_test.py` runs the whole harness against a stub CLI, so it exercises the real code path, subprocess call included, without spending a token:

| Platform | Python | Result | Where |
|---|---|---|---|
| Windows 11 | 3.14.0 | 14/14 pass | local |
| Linux 6.6 (WSL2, Ubuntu) | 3.12.3 | 14/14 pass | local |
| macOS (Darwin 25.5, arm64) | 3.10.11, 3.13.14 | 14/14 pass | CI |
| Ubuntu 24.04 | 3.10.20, 3.13.15 | 14/14 pass | CI |
| Windows Server 2025 | 3.10.11, 3.13.15 | 14/14 pass | CI |

macOS was untested for a while because no machine was available. It is now covered by `.github/workflows/portability.yml`, which runs the suite on all three operating systems at both ends of the supported Python range on every push. The suite needs no API key and no `claude` binary, which is what makes it runnable in CI at all.

```bash
python tests/portability_test.py
```

### CLAUDE_BIN

Set `CLAUDE_BIN` to point at a specific CLI, as a bare name, a path, or a full command:

```bash
CLAUDE_BIN="/opt/homebrew/bin/claude" python scripts/ab.py run v1
CLAUDE_BIN="npx claude" python scripts/ab.py run v1
```

This is not decoration. On Windows `subprocess` resolves a bare command name through the **parent** process environment, so prepending a directory to the child's `PATH` redirects nothing, and an explicit binary is the only reliable way to aim the harness at a different CLI. The portability test depends on it.

## Licence

MIT.
