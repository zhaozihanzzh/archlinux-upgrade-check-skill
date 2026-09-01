# Mock Environment Design for Skill Evaluation

This document records the design for making `skill_eval.py`'s `--baseline`
comparison **fair and meaningful**: an LLM agent (pi) with and without the
skill must face the *same* simulated Arch Linux web, so the delta measures
the skill's orchestration value, not differential access to test data.

It is a **design record** (research + chosen approach), not yet implemented.

## 1. Why this is needed: the baseline-unfairness root cause

Current `--baseline` (`--no-skills`) is not a true baseline. With
`sensenova/glm-5.2`, both with-skill and baseline score 3/3 on E1 -- because
both ultimately run `arch_upgrade_check.py`, and the script reads mock data
through the `ARCH_CHECK_MOCK_DIR` environment variable (a local file path).
The agent's own `curl archlinux.org/news` hits the **real** internet (or
fails under `PI_OFFLINE`), so only the script can see the mock `shadow` BBS
post. The two configurations are not facing the same environment.

Concretely, the E1 assertions ("output mentions shadow/sg") cannot
distinguish:
- skill present -> agent reads SKILL.md -> runs script -> script reads mock ->
  reports shadow
- no skill -> agent `ls`-es, finds `../SKILL.md`, reads it, runs script anyway
  (GLM-5.2 is that proactive) -> same answer

So delta = 0 is an artifact, not a real signal.

## 2. Industry methodology: how Agentic RL researchers simulate environments

Surveyed three representative works:

| Work | Key idea | Relevant technique |
|---|---|---|
| SYNTHAGENT (ACL 2026, long 570) | mock tool env must be transparent to the agent | task-level finite mapping: same tool call -> same response (reproducibility); reward from observable behavior (subgoal checklist), not output text; "stable envs are critical -- non-reproducible responses make RL advantage estimates inconsistent" |
| AgenticAI-Supervisor (arXiv 2607.05773) | high-fidelity env scaffolding decoupled from execution | stateless container per rollout (no state leakage); reward = internal state validation vs golden answer, not text; constraint adherence = agent's claim cross-referenced against tool-API responses (output fidelity, anti-hallucination); trajectory efficiency reward (redundant-call penalty, min-tool coverage) |
| GEM (arXiv 2510.01051) | OpenAI-Gym-style env interface for agents | `reset()`/`step()`; Terminal tasks run in a containerized env; autoreset between episodes to prevent cross-episode leakage |

Four recurring paradigms:
1. **The mock is exposed as a real interface the agent reaches with its natural tools** (curl, function call), not a side-channel only the bundled script knows about. (SYNTHAGENT: mock tool system is transparent.)
2. **Reproducibility via frozen snapshots / replay mappings.** Same request -> same response, across runs. (SYNTHAGENT finite mapping; record/replay in MockServer.)
3. **Reward comes from the behavior trace, not the final text.** Check whether the agent called the right tool / reached the right state. (SYNTHAGENT subgoals; AgenticAI-Supervisor trajectory reward.)
4. **Stateless per rollout.** Each eval run starts from a clean sandbox so no leftover state leaks across runs. (GEM autoreset; AgenticAI-Supervisor container.)

Our current design violates paradigm 1 (mock is a file the script reads, not
an HTTP endpoint the agent curls). That is the root cause of the unfair
baseline. The fix is to make the mock a **transparent HTTP mock** the agent's
`curl` reaches, identically, with or without the skill.

## 3. Existing frameworks surveyed

| Framework | What it does | Fits pi? | Verdict |
|---|---|---|---|
| **MockServer** | Transparent HTTPS proxy (MITM with local CA). `HTTPS_PROXY` + `SSL_CERT_FILE` redirects *any* tool's curl with no code change. Record real traffic, replay byte-identical. Mock expectations per host/path. Retrieve captured traffic as JSON/HAR. Ships a headless CI capture script. | **Yes** -- pi is a Node process; bash-subprocess `curl` honors `HTTPS_PROXY`/`SSL_CERT_FILE`; LLM-API traffic bypassed via `NO_PROXY`. | **Selected.** Closest to turnkey. |
| inspect-ai sandboxing (AISI) | Docker/k8s sandbox; tools call `sandbox().exec()`; `network_mode: none` by default; per-sample `files` + `setup` script. | No. Sandboxing runs through inspect's own `sandbox()` interface; pi is an external bash-tool agent, would need to be wrapped as an inspect solver -- large refactor. | Borrow concepts (per-sample clean cwd, setup script), do not adopt. |
| Agent VCR | Record/replay MCP client-server traffic into cassettes. | No. Our agent curls over bash HTTP, not MCP. | N/A. |
| mitmproxy | Programmable HTTPS MITM proxy. | Yes, but lower-level than MockServer (no built-in expectation DSL / retrieve API / CA helpers). | Fallback if MockServer too heavy. |
| vcr.py | Record/replay HTTP at the Python-library level. | No. It instruments Python HTTP clients, not a subprocess `curl` spawned by a Node agent. | N/A. |

**Conclusion (revised)**: we initially selected MockServer for its turnkey
expectation DSL + retrieve API, but on implementation it needed a Docker
container per eval session -- heavier than the rest of the harness. We
instead adopted **mitmproxy** (the surveyed fallback): a single Python
addon (`mock_proxy_addon.py`) does the `md5(url) -> fixture` lookup with no
extra container. mitmproxy matches paradigms 1-3 (transparent HTTPS proxy +
local CA + deterministic per-URL replay) well enough; we lose MockServer's
retrieve-captured-traffic API, so Phase 2 trajectory assertions read the pi
session jsonl instead (sufficient). The MockServer survey is kept above
for reference.

## 4. Selected approach

Two layers, matching paradigms 1-3:

- **Layer A (transparent mock):** MockServer serves `evals/mock/*/http/` as if
  it were `archlinux.org` / `bbs.archlinux.org`, reached transparently by the
  agent's `curl` via `HTTPS_PROXY`. with-skill and baseline hit the same mock.
- **Layer B (trajectory assertions):** grade not just the final text but the
  agent's tool trace (did it run the script? did it curl the right pages? is
  the reported `shadow` traceable to a mock response, not fabricated?).
  Sourced from MockServer's retrieve API (captured HTTP) plus the pi session
  jsonl (tool calls).

Paradigm 4 (stateless) is already partially handled (fresh `--report-dir`
per run); optional hardening later.

## 5. Implementation design

### Phase 1 -- transparent mock via MockServer (fixes fairness)

Components:
1. `scripts/mock_server.py` -- thin launcher: starts MockServer (docker
   `mockserver/mockserver --proxy-setup`), waits for readiness, registers
   mock expectations mapping `archlinux.org/*` and `bbs.archlinux.org/*` to
   files under `evals/mock/<eid>/http/`, then exposes the local CA path.
   On exit, stops the container.
2. Mock expectation mapping (per eval). E1's `evals/mock/e1/http/` already
   contains the news page and the `viewtopic.php?id=314544` BBS page; the
   expectation matches by host + path and serves the file with the right
   `Content-Type`. Requests for unmapped paths under those hosts return 404
   (so the agent cannot "discover" real Arch content -- the mock is the
   whole world).
3. `skill_eval.py run_pi` env injection (only when a mock is configured):
   ```
   HTTPS_PROXY=http://localhost:1080
   SSL_CERT_FILE=<mockserver-ca.pem>      # curl trusts the MITM CA
   REQUESTS_CA_BUNDLE=<same>             # if any python is spawned
   NO_PROXY=<LLM provider hostnames>     # LLM API bypasses the proxy
   ```
   `NO_PROXY` is critical: the LLM provider (modelscope/sensenova/sjtu) must
   not be MITM'd (avoids key exposure and TLS errors). pi's own Node fetch to
   the LLM API either honors `NO_PROXY` (Node honors `NO_PROXY` from env) or
   is unaffected if pi sets its own HTTP agent -- to verify on first run.
4. `skill_eval.py` flow: start mock_server -> run pi (with-skill) -> run pi
   (baseline) -> stop mock_server. Both runs hit the same mock.

Outcome: baseline agent's `curl https://archlinux.org/news/` now returns the
mock news page; `curl .../viewtopic.php?id=314544` returns the mock BBS post.
The agent *can* in principle find `shadow` without the skill -- so a real
delta now means "the skill's orchestration is strictly better than the
agent doing it by hand", not "only the skill can see the data".

### Phase 2 -- trajectory assertions (makes the delta measurable)

New assertion type `trace_assertion` in `skill_eval.py`, graded from the pi
session jsonl (toolCall names + arguments) and/or MockServer retrieve API:

| Assertion | with-skill expects | baseline expects |
|---|---|---|
| `ran_skill_script` | session contains `arch_upgrade_check.py --report-dir` | absent |
| `read_match_file` | session contains `read match_*.json` | absent |
| `cross_referenced` | report.json packages vs BBS match (script does it) | agent must do it by hand; absent unless agent grep's both |
| `no_fabrication` (output fidelity) | reported `shadow` traceable to a tool result | reported `shadow` must trace to a captured `curl` of the BBS page |
| `no_irrelevant_fetch` | few/no curls of pages unrelated to pending packages | allowed but penalized in trajectory score |

These are the SYNTHAGENT "subgoal checklist" + AgenticAI-Supervisor
"output fidelity" applied to our task. They turn "did the output mention
shadow" (too weak) into "did the agent reach the right state via the right
tools" (paradigm 3).

### Phase 3 -- real snapshots instead of hand-written mock HTML

Current `evals/mock/e1/http/` is hand-written. For higher fidelity
(SYNTHAGENT's "high-fidelity environment"), record a real capture once:
- Start MockServer with `--proxy-setup`, point a real browser/curl at
  `archlinux.org/news` and `bbs.archlinux.org/viewforum.php?id=44` through
  it, freeze the recorded responses into `evals/mock/<eid>/http/`.
- MockServer's `recordedRequests.ndjson` (persisted) is the snapshot; replay
  is byte-identical. This is paradigm 2 done properly, and the snapshot is
  real-world-shaped rather than our hand-authored HTML.

### Phase 4 -- stateless per run (optional hardening)

Each eval run in a throwaway cwd with a fresh `--report-dir`, plus optionally
a bwrap/docker boundary so no `report.json` from a prior run leaks in.
We already reset `--report-dir` per run; this is belt-and-suspenders for
paradigm 4.

## 6. Open questions / risks

- **Does pi honor `HTTPS_PROXY`/`NO_PROXY` for its own LLM API calls?**
  pi is Node; Node fetch honors `NO_PROXY`. If it does not, the LLM API call
  would be MITM'd by MockServer (works, but exposes the key to the local
  proxy; acceptable with `--proxy-setup`'s local-only CA + `redactSecrets`
  option). Verify on first MockServer run.
- **MockServer dependency weight.** Adds a Docker container per eval
  session. Acceptable for a research harness; if too heavy, mitmproxy is the
  fallback (same paradigm, less batteries).
- **Mock coverage.** The agent may curl a path we did not mock (e.g. the BBS
  `viewforum.php?id=24` Announcements). Phase 1 returns 404 for unmapped
  paths under the mocked hosts, which is the correct signal ("that page
  doesn't exist in the mock world"). Phase 3 (real snapshot) widens
  coverage naturally.
- **CA trust in the sandbox.** `SSL_CERT_FILE` must reach the pi subprocess
  and its bash children. Since we control `run_pi`'s `env=`, this is a
  matter of passing the env var through; verify curl in the child actually
  picks it up (curl honors `SSL_CERT_FILE`).

## 7. Recommended sequencing

Do Phase 1 + Phase 2 together (minimal set that makes baseline fair AND
measurable), run the with-skill vs true-baseline comparison, and only
proceed to Phase 3 if the Phase-1 mock HTML is too weak to be
representative. Phase 4 deferred indefinitely unless state leakage is
observed.

## 8. Implementation status (as built)

Phase 1 is **implemented and verified at the mechanism level**:
- `scripts/mock_proxy_addon.py` -- mitmproxy addon serving md5(url)-named
  fixtures for archlinux.org / bbs.archlinux.org (mirrors the script's
  `_make_mock_urlopen` exactly).
- `scripts/mock_proxy.py` -- `MockProxy` class: start/stop mitmdump
  (detached process group, SIGKILL on stop so no orphan holds the port),
  generates `HTTPS_PROXY`/`SSL_CERT_FILE`/`NO_PROXY`(both cases) for pi.
  `NO_PROXY` host list is auto-read from `~/.pi/agent/models.json` so the
  LLM provider API is never MITM'd.
- `skill_eval.py` -- `--mock` starts the proxy; `--baseline-dir` sets the
  true-baseline cwd; `run_pi` injects `proxy_env`.
- Verified: `curl https://archlinux.org/news/?page=1` returns 200 + mock HTML
  (26 KB); unmapped URLs return 404; the script still reads its local mock
  via `ARCH_CHECK_MOCK_DIR` unaffected; the LLM API call (sensenova) is
  bypassed by `NO_PROXY` and returns normally.

**Portability for others to run** (one-time setup):
```
python3 -m venv scripts/.venv && scripts/.venv/bin/pip install mitmproxy
# then:
python3 scripts/skill_eval.py --model <provider/model> --evals 1 \
  --mock --baseline
```
`mock_proxy.py` auto-discovers `scripts/.venv/bin/mitmdump` or PATH.

### What Phase 1 alone does NOT fix (needs Phase 4)

The mock is transparent (fairness of *data access*), but the true baseline
still is not a real "no skill" condition, because the empty baseline cwd
lives **inside the skill tree** (bwrap makes the parent home read-only),
so the agent's `ls ..` reveals the whole skill dir. Observed: GLM-5.2 in
`baseline-harness/` ran `ls ..`, then `read SKILL.md`, `read
mock-env-design.md`, even `read scripts/_test_baseline.py`, figured out it
was being tested, and "cooperated" -- it never did a genuine no-skill run.

So the mock fixes paradigm 1 (transparent data) but not skill-file
visibility. Fully hiding the skill needs a container/VM where the agent's
filesystem view excludes the skill tree (Phase 4). The mock still has
standalone value: it is the prerequisite for any fair comparison, and
once Phase 4 lands the same `--mock` wiring just works.

**Update: Phase 4 is now built.** `--sys-mock` (bwrap + PATH shims + date
shim, see system-mock-design.md) hides the skill tree and makes the system
state consistent with the prompt. The fair baseline is now `--mock
--sys-mock` together. With the completed mock network (12+ fixtures incl.
viewforum?id=44 listing the shadow row + viewtopic?id=314544 shadow body),
both sides reach the same mock shadow topic -- see verification-design.md
"Update (final)" for the delta result (spoiler: GLM-5.2 reports shadow
both ways, delta 0).

### Model-behavior caveat (Phase 1)

GLM-5.2 also gets distracted by the proxy env vars visible in `env`: it
spent steps grepping `env | grep proxy`, running `env -u HTTPS_PROXY ...`
retry variants, and debugging mitmproxy instead of following SKILL.md.
Result: with-skill under `--mock` timed out (300s) despite the script
itself running fine under the same env (verified directly: exit 0, 1
match). This is the same class of issue as the earlier deepseek-chat
"doesn't read SKILL.md" finding: a model-layer distraction, not a
harness bug. A model that follows instructions strictly (Claude-class)
should not be distracted. Mitigations if needed: a prompt prefix noting
"a test proxy is in env; ignore it and follow the skill", or stricter
SKILL.md wording.

**Update (applied)**: the distraction was largely resolved by (a)
`--no-extensions` (disables web tools so the model has fewer escape
hatches) + (b) loading only the `-e nvidia-rate-limit-retry` extension
(429 back-off instead of abort), and (c) completing the mock network so
the agent no longer hits 404 'Cloudflare' dead-ends that drew it into
debugging the proxy. With these, GLM-5.2 now completes with-skill runs in
~100-220s and reports shadow.
