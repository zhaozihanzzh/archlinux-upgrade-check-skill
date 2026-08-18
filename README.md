# archlinux-upgrade-check-skill

A skill that checks
**Arch Linux News** and the **BBS "Pacman & Package Upgrade Issues" forum**
for anything that might require manual intervention **before** you run
`pacman -Syu`. Tested on [pi](https://github.com/earendil-works/pi-coding-agent).

Before a system upgrade, Arch users may want to read the latest news and forum
posts. Doing that by hand is tedious — this skill automates it: it figures out
what you are about to upgrade, scans the official news and the upgrade-issues
forum since your last upgrade, cross-references every post against your
package list, and reports only the items that actually affect you.

## What it does

1. **Finds your upgrade window** — parses `/var/log/pacman.log` for the date of
   your most recent `pacman -Syu`, and runs `checkupdates` to list pending
   upgrades.
2. **Scans the sources** — scrapes Arch Linux News (14 pages, back to 2002)
   and the BBS upgrade-issues forum (since inception). HTML is used instead of
   RSS because the RSS feeds only cover ~9 months (news) and ~25 days (BBS).
3. **Cross-references** — matches package names from your update list against
   each news title and forum topic (title, first post, and recent replies).
   Matching is two-tier: full package name first, then a base-name fallback
   for hyphenated packages (e.g. `plasma` for `plasma-desktop`), with a
   blacklist and length gate to kill noise like `linux` / `python` /
   `archlinux`.
4. **Verifies and reports** — the LLM examines the per-package evidence
   (`title`, `first_post`, `recent_posts`, with snippets and `match_type`)
   to drop false positives (URL matches, generic mentions) and presents the
   verified findings to the user, with links.

If your last upgrade was more than a year ago, the scan window is capped to
12 months and the report sets `lookback_capped: true`, warning you to do
step-wise upgrades via <https://archive.archlinux.org/> instead of a direct
`pacman -Syu`.

## Repository layout

```
archlinux-upgrade-check-skill/
├── SKILL.md                     # instructions the LLM follows
├── scripts/
│   ├── arch_upgrade_check.py    # the checker script (the workhorse)
│   ├── test_integration.py      # Layer 3 integration tests (offline mock)
│   └── skill_eval.py            # Layer 4 end-to-end eval via `pi -p --skill`
├── tests/                       # Layer 1–2 unit tests + HTML fixtures
├── evals/                       # eval definitions + mock data (e1/e2/e3)
├── references/                  # design decisions & test plan
└── docs/testing-guide.md        # the four-layer testing strategy
```


## Example run

The interaction below was captured by spawning `pi` with the skill and an prompt, using mock data (`evals/mock/e1`) so it is reproducible
offline:

```bash
cd archlinux-upgrade-check-skill
export PI_OFFLINE=1 ARCH_CHECK_MOCK_DIR="$PWD/evals/mock/e1"
pi -p --skill "$PWD" --model opencode-go/deepseek-chat \
  "I'm about to run pacman -Syu on my Arch machine. Before that, please
   check Arch Linux official news and the BBS Pacman & Package Upgrade
   Issues forum for anything that might need manual intervention. I last
   updated about two weeks ago."
```

### Reproducible JSON report (from the script)

With the same mock data, the script produces:

```json
{
  "status": "has_matches",
  "since_date": "2026-08-03",
  "last_upgrade": "2026-08-03",
  "lookback_capped": false,
  "packages_to_update": [
    "glibc", "linux-firmware-intel", "nvidia-utils",
    "pipewire", "shadow", "systemd"
  ],
  "packages_count": 6,
  "matches": [
    {
      "type": "bbs",
      "date": "2026-08-09",
      "title": "sg dropped from shadow?",
      "link": "https://bbs.archlinux.org/viewtopic.php?id=314544",
      "topic_id": "314544",
      "matched_packages": ["shadow"],
      "title_matched": ["shadow"],
      "package_evidence": {
        "shadow": [
          { "source": "title", "snippet": "sg dropped from shadow?" }
        ]
      },
      "is_necrobump": false,
      "replies": 0
    }
  ],
  "match_count": 1
}
```

### Sample agent output

The agent verifies the candidate match against the user's update list and
reports back:

```
## Verification of candidate matches

### Match: BBS Topic "sg dropped from shadow?" (Aug 9)
- shadow → RELEVANT. Title directly mentions the package. The topic is
  about the `sg` command being dropped from the `shadow` package.
  You have shadow pending upgrade — this is exactly the upgrade in question.

None of the other scanned news articles or forum topics affect packages in
your update list.

## ⚠ Upgrade Check Results

Last full upgrade: 2026-08-03 (about 2 weeks ago) · 6 packages to update

### 🔴 Issue Found: `shadow` package — `sg` command removed
Topic: https://bbs.archlinux.org/viewtopic.php?id=314544 (Aug 9, 2026)

The `shadow` package has dropped the `sg` command. This is the version you'll
be updating to.

Action: If you use `sg` (the group-switching command), it will be missing
after the update. Switch to `newgrp` (still in shadow) or
`sudo -g <group>`.

### ✅ No other issues found
Safe to proceed with `pacman -Syu`, but be aware `sg` will be gone after the
`shadow` update.
```

## The script directly

You can also run the checker outside of an LLM session:

```bash
# print JSON to stdout
python3 scripts/arch_upgrade_check.py --json

# write JSON to a file (recommended — the package list can be large)
python3 scripts/arch_upgrade_check.py --json --report-file /tmp/report.json

# scan a custom window instead of "since last upgrade"
python3 scripts/arch_upgrade_check.py --days 90 --json
```

Flags for reproducible / offline runs: `--mock-pacman-log`,
`--mock-checkupdates`, `--mock-http-dir`, or just set
`ARCH_CHECK_MOCK_DIR=<dir>` to pick up `<dir>/pacman.log`,
`<dir>/checkupdates.txt`, and `<dir>/http/` automatically.

## Testing

Four layers, all but Layer 4 fully offline:

```bash
# Layer 1–2: unit tests (~0.1s)
python3 tests/test_find_packages.py
python3 tests/test_scraping.py

# Layer 3: script integration tests on mock data (~15s)
python3 scripts/test_integration.py

# Layer 4: end-to-end skill eval via `pi -p --skill` (needs pi + API key)
python3 scripts/skill_eval.py --model opencode-go/deepseek-chat
```

See [`docs/testing-guide.md`](docs/testing-guide.md) and
[`references/design-decisions.md`](references/design-decisions.md) for the
rationale behind the matching logic, the lookback cap, and the choice of HTML
scraping over RSS.
