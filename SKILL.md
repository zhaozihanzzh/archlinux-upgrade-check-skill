name: archlinux-upgrade-check-skill
description: Checks Arch Linux News and BBS (Pacman & Package Upgrade Issues) for manual intervention requirements before system upgrades.
---

The user wants to perform a full system upgrade on ArchLinux. You need to check the Arch Linux news and forums to determine whether the update requires manual intervention, then present relevant findings.

**Do not try to read the news or forum yourself.** A script in this skill (`scripts/arch_upgrade_check.py`) already does the fetching, parsing, and cross-referencing against the user's pending packages — running it is both faster and more accurate than you browsing pages. Your job is to run the script (Step 2) and verify its candidate matches (Step 3). Start by running the script.

## Step 1: Gather upgrade information

The check script does this for you — it parses `/var/log/pacman.log` to find the most recent `pacman -Syu` run (the scan time window) and runs `checkupdates` to list packages pending upgrade. You don't need to run those yourself; just invoke the script in Step 2.

If the host is not Arch Linux, the script detects this, prints a clear error to stderr, and exits with a non-zero status **without producing a report**. If that happens, **stop** — do not attempt any further check, do not fabricate a report, and do not run `pacman -Syu`. Just tell the user this machine is not Arch Linux so the upgrade check doesn't apply. (In mock/test mode the Arch guard is skipped, so a missing `/etc/arch-release` on its own is not proof.)

## Step 2: Run the check script

Run this first, before reporting anything. It is the source of truth for which packages and topics to report — do not substitute your own web browsing.

```bash
python3 <skill-dir>/scripts/arch_upgrade_check.py --report-dir /tmp/arch-upgrade-check
```

`<skill-dir>` is this skill's directory in your environment (the directory containing this `SKILL.md`).

This script scrapes Arch Linux News and BBS (Pacman & Package Upgrade Issues forum), cross-references against your package update list, and writes a **sharded report** to the directory you name with `--report-dir`:

- `report.json` — a slim summary (`status`, `since_date`, `lookback_capped`, `packages_count`, and per-match pointers: title/link/matched_packages/`is_necrobump`/`match_file`). It deliberately omits the full `packages_to_update` list (verification doesn't need it) and the forum-post text.
- `match_<k>.json` — one file per candidate match, holding that match's **full** `package_evidence`, `first_post`, and `recent_posts` (untruncated).

This split keeps your main conversation context small: you read only the slim `report.json`, and the heavy post text is loaded only by the subagent that verifies a given match (Step 3). The report does not list every pending package — if you need that list, run `checkupdates` directly.

Script output reference:

| `--report-dir <dir>` | Write slim `report.json` + per-match files (recommended). Keeps context small; full text loaded only on demand |
| `--report-file <path>` | Write the full report (all matches with post text) to a single file. Use for pipes / humans / when no subagent is available |
| `--json` | Print the full JSON to stdout |
| `--days <N>` | Override the time window (default: from last upgrade date) |

## Step 3: Verify the candidate matches

The slim `report.json` lists candidate matches — topics whose title or content mention a package from your update list. Each match points to a `match_<k>.json` file with the full evidence.

**You need to filter out false positives**: a topic may mention a package incidentally (in a URL, filename, or as a generic term) without being about an upgrade issue affecting that package.

### How you perform this verification

**Preferred — per-match subagent.** For each match, spawn a `worker` subagent (isolated context) with the verification rules below and a pointer to `match_<k>.json`. The worker reads the full post text in its own context and returns a one-line verdict per package:

```
<package>: RELEVANT | <reason>
<package>: NOT_RELEVANT | <reason>
<package>: UNCERTAIN | <reason>
```

This keeps the heavy post text out of your main context entirely; you only collect the verdict lines. If your harness can run subagents in parallel, dispatch all matches' workers at once.

A worker task looks like:

> Read `<dir>/match_<k>.json`. For each package in `matched_packages`, judge RELEVANT / NOT_RELEVANT / UNCERTAIN using the rules below. Reply with exactly one line per package: `<pkg>: <VERDICT> | <reason>`.

**Fallback — inline (no subagent available):** `read` each `match_<k>.json` one at a time, apply the rules, record the verdict, then move on — do not retain the raw post text in your reply.

### Verification rules

For each candidate match, examine **each matched package independently** using its `package_evidence`.

| Evidence source | What it means |
|---|---|
| `title` | Package name appears in the topic title |
| `first_post` | Package name appears in the first post's content |
| `recent_posts` | Package name appears in replies after the user's last update date |
| `match_type: "base"` | Only the base component matched (e.g., `archlinux` from `archlinux-keyring` via URL `archlinux.org`) |
| No evidence text | Base-name match with no exact text found |

Decision guidelines:

- **Relevant**: The topic directly discusses an issue with this package, or a change that affects users upgrading this package
- **Not relevant**: The package name appears only in a URL, filename, generic OS reference (`linux`), or tangential mention
- **Uncertain**: If context is insufficient, report it as "possibly relevant" with your reasoning. Err on the side of caution — a false positive is safer than a missed intervention

### Example

```
Topic: "sg dropped from shadow?"
  shadow → RELEVANT. Title mentions the package. Issue is about a
            command (sg) being removed from the shadow package.
  archlinux-keyring → NOT RELEVANT. Base-match from
            "gitlab.archlinux.org/archlinux/..." URL in a reply.
```

### How the script matches packages

For BBS topics, the script fetches every non-solved/non-closed topic in the time window and checks **all three sources**: title, first post, and recent replies (since the user's last upgrade). For news, only titles with intervention keywords are checked.

Matching has two levels:

1. **Full name match**: The complete package name appears as a whole word (e.g., `shadow` in "sg dropped from shadow?", `libxfont2` in an error message). Highest confidence.

2. **Base name match** (hyphenated packages only): For packages like `virtualbox-ext-vnc`, if the first component (`virtualbox`, ≥5 chars and not a common generic word) appears standalone. Lower confidence — marked as `match_type: "base"`.

Certain extremely common base words like `linux`, `python`, and `archlinux` are excluded from base-name matching to avoid the noise of every "Arch Linux" reference matching all `linux-firmware-*` packages. Their full names can still match.

## Step 4: Report to the user

Present the verified results clearly:

- Which news articles or forum topics are relevant
- Which specific packages are affected
- Link to the original source for details

If no relevant issues were found, state that the upgrade appears safe.

**Check `lookback_capped`**: If the report's `lookback_capped` field is `true`, the user's last upgrade was over a year ago. The scan window was limited to 12 months, and more importantly, the user should **not** run `pacman -Syu` directly. Instead, recommend step-wise upgrades using dated snapshots from `https://archive.archlinux.org/` (e.g., 6-month increments). The matches below are for awareness when they eventually catch up.

## Data Sources

| Source | Coverage |
|--------|----------|
| Arch Linux News (`/news/`) | 14 pages, 50 items/page, since 2002 |
| BBS Pacman & Package Upgrade Issues (`/viewforum.php?id=44`) | 482+ pages, 30 topics/page, since forum inception |

The RSS feeds have limited coverage (~9 months for news, ~25 days for BBS), so the scripts use HTML scraping.

## Script reference

```
archlinux-upgrade-check-skill/
├── SKILL.md                  # this file (instructions the LLM follows)
└── scripts/
    └── arch_upgrade_check.py # the checker script (the workhorse)
```

Test scaffolding (not part of the upgrade-check workflow) lives under
`tests/`, `evals/`, and the other `scripts/*.py` files — see the repo README.

### JSON report structure

Key fields for verification:

```json
{
  "status": "has_matches" | "safe" | "ok",
  "since_date": "2026-06-20",
  "matches": [
    {
      "type": "bbs",
      "title": "sg dropped from shadow?",
      "link": "https://bbs.archlinux.org/viewtopic.php?id=314544",
      "matched_packages": ["shadow", "archlinux-keyring"],
      "package_evidence": {
        "shadow": [
          {"source": "title", "snippet": "sg dropped from shadow?"}
        ],
        "archlinux-keyring": [
          {"source": "recent_posts", "snippet": "...gitlab.archlinux.org...",
           "match_type": "base"}
        ]
      },
      "first_post": "...",        // full first post content (untruncated; in match_<k>.json)
      "recent_posts": "...",      // full recent replies content (untruncated; in match_<k>.json)
      "is_necrobump": false,      // true if topic predates since_date
      "recent_post_count": 2,     // number of recent posts fetched
      "total_pages": 1
    }
  ]
}
```

Top-level fields:
- `lookback_capped`: true if the user's last upgrade was over 365 days ago; the scan window was capped and the user should use archive step-wise upgrades instead of direct `pacman -Syu`.

Each match's full `package_evidence`, `first_post`, and `recent_posts` live in its own `match_<k>.json` (untruncated); the slim `report.json` only carries pointers (`match_file` names). This split is why the heavy text stays out of your context — a subagent loads only the match file it verifies. With `--report-file` (single-file mode) the same content is one file instead.
