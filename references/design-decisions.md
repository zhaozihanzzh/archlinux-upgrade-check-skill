# Design Decisions

## Package Matching (`find_packages_in_text`)

Matching has two levels to handle how people naturally refer to packages in forum posts.

### Level 1: Full name match (high confidence)

The complete package name appears as a whole word, using `(?<!\w)` / `(?!\w)` lookarounds instead of `\b`. This is because `\b` doesn't work at non-word/non-word boundaries — e.g., `gtk+ 4.0` has `+` (non-word) followed by space (non-word), so `\b` after the `+` doesn't match.

```
"sg dropped from shadow?"  → packages=["shadow"] → "shadow" boundary-matched → ✅
"lib32-brotli"            → packages=["brotli"]  → "brotli" boundary-matched → ✅
```

### Level 2: Base name match (lower confidence)

For hyphenated (`-`) or plus-separated (`+`) packages, the script falls back to matching the first component as a standalone word. This handles common usage where people refer to a package by its base name:

```
帖子："plasma 好卡啊"  → plasma-desktop, plasma-workspace 的基础名 "plasma" 都命中
帖子："dovecot 配置"  → dovecot-core 的基础名 "dovecot" 命中
帖子："nvidia 驱动"   → nvidia-utils 的基础名 "nvidia" 命中
```

**Length gate (≥5 chars)**: Shorter bases like `lib`, `py`, `gtk` (from `libx11`, `python`, `gtk+`) are too common and produce too many false positives. Only bases ≥5 characters are eligible.

### Base name blacklist

Certain base words are so extremely common in generic context that matching them produces noise without signal. These are excluded from base-name matching only — full name matches still work.

| Base word | Package family | False positive source in text |
|-----------|---------------|-------------------------------|
| `linux` | `linux-firmware-intel`, `linux-firmware-realtek`, `linux-firmware-whence`, `linux-headers` | "Arch Linux", "Linux kernel" |
| `python` | `python-annotated-types`, `python-certifi`, `python-pip`, ... (15+ packages) | "Python 3.14", "Python code" |
| `archlinux` | `archlinux-keyring` | "archlinux.org" in URLs |

A post that actually says `python-pip` (full name) will still match — only the base-only fallback is blocked.

### Word boundary protection

Uses `(?<!\w)` / `(?!\w)` lookarounds instead of `\b` because `\b` fails at non-word/non-word boundaries — e.g., `gtk+ 4.0` has `+` (non-word) followed by space (non-word), so `\b` wouldn't detect a boundary after the `+`.

Combined with the length gate, this handles false substring matches.

| Text | Package | Result |
|------|---------|--------|
| `wire cable` | `wireplumber` | `wire` (4 chars) < 5 gate → ❌ |
| `shadowd` | `shadow` | `(?<!\w)shadow(?!\w)` doesn't match `shadowd` → ❌ |
| `python3` | `python` | `(?<!\w)python(?!\w)` doesn't match `python3` → ❌ |

### `find_snippet` evidence builder

When a match is found, the script builds per-package evidence showing where each package was found and in what textual context. The evidence supports LLM verification by providing:

1. **Source**: which field the match came from (`title`, `first_post`, `recent_posts`)
2. **Snippet**: ~80 chars of surrounding context
3. **match_type**: `"base"` if it was a base-name fallback (lower confidence)

The LLM uses this evidence to judge relevance independently per package within a topic.

---

## BBS Matching Strategy: Content-Over-Title

Previously, the script only fetched BBS topic content if the title matched a package. This missed topics where the affected package was mentioned only in the post body.

**Current approach**: Fetch every non-solved/non-closed topic in the time window, then check all three sources (title, first post, recent replies). Match any source, not just title.

**Performance trade-off**: For a 12-month window (~200-300 topics), this means ~200-300 HTTP requests. Acceptable for a one-time pre-upgrade check.

**Known blind spot accepted**: Topics whose first post and recent replies don't mention any package are skipped, even if a middle page of the thread does. The assumption is that the first post or most recent replies are the most likely places for package names to appear.

---

## Lookback Cap for Long-Unupgraded Systems

If the user hasn't run `pacman -Syu` in over 365 days, the scan window is capped to 12 months. The JSON report sets `lookback_capped: true`.

**Rationale**: Users this far behind should follow step-wise upgrades via [archive.archlinux.org](https://archive.archlinux.org/), not direct `pacman -Syu`. Scanning 3+ years of BBS history is both expensive and misleading — the relevant question for them isn't "what changed recently" but "how do I get current safely".

The 12-month scan still provides useful awareness of recent intervention requirements they'll encounter when catching up.

---

## Data Source Choice: HTML over RSS

Arch Linux's RSS feeds have insufficient historical range:
- **News RSS**: Returns the same 10 items regardless of pagination (observed: pages 1-20 identical)
- **BBS RSS**: Returns identical content for all pages (observed: ~25 days only)

HTML scraping covers the full history:
- **News HTML** (`/news/?page=N`): 14 pages × 50 items = 700 articles, 2002–present
- **BBS HTML** (`/viewforum.php?id=44&p=N`): 482+ pages × 30 topics, forum inception–present

---

## Necrobump Handling

When a topic's first post predates the user's last upgrade date but has recent replies, it's flagged as `is_necrobump: true`. Only posts after the user's upgrade date are checked for package mentions. This avoids matching on decade-old posts that happen to have one recent "me too" reply.
