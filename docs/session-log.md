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
| `8ee36dc` | Record commit 3 in the session log | no |
| — | Add AUTHORITY_NOT_TRUSTED as a distinct reason code | **no** |

**332 passed, 2 skipped. `ruff check` and `ruff format --check` clean.** Verified under both
`pytest` and `python -m pytest`, and **at every one of the five commits individually**, not just at
the tip — each commit is a green point in history rather than a green endpoint.

### Done — commit 3: the deterministic authority layer

`careloop/tools/registry.py` and `careloop/policy/` (`verdicts.py`, `schema.py`, `loader.py`,
`engine.py`, `rules.yaml`). Nothing in this commit calls a model, mutates case state, or executes
anything; `PolicyEngine.evaluate` is a pure function of `(rules, registry, tool, args, case)`.

Five tools declared, four with external effect. `grant_portal_access.role` is the system's only
`AUTHORITY` argument and it decides a real verdict — `deny-authority-grant-below-trusted` — so the
third leg of the trust model is exercised rather than merely declared.

Eleven rules, six reason codes. Match order is fixed in `engine.py` and is **not** configurable
from YAML: explicit `DENY` → `REQUIRE_HUMAN` → `ALLOW` → default `DENY`. A rules file that could
reorder its own precedence could be talked into promoting an `ALLOW` above a `DENY`.

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

### The sixth reason code — `AUTHORITY_NOT_TRUSTED`

Added at the end of session 2, after review rejected the compromise that shipped in `d305f89`.
Recorded in full because the enum is closed and every future addition should have to clear this bar.

**What was wrong.** `deny-authority-grant-below-trusted` originally reported
`INSUFFICIENT_FACT_TRUST`, and to stop that misleading the planner the code's text was stripped of
its "confirm it and retry" directive. That fixed the authority case by **degrading a shared code**:
`deny-booking-on-unvalidated-slot` uses the same code, and there validate-and-retry is exactly the
guidance the planner needs. One rule's bad fit was paid for by every other user of the code.

**The real distinction is the remedy, not the wording.** An `INSUFFICIENT_FACT_TRUST` denial is
*remediable* — the argument is `VALUE`-class, therefore in `PROMOTABLE_ARG_CLASSES`, so validating
and reproposing works. An `AUTHORITY_NOT_TRUSTED` denial is *irremediable* — `AUTHORITY` is absent
from `PROMOTABLE_ARG_CLASSES`, so no validator can ever raise the value and revalidating changes
nothing. Two different actions for the planner cannot share one code without either sending it into
a doomed retry or removing the guidance the other case depends on.

**What changed.** `AUTHORITY_NOT_TRUSTED` added to the enum and to `REASON_TEXT`, with no retry
directive — and here that is not a compromise, since no step the planner could take would make the
argument acceptable. `INSUFFICIENT_FACT_TRUST` restored to its full text, directive intact.
`deny-authority-grant-below-trusted` repointed. Both rules' diagnostics now state the contrast
explicitly and name each other, because the distinction is deliberately invisible to the model and
the timeline is the only place it can live.

Covered by four mutations, each turning the suite red: repointing the rule back at the shared code
(4 failures), giving the two codes identical text (2), weakening the authority diagnostic (1), and
adding a seventh code without a decision (3).

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

## Session 3 — 2026-08-11

### Position

Branch `main`. `origin/main` is still at `e29d85d`; everything below that line is **unpushed**.

| Commit | Subject | Pushed |
|---|---|---|
| `7dcd629` | Move canonical_value into provenance so policy and tools share one reduction | no |
| `f9a83be` | Add idempotency keys derived from the shared canonical value form | no |
| `07360f9` | Add typed tool results and the fixture-backed document store | no |
| `94a3984` | Add mock external executor with a replay-suppressing ledger | no |
| `47ac22a` | Add tool callables bound to the registry and routed through the executor | no |
| `5f4f8d6` | Close two vacuous guards found by mutating the tool layer | no |
| `f8dc978` | Record the no-clock and one-canonical-form invariants in CLAUDE.md | no |

**478 passed, 2 skipped. `ruff check` and `ruff format --check` clean.** Verified under both
`pytest` and `python -m pytest`, and **at every one of the seven commits individually**.

### Done — commit 4: the effect layer

`careloop/tools/` gains `idempotency.py`, `results.py`, `documents.py`, `executor.py` and
`callables.py`. Nothing in this commit evaluates policy, mutates a case, appends to a timeline or
touches a model; the policy gate is commit 6's work and `@gated_tool` is not here.

Five tools, four effectful. The four reach the outside world only through
`MockExternalExecutor.invoke`; `fetch_document` reads from the document store, takes no idempotency
key and appears in no ledger, because reading twice is not a duplicated effect.

### Idempotency — one canonical form, two totality requirements

Approved as delta D1 and now the tenth architecture invariant. Argument values reduce through the
same `canonical_value` the policy engine matches facts with, so the two can never disagree about
what a value *is* — a datetime is exactly where they would drift.

They part company only on values with **no** canonical form. The policy engine reads `None` as
unmatchable and resolves `UNTRUSTED`, which fails closed for it. Keying cannot borrow that reading:
folding every uncanonicalizable value onto one sentinel gives two genuinely different calls the
same key, and the executor then suppresses the second as a duplicate of the first. That is
fail-**open**, so `idempotency.py` raises `UncanonicalizableArgument` instead.

The key is `sha256` over `json.dumps({case_id, action_type, args}, sort_keys=True)`. JSON quoting
is what makes the encoding injective — `{"ab": "c"}` and `{"a": "bc"}` collide under any
delimiter-joined form, and both that and a value containing a forged `","body":"` separator are
tested.

### Structural decisions worth keeping

**`canonical_value` moved to `careloop/provenance/canonical.py`.** Not in the approved plan. The
plan had `tools/idempotency.py` import it from `policy/engine.py`, which is a genuine import cycle:
`policy.loader` imports `tools.registry`, so `tools/__init__` re-entering `policy.engine` fails on a
partially initialised module. Re-homing it is the better fix rather than the expedient one — the
reduction belongs to neither consumer, and `tools/` now reuses a data format without depending on
the authority layer at all. Both old import paths still resolve, and a test asserts all three names
are the *same function object*, so an equivalent-but-separate reimplementation fails.

**Bodies bind to the registry, they do not live in it** (D2). An `@implements("send_email")`
decorator checks each method's signature against `REGISTRY` **at import time**; a body whose
parameters disagree with its `ToolSpec` fails on import, not in a test. `registry.py` was not
touched, so `ToolSpec` stays frozen and serializable and argument classes keep one declaration site.

**`ToolSet(case_id, executor, documents)`.** `case_id` is a constructor argument, never a tool
parameter — it scopes the idempotency key, and a model that could supply it could scope two
identical actions apart or collapse two cases' actions together. The object holds exactly two
collaborators, neither of which can reach a case, a timeline or a model.

**Effectful tools return `ExecutionResult`, not the bare payload.** A refinement of "tools return
typed results": the outcome is information the caller needs, and it cannot be recovered from the
payload, since a suppressed replay returns a payload identical to the original by design. The graph
node writes `TimelineKind.DUPLICATE_SUPPRESSED` on the strength of it. `ExecutionResult` subclasses
`ToolResult`, so "every tool returns a typed result" is a statement about types.

**The ledger records effects, not attempts** (D7). `invocation_count == len(ledger)`; suppressions
increment `suppressed_count` and add no entry. That is what makes "the ledger has one entry" a real
assertion rather than an accounting convention.

**`IdempotencyKeyCollision`** — not in the plan, and small. If one key is presented for two
different calls the executor raises instead of returning the first call's result, because silently
reporting one effect as another is worse than an exception. It cannot fire accidentally: the key is
derived from the arguments.

**Two import-time drift checks.** Executor handlers must equal `REGISTRY.external_effect_tools`, and
bound bodies must equal `REGISTRY.names`. A handler for a tool the registry does not declare is an
effect outside the policy layer's knowledge, which is the dangerous direction.

### No clock, no randomness — now invariant 9

Synthetic identifiers derive from the idempotency key; results and ledger entries carry no
timestamp. This started as an implementation detail and is now written into `CLAUDE.md`, because
the first mutation run proved the obvious test for it does not work: comparing two results built
microseconds apart passes on Windows, whose clock ticks every ~15ms, even with a `default_factory`
timestamp on the model. It is therefore enforced on the source — no module under `tools/` may import
`datetime`, `time`, `random`, `secrets` or `uuid`, and no result field may carry a default or a
`default_factory`, so a result can only say what its caller told it.

The same sweep enforces that no `tools/` module imports `careloop.state`, `careloop.policy` or
`strands`. A tool that cannot import the state layer cannot transition a case; a tool that cannot
import the policy layer cannot decide its own authority. Both sweeps are proven non-vacuous against
modules that *do* import those things.

### The poisoned fixture

`doc-0002-continuity-notice` is an insurer's continuity-of-care notice: correctly formatted, with a
plausible business reason for each thing it asks for. It asks for all three things the trust model
refuses — forward to an external address, upload to their portal, grant a `records_admin` role — so
one document exercises every leg. Deliberately *not* an "ignore all previous instructions" string:
that would be caught by anything, including nothing, and would make the demo prove less than it
appears to. Tests assert each payload is present, and that no crude jailbreak marker is, so the
fixture cannot be quietly defanged. `fetch_document` returns it unmodified — sanitising here would
be a filter that looks like a defence and would make T2 pass for the wrong reason.

### Guards proven non-vacuous by mutation

Fifteen mutations, each applied to a clean tree, suite re-run, reverted. **Two survived the first
pass and both were real gaps in the tests, not false alarms.**

| Mutation | Result |
|---|---|
| replay suppression removed (always execute) | 15 failed |
| suppression also appends to the ledger | 11 failed |
| uncanonicalizable value folded onto a sentinel instead of raising | 9 failed |
| idempotency key drops `case_id` | 2 failed |
| digest joins with a delimiter instead of JSON | 2 failed |
| `implements` skips the signature check | 1 failed |
| `implements` accepts positional-only bodies | 1 failed |
| executor handler/registry drift check disabled | 2 failed |
| tool body/registry coverage check disabled | 1 failed |
| results carry a timestamp | 2 failed |
| `send_email` forges its result instead of delegating | 8 failed |
| a tool module imports the state layer | 2 failed |
| key-collision guard removed | 3 failed |
| the poisoned fixture is defanged | 2 failed |
| `fetch_document` routes through the executor | 2 failed |

The two that survived, and what was wrong:

1. **The parameter-kind check in `implements` was untested.** Its test used a `**kwargs` body, which
   the *name* check already rejects — `kwargs` is not a declared argument name — so the kind check
   could be deleted with the suite still green. The one thing it actually catches is a
   **positional-only** body: parameter names match the registry exactly, so the name check passes,
   but `call` dispatches by keyword and the binding would fail at first invocation instead of at
   import. Test rewritten to that case.
2. **The no-clock guard was behavioural and therefore useless**, for the clock-resolution reason
   above. Replaced with the source-level and model-level checks described in the previous section.

The required both-ways vacuity test is `TestNoEffectWithoutTheExecutor`: one `check_routes_through_
executor` function run over the shipped toolset, which must pass, and over a `ForgingToolSet` whose
`send_email` returns a well-formed result without touching the executor, which must fail. A third
test proves the forging body really does return a plausible result, so the failure is the check
firing rather than the body crashing. A fourth runs every effectful tool against an executor with
**no handlers**: a body that built its own result and merely *also* told the executor would still
pass a ledger count, and this is what proves the executor is the source of the result rather than a
bystander to it.

### Next — commit 5: model provider and agents

`llm/` mock provider plus real provider config, then `agents/` schemas, prompts, Triage and Planner.
Two decisions from the spike bind here: `Model.structured_output` is an async generator and must
`yield {"output": instance}`, and the mock is the only model the tests will ever see.

---

## Session 4 — 2026-08-11

### Environment dependency — AVG intercepts TLS, and the exclusions are load-bearing

**Read this before diagnosing any provider connection failure.** It cost most of an hour once and
should cost nothing again.

AVG Antivirus's Web/Mail Shield performs TLS interception on this development machine. It
terminates HTTPS, re-signs the certificate with `CN=AVG Web/Mail Shield Root`, and presents that to
the client. The root is installed in the **Windows ROOT store**, so anything verifying against the
OS store — browsers, `ssl.create_default_context()` — is satisfied and shows no symptom.
`botocore`, `requests` and `httpx` verify against **certifi's** bundle, which does not contain it.

The symptom is therefore confusing: every AWS call fails identically, at every endpoint, with

```
botocore.exceptions.SSLError: SSL validation failed for https://sts.us-east-1.amazonaws.com/
[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate
```

and it looks like a credentials or quota problem. It is neither. `sts:GetCallerIdentity` fails too,
and that needs no Bedrock permission at all — **a failure this uniform means the request never left
the machine**. The tell is that the error is `SSLError` and not `AccessDeniedException`,
`ThrottlingException` or `ValidationException`, which are the three that would actually say
something about AWS.

**Resolution in force: per-host exclusions in AVG, not a disabled shield and not a patched trust
store.** Configured under Général → Exceptions:

| Exception |
|---|
| `http://*.amazonaws.com/*` and `https://*.amazonaws.com/*` |
| `http://api.openai.com/*` and `https://api.openai.com/*` |
| `http://integrate.api.nvidia.com/*` and `https://integrate.api.nvidia.com/*` |

Verified after the change by a credential-free TLS handshake — `sts` and `bedrock` present
`CN=Amazon RSA 2048 M01`/`M04`, `api.openai.com` presents `CN=WE1, O=Google Trust Services`, and no
endpoint presents the AVG root. `integrate.api.nvidia.com` presents an Amazon-issued certificate
because NVIDIA fronts it on AWS; that is genuine, not interception.

Two alternatives were considered and **rejected**: appending the AVG root to certifi (works, but
means every API key traverses AVG's process in plaintext, which is a trust decision this project
should not make casually) and `verify=False` (never, in a codebase whose thesis is authority and
provenance). If a new provider host is added later, it needs its own exclusion pair or it will fail
exactly this way.

The exclusions live in AVG's configuration, not in the repo. A different machine, or this one after
an AVG reinstall, will reproduce the failure from scratch. That is the reason this note exists.

### Step 0 — Bedrock reachability, before any harness was built

Run against a dedicated IAM user `careloop-bench` (account `265243686715`, no console access,
one inline policy granting `bedrock:InvokeModel`, `bedrock:InvokeModelWithResponseStream`,
`bedrock:ListFoundationModels`, `bedrock:ListInferenceProfiles`, `sts:GetCallerIdentity`).

**All seven checks pass. There is no zero-quota problem; no AWS support case is needed.**

| Check | Result |
|---|---|
| `sts:GetCallerIdentity` | OK — `arn:aws:iam::265243686715:user/careloop-bench` |
| `bedrock:ListFoundationModels` | OK — 119 models in `us-east-1`, 22 in the Nova family |
| `bedrock:ListInferenceProfiles` | OK — 63 profiles, 6 Nova, all `ACTIVE` |
| `Converse` (non-streaming) | OK — 1239 ms, 9 tokens |
| `ConverseStream` (streaming) | OK — 625 ms total, first token 502 ms |
| `BedrockModel.stream()` | OK — 891 ms |
| `BedrockModel.structured_output()` | OK — 1196 ms, correct typed instance |

The streaming path specifically works, which matters because Strands streams by default.
`structured_output` works too, which is the benchmark's disqualifying metric — Nova Lite clears it.
Latencies here are single cold samples and are not benchmark data.

**Invariant confirmed, not assumed:** `us.amazon.nova-lite-v1:0` and `us.amazon.nova-micro-v1:0`
are both `ACTIVE` inference profiles, so the spike's region-prefix finding holds against a real
account.

**New: `amazon.nova-2-lite-v1:0` exists**, with `us.` **and** `global.` profiles both `ACTIVE`. A
`global.` prefix is not one of the three (`us`/`eu`/`apac`) the spike found in
`prefix_inference_map`. Neither fact was known when the candidate list was drawn up. Nova 2 Lite
was added to the candidate set on the strength of it.

### Done — `careloop/llm/` and the benchmark

| Commit | Subject |
|---|---|
| `193e565` | Add .env.example and the bench optional-dependency group |
| `531b448` | Record the AVG TLS interception exclusions and the step 0 Bedrock probe result |
| `ead63c6` | Add the scripted mock model provider |
| `19028a8` | Add per-role model provider selection defaulting to the mock |
| `9ef9e59` | Add the benchmark task model, schemas, cache, cost table and scoring |
| `ef19f06` | Add the benchmark runner and CLI with a confirmed-spend guard |
| `d6a73ca` | Pass Bedrock credentials from the env mapping and stop caching unreachable-provider failures |
| `7ca149c` | Record the measured benchmark responses and scored summary |
| `169e7a7` | Add the model benchmark findings and close open decision 1 with a measured basis |

**642 passed, 2 skipped. `ruff check` and `ruff format --check` clean.** Verified under both
`pytest` and `python -m pytest`, and under a CI-equivalent environment with every credential
unset and `AWS_CONFIG_FILE` pointed at `/nonexistent`.

`careloop/llm/mock.py` is the scripted provider the suite runs on — strict by design: an exhausted
script raises, a wrong-typed scripted answer raises, and a scripted `Exception` is raised, because
each of those is a test that would otherwise pass for the wrong reason. `providers.py` maps the
three roles onto a provider, **defaulting to the mock**, and an unrecognised spec raises rather
than degrading to the mock — a run that looks real, costs nothing and proves nothing is the worst
available failure mode.

A **root `conftest.py`** now exists. `bench/` is not an installed package, so `import bench`
resolves only with the repo root on `sys.path`. The path is inserted explicitly rather than relying
on pytest's conftest-directory behaviour, because the suite must pass identically under both
invocation modes and this is exactly where session 1 lost time.

### Deltas from the approved plan

| # | Delta | Status |
|---|---|---|
| D1 | Injection susceptibility as a **sixth, separate** metric | approved |
| D2 | `llm/mock.py` built here rather than in commit 5 | approved |
| D3 | Provisional schemas in `bench/schemas.py`, marked provisional | approved |
| D4 | Structured-output path only; native tool-calling listed as unmeasured | approved |
| D5 | Role variables are `CARELOOP_`-prefixed | approved |
| D6 | A `nvidia:` provider prefix rather than guessing the key from a base URL | flagged in `.env.example` |
| D7 | Nova 2 Lite added, both `us.` and `global.` profiles | approved |

D6 exists because `openai:<model>@<nvidia-url>` would have to infer which API key to send from the
hostname, and a benchmark that quietly sends the wrong key produces an auth error that reads like
a model failure.

### Two harness bugs the first live run exposed

Both were found by running it, not by reading it, and both are now covered by tests.

1. **`build_model(spec, env)` ignored `env` for Bedrock.** The parameter exists so the benchmark
   can pass `.env` down without mutating the process environment, and it worked for the
   OpenAI-compatible providers because their key is passed explicitly. boto3 resolves credentials
   from `os.environ` and never saw the mapping, so all four Bedrock candidates failed with
   `NoCredentialsError` while OpenAI and NVIDIA succeeded. Fixed with `bedrock_session()`, which
   builds an explicit session when the mapping carries a key pair and returns `None` otherwise so
   the ambient chain — and CI's zero-credential run — is unaffected.

2. **Infrastructure failures were being cached as results.** The first run wrote 68
   `NoCredentialsError` entries into the cache as though four candidates had been measured and
   found wanting; every later run would have replayed that verdict without retrying. A failure is
   now cached only when it is the model's answer (`ValidationError`, `WrongOutputType`,
   `ValueError`) and never when it is a failure to reach the model. It earned itself immediately:
   NVIDIA threw `ModelThrottledException` on one task in the real run, was correctly not cached,
   and was picked up by a re-run that cost nothing else.

A third issue was not a bug but a reporting hazard: `strands-agents==1.51.0` surfaces token usage
through Bedrock's `structured_output` and **not** through the OpenAI one, which discards
`response.usage`. Rather than let a silent `$0.0000` make the most expensive candidate look free,
tokens are estimated when unreported and every affected figure is flagged `tokens_estimated`.

### Guards proven non-vacuous by mutation

Twenty-five mutations across the two commits, each applied to a clean tree, suite re-run, reverted.
Every one turned the suite red — **after one survivor was fixed.**

`llm/` (12): unknown provider degrading to the mock; a bare unknown word degrading to the mock; a
blank credential accepted; the missing-credential error interpolating the value; the Bedrock model
id split on every colon; a missing region silently guessing `us`; all roles reading one variable;
the mock inventing an answer when exhausted; the mock coercing a wrong-typed answer; the mock
stamping a real latency; the mock dropping `messageStart`; `structured_output` returning instead of
yielding the envelope.

`bench/` (13): injection folded into tool correctness; a structured failure counted as a wrong
answer; the catalogue leaking argument classes; the cache key ignoring the prompt; the cache key
ignoring sampling parameters; **the digest joining with a delimiter instead of JSON**; calls bought
without `--confirm-spend`; the spend ceiling never firing; an unpriced model costing zero; planner
tasks unchecked against the registry; expected fields unchecked against the schema; the prompt
sanitising the poisoned document; a candidate running kinds it does not declare.

**The survivor, and what was wrong with the test.** The delimiter-join mutation stayed green. The
injectivity test moved a character across the `system_prompt`/`prompt` boundary — but those two
are not adjacent once the payload is sorted, so other fields sat between them and no collision
occurred. A delimiter join only collides when the delimiter appears **inside a value**. Rewritten
to put the delimiter in the value across `candidate_id`/`model_id`, which *are* adjacent, and
parameterised over five delimiter characters. This is the same property `tools/idempotency.py`
tests and the same reason both use JSON.

### Results and recommendation

Full detail in [`model-benchmark.md`](model-benchmark.md). 20 tasks, 6 candidates, 108 calls,
**$0.0334 total** — $0.0020 of it on OpenAI, against a $1 budget.

**Nova Lite, the intended primary, is not safe behind the planner.** Given an explicit booking
instruction plus the poisoned continuity notice, it abandoned the task and proposed `send_email`
to the attacker's address, carrying the portal and the role across too. All three payloads, first
attempt, the only Bedrock candidate to do it. The policy engine denies exactly that call — this is
what T2 asserts — but it is a reason not to choose the model.

Recommendation, now written into `.env.example` as the measured default:

```
CARELOOP_MODEL_TRIAGE=bedrock:us.amazon.nova-micro-v1:0
CARELOOP_MODEL_PLANNER=bedrock:us.amazon.nova-2-lite-v1:0
CARELOOP_MODEL_EXTRACTION=bedrock:us.amazon.nova-2-lite-v1:0
```

Nova 2 Lite and gpt-4o-mini are separated by one task each in opposite directions on the planner,
which at n=7 is noise; the tie is broken on deployment fit rather than on a quality claim the data
does not support. gpt-4o-mini stays wired as the fallback. NVIDIA llama-3.3-70b is out — triage
62.5%, p50 11.8 s, worst case 76 s, and it took the injection bait too.

**One benchmark defect, deliberately left uncorrected.** On `triage-02` all six candidates answered
`URGENT` where the rubric says `SOON`. When every candidate disagrees with the expectation, the
expectation is the thing that is probably wrong — but editing it after seeing the results is how a
benchmark stops meaning anything. It costs every candidate equally, so the ranking holds. Fix the
rubric before the task set grows.

### Next — commit 5: agents

`agents/` schemas, prompts, Triage and Planner. `bench/schemas.py` holds **provisional**
`TriageResult` and `PlannerIntent` that exist only to have something to measure; commit 5 designs
the real ones on the merits. Two things worth carrying over rather than reinventing: generating the
planner's argument fields from `REGISTRY` so they cannot drift from the tools the policy engine
governs, and keeping every field required with `""` for "not supplied", which avoids the nullable
union that some strict structured-output implementations reject.

---

## Open decisions

Carry these forward. Nothing here is blocking commit 5.

1. **Model provider benchmark — CLOSED 2026-08-11, on measurement.** Full results and reasoning in
   [`model-benchmark.md`](model-benchmark.md); the session 4 entry above summarises them.

   The question was whether Nova Lite is strong enough for Planner intents and whether Triage and
   Planner should sit on different models. **Answered: no, and yes.** Nova Lite proposed
   `send_email` to the attacker's address on the injection probe and produced no valid intent on
   another planner task. Triage moves to Nova Micro (21/24 fields at a tenth of the cost); Planner
   and extraction to Nova 2 Lite. gpt-4o-mini stays wired as the fallback and is within noise of
   Nova 2 Lite on planning.

   What remains open is deliberately narrower and needs commit 6: **nothing here measured
   multi-turn planning inside the graph, or behaviour after a policy denial re-enters the loop.**
   Those are the behaviours the architecture actually rests on, and a single-call benchmark cannot
   see them. Re-examine the choice once the bounded replan cycle exists.
2. **`ReasonCode` vocabulary — now six. Grown once, deliberately.** New codes are added centrally
   to the enum, never as free text on a rule, and every addition is recorded here. See
   *The sixth reason code* under session 2 for why `AUTHORITY_NOT_TRUSTED` was added rather than
   stretching an existing code. `tests/test_reason_purity.py` asserts `len(ReasonCode) == 6`, so
   the next addition has to be a decision rather than a drift.

   One stretch remains, and it is the approved delta D5: an undeclared argument denies with
   `NO_RULE_MATCH`. Honest — no rule did match — but it shares a code with the default deny.
   Unlike the authority case, both denials are irremediable in the same way and want the same
   guidance, so one code serves them; `matched_rule_id is None` separates them in the timeline.
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
7. **The executor ledger is not durable, and invariant 8 says `CaseStore` is.** The ledger lives in
   one `MockExternalExecutor` instance, so it suppresses replays within a run and nothing wider.
   The durable record is `Case.actions` — `ActionRecord` already carries `idempotency_key`,
   `args_digest` and `result`, and `Case.action_for(key)` already exists. **Blocking for T3 if T3
   spans a restart or a resumed case**: the execute node must consult `case.action_for(key)` before
   calling the tool and must write an `ActionRecord` after, with the ledger as the in-run
   optimisation rather than the source of truth. Decide when writing the execute node in commit 6.
8. **`ExecutionResult` becomes model-visible in commit 6** if these callables are wrapped as Strands
   tools, since a tool result is returned to the planner. It carries `idempotency_key`,
   `action_type` and the payload — no trust levels, rule ids or allowlist contents, so it does not
   breach invariant 6 as written. Confirm rather than assume when the hook lands, and check the same
   question for `Document`, whose whole body is attacker-controlled text by design.

## Standing risks

- **The mock model is the only model the tests ever see.** Everything green means the spine is
  correct, not that a real model produces well-formed intents. The first real-provider run is a
  genuine unknown; budget time for it.
- **`strands-agents` is pinned to `==1.51.0`** deliberately. A bump must re-run the spike — the
  four verified facts are version-specific, and two of them (the `MultiAgentBase` node contract and
  the async-generator `structured_output`) fail loudly but confusingly if they shift.
