# Session log

Where the work stopped and what is still undecided. [`CLAUDE.md`](../CLAUDE.md) holds the standing
rules; this file holds the position. Update it at the end of every session.

---

## Session 1 — 2026-08-11

### Position

Branch `main`. `origin/main` is at `e29d85d`; everything below that line is **unpushed**.

| Commit | Subject | Pushed |
|---|---|---|
| `db3b797` | Scaffold project and pin Strands SDK surfaces | yes |
| `1499db4` | Add case state machine, trust model, and case stores | yes |
| `ddca021` | Restore natural comparison order in set assertions | yes |
| `a4ada01` | Add CLAUDE.md with standing project context | yes |
| `6eace34` | Pin strands-agents and add session log | yes |
| `a8cb417` | Pin pytest timeout method to thread so hangs fail identically on Windows and Linux | yes |
| `9cf338f` | Add GitHub Actions CI running ruff check, ruff format --check and pytest on Python 3.12 | yes |
| `e29d85d` | Add CI status badge to README | yes |
| — | Move shared test constructors out of conftest into tests/helpers.py | **no** |
| — | Drop the careloop console script until cli.py exists | **no** |
| — | Record CI and test-hygiene work in the session log | **no** |

**183 passed, 2 skipped. `ruff check` and `ruff format --check` clean.** The 2 skips are terminal
states in `test_failed_is_reachable_from_every_non_terminal_state` — correctly skipped, not
silently passing.

The suite is verified green under **both** `pytest` and `python -m pytest`, from a clean clone with
a fresh virtualenv. Both modes matter: see the resolved items below.

### Done

- **Commit 1** — repo scaffolding (Apache-2.0, `.gitignore`, `.gitattributes`, `pyproject.toml`)
  and the SDK spike. Findings in [`spike-findings.md`](spike-findings.md); four environment facts
  are mirrored into `CLAUDE.md` because they were expensive to establish.
- **Commit 2** — `provenance/` (TrustLevel, ArgClass, Fact, `promote()`) and `state/` (frozen
  `Case`, transition table, `CaseStore` protocol, SQLite + in-memory stores, timeline).
  `provenance/` moved forward from the planned commit 3 because `Case` embeds `Fact`.

Two invariants added during commit 2 that were **not** in the approved plan, both now covered by
tests: promotion is non-idempotent (otherwise trust launders by validating twice), and `Case`
collections are tuples so the freeze is deep rather than shallow.

### Out of band — CI and test hygiene

Not part of the numbered commit plan; the plan numbering below is unchanged.

- **CI.** `.github/workflows/ci.yml` — push and PR on `main`, Ubuntu, Python 3.12 only, no matrix.
  Installs `-e ".[dev]"`, then `ruff check`, `ruff format --check`, `python -m pytest -q`.
  **Runs with zero secrets configured**, which is checkable rather than aspirational: nothing in
  `careloop/` or `tests/` touches boto3, `os.environ`, or a provider SDK. `AWS_CONFIG_FILE` and
  `AWS_SHARED_CREDENTIALS_FILE` point at `/nonexistent` and metadata lookup is disabled, so a
  future test that quietly starts reaching for real credentials fails fast instead of passing on
  a developer's configured machine.
- **`timeout_method = "thread"` pinned** in `pyproject.toml` alongside the existing `timeout = 60`.
  The default is platform-dependent — SIGALRM on Linux, threads on Windows — so pinning is what
  makes a local Windows run and a CI Linux run agree. `thread` over `signal` because a signal
  timeout raises an ordinary exception *inside the test*, which a broad `except Exception` in the
  code under test can swallow; `sqlite_store.py` has two such handlers. **This is what has to hold
  for T4**: the loop-bound test must fail loudly rather than hang. Verified by execution — a
  synthetic unbounded loop dumps every thread's stack and exits 1 — and the ini key is confirmed
  live, since an invalid value fails startup.

### Resolved

Both were found by running CI's commands against a clean clone rather than the working tree.

1. **Test suite depended on the pytest invocation mode — FIXED.**
   `tests/test_state_machine.py` imported `tests.conftest`, which resolves only because
   `python -m pytest` puts the working directory on `sys.path`; under a bare `pytest` the suite
   failed at collection with `ModuleNotFoundError: No module named 'tests'`. Fixed at the cause:
   `make_case` and `FIXED_NOW` now live in `tests/helpers.py`, and `conftest.py` is fixtures only —
   pytest loads it, test modules do not import it. Verified under both invocation modes.
2. **Dead `careloop` console script — REMOVED.**
   `[project.scripts]` declared `careloop = "careloop.cli:main"` against a `careloop/cli.py` that
   does not exist, so the installed launcher failed on invocation for anyone following the README.
   Removed rather than stubbed. Reinstating it is one line and belongs with `cli.py` in commit 9,
   which the comment in `pyproject.toml` now says.

### Next — commit 3: policy engine and tool registry

Built together, not stubbed: load-time validation that cannot resolve against a real registry is
not validation.

- `tools/registry.py` — tool names and **argument classes** (registry owns them; `rules.yaml` may
  reference classes, never define them). Names and arg classes only at this stage; no tool bodies.
- `policy/verdicts.py` — `Verdict` enum and the closed `ReasonCode` enum:
  `DESTINATION_NOT_TRUSTED`, `REQUIRES_HUMAN_APPROVAL`, `WRONG_CASE_STATE`,
  `INSUFFICIENT_FACT_TRUST`, `NO_RULE_MATCH`. Extend centrally as rules demand; keep it closed.
- `policy/schema.py` — Pydantic model of the rules file. Each rule carries `reason_code`
  (model-visible, enum member) and `diagnostic` (timeline-only, free text).
- `policy/engine.py` — deny-by-default; match order **DENY → REQUIRE_HUMAN → ALLOW → default**,
  fixed in code and not configurable from YAML.
- `policy/rules.yaml` — the three rules from the approved plan.
- **Load-time validation fails startup** if any rule names an unknown tool, an unknown argument, or
  an unknown reason code.

Tests for commit 3: verdict matrix, reason-code closure, and the drift check (every rule resolves
against the registry).

### Then

4. `tools/` bodies, mock executor, idempotency keys.
5. `llm/` mock provider + real provider config; `agents/` schemas, prompts, Triage, Planner.
6. `graph/` nodes, conditions, GraphBuilder wiring, `BeforeToolCall` policy hook.
7. `events/` simulator and fixtures, then **T1** (happy path).
8. **T2** (injection), **T3** (idempotency), **T4** (loop bound).
9. `cli.py`, README, final ruff pass. Restore `[project.scripts]` in `pyproject.toml` in this
   commit — it was removed rather than left pointing at a module that does not exist.

---

## Open decisions

Carry these forward. Nothing here is blocking commit 3.

1. **Model provider benchmark — PENDING.** Bedrock Nova Lite is the intended primary and an
   OpenAI-compatible endpoint the fallback, both wired via env vars and never exercised in tests.
   Neither has been run against a real endpoint. No AWS credentials exist in the dev environment.
   Open: whether Nova Lite is strong enough for Planner intents, or whether Triage and Planner
   should sit on different models. Decide before the demo, not before the spine is finished.
2. **`ReasonCode` vocabulary is expected to grow.** The five codes cover the planned rules. New
   codes are added centrally to the enum, never as free text on a rule. Any growth should be
   recorded here so the vocabulary does not sprawl unnoticed.
3. **`WAITING_EXTERNAL` is in the state machine but has no producer yet.** Nothing transitions into
   it until a tool can genuinely block on a third party. It is tested as a table entry only. Either
   wire it in a later session or note in the README that it is reserved.
4. **Approval expiry is unmodelled.** `Approval` has `PENDING` / `APPROVED` / `REJECTED` and no
   timeout. A case can sit in `AWAITING_HUMAN` forever. The `AWAITING_HUMAN → FAILED` edge exists
   for it; nothing drives it. Fine for the hackathon spine; flag it in the README as known.
5. **Verification is not yet specified.** `VERIFYING` exists and `DONE` is reachable only from it,
   but what "verified" *means* per action type is undefined. T1 needs a concrete answer, even a
   mock one. Decide when writing the verify node.
6. **Repo visibility.** Private now, public at submission (2026-09-14). Nothing in the history
   contains secrets; the `.gitignore` covers `.env`. Re-check before flipping.

## Standing risks

- **The mock model is the only model the tests ever see.** Everything green means the spine is
  correct, not that a real model produces well-formed intents. The first real-provider run is a
  genuine unknown; budget time for it.
- **`strands-agents` is pinned to `==1.51.0`** deliberately. A bump must re-run the spike — the
  four verified facts are version-specific, and two of them (the `MultiAgentBase` node contract and
  the async-generator `structured_output`) fail loudly but confusingly if they shift.
