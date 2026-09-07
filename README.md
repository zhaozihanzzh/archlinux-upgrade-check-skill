# archlinux-upgrade-check-skill

A skill that checks **Arch Linux News** and the **BBS "Pacman & Package
Upgrade Issues" forum** for anything that might require manual
intervention **before** you run `pacman -Syu`. Tested on
[pi](https://github.com/earendil-works/pi-coding-agent).

Before a system upgrade, Arch users may want to read the latest news and
forum posts. Doing that by hand is tedious -- this skill automates it: it
figures out what you are about to upgrade, scans the official news and the
upgrade-issues forum since your last upgrade, cross-references every post
against your package list, and reports only the items that actually affect
you.

## What it does

1. **Finds your upgrade window** -- parses `/var/log/pacman.log` for the
   date of your most recent `pacman -Syu`, and runs `checkupdates` to list
   pending upgrades.
2. **Scans the sources** -- scrapes Arch Linux News (14 pages, back to
   2002) and the BBS upgrade-issues forum (since inception). HTML is used
   instead of RSS because the RSS feeds only cover ~9 months (news) and
   ~25 days (BBS).
3. **Cross-references** -- matches package names from your update list
   against each news title and forum topic (title, first post, and recent
   replies). Matching is two-tier: full package name first, then a
   base-name fallback for hyphenated packages (e.g. `plasma` for
   `plasma-desktop`), with a blacklist and length gate to kill noise like
   `linux` / `python` / `archlinux`.
4. **Verifies and reports** -- the LLM examines the per-package evidence
   (`title`, `first_post`, `recent_posts`, with snippets and `match_type`)
   to drop false positives (URL matches, generic mentions) and presents
   the verified findings to the user, with links.

If your last upgrade was more than a year ago, the scan window is capped
to 12 months and the report sets `lookback_capped: true`, warning you to
do step-wise upgrades via <https://archive.archlinux.org/> instead of a
direct `pacman -Syu`.

## Limitations (what it does NOT do)

This skill is a **pre-flight check, not a guarantee**. It scans two
sources (official News + the BBS upgrade-issues forum) and lets an LLM
judge the hits. It does NOT cover:

- **Other sources** -- AUR comments, other BBS boards, IRC, the
  arch-dev-public mailing list, bug trackers, and non-English community
  forums. Scanning is limited to archlinux.org News and the BBS "Pacman &
  Package Upgrade Issues" board (forum id 44); issues discussed only
  elsewhere are invisible to this skill. (Posts in any language that DO
  appear on the scanned board are still matched -- package names are
  ASCII and the verifying LLM is multilingual.)
- **Brand-new posts** -- the BBS scrape has a small lag; a thread posted
  hours before your upgrade may not be scanned yet.
- **Package names not mentioned at all** -- matching is by explicit
  package-name string in the title, first post, or replies. A thread
  whose title lacks the name is still caught (the body is scanned too);
  only a thread that never names the package (e.g. a pure symptom report
  like "system won't boot" with no "glibc" anywhere) cannot be linked to
  your update list.
- **Partial-upgrade risk** -- the skill does not detect that you ran
  `pacman -Sy` without `-u` (a common Arch footgun); it only checks
  news/forum content, not your pacman history.
- **LLM judgement** -- the final RELEVANT / NOT_RELEVANT verdict is an
  LLM call; it can over-report (flag a routine upgrade as intervention) or
  under-report (miss an incidental mention). Always read the linked
  thread yourself before acting.

**Before upgrading**, still: read the pacman output during `-Syu`, keep a
backup / timeshift, and know how to rollback (downgrade via
`/var/cache/pacman/pkg`).

## Repository layout

```
archlinux-upgrade-check-skill/
├── SKILL.md                     # instructions the LLM follows
├── scripts/
│   ├── arch_upgrade_check.py    # the checker script (the workhorse)
│   ├── test_integration.py      # integration tests (offline mock)
│   └── skill_eval.py            # end-to-end eval via `pi -p`
├── tests/                       # unit tests + HTML fixtures
├── evals/                       # eval definitions + mock data
└── docs/                        # testing + development notes
```

## Example run

The interaction below was captured by spawning `pi` with the skill and a
prompt, using mock data (`evals/mock/e1`) so it is reproducible offline:

```bash
cd archlinux-upgrade-check-skill
export PI_OFFLINE=1 ARCH_CHECK_MOCK_DIR="$PWD/evals/mock/e1"
pi -p --skill "$PWD" --model <provider/model> \
  "I'm about to run pacman -Syu on my Arch machine. Before that, please
   check Arch Linux official news and the BBS Pacman and Package Upgrade
   Issues forum for anything that might need manual intervention. I last
   updated about two weeks ago."
```

The script writes a **sharded JSON report** (a slim `report.json` summary
plus one `match_<k>.json` per candidate match with the full post text).
The LLM reads the slim report, then verifies each match and reports back,
for example:

```
## Upgrade Check Results

Last full upgrade: 2026-08-17 (about 2 weeks ago) - 6 packages to update

### Issue Found: `shadow` package - `sg` command removed
Topic: https://bbs.archlinux.org/viewtopic.php?id=314544 (Aug 25, 2026)

The `shadow` package has dropped the `sg` command. This is the version
you'll be updating to.

Action: If you use `sg` (the group-switching command), it will be missing
after the update. Switch to `newgrp` (still in shadow) or
`sudo -g <group>`.

### No other issues found
Safe to proceed with `pacman -Syu`, but be aware `sg` will be gone after
the `shadow` update.
```

## Running the script directly

You can run the checker outside an LLM session (for pipes, cron, or
inspection). The full option list is in the script's docstring and
`--help`; the common ones:

```bash
# print JSON to stdout (for pipes / inspection)
python3 scripts/arch_upgrade_check.py --json

# write a sharded report (slim report.json + per-match files; recommended)
python3 scripts/arch_upgrade_check.py --report-dir "$(mktemp -d /tmp/arch-upgrade-check.XXXXXX)"

# scan a custom window instead of "since last upgrade"
python3 scripts/arch_upgrade_check.py --days 90 --json
```

The JSON report has `status`, `since_date`, `lookback_capped`,
`packages_count`, and a `matches` array; each match carries
`matched_packages`, `package_evidence`, and the post text. Run
`python3 scripts/arch_upgrade_check.py --help` for the mock /
reproducibility flags (`--mock-pacman-log`, `--mock-checkupdates`,
`--mock-http-dir`, or `ARCH_CHECK_MOCK_DIR=<dir>`).

## Testing

Four layers, layers 1-3 fully offline:

- **Layers 1-2 -- unit tests** (`tests/test_find_packages.py`,
  `tests/test_scraping.py`): the matching and scraping functions in
  isolation.
- **Layer 3 -- integration** (`scripts/test_integration.py`): the full
  script on offline mock data (checkupdates + pacman.log + HTTP
  fixtures).
- **Layer 4 -- end-to-end** (`scripts/skill_eval.py`): `pi -p` with the
  skill against mock data, graded by assertions. Needs pi + an API key.

```bash
# Layers 1-2 (~0.1s)
python3 tests/test_find_packages.py
python3 tests/test_scraping.py

# Layer 3 (~15s)
python3 scripts/test_integration.py

# Layer 4 (needs pi + API key; replace <provider/model>)
python3 scripts/skill_eval.py --model <provider/model>
```

See [`docs/development/`](docs/development/) for the testing strategy,
mock environment, evaluation rationale, and the change log.
