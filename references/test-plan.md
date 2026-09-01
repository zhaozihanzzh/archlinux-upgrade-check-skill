# Test Plan

Four layers of testing:
- **Layer 1–2**: Deterministic offline unit tests (package matching, HTML parsing)
- **Layer 3**: Script-level integration tests with mock data (deterministic, uses `--mock-*` flags)
- **Layer 4**: End-to-end Pi skill evaluation (tests LLM + SKILL.md orchestration)

## Layer 1: Unit Tests — Package Matching

`tests/test_find_packages.py` — tests `find_packages_in_text()` with static inputs.

### Test cases

| ID | Test | `packages` | Text | Expected matches | Rationale |
|----|------|-----------|------|-----------------|-----------|
| U1 | full-match-simple | `["shadow", "systemd"]` | `"sg dropped from shadow?"` | `{"shadow"}` | Full name, word boundary |
| U2 | full-match-hyphenated | `["libxfont2"]` | `"failed to retrieve libxfont2-2.0.9-1"` | `{"libxfont2"}` | Full name with hyphen, whole word |
| U3 | base-name-≥5-chars | `["dovecot-core"]` | `"dovecot config"` | `{"dovecot-core"}` | Base ≥5, standalone word |
| U4 | base-name-too-short | `["libx11"]` | `"lib files"` | `∅` | `lib` is 3 chars < 5 |
| U5 | base-name-with-plus | `["gtk+"]` | `"gtk 4.0"` | `∅` | `gtk` is 3 chars < 5; `gtk+` full match not present |
| U6 | base-name-plus-full | `["gtk+"]` | `"gtk+ 4.0"` | `{"gtk+"}` | Full name with `+` matches |
| U7 | blacklist-linux | `["linux-firmware-intel", "linux-headers"]` | `"Arch Linux"` | `∅` | `linux` blacklisted from base match |
| U8 | blacklist-python | `["python-pip", "python-certifi"]` | `"Python 3.14"` | `∅` | `python` blacklisted from base match |
| U9 | blacklist-archlinux | `["archlinux-keyring"]` | `"archlinux.org"` | `∅` | `archlinux` blacklisted from base match |
| U10 | blacklist-still-matches-full | `["python-pip"]` | `"python-pip is installed"` | `{"python-pip"}` | Full name still matches despite blacklist |
| U11 | word-boundary-protection | `["shadow"]` | `"shadowd"` | `∅` | `\b` prevents substring match |
| U12 | multiple-matches | `["shadow", "systemd", "glibc"]` | `"systemd and glibc issues, shadow too"` | `{"shadow", "systemd", "glibc"}` | Multiple packages in one text |
| U13 | no-match | `["alsa-utils", "mesa"]` | `"kernel updated"` | `∅` | Nothing matches |
| U14 | base-special-chars | `["pipewire-jack"]` | `"pipewire setup"` | `{"pipewire-jack"}` | Base `pipewire` 8 chars ≥5, standalone |

### Running

```bash
python3 tests/test_find_packages.py
```

Exit code 0 = all pass.

---

## Layer 2: Unit Tests — HTML Parsing

`tests/test_scraping.py` — tests all scraping/parsing functions against local HTML fixtures.

**No network required** — uses pre-downloaded HTML snapshots in `tests/fixtures/`. If
Arch Linux changes their site HTML structure, these tests fail and signal that the
scraping code needs updating.

### Fixtures

| Fixture | Source | Purpose |
|---------|--------|---------|
| `news_page_1.html` | `/news/?page=1` | Latest 50 news items, has next page |
| `news_page_14.html` | `/news/?page=14` | Oldest page (2002-2004), no next page → tests termination |
| `bbs_page_1.html` | `/viewforum.php?id=44` | Latest BBS topics, has next page |
| `bbs_page_24.html` | `/viewforum.php?id=44&p=24` | Older BBS topics, tests date range |
| `bbs_page_50.html` | `/viewforum.php?id=44&p=50` | Very old page (mostly sticky), tests edge case |
| `bbs_topic_314363.html` | `/viewtopic.php?id=314363` | Multi-page topic with 25 posts, tests pagination |
| `bbs_topic_solved.html` | `/viewtopic.php?id=314096` | Single-page solved topic, tests [SOLVED] handling |

### Test cases (103 assertions)

| Category | Test | What it verifies |
|----------|------|-----------------|
| News parsing | `test_news_page_parsing` | Date format, title length, absolute URL, `has_more=True` |
| News parsing | `test_news_page_14_parsing` | Articles from < 2010, `has_more=False` |
| News filtering | `test_news_exclusions` | Election/CVE/celebration titles excluded; intervention titles kept |
| News filtering | `test_intervention_keywords` | 7 keywords match, 3 non-intervention phrases don't |
| BBS parsing | `test_bbs_page_1_parsing` | id/date/title/link/solved/closed/reply count → correct types |
| BBS parsing | `test_bbs_page_24_parsing` | Older page has older dates than page 1 |
| BBS parsing | `test_bbs_page_50_parsing` | No crash when page has mostly sticky threads |
| BBS parsing | `test_bbs_solved_detection` | `[solved]`/`[resolved]` flags match regex for every topic |
| BBS content | `test_bbs_topic_content_parsing` | First post extracted, pagination detected, HTML stripped |
| BBS content | `test_bbs_topic_necrobump` | `is_necrobump=True` when since_date > first_post_date |
| BBS content | `test_bbs_topic_without_pagination` | `total_pages=1` for single-page topic, no HTML tags in content |
| News fetch | `test_fetch_news_full_range` | Wide date range when since_date covers all history |
| News matching | `test_find_news_matches` | Keyword + package intersection; elections/CVEs excluded |

### Running

```bash
python3 tests/test_scraping.py
```

Exit code 0 = all pass. Updates fixtures by re-downloading if site structure changes.

---

## Layer 3: Script-Level Integration Tests

`python3 scripts/test_integration.py` — runs `arch_upgrade_check.py` as a black-box CLI with mock data.

**Fully deterministic**: uses `--mock-http-dir`, `--mock-pacman-log`, `--mock-checkupdates` to
replace network calls and local system state. No network or `/var/log/pacman.log` required.

Reads test definitions from `evals/evals.json` — each eval specifies:
- `mock_args`: which mock files to use
- `script_args`: CLI flags to pass (e.g., `["--json"]`)
- `script_assertions`: what to check (exit code, JSON fields, text output, timeout)

### Setup

Before first run (or when test fixtures change):

```bash
python3 scripts/prepare_mock_fixtures.py
```

This copies HTML fixtures from `tests/fixtures/` into `evals/mock/e{1,2,3}/http/`
renamed by MD5 hash (matching the URL→md5 lookup in the script's mock system).

### Running

```bash
# All tests
python3 scripts/test_integration.py

# Specific tests
python3 scripts/test_integration.py --tests 1,3

# Output to directory
python3 scripts/test_integration.py --output-dir /tmp/integration-tests

# With custom timeout (default: 120s)
python3 scripts/test_integration.py --timeout 60
```

### Supported assertion types (`script_assertions`)

| Type | Checks |
|------|--------|
| `exit_code` | Exit code == 0 |
| `json_valid` | stdout parses as JSON |
| `json_fields` | JSON contains specific keys |
| `json_field_value` | JSON field equals expected value |
| `text_contains` | String found in stdout or stderr |
| `timeout` | Completes within max_seconds |

### Mock data files

Each eval in `evals/mock/e<id>/` has:

| File | Source | Purpose |
|------|--------|---------|
| `http/*.html` | `tests/fixtures/` (md5-hashed) | HTTP responses for news/BBS/topics |
| `pacman.log` | Hand-crafted | Last upgrade date |
| `checkupdates.txt` | Hand-crafted | Packages to update |

### Running directly (without test_integration)

```bash
python3 scripts/arch_upgrade_check.py \
  --json \
  --mock-pacman-log evals/mock/e1/pacman.log \
  --mock-checkupdates evals/mock/e1/checkupdates.txt \
  --mock-http-dir evals/mock/e1/http/
```

This produces the same output `test_integration.py` checks — useful for debugging.

---

## Layer 4: End-to-End Pi Skill Evaluation

`python3 scripts/skill_eval.py` — runs each eval prompt through `pi -p --skill`, then grades
the **LLM's natural language output** against `skill_assertions`.

This layer tests the full chain:
```
user prompt → LLM + SKILL.md → script invocation → result interpretation
```

### How mock data works in Layer 4

Mock data is provided via `ARCH_CHECK_MOCK_DIR` environment variable. `skill_eval.py` sets
this before spawning `pi -p`, and `arch_upgrade_check.py` auto-detects it when `--mock-*`
flags are not given. The LLM does **not** know it's being tested — it sees the same mock
data the same way it would see real data.

Pi-specific — requires the `pi` CLI tool installed:
```bash
which pi
```

### Supported assertion types (`skill_assertions`)

These check the LLM's conversational output, not the script's JSON:

| Type | Checks |
|------|--------|
| `exit_code` | pi -p exited successfully |
| `text_contains` | LLM output contains expected text |
| `timeout` | Complete within max_seconds |

### Running

```bash
# All evals
python3 scripts/skill_eval.py --model <your-model>

# Specific evals
python3 scripts/skill_eval.py --model <your-model> --evals 1,3

# Output to directory
python3 scripts/skill_eval.py --model <your-model> --output-dir /tmp/skill-eval

# Custom timeout (default: 300s)
python3 scripts/skill_eval.py --model <your-model> --timeout 200

# Compare with-skill vs no-skill baseline (recommended)
python3 scripts/skill_eval.py --model <your-model> --baseline --output-dir /tmp/skill-eval

# Repeat each eval N times to dampen LLM variance
python3 scripts/skill_eval.py --model <your-model> --repeat 3
```

### Output structure

With `--output-dir DIR/`, the output directory contains:

```
DIR/
├── benchmark.json         # JSON results
└── benchmark.md           # Human-readable summary
```

### Test cases

| ID | Test | Prompt (English gloss) | Skill assertions |
|----|------|-------------------------|-----------------|
| E1 | regular-upgrade | "I'm about to run pacman -Syu; before that, check Arch official news and the forum for anything needing manual intervention. I last updated about two weeks ago." | no-crash |
| E2 | long-time-no-upgrade | "I have a server that hasn't been updated for a year and a half; I used to just run pacman -Syu directly. Help me check if there's anything to watch out for this upgrade." | no-crash |
| E3 | custom-days | "Help me check whether the Arch community has mentioned any pipewire-upgrade-related issues in the last 90 days." | no-crash, uses-days-flag, mentions-pipewire |

The verbatim (originally Chinese) prompts are stored in `evals/evals.json`; the
table above is an English gloss.

### Full test suite (all 4 layers)

```bash
# Layers 1-2: Unit tests (fast, deterministic)
python3 tests/test_find_packages.py && \
python3 tests/test_scraping.py && \

# Layer 3: Integration tests (fast, deterministic, mock data)
python3 scripts/test_integration.py && \

# Layer 4: Skill evaluation (slow, requires pi CLI + API access)
python3 scripts/skill_eval.py --model <model-id> --output-dir /tmp/layer4
```

---

## File Structure

```
archlinux-upgrade-check-skill/
├── SKILL.md
├── references/
│   ├── design-decisions.md
│   └── test-plan.md              ← this file
├── scripts/
│   ├── arch_upgrade_check.py      ← Main script (supports --mock-* flags + ARCH_CHECK_MOCK_DIR env var)
│   ├── test_integration.py        ← Layer 3: integration test runner (reads evals.json, uses mock)
│   ├── skill_eval.py              ← Layer 4: Pi skill evaluation (pi -p --skill, tests LLM)
│   └── prepare_mock_fixtures.py   ← Generate mock HTTP fixtures from tests/fixtures/
├── tests/
│   ├── test_find_packages.py      ← Layer 1: package matching unit tests (19 assertions)
│   ├── test_scraping.py           ← Layer 2: HTML parsing tests (103 assertions)
│   └── fixtures/                  ← HTML snapshots for offline scraping tests
│       ├── news_page_1.html
│       ├── news_page_14.html
│       ├── bbs_page_1.html
│       ├── bbs_page_24.html
│       ├── bbs_page_50.html
│       ├── bbs_topic_314363.html
│       └── bbs_topic_solved.html
└── evals/
    ├── evals.json                 ← Test/eval definitions (script_assertions + skill_assertions)
    └── mock/                      ← Mock data for deterministic/reproducible testing
        ├── e1/pacman.log          ← E1: 14 days since last upgrade
        ├── e1/checkupdates.txt    ← 6 packages
        ├── e1/http/*.html         ← md5-hashed HTML fixtures
        ├── e2/pacman.log          ← E2: 550+ days → lookback_capped=true
        ├── e2/checkupdates.txt    ← 6 packages
        ├── e2/http/*.html         ← same HTML fixtures
        ├── e3/checkupdates.txt    ← E3: pipewire-focused packages
        └── e3/http/*.html         ← same HTML fixtures
```

## Test Order

1. Run Layer 1: `python3 tests/test_find_packages.py` (fast, deterministic)
2. Run Layer 2: `python3 tests/test_scraping.py` (fast, deterministic, uses fixtures)
3. Run Layer 3: `python3 scripts/test_integration.py` (fast, deterministic, uses mock data)
4. Run Layer 4: `python3 scripts/skill_eval.py --model <model>` (slow, requires pi CLI + API)
5. Review outputs
