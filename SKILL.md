---
name: prompt-ab
description: 'Use this skill whenever the user wants to know whether a change to a system prompt, CLAUDE.md, AGENTS.md or output style actually changed the model output, even if they do not ask for a measurement. Trigger on "A/B this prompt", "test my system prompt", "compare these CLAUDE.md versions", "does this rule actually work", "measure whether this prompt change did anything", "is this technique worth adopting", "prove this prompt helps", and on any request to adopt a prompt rule from an article, video or colleague. Also trigger when the user is about to edit CLAUDE.md and wonders whether it is worth it. It runs one fixed task through each prompt variant headlessly, counts what changed (visible-text tokens, reasoning tokens, tic phrases, em dashes, structure, reference codes), measures the model noise floor first so a random difference is never reported as an effect, and renders every answer side by side with a prompt diff. Do NOT use it to judge which answer is better written: counters cannot rank quality.'
---

# prompt-ab

A/B harness for prompt-layer changes. It answers one question: **did this edit change the output, or am I looking at randomness?**

## The trap this exists to avoid

A single run proves nothing. In the session that produced this skill, the same variant on identical input returned 1026 and 1721 output tokens on two consecutive runs, a 68% spread. The first run alone read as a 55% improvement. It was noise.

So the harness always measures a noise floor before it reports a difference.

## When to use

- Someone (an article, a video, a colleague) claims a prompt technique improves output. Test it before adopting it.
- You are about to add a rule to `CLAUDE.md` and want to know whether it does anything.
- You changed a prompt and want to check it did not break something that worked.

## When not to use

- **"Which output is better?"** needs a judge, not counters. Counters measure length, structure and banned phrases. They cannot rank quality. Reach for a judged harness instead.
- **Testing a skill rather than a prompt.** Different dimension, different tooling.

## Procedure

1. **Pick the workdir.** Default `./prompt-ab`, override with `--workdir`. The skeleton is created on first run.

2. **Write `task.md`.** The user prompt, with `{ARTICLE}` where the input text goes. Put the input in `article.md`.

   The task must exercise the rules being tested. A summarize-an-article task measures verbosity and nothing else; it will not tell you whether an alias, a decision-format rule, or a scope boundary works. Match the task to the rule.

3. **Write `variants/<name>.md`.** Each file is appended via `--append-system-prompt-file`, on top of whatever `CLAUDE.md` is already loaded. Variants add; they never replace.

4. **Run.** Baseline (no appended file) is computed once with repeats and cached. Each variant costs one call per iteration.

   ```bash
   python scripts/ab.py run v1 --ref-repeat 3 --repeat 1
   python scripts/ab.py run v2 --repeat 1
   python scripts/ab.py diff baseline v1 v2
   ```

5. **Read the noise floor first.** The `rozrzut` column is baseline min..max. Any variant inside that band is indistinguishable from randomness, no matter how good the percentage looks.

6. **Read the answers, not only the table.** `report.html` renders every variant side by side. The table says what changed; only the text says whether you want it.

## Gotchas, each one paid for

These are the reason this skill exists. The script is replaceable; this list is not.

1. **Pin `--effort` explicitly on every arm.** Otherwise you measure reasoning, not prompt. Measured: the same setup produced 1600 tokens of reasoning at `xhigh` against 384 at default, while emitting *less* visible text. If one arm inherits `settings.json` and another does not, the comparison is worthless.

2. **`--setting-sources project` is the only thing that cuts `CLAUDE.md`** (both global and project). Verified by probe: without it the agent answers yes to "do your instructions contain <word from CLAUDE.md>", with it, no. But it also drops `settings.json`, which silently changes effort. That is two variables, so a "vanilla" arm is not comparable to a "baseline" arm unless effort is pinned on both.

3. **`output_tokens` is not answer length.** It bundles reasoning. Split it: estimate visible-text tokens from the text itself and treat the remainder as reasoning. A variant can cut visible text while raising total tokens.

4. **Recompute metrics from the stored text, never read them from the stored JSON.** Then changing a metric definition reapplies to every historical run for free, instead of costing a re-run.

5. **The input text must not be about the thing being measured.** A tic counter run over an article that discusses those exact tics counts the topic, not the style. Discount quoted and backticked spans as well.

6. **Cache the baseline.** It does not change between iterations. Recompute it only when the task, the article, or the effort level changes, via `--force-base`.

7. **Negative rules are weak.** A `NEVER use X` rule sat loaded in context and was violated five times in one answer. Rewrite each prohibition as an instruction naming the substitute: not "do not use em dashes" but "where a dash is needed, use a comma, a colon, parentheses, or a plain hyphen". This is Anthropic's own guidance and it is measurable here.

8. **Tic lists do not transfer between models.** The phrases one model overuses are not the phrases another overuses. Harvest the list from that model's own output rather than copying someone else's.

9. **`total_cost_usd` is a valuation, not an invoice.** On a subscription these runs consume quota, priced at published API rates for reporting only. Say "quota" unless an API key is actually set.

10. **Skills are off by default, and that is not neutral.** Runs pass `--disable-slash-commands`, verified by probe: the agent answers "No" to "is skill X available", and "Yes" without the flag. That isolates the prompt, but it also means the baseline measured here is not the baseline you actually work in, because a skill that reshapes output never gets to run. Use `--with-skills` when the question is "what happens in real conditions" rather than "what does this prompt do".

11. **Report the run nearest the median, never the last one.** A card showing the last run contradicted its own table: one variant had a median of 379 tokens and a last run of 643, so the column read as longer than baseline when the median said 37% shorter. Pick the representative run and label which one it is.

12. **Reference codes must not leak into anything durable.** If a variant teaches the model to tag items `F1`/`R2`, keep those out of stored memory, notes, commits and PR bodies. They are conversation-scoped and become dangling references everywhere else.

## Commands

| Command | Effect |
|---|---|
| `run <variant>` | Runs baseline (cached) then the variant; writes both reports |
| `run <variant> --repeat N` | N iterations of the variant |
| `run <variant> --ref-repeat N` | N iterations of baseline; this sets the noise floor |
| `run <variant> --force-base` | Discards the cached baseline and recomputes |
| `run vanilla` | Reference arm with no `CLAUDE.md` at all, for diagnosis |
| `diff <a> <b> ...` | Table for the named arms only |
| `report [names...]` | Rebuilds `report.md` and `report.html` |
| `--workdir PATH` | Workdir; default `./prompt-ab` |
| `--effort LEVEL` | `low`/`medium`/`high`/`xhigh`/`max`, pinned on every arm |
| `--model NAME` | Default `opus` |

## Output

| Column | Meaning |
|---|---|
| `tekst tok` | Estimated tokens of visible answer text |
| `rozrzut` | Baseline min..max, the noise floor; read this before any delta |
| `vs baseline` | Change in visible text against the current setup |
| `myslenie` | Total output tokens minus visible text, so reasoning |
| `em dash`, `tiki` | Style counters; tics discount quoted spans |
| `ref kody` | Distinct `F1`/`D1`/`R1`-style codes emitted |

`report.html` carries the table plus every answer rendered side by side, in light and dark. Each column keeps its name **and its metrics** pinned while the answer scrolls, so a number is never read against the wrong variant.

It also records what produced the numbers, which is what makes the file readable a week later:

- One panel at the top holds the shared input: `task.md` in full and the head of `article.md` with its word count.
- Each column holds the exact system prompt behind it, with size in characters and tokens. Reference arms say so explicitly rather than showing nothing, so "no appended prompt" is distinguishable from "prompt missing".

`report.md` carries the same content with answers fenced as source.

## Isolation

Runs execute outside the project tree with `--strict-mcp-config` and `--disable-slash-commands`, so no MCP server and no skill contributes to the measurement. The loaded `CLAUDE.md` stays in place on every arm by design: the question is normally "does this addition beat what I already have", not "what would a bare model do".

## Requirements

Python 3.10+ and the `claude` CLI on PATH. No third-party packages.
