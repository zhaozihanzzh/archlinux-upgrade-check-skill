# System-layer Mock Design (Phase 4: hiding the skill from the agent)

Research/design record. Builds on `mock-env-design.md` (Phase 1 = transparent
HTTP mock). This document addresses the **second crack** that Phase 1 alone
leaves open: the agent's own reads/runs of system state hit *real* host data,
inconsistent with the mock, so a curious model notices and stops being a
genuine baseline.

## 1. Problem: the second crack (system-state inconsistency)

`ARCH_CHECK_MOCK_DIR` only affects the **bundled script** -- it makes the
script read `mock_dir/pacman.log`, `checkupdates.txt`, and `http/`. The agent
itself, in baseline mode, does its own probing:

- `grep ... /var/log/pacman.log` -> the **real** host upgrade history (date
  unrelated to the mock's 2026-08-03, contradicting the "two weeks ago"
  prompt).
- `checkupdates` -> the **real** pending packages (not shadow/glibc/...).
- `pacman -Q <pkg>` -> the **real** installed set.

Cross-referencing these against what `curl` (mock) returns makes the mock
obvious. Phase 1 fixed *network* transparency; this document fixes *system*
transparency so every entry the agent can touch returns consistent mock data.

## 2. The command-interception idea (user proposal)

Intercept the agent's shell commands and answer the mock-related ones from
prepared outputs: `checkupdates` -> fixture; `pacman ...` -> splice
`--dbpath <mock-db>` (and `--logfile <mock>`); redirect `pacman.log` reads to
the mock file. This is a **PATH-shim** approach: put wrapper scripts earlier
in `PATH` than `/usr/bin`, so the agent's commands hit the wrappers first.

Confirmed viable:
- `checkupdates` is a bash script (`/usr/bin/checkupdates`) -- a PATH shim
  that just `cat`s a fixture works and is invisible.
- `pacman` accepts `--dbpath`/`-b`, `--logfile`, `--config` (verified:
  `pacman -Q --dbpath <mock> --logfile <mock>` exits 0).
- bwrap 0.11.2 is available (user-namespace, no root needed).

## 3. Three isolation layers compared

| Aspect | PATH shims only | bwrap + PATH shims | docker + archlinux image |
|---|---|---|---|
| Intercept `checkupdates` | yes (PATH) | yes (PATH, via --setenv) | yes (PATH, via ENV) |
| Intercept `pacman` (`-Q`/`-Qu`/`-Qi`) | yes (PATH) | yes | yes |
| Cover `read /var/log/pacman.log` (pi `read` tool, not shell) | **no** -- `read` bypasses PATH | yes (`--ro-bind mock /var/log/pacman.log`) | yes (`-v mock:/var/log/pacman.log:ro`) |
| Hide the skill tree from `ls ..` | **no** -- cwd still in skill tree | yes (bind a clean cwd) | yes (mount clean cwd) |
| Hide host residues (`/home`, other pkgs) | no | partial (only bound paths visible) | yes (container is clean) |
| Root / daemon needed | no | no (user namespace) | docker daemon |
| Weight | lightest | light | heavy (image pull) |
| Portability | high | high (Linux) | highest (cross-platform, Dockerfile) |

**Key gap of PATH-only**: the pi `read` tool reads files directly, not
through a shell, so a `cat`/`pacman.log` PATH shim cannot redirect what
`read /var/log/pacman.log` returns. To cover that entry you need a
filesystem-layer bind (bwrap or docker). Everything else (commands) PATH
shims handle fine.

**Recommendation: bwrap + PATH shims.** It is the lightest option that closes
both cracks (commands via shims, files via bind), needs no root/daemon, runs
on the same machine pi already uses bwrap on, and is far simpler than docker.
docker remains the option if host-residue hiding or cross-platform
reproducibility later matters.

## 4. Mocking each entry point

### 4a. checkupdates -- PATH shim (fixture)
```
mock-bin/checkupdates   # earlier in PATH than /usr/bin
  -> cat <mock_dir>/checkupdates.txt   # shadow 4.16.0-1; glibc 2.41-1 ...
```

### 4b. pacman -- PATH shim, two strategies
**Strategy B (intercept-and-fixture, lighter):** the shim recognizes the
common query shapes (`-Q`, `-Qu`, `-Qi <pkg>`, `-Qs`) and `cat`s prepared
outputs. No mock local db needed. Covers what an agent realistically asks.
Risk: an unusual pacman subcommand falls through to real pacman and leaks.
**Strategy A (--dbpath passthrough, what the user proposed):** the shim
rewrites the command as `/usr/bin/pacman --dbpath <mock-local-db> --logfile
<mock-pacman.log> --config <mock-pacman.conf> "$@"` and runs the real pacman
against a fabricated local db. More faithful (real pacman logic) but
requires building a mock `/var/lib/pacman/local/<pkg>-<ver>/desc` tree for
the 6 pending packages.

Start with **B** (covers agent probing, cheap); escalate to **A** only if
an agent's `pacman` query falls through and leaks.

### 4c. /var/log/pacman.log -- bwrap file bind (PATH can't reach `read`)
```
bwrap ... --ro-bind <mock_dir>/pacman.log /var/log/pacman.log
```
Covers both `grep ... /var/log/pacman.log` (shell) and the pi `read` tool.

### 4d. Clean cwd (hide the skill tree)
```
bwrap ... --bind <empty-dir> /workspace --chdir /workspace
```
The agent's `ls ..` sees only the empty workspace's parent (which we
control), never the skill tree. This is what makes the baseline a real
"no skill" run.

### 4e. Network (Phase 1, already built)
mitmproxy `--mock` injects `HTTPS_PROXY`/`SSL_CERT_FILE`/`NO_PROXY`. Under
bwrap these env vars are passed through `--setenv`.

## 5. Do we still need to mock archlinux.org right now?

Short answer: **for the `shadow` assertion, no; for the `sg` assertion, yes.**

- `shadow` is a package name. The mock `checkupdates` fixture already lists
  `shadow 4.16.0-1` as pending. A system-layer-only mock lets the baseline
  agent learn "shadow is pending" *consistently* (same as the script), so the
  `mentions-shadow-issue` assertion can be fair without HTTP mock.
- `sg` / `newgrp` come from the mock **BBS post body** (`viewtopic.php?id=
  314544`). The agent only sees that text if its `curl` reaches the mock
  fixtures. Without the HTTP mock, the agent curls the *real* bbs and will
  not find our fabricated "sg dropped from shadow?" thread -- so it cannot
  mention `sg`, and that assertion is unreachable for the baseline.

So a **system-layer-only** Phase 4 already removes the biggest give-aways
(real pacman.log date, real checkupdates list) and makes the `shadow`
assertion fair. The `sg` assertion needs Phase 1's HTTP mock turned on too.
They compose: `--mock` (HTTP) + the future `--sys-mock` (system) are
independent flags; turn both on for a fully immersed baseline, or just
`--sys-mock` to validate system-layer immersion cheaply first.

One subtlety if HTTP mock is off while system mock is on: the agent, having
learned `shadow` is pending from the (mocked) `checkupdates`, may `curl` the
*real* archlinux.org and find real shadow-related news/BBS posts that differ
from our mock narrative. That is fine for fairness (both with-skill script
and baseline agent would then see the same real web), but it decouples from
the canned `sg` story. Keep E1's `sg` assertion behind `--mock` (HTTP on).

## 6. Implementation (built) and verified

Built under `scripts/sys_mock/`:

```
scripts/sys_mock/
  bwrap-run.sh          # assembles the bwrap invocation (hardened)
  gen_local_db.py       # generates mock local-db from checkupdates.txt
  mock-bin/checkupdates # inlined shim (binds over /usr/bin/checkupdates)
  mock-bin/pacman       # inlined shim (binds over /usr/bin/pacman)
  mock-bin/date         # pins "today" to 2026-08-31 (binds over /usr/bin/date)
evals/mock/e1/
  pacman.log            # ~69-line realistic history (last upgrade 08-17, FIXED)
  checkupdates.txt      # "name newver oldver" (self-consistent)
  local-db/<pkg>        # installed versions (== oldver)
  http/<md5>.html        # mock BBS/news fixtures (mitmproxy only); incl.
                         # viewforum?id=44 (lists shadow row @8-25),
                         # viewforum?id=27 (a board a curious agent curls),
                         # viewtopic?id=314544 (shadow topic body)
```

`skill_eval.py` gained `--sys-mock`: wraps only the baseline `pi` run in
`bwrap-run.sh` (with-skill must NOT use it -- bwrap hides the skill tree,
which with-skill needs to see for `.pi/skills` discovery). clean_cwd is a
throwaway tmpdir OUTSIDE skill_path (bwrap `--tmpfs SKILL_DIR` overlaps the
whole tree, including baseline-harness inside it).

Hardening (a curious agent that `find /`, `echo $ENV`, `ls` cwd, `read`
shims, `ps aux` was progressively closed):
- shim data INLINED into the script body (no `ARCH_CHECK_INLINE_*` env vars)
- shim SOURCES live in a host `mktemp -d` outside bwrap (cwd has no .shim-bin)
- shims bind over the REAL `/usr/bin/checkupdates` + `/usr/bin/pacman`
  (`which` shows a normal path; no mock data dir exists inside the sandbox)
- `--unshare-pid`: `ps aux` sees only the sandbox, not host mitmdump
- `ARCH_CHECK_SYS_MOCK` / `ARCH_CHECK_MOCK_DIR` env vars removed (leaks)
- skill tree overlaid with empty tmpfs; cwd is an empty clean dir

Verified (sensenova/glm-5.2): the baseline agent used `pacman -Qs` (mock
answers), `cd`-ed into the skill path and saw NOTHING, and never found mock
fixtures. The system layer is fully convincing.

Residual exposure (accepted): a determined agent can still `read`
`/usr/bin/checkupdates` and see it is a bash script echoing fixture lines.
Fully closing that requires a compiled (binary) shim or a container-level
fake rootfs -- out of scope for the PATH-shim approach. In 5 test runs no
model actually did this; they used the commands, not inspected them.

## 7. Open questions (resolved)

- bwrap `--ro-bind <file> /var/log/pacman.log`: the target pre-exists in the
  container's `/var/log` (real pacman.log), so the bind overlays it fine.
- pi runs inside bwrap fine (pi's own sandbox is not bwrap-based; nesting
  works without `--no-sandbox`).
- Strategy B's fixture set: the pacman shim answers -Q/-Qu/-Qi/-Qs from the
  inlined db; anything else returns an error. Mined session logs showed
  agents only issue -Q variants for this task.
- `--mock` (HTTP) is now used WITH `--sys-mock` for the fair baseline:
  the completed fixture set (news index, RSS, BBS home, viewforum id=2 /
  id=27 / id=44, viewtopic 314544 / 314363 / 314096) covers the URLs a
  baseline agent actually curls, so it no longer sees through the mock.
  `--sys-mock` alone was the clean baseline earlier, but it left a data gap
  (shadow's sg signal lives in the mock BBS post, unreachable without
  --mock). The fair baseline is `--mock --sys-mock` together (see
  verification-design.md "Update (final)").
- **date shim rationale**: the script's `since_date` comes from
  `pacman.log` (last upgrade), NOT from `datetime.now()`, so the script
  itself is unaffected by the real clock. But the agent's shell `date`
  command IS used (the agent checks "today" to interpret "two weeks ago").
  The mock `date` pins today to 2026-08-31 so the agent's notion of time
  matches the fixed `pacman.log` (08-17) and the fixed fixture dates
  (shadow topic 08-25). Note this only intercepts the shell `date`; python
  `datetime.now()` inside the script reads the real clock -- acceptable
  because the script does not use `now` for `since_date`.
