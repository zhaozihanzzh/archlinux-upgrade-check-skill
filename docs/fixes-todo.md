# Fixes TODO List

This file records the review findings and fix items for the
`archlinux-upgrade-check-skill`, ordered by priority. Each item has: the
problem, evidence (line numbers / measured behavior), the fix, and status.

> Status legend: [ ] todo - # in progress - [x] done - (n/a) withdrawn

---

## P0 -- must fix (skill does not work as SKILL.md describes)

### F1. `--json --report-file` together does not write the report file
- **Status**: [x]
- **Evidence**: `scripts/arch_upgrade_check.py:851-855`
  ```python
  if args.json:
      print(json.dumps(result, ...))   # printed to stdout, then return
      return
  if args.report_file:                 # never reached
      ...
  ```
  Measured: after `--json --report-file /tmp/x.json` the file does not
  exist; the full JSON (including the huge `packages_to_update`) goes to
  stdout. SKILL.md:17 and README both tell the LLM to use this combo;
  SKILL.md:25/155 claims it is "recommended to reduce context" -- the
  opposite of reality.
- **Fix**: make `--report-file` and `--json` two mutually exclusive output
  modes. When `--report-file` is set: only write the file, do not print
  JSON to stdout; emit one line `Report written to <path>` to stderr (does
  not enter LLM context). Keep `--json` (stdout) for standalone / pipe use.
  See F11 (default combo `--report-file + --minimal`).
- **Test**: Layer 3 assertion -- after `--report-file`, stdout has no full
  JSON, the file exists and `json.load` succeeds.

### F2. SKILL.md hardcodes the literal `/path/to/skill`
- **Status**: [x]
- **Evidence**: `SKILL.md:16-17`
  ```bash
  cd /path/to/skill
  python3 scripts/arch_upgrade_check.py --json --report-file /tmp/...
  ```
  This is a placeholder. The LLM's CWD is the user's project dir, not the
  skill dir. When pi triggers the skill it exposes the real skill path to
  the LLM, but SKILL.md never tells the LLM to use that path.
- **Fix**: drop `cd /path/to/skill`, reference the skill's own location:
  ```bash
  python3 <skill-dir>/scripts/arch_upgrade_check.py --report-file /tmp/arch-upgrade-check.json
  ```
  plus a note that "`<skill-dir>` is the real path of this skill in your
  environment". (With F1: drop `--json`; with F11: add `--minimal`.)
- **Test**: Layer 4 verifies the LLM can locate and run the script (E1 not
  crashing is indirect proof).

---

## P1 -- strongly recommended (data correctness / safety)

### F3. BBS topic `replies` field always 0
- **Status**: [x]
- **Evidence**: `scripts/arch_upgrade_check.py:395`
  ```python
  "replies": int(re.search(r"<td>(\d+)</td>", row).group(1)) if re.search(...) else 0,
  ```
  Measured on `tests/fixtures/bbs_page_1.html`: normal topic rows have a
  `class` attr on the reply-count cell (`<td class="...">2</td>`); the regex
  `<td>(\d+)</td>` (no attributes allowed) never matches -> every
  non-sticky topic gets `replies=0`. The README example `"replies": 0` is
  exactly this bug's product.
- **Use check**: `replies` is only put into the match dict at `:697`;
  SKILL.md / README LLM judgement table (title/first_post/recent_posts +
  package_evidence) does NOT include it. The only consumer is the
  human-readable meta line. So it contributes nothing to LLM judgement and
  was measured always-wrong.
- **Fix (fix it correctly, keep it)**: use `<td[^>]*>(\d+)</td>` and take
  the value **by column position** (BBS row is fixed: col0=index,
  col1=replies, col2=views, col3=date), not "first numeric td". Fixed
  correctly it is a weak "topic liveness" signal (`recent_post_count`
  measures after since_date, a different semantic).
- **Test**: Layer 2 `test_bbs_page_1_parsing` adds an assertion: some known
  topic has `replies > 0` and is an int.

### F4. `checkupdates` failure treated as "system up to date"
- **Status**: [x]
- **Evidence**: `scripts/arch_upgrade_check.py:117-125`
  ```python
  if result.returncode != 0:
      if result.returncode == 1 and "::" not in result.stderr:
          return set()   # mis-judged "no updates"
      return set()       # error also returns empty
  ```
  Then `main()` prints "System is up to date!" and exits. For a pre-upgrade
  safety tool, "checkupdates not installed / timed out / bad mirror" being
  reported as "safe to upgrade" is dangerous.
- **Fix**: distinguish "no updates" (legitimate empty set, returncode
  signature) from "error" (return `None` or raise). `main()` on error
  prints a clear reason (e.g. "pacman-contrib not installed") and exits
  non-zero; never prints "up to date".
- **Test**: Layer 3 case -- mock `checkupdates` returns non-zero + stderr;
  assert script exit != 0 and stderr contains an error hint.

### F5. Non-Arch systems not detected
- **Status**: [x]
- **Evidence**: SKILL.md Step 1 says "If the system is not ArchLinux, stop
  and tell the user", but the script never checks (no `/etc/arch-release` /
  `os-release` check). On a non-Arch machine: pacman.log missing -> silent
  fallback to 90 days; checkupdates missing -> goes through F4's "up to
  date" branch -> outputs "ok". Fully violates the promise.
- **Fix**: add `assert_archlinux()` at the top of `main()` (check
  `/etc/arch-release` exists, or `os-release` has `ID=arch`). On failure
  print a clear error and exit non-zero. Skip this check in mock mode (test
  env has no `/etc/arch-release`).
- **Test**: Layer 3 case (skipped under mock; or force the check path via
  an env var).

### F6. `description` trigger words under-covered, not "pushy" enough
- **Status**: [x] (with caveat)
- **Evidence**: `SKILL.md` frontmatter description was one English sentence,
  not covering Chinese/English colloquial forms. skill-creator is explicit:
  description is the ONLY trigger mechanism; it must be pushy and cover
  many phrasings. Real users say "I'm about to pacman -Syu, check for
  pitfalls for me", "pre-upgrade check", "before updating, check
  news/forum for manual intervention", none of which were in the
  description.
- **Fix (done, but see CRITICAL below)**: expand the description to cover
  bilingual trigger phrasings. NOTE: the single most important discovery
  was that pi's YAML frontmatter parser is sensitive to special characters
  in `description` (backticks, double-quotes, `--`). A single-line
  description with those chars made pi fail to parse the frontmatter, so
  the skill never entered `available_skills` -- it was invisible. The fix
  is a clean `>` folded-scalar block, plain ASCII, no inline code spans /
  quotes / dashes. See `references/verification-design.md`.
- **Test**: optional -- skill-creator's description-optimization flow
  (`run_loop.py`). Lower priority than functional fixes.

---

## P2 -- eval and methodology improvements

### F7. Layer 4 assertions too weak, not discriminating
- **Status**: [x]
- **Evidence**: `evals/evals.json`
  - E1, E2 `skill_assertions` had only `no-crash` (exit_code==0) -- pi not
    crashing passes, with no check that the LLM did the right thing.
  - E3's `text_contains "90"` and `"pipewire"`: the user prompt itself
    contains "90" and "pipewire", so even an LLM that just restates the
    task hits them. Cannot distinguish "done correctly" from "did
    nothing".
- **Fix**: switched to real-behaviour assertions:
  - E1: output mentions the shadow topic `viewtopic.php?id=314544` and
    `sg` or `newgrp`
  - E2: detects `lookback_capped` and recommends `archive.archlinux.org`
  - E3: mentions the glibc-crash topic the script found (not just restating
    the prompt's pipewire). New `text_contains_any` assertion type.
- **Test**: re-run Layer 4 after the change; assertion failure should now
  separate good from bad skill.

### F8. Layer 4 has no baseline comparison
- **Status**: [x]
- **Evidence**: `scripts/skill_eval.py` only ran with-skill, no control.
  skill-creator methodology requires with/without comparison, otherwise
  the skill's incremental value cannot be judged.
- **Baseline definition** (confirmed with user):
  - "without skill" = SKILL.md NOT loaded, but the script files are still
    in the repo and can be discovered by the LLM.
  - Same prompt, same mock, `pi -p` WITHOUT `--skill`.
  - This surfaces SKILL.md's increment: without it the LLM grovels through
    `arch_upgrade_check.py`, may run the script but not understand
    verification rules / lookback_capped, may mis-report false positives --
    exactly what a baseline should expose.
  - (Not giving the script at all = testing raw LLM ability, a different
    experiment, not skill evaluation.)
- **Fix**: `skill_eval.py` gained `--baseline`: runs the same eval without
  `--skill`, results compared side by side. benchmark adds a with_skill vs
  baseline pass-rate delta. Later hardened with `--sys-mock` (see F8b
  below).
- **Test**: expect with-skill > baseline (especially after F7).
- **F8b (hardened baseline)**: the original `--baseline` left the skill
  files visible on disk; GLM-5.2 would `ls ..`, find `SKILL.md`, read it
  and run the script anyway -> delta=0 artifact. Fixed by `--sys-mock`
  (bwrap overlay hides the skill tree; mock `checkupdates`/`pacman`/`pacman.log`
  consistent with the prompt). See `references/system-mock-design.md` and
  `references/verification-design.md`.

### F9. Layer 4 runs each eval only once
- **Status**: [x]
- **Evidence**: `scripts/skill_eval.py` runs each eval once. LLM output is
  stochastic; single-run noise is high. skill-creator emphasizes 3 runs
  per query for the trigger rate in description optimization.
- **Fix**: `skill_eval.py` gained `--repeat N` (default 1, suggest 3); runs
  each eval N times and reports pass_rate mean +/- stddev. benchmark keeps
  each run's output plus aggregate stats.
- **Test**: re-run with N=3, observe variance.

---

## P3 -- minor / hygiene

### F10. `fetch_news` does not early-terminate by date
- **Status**: [x]
- **Evidence**: `scripts/arch_upgrade_check.py:324`
  ```python
  if oldest_date < since_date and not has_more:   # has_more makes it redundant -> no-op
      break
  if not has_more:
      break
  ```
  Should be "stop once this page's oldest article is before since_date", but
  the `and not has_more` defeats it, always paging to page 14. News only
  has 14 pages so the impact is small, but the logic does not match intent;
  `fetch_bbs`'s equivalent is correct -- this was an omission.
- **Fix**: drop `and not has_more`:
  ```python
  if oldest_date < since_date:
      break
  if not has_more:
      break
  ```
- **Test**: Layer 2 `test_fetch_news_stops_correctly` was a placeholder
  empty assertion; add a real termination test (mock multiple pages, assert
  only the necessary pages are fetched past since_date).

### F11. `--minimal` flag was dead code, not exposed in SKILL.md
- **Status**: [x] (then superseded -- see refactor below)
- **Evidence**: defined at `arch_upgrade_check.py:715`, handled at `:837`;
  repo-wide grep only the script references it; SKILL.md/evals/README/docs
  never mention it. The LLM would never add the flag.
- **Fix (activate, not delete) -- then revised**: initially exposed
  `--report-file + --minimal` in SKILL.md Step 2. On review, `--minimal`
  truncates `recent_posts` (real data up to 16513 chars) to 1000, losing
  ~94% of content and harming Step 3 false-positive judgement. So it was
  NOT made the default; docs explain the trade-off. Later `--minimal` was
  REMOVED entirely and replaced by `--report-dir` sharding (per-match
  files, no truncation) -- see "Refactor: per-match subagent verification"
  below.

### F12. No `.gitignore`
- **Status**: [x]
- **Evidence**: repo had no `.gitignore`. `tests/__pycache__/`,
  `scripts/__pycache__/`, `evals/output/` (see F14) could be `git add .`'d
  by accident.
- **Fix**: added `.gitignore`:
  ```
  __pycache__/
  *.pyc
  evals/output/
  /tmp/
  ```
- **Test**: `git status` clean.

### F13. `test_integration.py` fragile import style
- **Status**: [x]
- **Evidence**: `scripts/test_integration.py:331`
  ```python
  if __name__ == '__main__':
      from datetime import datetime, timezone
      main()
  ```
  Relies on module-level side effects so `main()` can use `datetime`. Move
  it into the function or change import order and it breaks.
- **Fix**: hoist `from datetime import ...` to the top with the other
  imports.
- **Test**: existing tests do not regress.

### F14. `evals/output/benchmark.json` was a stale local artifact, no refresh
- **Status**: [x]
- **Evidence** (verified, correcting earlier claim):
  - `git status --short evals/output/` -> `?? evals/output/` (UNtracked,
    not a committed old file; the earlier "committed" claim was wrong).
  - File mtime `2026-08-18`, internal timestamp the same -- a manual
    `--output-dir evals/output` run 8 days ago, never refreshed.
  - `test_integration.py` defaults to stdout; only manual `--output-dir
    evals/output` generates it. Leaving it misleads (readers think it is
    current).
- **Fix**:
  - (a) `.gitignore` ignores `evals/output/` (merged with F12)
  - (b) README/Makefile pin the generation command:
    `python3 scripts/test_integration.py --output-dir evals/output`, note
    "refresh manually when needed"
- **Test**: N/A.

### F15. SKILL.md "Script reference" listed test scaffolding in the layout
- **Status**: [x]
- **Evidence**: SKILL.md's directory tree listed `test_integration.py`,
  `skill_eval.py`. These are test scaffolding, not the upgrade-check flow.
  Listed in the main instructions the LLM reads, they may induce the LLM to
  run tests instead of doing the check.
- **Fix**: SKILL.md layout table keeps only the files the skill needs to
  run (SKILL.md, `arch_upgrade_check.py`); test scripts moved to
  README/testing-guide.
- **Test**: N/A.

### F16. SKILL.md Step 1 ambiguous wording
- **Status**: [x]
- **Evidence**: `SKILL.md:8-10` "Parse `/var/log/pacman.log`... and run
  `checkupdates`" reads like instructing the LLM to run those itself, but
  Step 2's script already does the same, so the LLM may run `checkupdates`
  twice.
- **Fix**: Step 1 changed to descriptive ("the script does the following"),
  not a to-do instruction.
- **Test**: N/A.

### F17. Private model id appeared in example text
- **Status**: [x]
- **Evidence**: verified, `opencode-go/deepseek-chat` is NOT in any code
  execution path -- `skill_eval.py:218` `--model` is a `required=True` CLI
  arg, value passed by the runner; `:119` just forwards it. The private id
  appeared only in human-facing example text: `README.md`,
  `docs/testing-guide.md`, `references/test-plan.md`, and `skill_eval.py`'s
  docstring + argparse `--help`. (No hardcoded logic, so no `.env`/key
  management needed -- that would be over-engineering.)
- **Fix**: docs desensitization + convention (no `.env`). All example
  `opencode-go/deepseek-chat` -> placeholder `<your-model>`. README adds a
  note: Layer 4 eval uses a locally-available model passed via `pi -p
  --model`. Real secrets (API keys) belong to pi config, not this repo.
- **Optional** (later): `--model` optional, falls back to
  `SKILL_EVAL_MODEL` env. No `.env` file, zero new deps.
- **Test**: `grep -rn 'deepseek\|opencode-go\|zhiyuan' .` (excl. `.git`)
  should be empty.

---

## Withdrawn (judged no fix needed after review)

### R1. Expand `_COMMON_BASE_BLACKLIST` (original issue 15)
- **Status**: (n/a) withdrawn
- **Verification**: ran the e1 package list x 7 fixtures; the only
  base-match trigger was `nvidia` (3 times, pending context check for
  true/false). The `fonts`/`tools`/`utils`/`gconf`/`gnome` I cited never
  appeared in the fixture corpus -- their packages are not in the e1 list,
  and the fixture corpus is small and unrepresentative. No evidence backs
  expanding the blacklist.
- **Conclusion**: withdrawn. YAGNI -- a bigger blacklist is more likely to
  miss real matches. If a future real Layer-4 run shows some word producing
  many false hits, add it based on data, not gut feeling.

---

## Suggested implementation order

1. P0: F1 -> F2 (let the skill basically work as described)
2. P1: F3 -> F4 -> F5 -> F6 (data correctness + triggering)
3. P3 hygiene can run in parallel with P1: F12 -> F14 -> F13 -> F15 -> F16
   -> F10
4. P2 eval: F7 -> F8 -> F9 (improve eval after the function is stable,
   better reflects real quality)
5. P3 last: F11 (activate `--minimal`, depends on F1) -- later superseded
   by the `--report-dir` refactor

Run Layers 1-3 after each change to confirm no regression.

---

## Implementation record

The fixes below are done, with measured verification.

### Done (P0)
- **F1** [x] `--report-file` now only writes the file, stdout is quiet,
  stderr one-line confirmation; `--json` still goes to stdout. Measured:
  file exists, stdout 0 bytes, `--minimal` drops `packages_to_update`.
- **F2** [x] SKILL.md drops `cd /path/to/skill`, uses `<skill-dir>`
  reference with a note on its meaning.

### Done (P1)
- **F3** [x] `replies` uses `<td class="tc2">` by column. Measured: 27
  topics no longer always 0 (17/18/20/9/1...). Layer 2 adds `replies>0`
  assertion, 104/104 pass.
- **F4** [x] `get_checkupdates` returns `None` on error; `main()` errors
  and exits 1, no longer mis-reports "up to date". Distinguishes exit 2
  (no updates) from non-zero (failure).
- **F5** [x] `_is_archlinux()` checks `/etc/arch-release` or `os-release`;
  non-Arch errors out. Skipped in mock mode. SKILL.md Step 1 further: when
  the script errors out, no report is produced, the LLM must stop, not
  fabricate results, and tell the user directly.
- **F6** [x] description expanded, BUT the critical part was the format
  fix (see F6 fix note + verification-design.md): a clean `>` folded
  scalar, plain ASCII, no backticks/quotes/dashes. Without that the skill
  is invisible regardless of wording. Description-optimization flow
  deferred.

### Done (P3)
- **F10** [x] `fetch_news` drops the redundant `and not has_more`,
  early-terminates by date.
- **F11** [x] -> then superseded. Initially `--minimal` was documented in
  SKILL.md/README but NOT made the default, because truncating
  `recent_posts` (up to 16513 chars) to 1000 loses ~94% and harms
  false-positive judgement. Later `--minimal` was REMOVED and replaced by
  `--report-dir` sharding (per-match files, no truncation) -- see refactor
  below.
- **F12** [x] added `.gitignore` (`__pycache__/`, `*.pyc`,
  `evals/output/`).
- **F13** [x] `test_integration.py`'s `from datetime import` hoisted to
  the top.
- **F14** [x] `.gitignore` ignores `evals/output/`; that dir is an
  untracked local artifact.
- **F15** [x] SKILL.md layout table keeps only run-needed files; test
  scaffolding moved to README.
- **F16** [x] SKILL.md Step 1 made descriptive (the script does it), not
  an instruction for the LLM to run commands itself.
- **F17** [x] private model id desensitized: `opencode-go/deepseek-chat` ->
  `<your-model>` (README/docs/skill_eval docstring & help); README adds a
  model-agnostic note. `grep` verified no residual.

### Done (P2 eval improvements)
- **F7** [x] Layer 4 assertions switched to real behaviour: E1 requires
  mentioning the shadow topic and sg/newgrp; E2 requires recommending
  archive.archlinux.org and warning against a direct -Syu; E3 requires
  mentioning the glibc-crash topic the script found (not restating the
  prompt's pipewire). New `text_contains_any` assertion type.
- **F8** [x] `skill_eval.py` gained `--baseline`: same prompt/mock but no
  `--skill`, to compare the skill's incremental value. Later hardened by
  `--sys-mock` (F8b) so the baseline cannot discover the skill on disk.
- **F9** [x] `skill_eval.py` gained `--repeat N`: multiple runs,
  mean +/- stddev, dampens LLM variance.

### New findings (to evaluate)
- **N1**: an earlier E1 with-skill and baseline both failed the shadow
  assertion. with-skill, the LLM seemingly scraped the mock news HTML's
  linux-firmware/Plasma content itself, not faithfully using the script's
  shadow report; baseline, the LLM even reported packages not in the mock
  (possible hallucination or bypassing PI_OFFLINE). This showed F7's
  assertions correctly surfaced a skill-behaviour problem (the LLM did
  not read report-file) -- worth a later iteration on whether the LLM
  really ran the `--report-file` flow. Not blocking this round.

### Refactor: per-match subagent verification (see verification-design.md)
The user raised two root questions: (1) why give the LLM the full
`packages_to_update`? (2) `--minimal` truncation is wrong, can a subagent
manage context? Verified and implemented:
- **Script**: new `--report-dir <dir>` sharded output -- slim
  `report.json` (summary + per-match pointer/`match_file`, NO
  `packages_to_update`, NO full `first_post`/`recent_posts`) + one
  `match_<k>.json` per match (full evidence, UNtruncated). REMOVED
  `--minimal` (truncation harms verification; sharding replaces it).
  REMOVED `packages_to_update` output (verification does not need it;
  `packages_count` stays).
- **Bug fix**: `fetch_bbs_topic`'s except branch returned a mis-ordered
  tuple (`("",None,1,0,False,False)` -> `first_post_date=1` /
  `total_pages=0` / `recent_count=False`), fixed to
  `("","",None,1,0,False)` matching the signature. This was the root cause
  of `recent_post_count: false` / `total_pages: 0`.
- **SKILL.md**: Step 2 recommends `--report-dir`; Step 3 makes per-match
  subagent verification the primary path -- for each match spawn a
  `worker` subagent that reads `match_<k>.json` and returns `pkg:
  RELEVANT|NOT_RELEVANT|UNCERTAIN | reason`; falls back to inline
  per-match read when no subagent is available. The verification-rules
  table stays as the worker's judgement contract.
- **Subagent choice**: use the existing `worker` agent (deepseek-reasoner,
  isolated context); no dedicated verifier agent (user decision, YAGNI).
- **Measured**: e1 end-to-end -- script shards -> worker subagent verifies
  `match_0.json` -> returns `shadow: RELEVANT | <reason>`; the main
  context only receives a one-line verdict, full post text never enters
  main context.

### Later work (mock fairness + delta measurement)

After the refactor, effort moved to making the with-skill vs baseline
delta *real* rather than a data-access artifact. The full story (industry
methodology survey, framework selection, transparent HTTP mock via
mitmproxy, system-layer mock via bwrap + PATH shims, the completed mock
network, date-shim pinning) is in:

- `references/mock-env-design.md` -- transparent HTTP mock (Phase 1)
- `references/system-mock-design.md` -- system-layer mock (Phase 4)
- `references/verification-design.md` -- the delta results and what they
  mean

Headline result (sensenova/glm-5.2, E1, completed mock network + date
shim + `--sys-mock`):

| config | reports shadow/sg? | how |
|---|---|---|
| with-skill | 3/3 | script: checkupdates + fetch_bbs id=44 + fetch_bbs_topic 314544 |
| baseline --mock+sys-mock | 1/1 (also reports) | curls viewforum?id=44 + viewtopic?id=314544 by itself |

The delta is **0** for GLM-5.2: the model is strong enough to curl the
right BBS board (Pacman & Package Upgrade Issues = id=44, the same board
the script hardcodes) and follow the shadow topic on its own, without the
skill. This is an honest result -- it does not mean the skill is
valueless, only that GLM-5.2 is above the skill's usefulness threshold on
this task. Measuring a real positive delta needs either a weaker model
(whose baseline would not curl id=44) or a harder task. Details and the
recommended next experiment are in `references/verification-design.md`.

### Verification
- Layer 1: 19/19 [x]
- Layer 2: 104/104 [x] (includes F3's new replies>0 assertion)
- Layer 3: 9/9 [x]
- Layer 4: F7/F8/F9 mechanism verified (with-skill 3/3 reports shadow;
  baseline 1/1 also reports shadow -> delta 0 for GLM-5.2). The mechanism
  is ready; the positive-delta experiment needs a weaker model.
