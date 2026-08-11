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

### Superseded — commit 3 as planned in session 1

The plan below is what session 1 anticipated. Session 2 built it with six approved deltas; read
[Session 2](#session-2--2026-08-11) for what actually shipped. Kept for the record.

<details>
<summary>Original commit 3 plan</summary>

#### Next — commit 3: policy engine and tool registry

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

</details>

### Then

4. `tools/` bodies, mock executor, idempotency keys.
5. `llm/` mock provider + real provider config; `agents/` schemas, prompts, Triage, Planner.
6. `graph/` nodes, conditions, GraphBuilder wiring, `BeforeToolCall` policy hook.
7. `events/` simulator and fixtures, then **T1** (happy path).
8. **T2** (injection), **T3** (idempotency), **T4** (loop bound).
9. `cli.py`, README, final ruff pass. Restore `[project.scripts]` in `pyproject.toml` in this
   commit — it was removed rather than left pointing at a module that does not exist.

---

## Session 2 — 2026-08-11

### Position

Branch `main`. `origin/main` is still at `e29d85d`; everything below that line is **unpushed**.

| Commit | Subject | Pushed |
|---|---|---|
| `c578f32` | Move shared test constructors out of conftest into tests/helpers.py | no |
| `f1b97fa` | Drop the careloop console script until cli.py exists | no |
| `a9d899f` | Record CI and test-hygiene work in the session log | no |
| `0c1b294` | Add tool registry declaring argument classes and external effect | no |
| `d305f89` | Add policy verdict and reason-code vocabulary | no |
| `14d384b` | Add policy rules schema, loader and rules.yaml with load-time validation | no |
| `b74f17e` | Add deterministic policy engine with fixed match ordering | no |
| `9bd5ce5` | Add policy coverage and reason-code purity vacuity tests | no |

**328 passed, 2 skipped. `ruff check` and `ruff format --check` clean.** Verified under both
`pytest` and `python -m pytest`, and **at every one of the five commits individually**, not just at
the tip — each commit is a green point in history rather than a green endpoint.

### Done — commit 3: the deterministic authority layer

`careloop/tools/registry.py` and `careloop/policy/` (`verdicts.py`, `schema.py`, `loader.py`,
`engine.py`, `rules.yaml`). Nothing in this commit calls a model, mutates case state, or executes
anything; `PolicyEngine.evaluate` is a pure function of `(rules, registry, tool, args, case)`.

Five tools declared, four with external effect. `grant_portal_access.role` is the system's only
`AUTHORITY` argument and it decides a real verdict — `deny-authority-grant-below-trusted` — so the
third leg of the trust model is exercised rather than merely declared.

Eleven rules. Match order is fixed in `engine.py` and is **not** configurable from YAML: explicit
`DENY` → `REQUIRE_HUMAN` → `ALLOW` → default `DENY`. A rules file that could reorder its own
precedence could be talked into promoting an `ALLOW` above a `DENY`.

### Trust resolution — the decision the plan did not contain

The brief specified `evaluate(tool_name, args, case)` but never said how a raw argument value
acquires a `TrustLevel`. The whole engine rests on the answer, so it is recorded here in full.

**An argument's trust comes from looking its value up in `case.facts`. The lowest trust level among
matching facts wins. A value the case never recorded resolves `UNTRUSTED`.**

Consequences, all intended:

- Deny-by-default extends from rules to provenance. An argument that appeared from nowhere is
  untrusted, not unclassified.
- There is **no allowlist**. `TRUSTED` destinations are `Fact`s seeded from trusted config, so
  invariant 6's "no allowlist contents reach the model" is true because there is nothing to leak.
- Lowest-wins means a poisoned duplicate cannot be laundered by a legitimate twin: if a string was
  seen both in trusted config and in an attacker-controlled PDF, the PDF wins.
- One resolution path only. `Fact`-valued arguments are deliberately *not* also accepted — two
  paths that can disagree is one path too many.

**Matching is exact.** No case-folding, no whitespace trimming, no unicode normalisation.
Normalisation is a laundering surface (homoglyphs, casing, a trailing dot), and a fail-closed false
denial costs a replan while a false trust costs the property. Six near-miss variants of a trusted
address are tested, each alongside the verbatim address, so exactness is not achieved by denying
everything.

**Canonical comparison form**, in `engine.canonical_value` — closed and total, because a typed
argument with no canonical form would otherwise resolve `UNTRUSTED` silently:

| Type | Form |
|---|---|
| `str` | verbatim |
| `bool` | `"true"` / `"false"` (checked before `int`, which it subclasses) |
| `int` | decimal |
| `datetime` / `date` | `isoformat()`, **no timezone conversion** |
| anything else, incl. `float` and `None` | none — unmatchable, resolves `UNTRUSTED` |

No timezone conversion for the same reason as no case-folding: two spellings of one instant must
not match. `12:00+02:00` and `10:00+00:00` are the same moment and are tested to resolve
differently. `float` is excluded because its repr is unstable enough to be both a laundering and a
false-denial hazard, and no argument needs one.

### Deltas from the approved plan

All six were flagged before implementation and approved, with three corrections. Nothing was
adapted silently.

| # | Delta |
|---|---|
| D1 | 11 rules, not 3 — the coverage vacuity test requires a rule per external-effect tool |
| D2 | Extra module `policy/loader.py`, keeping YAML I/O out of the declarative schema |
| D3 | Registry fixes five tools; commit 4 inherits the list |
| D4 | `reason_code` required for `DENY`/`REQUIRE_HUMAN`, forbidden for `ALLOW` |
| D5 | Undeclared-argument denial reuses `NO_RULE_MATCH` rather than opening a sixth code |
| D6 | Trust resolution as recorded above |
| D7 | Condition vocabulary: `case_state_in`, `case_state_not_in`, `arg_trust_below`, `arg_trust_at_least`, `args_present` |

`arg_class` inside a rule is an **assertion, not a definition**. It has no runtime effect; its only
job is to fail startup when a rule's belief contradicts the registry, so a rule can document what
it guards without becoming a second declaration site.

### Guards proven non-vacuous by mutation

Every defence was deleted or inverted in turn and the suite re-run. Each mutation turned the suite
red; each was then reverted and the suite confirmed green.

| Mutation | Result |
|---|---|
| lowest-wins trust → highest-wins | 1 failed |
| match order → `ALLOW` first | 1 failed |
| `canonical_value` gains `.strip().lower()` | 1 failed |
| undeclared arguments ignored rather than denied | 1 failed |
| `deny-email-to-untrusted-recipient` deleted from `rules.yaml` | 1 failed |
| load-time `arg_class` cross-check disabled | 1 failed |
| `model_visible_reason` interpolates the tool name | 3 failed |
| an ungoverned external-effect tool added to the registry | 5 failed |
| a tool name written into a `REASON_TEXT` literal | 2 failed |

Both required vacuity tests assert in **both** directions, and each has a third test guarding
against passing trivially:

- **Coverage** (`test_policy_coverage.py`) — the shipped registry is fully covered; a synthetic
  registry carrying `wire_transfer` reports it; and `test_the_shipped_registry_actually_contains_
  external_effect_tools` stops full coverage of an empty set from counting. The converse drift is
  covered too: no rule may name a tool the registry does not have.
- **Reason purity** (`test_reason_purity.py`) — static half checks the mapping is total, distinct
  and free of every structured token; behavioural half evaluates every verdict path over
  adversarial values (`attacker@evil.example`, an injection string, a fake record id) across all
  ten case states and asserts each reason is byte-identical to a literal. The leak detector itself
  is tested against interpolated trust levels, rule ids, tool names and case states, and
  `test_diagnostics_do_carry_the_detail_the_timeline_needs` proves purity comes from separation
  rather than from the diagnostic also being empty.

Token matching is case-sensitive on the uppercase enum values, since an interpolated `StrEnum`
renders uppercase and that is the actual signature of a leak. Argument *names* are not checked:
invariant 6 does not list them, and the model already knows them because it is the caller.

### Resolved during the session

1. **Two commits were briefly red in history — FIXED by rewriting them.** `policy/__init__.py` was
   first committed exporting `engine`, which had not been committed yet; and the two new test
   modules imported `from tests.helpers`, which resolves only under `python -m pytest` — the exact
   regression session 1 fixed. The package exports are now built up incrementally across the three
   policy commits, the imports use `from helpers`, matching `test_state_machine.py`, and every
   commit was re-verified individually under both invocation modes.

### Carried into commit 6 — decision-cache TOCTOU

**Must not be forgotten.** The `BeforeToolCall` hook and the `@gated_tool` backstop are intended to
share a cached decision keyed on `(case_id, tool, args)`, so that layering the two enforcement
points costs one evaluation rather than two. That key is **insufficient**: it does not identify the
facts the decision was computed against. A decision computed before facts change would be reused
after, which is a TOCTOU window between the two enforcement points — precisely between the two
places the architecture claims are independent.

The key must also include whatever identifies the current facts state — `case.version`, or a
fingerprint over the facts tuple. Not in scope for commit 3; blocking for commit 6.

### Next — commit 4: tool bodies, mock executor, idempotency keys

The registry declares five tools with no callables. Commit 4 implements them against that
declaration; the registry is now the contract, so a body whose signature disagrees with its
`ToolSpec` should fail loudly.

---

## Open decisions

Carry these forward. Nothing here is blocking commit 3.

1. **Model provider benchmark — PENDING.** Bedrock Nova Lite is the intended primary and an
   OpenAI-compatible endpoint the fallback, both wired via env vars and never exercised in tests.
   Neither has been run against a real endpoint. No AWS credentials exist in the dev environment.
   Open: whether Nova Lite is strong enough for Planner intents, or whether Triage and Planner
   should sit on different models. Decide before the demo, not before the spine is finished.
2. **`ReasonCode` vocabulary is expected to grow — still five, with one known imprecision.**
   New codes are added centrally to the enum, never as free text on a rule. Two places currently
   stretch a code rather than open a new one, both deliberate and both worth revisiting:
   - `deny-authority-grant-below-trusted` reports `INSUFFICIENT_FACT_TRUST`. The shape is right
     ("not confirmed well enough") but the implication is not: an `AUTHORITY` argument can *never*
     be confirmed into acceptability, whereas a `VALUE` can. The mapped text carries no "confirm
     it and retry" directive for exactly this reason. If the distinction ever needs to be visible
     to the planner or greppable in the timeline, `AUTHORITY_NOT_TRUSTED` is the sixth code.
     `matched_rule_id` distinguishes the two cases in the timeline today.
   - An undeclared argument denies with `NO_RULE_MATCH` (approved delta D5). Honest — no rule
     matched — but it shares a code with the default deny.
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
