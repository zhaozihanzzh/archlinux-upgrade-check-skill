# Match Verification Design (subagent-based)

This document records the design for how the LLM verifies candidate matches
produced by `arch_upgrade_check.py`. It supersedes the inline / `--minimal`
approach described in earlier drafts.

## CRITICAL: SKILL.md frontmatter must parse cleanly

During debugging we discovered the real reason the skill wasn't triggering:
**pi's YAML frontmatter parser is sensitive to special characters in the
`description` field.** A single-line description containing backticks,
double-quotes, and `--` (e.g. `do NOT scrape archlinux.org -- ...`) caused pi
to fail to parse the frontmatter, so the skill never entered the
`available_skills` list the LLM sees in its system prompt. The LLM then had
no idea the skill existed and fell back to curling archlinux.org itself.

**Fix**: the `description` must be a clean YAML block. We use the `>` folded
scalar form (multi-line, plain ASCII, no backticks/quotes/dashes inside):

```yaml
description: >
  Arch Linux pre-upgrade safety check: scans official News and the BBS
  Pacman and Package Upgrade Issues forum before running pacman -Syu ...
```

Keep it plain ASCII, one folded block, no inline code spans. This is the
single most important lesson from debugging -- without it the skill is
invisible to the model regardless of how good the body is.

Verification: after switching to the folded form, `pi -p` lists the skill
in `available_skills` with its real path, and a real E1 prompt drives the
LLM to `read SKILL.md` -> run `arch_upgrade_check.py --report-dir` -> read
`report.json` -> read `match_0.json` -> verify and report. End to end.

Caveat (model-dependent): whether the LLM actually *reads* SKILL.md on a
given run is stochastic with `zhiyuan-ai/deepseek-chat`. On E1, repeat=3
shows ~1-2 of 3 runs read the skill and run the script; the other runs
shortcut to `bash`+`curl` despite the description saying not to. Disabling
web tools (`--no-extensions`) helps by removing `web_search`/`fetch_content`,
but the model can still `bash`-curl. A more compliant model (e.g. Claude)
would likely read reliably. This is a model-layer limitation, not a
skill-layer bug; the skill is correct and works when read.

## Testing harness (skill_eval.py)

`skill_eval.py` runs each eval with `pi -p` from a *harness dir*
(`<skill>/skill-test/` by default), which contains a `.pi/skills/<skill>`
symlink to the skill. With `--approve` + `--no-extensions`, pi discovers the
skill via project-local discovery (more reliable than `--skill <path>` on
the CLI) and the model has no web tools to shortcut with. `--harness-dir`
overrides the directory; `--repeat N` runs each eval N times to measure
the stochastic trigger rate.

## Background: why the old approach was wrong

The script outputs one JSON report. Each candidate match carries
`package_evidence` (where each package name was found) plus the full
`first_post` and `recent_posts` text. In real threads `recent_posts` can run
~16,000 characters, so a report with several matches is large.

The LLM's job in Step 3 is to **filter false positives** -- for each match,
decide whether each matched package is a *real* upgrade issue or an
*incidental* mention. This judgement needs the post text, but only for the
one match being judged.

Three problems with the old design:

1. **Inline (read whole report)** -- the entire report, including every
   match's full post text, floods the main conversation context.
2. **`--minimal` (truncate)** -- truncates `first_post` to 300 chars and
   `recent_posts` to 1000. On a 16,513-char `recent_posts` that loses 94%
   of the content, destroying the very context needed to judge false
   positives. Verification quality degrades.
3. **LLM unreliability (observed as N1)** -- when the main LLM reads a big
   report, it sometimes ignores the script's matches and chases other
   content it noticed (e.g. real news headlines embedded in the mock HTML),
   reporting things the script did not find.

The root issue: the old design forced a trade-off between *context size*
and *verification quality*. Subagents break that trade-off.

## Solution: per-match subagent verification

Each match is judged by a **subagent in an isolated context**. The main
context holds only a small summary plus the one-line verdicts it collects.

```
main LLM
  │
  ├── Step 2:  python3 arch_upgrade_check.py --report-dir <dir>
  │            writes:
  │              <dir>/report.json       (small summary, no post text, no packages_to_update)
  │              <dir>/match_0.json     (one match: full evidence + first_post + recent_posts, untruncated)
  │              <dir>/match_1.json
  │              ...
  │
  ├── Step 3:  read report.json  (small -- stays in main context)
  │            for each match k:
  │              spawn worker subagent -> "pkg: RELEVANT|NOT_RELEVANT|UNCERTAIN | reason"
  │              (the match's full post text lives only in the worker's isolated context)
  │
  └── Step 4:  present the verified findings to the user
```

### Why this is strictly better

| Old problem | Resolution |
|---|---|
| `packages_to_update` is useless for verification (Q1) | Removed from the report entirely. Verification uses `package_evidence`, not the full package list. |
| `--minimal` truncation harms verification (Q2) | `--minimal` removed. Match files keep full, untruncated text. |
| Big report floods main context | Main `report.json` is a slim summary. Full text lives in per-match files, loaded only by a subagent. |
| Main LLM chases wrong content (N1) | The main LLM never reads the big post text; the worker judges one match in isolation and returns a one-line verdict. |

## Subagent choice

Pi does not build in subagents (`docs/usage.md` notes this intentionally),
but an extension provides them. Available agents live in
`~/.pi/agent/agents/`: `planner`, `reviewer`, `scout`, `worker`.

We use **`worker`**:
- "General-purpose subagent ... isolated context window ... handle delegated
  tasks without polluting the main conversation" -- exactly the match-judgement role.
- Backed by `zhiyuan-ai/deepseek-reasoner` (strong reasoning for the
  RELEVANT / NOT_RELEVANT / UNCERTAIN call).

A dedicated `verifier` agent was considered (faster `deepseek-chat`, hardcoded
rules) but **deferred** -- `worker` is good enough and avoids maintaining an
extra agent file. If volume later makes `deepseek-reasoner` too slow, revisit.

### Worker task contract

The main LLM spawns one `worker` per match with a task like:

> Read `<dir>/match_k.json`. For each package in `matched_packages`, judge
> RELEVANT / NOT_RELEVANT / UNCERTAIN using the verification rules. Reply with
> exactly one line per package: `<pkg>: <VERDICT> | <reason>`.

The verification rules (title / first_post / recent_posts evidence sources,
base-match lower confidence, false-positive patterns) are stated in the task,
copied from SKILL.md Step 3. The worker returns only the verdict lines; the
full post text never enters the main context.

## Script changes (`arch_upgrade_check.py`)

1. **`--report-dir <dir>` (new)** -- write a slim `report.json` plus one
   `match_<k>.json` per match. This is the recommended output mode.
2. **`--report-file <path>` (kept)** -- single-file mode, retained for
   backward compat / pipes / humans. Writes the full report (matches with
   their post text) to one file.
3. **`packages_to_update` removed from output.** It is not used by
   verification; `packages_count` remains so the scale is still visible.
   (Humans who want the list run `checkupdates` directly.)
4. **`--minimal` removed.** Truncation conflicted with verification quality;
   sharding replaces its context-saving purpose.
5. **Bug fix (found during design):** `fetch_bbs_topic`'s `except` branch
   returned a mis-ordered tuple `("", None, 1, 0, False, False)` mapping to
   `first_post_date=1` (int, not datetime), `total_pages=0`, `recent_count=False`
   (bool, not int). Fixed to `("", None, None, 1, 0, False)` matching the
   function's return signature. This is what produced `recent_post_count: false`
   and `total_pages: 0` on matches whose topic fetch failed (e.g. the e1
   `shadow` match, which has no mock HTML).

## SKILL.md changes

- **Step 2** recommends `--report-dir`.
- **Step 3** makes subagent verification the primary path: for each match,
  spawn a `worker` with the verification rules and a pointer to
  `match_<k>.json`; collect one-line verdicts. Falls back to inline
  `read match_<k>.json` per match when no subagent is available. The
  verification-rules table is kept (it is the contract the worker follows).
- `--minimal` references removed.

## Trade-offs & open items

- **Cost/latency**: one worker spawn per match (parallelizable via the
  `subagent` tool's parallel mode). Acceptable for a one-time pre-upgrade
  check; matches are usually few.
- **Mock gap**: the e1 `shadow` match has no topic HTML in
  `evals/mock/e1/http/`, so its `first_post`/`recent_posts` are empty and
  the worker judges from the title evidence only. This is fine for the
  shadow case (title is decisive) but a richer mock would exercise the
  full flow better. Left as a future mock-data improvement.
- **Dedicated `verifier` agent**: deferred (see above).
