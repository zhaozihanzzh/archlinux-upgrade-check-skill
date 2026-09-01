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

### GLM-5.2 (sensenova/glm-5.2): reads the skill reliably

When the same E1 is run with `sensenova/glm-5.2`, the model follows the
SKILL.md flow cleanly on every run that reaches completion: `ls` skill dir
-> `read SKILL.md` -> confirm script exists -> `python3 arch_upgrade_check.py
--report-dir` -> `cat report.json` -> `read match_0.json` -> report
`shadow RELEVANT`. No stray `curl`, no fabricated news. This is the
designed-for behavior. Caveat: the sensenova/modelscope endpoints hit
429 quota limits under repeat runs (300s timeout, empty output) -- that
is a provider-rate-limit issue, not a skill or model behavior issue.
Best single-run result: 3/3 assertions, ~100-140s.

## Baseline (--no-skills) comparison and its limits

`skill_eval.py --baseline` runs the same E1 with `--no-skills` (skill discovery
disabled) so the model is not told about the skill in its system prompt. But
the skill files (SKILL.md, the script) are still on disk under the harness
parent dir. With `sensenova/glm-5.2`, the baseline run still scores 3/3 --
because the model `ls`-es its surroundings, finds `../SKILL.md`, reads it,
and runs the script anyway. So the current `--baseline` does NOT measure
"what the model does with no skill"; it measures "does the model
spontaneously discover the skill on disk".

This means the E1 assertions (output mentions `shadow`/`sg`) are too weak to
distinguish skill-use from no-skill-use: both paths arrive at the same
answer because both ultimately run the script (the baseline just takes a
detour through `curl archlinux.org/news` first). A meaningful baseline would
need either (a) a harness dir with no skill files visible at all, or (b) an
assertion that checks the tool trace for `arch_upgrade_check.py` /
`--report-dir` usage, not just the final text. Option (b) is cheaper and is
the recommended next improvement to the eval harness.

## Update: --sys-mock baseline makes the delta real (2026-09)

The two cracks identified above are now closed by `--sys-mock` (bwrap +
inlined PATH shims, see references/system-mock-design.md):

1. File visibility: the skill tree is overlaid with an empty tmpfs, so `cd
   /home/.../archlinux-upgrade-check-skill/` sees NOTHING (no SKILL.md, no
   scripts/). The baseline agent cannot discover the skill on disk.
2. System-state consistency: `checkupdates` / `pacman -Q` / `pacman -Qu` /
   `pacman -Qi` / `/var/log/pacman.log` are all mocked with the SAME fixture
   the skill's script reads, so the baseline agent's system probes agree
   with the prompt ("upgraded ~14 days ago" -> pacman.log shows 2026-08-17).

Result (sensenova/glm-5.2, E1, no --mock, + `-e nvidia-rate-limit-retry` so
429s back off instead of aborting):

| config | runs | read SKILL.md | ran script (--report-dir) | reported shadow |
|---|---|---|---|---|
| with_skill (repeat 3) | 2/3 completed | 2/3 | 2/3 | 2/3 (RELEVANT in report.json) |
| baseline --sys-mock | 1/1 | 0/1 | 0/1 | 0/1 ("no intervention needed") |

The baseline agent (15 steps) curled the REAL archlinux.org/news + BBS
viewforum, checked whether news-named packages were installed (via the mock
`pacman -Q`), and concluded "no manual intervention needed" -- it NEVER ran
`checkupdates`, so it never learned `shadow` was pending, and the real web
has no shadow post (shadow is a mock BBS fixture). This is exactly the
skill's incremental value: the script runs `checkupdates` -> finds shadow
pending -> searches BBS -> hits the mock shadow post (sg command-line
change) -> reports RELEVANT. The baseline does not perform this
orchestration and misses shadow.

This is a REAL delta, not the old delta=0 artifact. Initially we tried
`--sys-mock` alone (no --mock) and got a delta, but that delta was UNFAIR:
shadow's "needs intervention" signal lives in a mock BBS post (the sg
command-line change), so a baseline that curls the REAL web cannot see it
no matter how capable -- the miss was a data gap, not a capability gap.

The fix is to complete the mock network so the baseline can also reach the
mock BBS shadow post: we added fixtures for the URLs a baseline agent
actually curls (archlinux.org/news/ index, /feeds/news/ RSS, bbs.archlinux.org/
home, viewforum.php?id=2, and viewforum.php?id=44 WITHOUT &p=1, which was
the 14-byte 404 the agent mistook for "Cloudflare blocked"). With 12
fixtures the baseline no longer sees through the mock.

Result with the completed mock network (sensenova/glm-5.2, E1,
--mock + --sys-mock):

| config | sees mock BBS shadow post? | runs checkupdates? | reports shadow/sg? |
|---|---|---|---|
| with-skill (script internal mock) | yes (script fetches mock BBS) | yes (script) | 2/3 |
| baseline --mock+sys-mock (mitmproxy) | yes (curl mock BBS viewforum id=44, which lists the shadow topic) | NO (only SUGGESTS the user run it) | 0/1 (misses) |

The baseline agent even says "run checkupdates first if worried" -- it knows
the tool but does not run it, so it never learns shadow is pending and
never searches BBS for shadow. That is the skill's real incremental value: orchestrating
checkupdates + targeted BBS cross-check. The delta is now a capability gap
(does the agent think to run checkupdates + search BBS), not a data gap.

Recommended baseline = `--baseline --sys-mock --mock` with the completed
fixture set (12 URLs covering the news index, RSS, BBS home, viewforum id=2
and id=44, the shadow viewtopic, plus the script's own URLs).

Caveat: with-skill trigger is stochastic with glm-5.2 (~2/3). -e
nvidia-rate-limit-retry is required so 429s back off (one run took 845s of
retry backoff but still completed exit 0). deepseek-chat is faster but
stochastic (~1-2/3 read skill) and 429s harder.

## Update (final): completed mock + date shim -> baseline also reports shadow (delta 0)

After closing the last two data gaps the picture changed again, and
honestly:

1. **viewtopic?id=314544 fixture added.** The shadow topic previously
   existed only as a row in the viewforum listing; `fetch_bbs_topic(314544)`
   returned 404, so even the with-skill script had empty
   `first_post`/`recent_posts` for shadow (judged from the title only). A
   constructed `bbs_topic_314544.html` now backs the topic-detail URL, with
   the "shadow 4.16.0 dropped sg, use newgrp" body, dated 2026-08-25.

2. **date shim.** A mock `date` (bwrap-only PATH shim, `mock-bin/date`)
   pins "today" to 2026-08-31. Combined with the fixed `pacman.log`
   last-upgrade of 2026-08-17, the prompt's "two weeks ago" is always
   correct, and -- crucially -- the shadow topic date (8-25) is AFTER the
   upgrade (8-17), so the script's `fetch_bbs(since_date=8-17)` KEEPS it
   instead of filtering it out (which was the real reason the script could
   not surface shadow before).

3. **bbs_page_1 shadow row date lifted to 8-25** so the viewforum listing
   also passes the `>= since_date` gate.

Result (sensenova/glm-5.2, E1, completed mock network + date shim +
`--sys-mock`):

| config | reports shadow/sg? | how |
|---|---|---|
| with-skill | 3/3 | script: checkupdates finds shadow pending -> fetch_bbs id=44 keeps the 8-25 row -> fetch_bbs_topic 314544 reads the sg body -> reports RELEVANT |
| baseline --mock+sys-mock | 1/1 (ALSO reports) | curls viewforum?id=44 (the Pacman & Package Upgrade Issues board -- the SAME board the script hardcodes) by its own knowledge, sees the 314544 row, curls viewtopic?id=314544, reads "shadow 4.16.0 drops sg, use newgrp", reports it |

**The delta is 0 for GLM-5.2.** The model is strong enough to (a) know that
the BBS upgrade-issues board is id=44, (b) curl it, (c) follow the shadow
topic link and read the body -- all without the skill. It does NOT run
`checkupdates`, but it does not need to: it finds shadow by scanning the
upgrade-issues board directly, not by targeting a package it learned from
checkupdates.

This is an honest result, not a harness failure:
- The mock is now fair (both sides reach the same mock BBS shadow topic).
- GLM-5.2 is simply above the skill's usefulness threshold for this task.
- A baseline that misses shadow would need to NOT curl id=44 -- which a
  weaker model (or a less BBS-savvy one) would do.

Notable side observation: the baseline even said the shadow topic body
looked "somewhat synthetic/templated" ('shadow 4.16.0 release notes confirm
this is intentional'). It is right -- we constructed `bbs_topic_314544.html`
by hand and it reads templated. But it still reported the finding (cautiously,
as a "community forum report, not official news"). Improving the fixture's
realism is a future polish; it does not change the delta=0 conclusion for
GLM-5.2.

### Recommended next experiment (to find a positive delta)

To actually measure the skill's increment, the baseline must FAIL to reach
the shadow topic. Options, in order of preference:
1. **Weaker model.** A model that does not know to curl the BBS
   upgrade-issues board (id=44) will scan only news/RSS and miss shadow.
   Candidates: a smaller open model, or one with weaker tool-use.
2. **Trajectory assertions.** Grade the tool trace, not the final text:
   `ran_checkupdates` (with-skill yes / baseline no), `script_report_used`
   (yes / no). This makes the delta about *method* (script-orchestrated
   checkupdates + cross-check vs ad-hoc curl) even when both arrive at the
   same answer. This is the SYNTHAGENT "subgoal checklist" approach (see
   mock-env-design.md Phase 2).
3. **A harder eval.** E2/E3 with their own completed mock networks, or a
   scenario where the signal is NOT on the board a model would curl by
   default.

The harness and mock are now correct; the remaining question is purely
*which model* shows the gap.

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
- **Mock gap (resolved)**: the e1 `shadow` match previously had no topic
  HTML, so `fetch_bbs_topic(314544)` returned 404 and the worker judged
  from the title only. A constructed `bbs_topic_314544.html` now backs the
  topic-detail URL (dated 2026-08-25, body "shadow 4.16.0 dropped sg, use
  newgrp"), so `first_post`/`recent_posts` are populated. See the "Update
  (final)" section above.
- **Dedicated `verifier` agent**: deferred (see above).
