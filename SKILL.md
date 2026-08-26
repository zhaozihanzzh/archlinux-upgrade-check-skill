name: archlinux-upgrade-check-skill
description: Checks Arch Linux News and BBS (Pacman & Package Upgrade Issues) for manual intervention requirements before system upgrades.
---

The user wants to perform a full system upgrade on ArchLinux. You need to check the Arch Linux news and forums to determine whether the update requires manual intervention, then present relevant findings.

## Step 1: Gather upgrade information

The check script does this for you — it parses `/var/log/pacman.log` to find the most recent `pacman -Syu` run (the scan time window) and runs `checkupdates` to list packages pending upgrade. You don't need to run those yourself; just invoke the script in Step 2.

If the host is not Arch Linux, the script detects this, prints a clear error to stderr, and exits with a non-zero status **without producing a report**. If that happens, **stop** — do not attempt any further check, do not fabricate a report, and do not run `pacman -Syu`. Just tell the user this machine is not Arch Linux so the upgrade check doesn't apply. (In mock/test mode the Arch guard is skipped, so a missing `/etc/arch-release` on its own is not proof.)

## Step 2: Run the check script

```bash
python3 <skill-dir>/scripts/arch_upgrade_check.py --report-file /tmp/arch-upgrade-check.json
```

`<skill-dir>` is this skill's directory in your environment (the directory containing this `SKILL.md`).

This script scrapes Arch Linux News and BBS (Pacman & Package Upgrade Issues forum), cross-references against your package update list, and writes a structured JSON report to the file you name with `--report-file`. Writing to a file keeps the often-large report (notably the full `packages_to_update` list and forum-post text) out of your conversation context — you only pull in what you need when verifying matches in Step 3.

Script output reference:

| `--report-file <path>` | Write JSON to a file only; a one-line confirmation goes to stderr. Recommended — keeps large output out of context |
| `--json` | Print machine-readable JSON to stdout instead (for pipes / inspection). Mutually exclusive in practice with `--report-file` |
| `--days <N>` | Override the time window (default: from last upgrade date) |
| `--minimal` | Omit the full `packages_to_update` list and truncate each match's `first_post`/`recent_posts` to 300/1000 chars. Optional, for when context is tight — see note below |

**About `--minimal`**: it trades verification quality for a smaller report. The full `packages_to_update` list is not needed for verifying matches (Step 3 uses per-match `package_evidence` instead), so dropping it costs nothing. But truncating `first_post` and `recent_posts` can cut the very context you need to judge whether a package mention is a real intervention issue or a false positive — in real threads these fields can run thousands of characters. Prefer running **without** `--minimal`; reach for it only when the update set is unusually large and context is genuinely constrained, and read the fuller report file directly when a match is borderline.

## Step 3: Verify the candidate matches

The JSON report contains candidate matches — topics whose title or content mention a package name from your update list. Each match includes `package_evidence`: per-package data showing where each package name was found and in what context.

**You need to filter out false positives**: a topic may mention a package incidentally (in a URL, filename, or as a generic term) without being about an upgrade issue affecting that package.

### How you perform this verification

Different agent harnesses have different capabilities (sub-agents, context management, etc.). Choose the approach that works best for your environment:

- **Inline**: Read the report and examine each match's evidence directly
- **Sub-agent**: Delegate verification to a focused sub-agent if available
- **File-based**: Read the report file, write a summary, then discard the raw report from context
- **Other**: Any approach that correctly applies the verification rules below

The important thing is to avoid retaining large intermediate data (full package list, raw report JSON) in your conversation context longer than needed.

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
      "first_post": "...",        // first post content (up to 1000 chars)
      "recent_posts": "...",      // recent replies content (up to 3000 chars)
      "is_necrobump": false,      // true if topic predates since_date
      "recent_post_count": 2,     // number of recent posts fetched
      "total_pages": 1
    }
  ]
}
```

Top-level fields:
- `lookback_capped`: true if the user's last upgrade was over 365 days ago; the scan window was capped and the user should use archive step-wise upgrades instead of direct `pacman -Syu`.

Full output includes `packages_to_update` (the complete update list) and the full `first_post`/`recent_posts` text for each match. This can be large, which is why `--report-file` (writing to a file, not stdout) is the recommended invocation — the file keeps it out of context while preserving the full text you need for verification. Use `--minimal` only when context is genuinely tight (see Step 2).
