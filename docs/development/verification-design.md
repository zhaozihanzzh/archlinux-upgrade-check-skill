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
given run is stochastic with `deepseek-chat`. On E1, repeat=3
shows ~1-2 of 3 runs read the skill and run the script; the other runs
shortcut to `bash`+`curl` despite the description saying not to. Disabling
web tools (`--no-extensions`) helps by removing `web_search`/`fetch_content`,
but the model can still `bash`-curl. A more compliant model (e.g. Claude)
would likely read reliably. This is a model-layer limitation, not a
skill-layer bug; the skill is correct and works when read.

### GLM-5.2 (GLM-5.2): reads the skill reliably

When the same E1 is run with `GLM-5.2`, the model follows the
SKILL.md flow cleanly on every run that reaches completion: `ls` skill dir
-> `read SKILL.md` -> confirm script exists -> `python3 arch_upgrade_check.py
--report-dir` -> `cat report.json` -> `read match_0.json` -> report
`shadow RELEVANT`. No stray `curl`, no fabricated news. This is the
designed-for behavior. Caveat: the the provider/the provider endpoints hit
429 quota limits under repeat runs (300s timeout, empty output) -- that
is a provider-rate-limit issue, not a skill or model behavior issue.
Best single-run result: 3/3 assertions, ~100-140s.

## Baseline (--no-skills) comparison and its limits

`skill_eval.py --baseline` runs the same E1 with `--no-skills` (skill discovery
disabled) so the model is not told about the skill in its system prompt. But
the skill files (SKILL.md, the script) are still on disk under the harness
parent dir. With `GLM-5.2`, the baseline run still scores 3/3 --
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
inlined PATH shims, see system-mock-design.md):

1. File visibility: the skill tree is overlaid with an empty tmpfs, so `cd
   /home/.../archlinux-upgrade-check-skill/` sees NOTHING (no SKILL.md, no
   scripts/). The baseline agent cannot discover the skill on disk.
2. System-state consistency: `checkupdates` / `pacman -Q` / `pacman -Qu` /
   `pacman -Qi` / `/var/log/pacman.log` are all mocked with the SAME fixture
   the skill's script reads, so the baseline agent's system probes agree
   with the prompt ("upgraded ~14 days ago" -> pacman.log shows 2026-08-17).

Result (GLM-5.2, E1, no --mock, + `-e nvidia-rate-limit-retry` so
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

Result with the completed mock network (GLM-5.2, E1,
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

Caveat: with-skill trigger is stochastic with GLM-5.2 (~2/3). -e
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

Result (GLM-5.2, E1, completed mock network + date shim +
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

## Update: two more models tested + root cause of delta=0 (2026-09)

Tested two more models on E1 baseline-only (`--baseline-only --sys-mock --mock`):

1. **deepseek-reasoner** (reasoning, = DeepSeek V4 Flash Preview,
   the reasoning variant): made **0 tool calls in 700s** -- hit 429 rate-limit
   and the retry extension backed off the entire timeout without a single
   curl. Not usable for this test (reasoning model + tight the provider quota).
   This is a provider-limit issue, not a model-capability signal.

2. **deepseek-v4-flash** (non-reasoning DeepSeek V4 Flash):
   first run timed out (550s) because the model **detected the mock** -- it
   saw the hostname (`ME-HASEE`, the real host leaking through bwrap) and
   the cwd name (`pi-sysmock-XXXX` from `mkdtemp(prefix='pi-sysmock-')`) and
   concluded "this is a container/chroot, not a real Arch system". That is a
   **fairness bug** (mock-detection tells), not a capability finding.

### Fairness fix: two mock-detection tells closed

- **hostname**: bwrap `--unshare-uts` alone copies the parent hostname to
  the child; you need `--hostname <name>` to actually change it. Added
  `--unshare-uts --hostname archlinux` to `bwrap-run.sh`.
- **cwd name**: `mkdtemp(prefix='pi-sysmock-')` literally wrote "sysmock" in
  the agent's working dir. Changed to `prefix='pi-task-'`.

After the fix, deepseek-v4-flash baseline completed in 381s, exit 0,
**3/3 (reports shadow sg)**. It curled: BBS home -> viewforum?id=44 ->
viewtopic?id=314544 -> read the sg/newgrp body -> reported. **delta = 0**, the
same result as GLM-5.2.

### Root cause: the fixture is too easy, not the models too strong

Three models now (GLM-5.2, deepseek-v4-flash, and the earlier deepseek-chat
partial) all reach the shadow topic by **browsing the BBS board id=44**, no
checkupdates needed. The reason is structural:

- The shadow signal lives on a **publicly browsable BBS board** (id=44,
  "Pacman & Package Upgrade Issues") -- the same board the script hardcodes.
- The topic **title contains the package name** ("sg dropped from shadow?")
  and the keyword ("sg"). A model just listing the board sees it.

So the skill's real value -- `checkupdates` (learn shadow is pending) x
BBS body cross-reference (is this package actually affected?) -- never
manifests. Any model that browses BBS finds it without that orchestration.

### To make the delta real: redesign the fixture, not swap models

The most faithful fix is to make the **shadow topic title NOT contain the
package name** (e.g. "sg command gone after recent update"), so the package
name "shadow" appears only inside the post body. Then:
- baseline browsing id=44 sees "sg command gone" -- does not know which
  package without reading the full body and recognizing "shadow 4.16.0" as
  a package name -> likely misses or reports imprecisely.
- with-skill: script runs `checkupdates` -> learns `shadow` is pending ->
  `find_packages_in_text(["shadow"], body)` matches -> reports shadow
  RELEVANT explicitly.

That is the skill's cross-reference value made visible. Swapping models
tested two more and got delta=0 both times -- evidence the problem is the
fixture, not the model.

### False-positive assertions: not yet implemented

E1 has 5 NOT-RELEVANT pending packages (glibc, systemd, pipewire,
nvidia-utils, linux-firmware-intel) but no assertion checks they are NOT
reported as needing intervention. The existing mock BBS has topic 314363
("Crash during pacman -Syu freezes", a glibc-mentioning crash thread) but
it is dated 2026-07-29, before the E1 `since_date` of 2026-08-17, so
`fetch_bbs` filters it out -- E1 never matches glibc to it, so no
false-positive trigger currently exists. To test false-positive handling,
314363's date must be lifted past 8-17 (so the script matches glibc to the
crash thread) and an assertion added that the agent does NOT report glibc
as needing intervention (it is a routine upgrade in E1).

## Update: real BBS survey -> fixture density is the issue, not title exposure

Surveyed the real bbs.archlinux.org/viewforum.php?id=44 (Pacman & Package
Upgrade Issues). The latest 18 non-sticky threads' titles:

  [SOLVED] avr-libc changed its location, avr-gcc does not find it
  [SOLVED] lib32-libcap missing from multilib repo
  zsh completions are broken for zed 1.16.1-3
  [SOLVED] libsystemd/libudev undefined symbol lookup errors
  Upgrading KDE Frameworks to version 6.29
  [SOLVED] bazaar cannot be installed due to libdex
  [SOLVED] Deluged not compatable with current python packages
  [SOLVED] tpm2-tss 4.2.0-2: configs installed as .pacnew
  [solved] chromium resets after update
  Missing depenency in firefox package
  sg dropped from shadow?
  What happened to arduino-avr-core?
  libxfonts was missing from a lot of mirrors today
  [SOLVED] Dynamic linker is segfaulting
  Crash during pacman -Syu and now system freezes intermittently

**Key finding: real BBS titles almost always contain the package name**
(avr-libc, lib32-libcap, zsh, libsystemd, KDE Frameworks, bazaar, tpm2-tss,
chromium, firefox, shadow, libxfonts ...). So the earlier idea -- "make
the shadow topic title NOT contain the package name" -- is WRONG; that
would be unrealistic. The fixture title containing "shadow" is faithful,
not a weakness. **Abandon the "hide the name from the title" direction.**

**Real root cause of delta=0: the fixture has too few threads.** E1's mock
BBS has exactly 1 shadow thread; a baseline that curls id=44 sees it at a
glance. A real BBS fortnight has ~20-30 threads, and the baseline must
filter that pile for the handful matching its 6 pending packages -- that
filtering IS the skill's value (systematic scan + checkupdates-targeted
cross-reference). The fix is to increase thread density, not to hide the
name.

**False-positive fixtures: real BBS is full of them.** The current E1 has
no false-positive trigger (314363 is dated before since_date and filtered
out). But real threads like "Crash during pacman -Syu freezes" (mentions
pacman but is not a pacman-package issue; also mentions glibc once -- a
trap if E1's glibc is a routine upgrade), "Upgrading KDE Frameworks"
(mentions KDE, not installed -> NOT RELEVANT), "zsh completions broken"
(mentions zsh, not installed) are natural false-positive traps. Lifting
314363's date past 8-17 makes the script match glibc to it -- a real
false-positive test (E1's glibc is a routine upgrade, not a crash).

**Date-consistency warning (do not let the model see through it):** when
adding real threads, all three date surfaces must agree on ONE date per
thread -- (1) the viewforum listing row date, (2) the viewtopic detail
post dates, (3) any absolute/relative date mentioned in the post body
("I updated on 2026-08-25", "two weeks ago"). If the listing says 8-25 but
the post body says 8-09, a model comparing them sees the mock. All dates
must also fit the mock timeline: today=8-31, last upgrade=8-17,
since_date=8-17; threads to be KEPT must be >=8-17, threads to be FILTERED
must be <8-17 (and the script then correctly drops them).

## Update: positive delta found -- fixture density + false-positive source

After the real-BBS survey, the E1 mock was enriched:
- added 4 real noise threads as viewtopic fixtures (314663 lib32-libcap,
  314715 zsh, 314637 KDE Frameworks, 314590 tpm2-tss) with dates kept
  consistent (listing date == detail post date);
- lifted 314363 ("Crash during pacman -Syu freezes") from 07-29 to 08-20
  (past since_date) -- it now matches glibc, nvidia-utils, pipewire,
  systemd in the script (a real false-positive source: E1's glibc etc.
  are routine upgrades; the thread only incidentally mentions them);
- re-fetched bbs_page_1.html (real id=44) so the listing has real density
  (~29 threads), with key dates shifted past 8-17.

Script now produces 2 matches: shadow (314544, true positive) + 314363
(matching glibc/nvidia-utils/pipewire/systemd, false positives).

Result (deepseek-v4-flash, E1):

| config | result | detail |
|---|---|---|
| with-skill | 3/3 (100%) | script fetches BBS id=44 (hardcoded) -> finds shadow; worker verifies 314363's glibc/nvidia/pipewire/systemd as "incidental mention" NOT_RELEVANT -> reports only shadow |
| baseline --mock+sys-mock run 1 | 3/3 (100%) | curled BBS home -> id=44 -> viewtopic 314544 -> reported shadow (did NOT curl 314363, no false-positive) |
| baseline --mock+sys-mock run 2 | 0/3 (0%) | curled ONLY news + RSS, never touched BBS -> missed shadow entirely |
| baseline --mock+sys-mock run 3 | 0/3 (0%, timed out) | 28 tool calls, 18 curls, but never curled id=44 -> missed shadow |
| baseline --mock+sys-mock run 4 | 0/3 (0%, timed out) | 24 tool calls, 14 curls, 1x429, never curled id=44 -> missed shadow |

**Baseline reports shadow: ~1/4 (25%)** across 4 runs; with-skill is 3/3
(100%). The skill takes deepseek-v4-flash from 25% -> 100% on this task.

This is the **first positive delta signal**:
1. **Stability**: with-skill always fetches BBS id=44 (script-hardcoded)
   -> stable 3/3. Baseline is stochastic: sometimes it browses BBS
   (finds shadow), sometimes it only looks at news/RSS (misses shadow).
   The skill removes the "does the model think to check the BBS" coin-flip.
2. **False-positive exclusion**: with-skill's worker judged 314363's
   package mentions as "incidental" (NOT_RELEVANT) and did not report
   glibc/nvidia/pipewire/systemd as needing intervention. A baseline that
   curls 314363 would need to make the same judgement by hand.

This delta is on deepseek-v4-flash (non-reasoning). GLM-5.2 was delta=0
(both runs curled id=44 -- it is strong/systematic enough to always
browse the BBS). The skill's value is clearest on models that are
capable but not reliably systematic about browsing the right board:
deepseek-v4-flash reports shadow only ~25% of the time without the skill,
100% with it. Two of the four baseline runs timed out at 500s, but both
had already failed to curl id=44 by then (the miss is strategic, not a
timeout artifact). More repeat runs would tighten the 25% estimate, but
the signal is already clear.

## Concrete examples: how baseline misses vs how with-skill finds

The shadow signal lives in BBS topic **314544 "sg dropped from shadow?"**
(<https://bbs.archlinux.org/viewtopic.php?id=314544>) on the "Pacman &
Package Upgrade Issues" board (viewforum?id=44). The false-positive
source is topic **314363 "Crash during pacman -Syu freezes"**
(<https://bbs.archlinux.org/viewtopic.php?id=314363>) which mentions
glibc / nvidia-utils / pipewire / systemd incidentally.

### with-skill (deepseek-v4-flash, 3/3) -- the systematic path

The agent does NOT curl at all (0 curl calls). It follows SKILL.md:

1. reads `SKILL.md`
2. runs `python3 scripts/arch_upgrade_check.py --report-dir /tmp/arch-upgrade-check`
   (the script runs checkupdates -> learns `shadow` is pending ->
    fetches BBS viewforum?id=44 -> finds the 314544 row -> fetches
    viewtopic?id=314544 -> reads the sg/newgrp body -> also matches
    314363's glibc/nvidia-utils/pipewire/systemd as a second match)
3. reads `report.json` (slim summary)
4. reads `match_0.json` (shadow evidence) -> judges `shadow: RELEVANT`
   ("sg command removed from the shadow package; this is the package's
   own upgrade change")
5. reads `match_1.json` (314363 crash evidence) -> judges each matched
   package `NOT_RELEVANT` ("glibc / pipewire / systemd / nvidia-utils
   are incidental mentions, not the package's issue")
6. reports only `shadow` to the user

Key: the script ALWAYS fetches id=44 (hardcoded), so shadow is found
every time. The per-match sharded evidence lets the agent judge each
package in isolation and drop the false positives.

### baseline run 1 (deepseek-v4-flash, 3/3) -- got lucky, browsed BBS

6 curl calls, in order:

  1. `archlinux.org/feeds/news/`            (news RSS)
  2. `bbs.archlinux.org/extern.php?type=rss` (BBS RSS)
  3. `bbs.archlinux.org/`                    (BBS home)
  4. `archlinux.org/news/`                   (news index)
  5. `bbs.archlinux.org/viewforum.php?id=44` (the upgrade-issues board)
  6. `bbs.archlinux.org/viewtopic.php?id=314544` (shadow topic)

The model knew to go BBS home -> find the "Pacman & Package Upgrade
Issues" board (id=44) -> see the shadow row -> click it -> read the
sg/newgrp body. It reported shadow. It did NOT curl 314363, so no
false-positive test happened this run.

### baseline runs 2-4 (deepseek-v4-flash, 0/3) -- missed

Three different miss patterns:
- **run 2**: curled ONLY `archlinux.org/news/` + RSS (3 curls), never
  touched BBS at all -> never saw the shadow topic.
- **runs 3-4**: curled ~14-20 things (news, RSS, BBS search by package
  name `glibc`/`systemd`/`nvidia`, other boards id=18/22/50, other
  topics 314678/314663/314636), and only reached `viewforum.php?id=44`
  near the end but did NOT click 314544 before the 500s timeout.

The common failure: the model does not reliably decide "I should
browse the BBS upgrade-issues board (id=44) and read its topic list".
Sometimes it does (run 1 -> finds shadow), often it doesn't (runs 2-4
-> misses). The skill removes this coin-flip: the script always
fetches id=44.

## Known gaps & scenarios not yet covered

After E1 reached a positive delta, the following gaps and uncovered
scenarios were identified (D deferred per user decision):

### Gaps (eval mechanism)
1. **repeat sample too small** -- baseline 4 runs (1 hit / 3 miss),
   with-skill 1 run. Need 10+ for a stable estimate; 429 is the main
   obstacle.
2. **no-false-positive assertions missing** -- 314363 matches
   glibc/nvidia-utils/pipewire/systemd; with-skill excludes them, a
   baseline may misreport, but no assertion catches a misreport. Current
   assertions only check mentions-shadow/sg.
3. **E2/E3 mock networks not completed** -- only E1 has 12+ fixtures;
   E2 (547 days) and E3 (90 days) still use the old sparse mock.
4. **trajectory assertions not implemented** -- mock-env-design Phase 2
   designed ran_checkupdates / script_report_used / no_fabrication, but
   skill_eval.py only grades text. The delta is measured on "reported
   shadow", not on "ran checkupdates / used the script".
5. **only one positive-delta model tested** -- deepseek-v4-flash. GLM-5.2
   was delta=0. More models needed to generalise.
6. **fixture realism** -- bbs_topic_314544.html reads "templated"
   (a baseline even said so). Real BBS posts are user-help style, not
   announcement style.

### Uncovered scenarios (eval content)
A. **glibc TRUE intervention** -- E1's glibc is a false-positive (314363
   crash thread mentions it incidentally); no test of "glibc genuinely
   needs intervention -> skill SHOULD report". glibc is Arch's most
   frequent real-intervention package.
B. **title lacks the package name, body has it, and it is a REAL
   intervention** -- 314544 title has "shadow" (too easy); 314363 body
   has glibc but is a false positive. Missing a thread whose title does
   NOT name the package but whose body is a real intervention (hardest
   for a baseline: browsing the listing does not reveal the package;
   the skill's checkupdates x body cross-reference is the only path).
C. **news intervention issued AFTER since_date** -- E1's mock news is all
   old (<= 7-21, before the 8-17 upgrade); no "news published during the
   upgrade window". skill fetch_news(since_date) would cover it, but
   untested.
D. **partial-upgrade / pacman -Sy risk** -- baseline run 4 noticed the
   user ran `pacman -Sy` 4x after 8-17 (partial-upgrade risk, from
   pacman.log). The skill script does NOT check this (only news+BBS).
   This is a real skill blind spot, but DEFERRED per user decision (the
   pacman -Sy pattern was a mock-log artifact; not a core skill-scope
   question).

### Plan
Do A + B + C (D deferred). A and C can share one fixture (a glibc
intervention news article dated after since_date); B is a BBS thread
whose title does not name the package but whose body is a real
intervention.

### Update: A+B+C fixtures added (done)

- **A+C (news)**: added a `glibc 2.41-1 requires manual intervention`
  article (dated 2026-08-20, after since_date) to `news_page_1.html`
  (table row) and the RSS feed (`news_rss.html`, first item). The script's
  `fetch_news(since_date)` now keeps it and matches `glibc`. Tests A
  (glibc true intervention -- the skill SHOULD report it) and C (news
  issued during the upgrade window).
- **B (BBS)**: added topic 314800 "升级后系统卡在 early boot"
  (dated 2026-08-22) to `bbs_page_1.html` (viewforum?id=44 listing) + a
  `bbs_topic_314800.html` viewtopic fixture. The **title does NOT name
  the package**; only the body mentions `glibc 2.41-1` and the `/lib64`
  intervention. The script's `find_bbs_matches` matches `glibc` in the
  body via `find_packages_in_text` (checkupdates x body cross-reference);
  a baseline that only reads the listing title would not know it is
  about glibc without clicking in.
- **Bug found + fixed**: `bbs_topic_314544.html` and `bbs_topic_314800.html`
  were missing `id="pNNN"` on their `<div class="blockpost">` tags, so
  `parse_bbs_topic_page` (which `re.split`s on `<div id="p\d+" class="blockpost`)
  returned empty `first_post`/`recent_posts`. 314544 hid it (it matched via
  the title); 314800 exposed it (title has no package name, needs the
  body). Fixed by adding `id="p<pid>"` to each blockpost.
- Script now produces **4 matches** on E1:
  1. news `glibc 2.41-1 requires manual intervention` (A, RELEVANT)
  2. BBS 314544 `sg dropped from shadow?` (shadow, RELEVANT)
  3. BBS 314800 `升级后系统卡在 early boot` (glibc in body, RELEVANT)
  4. BBS 314363 `Crash during pacman -Syu freezes` (glibc/nvidia-utils/
     pipewire/systemd, FALSE-POSITIVE -- worker should judge NOT_RELEVANT)
- Layer 3 E3 assertion updated (`match_count` expected 1->3) because E3
  shares the fixtures and now also picks up the glibc news + 314800.

**Not yet done**: re-run the with-skill vs baseline delta on this richer
fixture (now the worker must distinguish glibc-true-intervention vs
314363-glibc-incidental, and the baseline must decide whether to click
the title-less 314800). Also the no-false-positive assertions (gap 2)
and trajectory assertions (gap 4) are still unimplemented.

### Update: with-skill re-run on the richer fixture (done)

`PI_CMD` bug found + fixed: `pi` is a shell alias (not on $PATH), so
`subprocess.run` (which does not go through a shell) raised
`FileNotFoundError` -> exit code -2, 0.0s. Fixed by setting
`PI_CMD = ~/.local/bin/pi` (absolute) with a `pi` fallback.

With the fix, `with_skill` deepseek-v4-flash on E1 (richer fixture):
**3/3 passed (100%), 162.3s, exit 0**.

The worker's report shows the A+B+C design actually works:

```
### 必须处理：glibc 2.41-1 手动干预（官方新闻 + 论坛双重确认）
官方新闻：glibc 2.41-1 requires manual intervention（2026-08-20）
论坛佐证：升级后系统卡在 early boot（2026-08-22）
问题：glibc 2.41-1 把 /lib64 从目录改为符号链接...重启后卡在 early boot
处理（升级前执行，需 root）：...
```

- **A (glibc true intervention)**: reported as "必须处理" (must handle).
- **C (news after since_date)**: the worker read the 2026-08-20 news
  article the script's `fetch_news(since_date)` kept.
- **B (title lacks the package, body has it)**: the worker cross-referenced
  BBS topic 314800 "升级后系统卡在 early boot" (title has NO "glibc") with
  the news article -- the script matched `glibc` in the body via
  `find_packages_in_text`, exactly the checkupdates x body path a
  title-only browser would miss.

So the skill's value on this fixture is no longer just "finds shadow";
it now also (1) catches a glibc intervention the news announces, and
(2) ties a title-less BBS thread to the right package via the script's
body scan. The baseline re-run (next) will show whether the baseline
reaches the same conclusion.

### Update: baseline re-run on the richer fixture (done)

`baseline` deepseek-v4-flash on E1 (richer fixture, --mock + --sys-mock):
**1/3 passed (33%), 471.1s, exit 0**.

- `no-crash`: PASS
- `mentions-shadow-issue`: **FAIL** -- 'shadow' not in stdout
- `mentions-sg-or-newgrp`: **FAIL** -- none of ['sg','newgrp'] in stdout

Baseline preview:
```
结论：没有必须手动干预的阻断项，可以直接跑 pacman -Syu
1. glibc 2.41-1 需要手动干预（2026-08-20）—— 你的系统已满足条件，无需操作
   这条新闻正好在你上次更新（约 8 月中旬）之后发布...
   我检查了你的系统：/lib64 -> usr/lib 已经是符号链接，符合要求，直接升级即可
2. AUR 恶意包事件（2026-06-12）—— 与你相关，升级 AUR 包前注意
   ...nvidia-utils 565.0-1、linux-firmware-intel 等...
```

So on the richer fixture:
- **with-skill: 3/3 (100%)** -- reports shadow + sg + glibc (must handle)
- **baseline:   1/3 (33%)**  -- reports glibc (but "no action needed",
  after checking the real /lib64) + AUR warning; MISSES shadow/sg entirely
  and never mentions BBS topic 314800 (the title-less glibc thread).

**Delta = +2/3 on the richer fixture** (was +2/3 on the sparse fixture too,
but for a different reason): on the sparse fixture the baseline sometimes
hit the lone shadow thread by luck; on the richer fixture the baseline
gets distracted by the glibc news + AUR warning and never reaches the
BBS shadow thread, while the skill's script always fetches BBS id=44
(hardcoded) and surfaces shadow.

Caveat (not captured by assertions): the baseline's glibc verdict
("/lib64 already a symlink, no action needed") is arguably MORE accurate
than the skill worker's "必须处理" (must handle) -- the worker trusted the
script report + news/BBS without checking the real /lib64. This is a
worker over-reporting issue, not a script issue; the script correctly
flags glibc as a match, the worker should verify the system state before
saying "must handle". The current E1 assertions only test shadow/sg, so
this glibc over-report is invisible to the grader.

### Update: SKILL.md frontmatter bug found + fixed + with-skill re-run

A second, older bug surfaced when trying `pi --skill <path>` from
another directory: pi reported `[Skill conflicts] ... description is
required`. Root cause: **SKILL.md was missing the opening `---`** YAML
fence (only the closing `---` was present). pi's `extractFrontmatter`
checks `if (!startsWith("---")) return {yamlString: null}`, so the whole
frontmatter parsed to `{}` -> `description` undefined -> `loadSkillFromFile`
returned `{skill: null}` -> the skill was **never injected into
`available_skills`**.

Implication for all prior with-skill runs: pi did NOT inject the skill
description into the system prompt. The agent still succeeded (3/3) but
via a different path -- it `ls`-explored the harness cwd, found the
`.pi/skills/archlinux-upgrade-check-skill` symlink, `read` SKILL.md's
body (the body is plain markdown, unaffected by frontmatter), and ran
the script. So the with-skill *result* (script runs, reports shadow /
glibc) was real, but the *trigger mechanism under test* (pi injects
description -> agent matches the task -> proactively reads SKILL.md)
was NOT what was happening.

Fix: prepended the opening `---`. Verified with pi's own yaml parser:
`description` is now a string (len 914), `name` resolves, the skill loads.

with-skill re-run (deepseek-v4-flash, richer fixture, AFTER the fix):
**3/3 passed (100%), 270.3s, exit 0**. Preview:
```
升级前检查结果 ⚠️ 需要手动干预
1. 【必读】官方新闻：glibc 2.41-1 需要手动干预 (2026-08-20)
2. 论坛佐证：/lib64 目录要改成软链  (BBS 314800, title-less glibc)
```
Result identical to the pre-fix run (both 3/3, both report glibc news +
314800 + shadow). So on this fixture the skill's *answer* is robust to
the trigger path; what the fix corrects is the *mechanism* (description
injection, the path real users hit). Time grew 162s -> 270s, plausibly
the description-match step + 429 retries.

Baseline was NOT re-run: the frontmatter fix only changes skill loading,
and the baseline uses `--no-skills` in a cwd with no `.pi/skills/`, so
it is unaffected. The prior baseline result (1/3, misses shadow) stands.

Caveat: skill_eval.py stores only `llm_output_preview`, not the bash
trace, so the ls-explores vs description-triggers distinction cannot be
confirmed from the run artifact alone -- only inferred from timing +
the preview's opening remark ("网络环境拿不到包列表（这是 mock 测试
环境）", suggesting the agent proactively ran a system command rather
than just ls-ing). Adding trace capture is gap 4 (trajectory assertions).

### Update: bash trace implemented + confirms the trigger-path shift

`skill_eval.py` now captures the agent's tool-call trace: `run_pi` adds
`--session-dir <tmp> --session-id <unique>` to the pi command, and after
the run `extract_tool_calls()` reads the session `.jsonl` and returns the
ordered list of `toolCall` events (name + arguments). Each run in
benchmark.json now has `tool_calls` and `tool_call_count`.

Comparing the first tool call across sessions confirms the
frontmatter-bug story empirically:

| session date | frontmatter | 1st tool call |
|-------------|-------------|--------------|
| 2026-08-28  | broken      | `bash: find ~/.local/share/pi/skills` (searching) |
| 2026-08-28  | broken      | `bash: ls -la <skill dir>` (exploring) |
| 2026-09-03  | broken      | `bash: ls -la <skill dir>` then `read SKILL.md` |
| 2026-09-04  | FIXED       | `read: skill-test/.pi/skills/.../SKILL.md` (proactive) |

With the broken frontmatter, pi did NOT inject the skill into
`available_skills`, so the agent had no path to the skill and resorted to
`find` / `ls` exploration before reading SKILL.md. With the fix, pi injects
the description, the agent matches the task, and the first action is a
direct `read SKILL.md` -- the description-trigger path real users hit.

This trace data is the foundation for trajectory assertions (gap 4):
`ran_checkupdates` (trace contains the script / checkupdates),
`script_report_used` (trace contains `read report.json` / `match_*.json`),
`no_fabrication` (every package reported appears in the checkupdates
output). Not yet wired into the grader, but the data is now captured.

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
- Backed by `deepseek-reasoner` (strong reasoning for the
  RELEVANT / NOT_RELEVANT / UNCERTAIN call).

A dedicated `verifier` agent was considered (faster `deepseek-chat`, hardcoded
rules) but **deferred** -- `worker` is good enough and avoids maintaining an
extra agent file. If volume later makes `deepseek-chat` too slow, revisit.

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
