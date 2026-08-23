# Frontend contract map

Session D1, 2026-08-13. **Diagnosis only** — no adapter code was written, no UI was changed, and
no backend was touched. What follows is the field-by-field reconciliation between the approved
frontend package and the API surface session C closed.

> **Session D2 built it, and four things here turned out to be wrong.** They are corrected in place
> and marked **`D2:`**, with the original claim left visible — a map that quietly rewrites itself is
> a map nobody can check. The corrections are:
>
> 1. **`EvidenceLens` is not without a data path** (§3.4, §4 question 4). Extraction records one
>    fact per document page whose value is the whole page, and those reach the API.
> 2. **The evidence grid needs a filter** it did not know about, for the same reason.
> 3. **The timings are measurable now** (§5 divergence 5), and they are fast enough that the
>    dashboard needs pacing to show what it is showing.
> 4. **`VITE_API_BASE_URL` was serving two purposes** (§5 divergence 9) and could not.
>
> The rest of the map held. Session D2's own record is in
> [`session-log.md`](session-log.md#session-d2--2026-08-13).

The frontend is the Claude Design handoff, unpacked to `frontend/` at the repository root — the same
separation `demoworld/` and `bench/` have. It is **approved and locked**: `frontend/HANDOFF.md` §16
lists what must not change, and visual divergence from `frontend/reference/screenshots/` is a defect.

Read this with [`CLAUDE.md`](../CLAUDE.md) and [`session-log.md`](session-log.md) — particularly
open decisions 18 and 19, which this session confirms and does not close.

---

## 1. Does it build

Yes, unmodified. Node v24.11.0, npm 11.6.1, Windows.

| Command | Result |
|---|---|
| `npm install` | exit 0 — 120 packages, `package-lock.json` generated. 4 advisories (3 moderate, 1 high), all dev-only in the vite/esbuild chain. |
| `npm run typecheck` | clean |
| `npm run build` | clean — 67 modules, `index-*.js` 209.93 kB (64.18 kB gzipped), CSS 2.62 kB |
| `npm run dev` | serves on `:5173` |

**Nothing was fixed, because nothing was broken.** `HANDOFF.md` §4 warns that these commands were
never executed in the design environment and asks that anything surfacing be fixed before the UI is
touched. Nothing surfaced.

### The Python CI job does not pick it up

`pyproject.toml` sets `testpaths = ["tests"]`, so pytest never descends into `frontend/`. Ruff lints
Python only and honours `.gitignore`. Verified with the frontend present and installed:

```
ruff check .            All checks passed!
ruff format --check .   148 files already formatted
pytest -q --collect-only 1722 tests collected
```

`frontend/.gitignore` keeps `node_modules`, `dist` and `.env` out; `git add -An frontend` stages 66
files with zero from either. `package-lock.json` **is** committed — it is the record of what the
build above was verified against.

### Visual comparison — no divergence in the application

All four routes captured in both themes plus the Under the Hood drawer, headless Chromium, at the
goldens' own dimensions. Zero page errors, zero console errors, zero failed requests apart from one
transient Google Fonts `woff2` abort in one of twelve runs. Layout, copy, colour and typography match
all ten references.

Three findings about the **reference material**, not the app:

1. **All ten `reference/screenshots/*.png` are JPEGs.** Every file begins `ff d8 ff e0` — JPEG
   SOI/JFIF, not the PNG signature — at 909×540, lossy, cropped to the fold. They are eyeball
   references. They cannot be pixel goldens for a visual regression test, and a future session that
   assumes otherwise will waste an hour discovering it.
2. **The capture viewport is 909px, not 1280px.** `DESIGN_REFERENCE.md` said 1280. Home's content
   column is `max-width: 820px` centred and its left edge sits at x=44 in the golden, which is
   exactly `(909 − 820) / 2`. A first comparison pass at 1280 showed the demo launcher's cards not
   wrapping and read as a divergence; at 909 the capture is pixel-identical. **Corrected in
   `DESIGN_REFERENCE.md` in this session** — it is a one-line doc fix and leaving it wrong would cost
   the next session the same hour.
3. `useTheme`'s localStorage key is `careloop:theme`. `HANDOFF.md` §10 says "persists to
   localStorage" without naming it; anything scripting the themes needs the exact string.

### One runtime finding, deferred to D2

Newsreader, Instrument Sans and IBM Plex Mono are fetched from `fonts.googleapis.com` at page load
(`frontend/index.html`). This is not a build break, but it is a **filming hazard**: a bad network
during the demo recording renders the entire editorial identity in fallback faces, and the type is
most of what §8 of the handoff is defending. **Self-hosting the three families is a deployment
requirement for D2, not a preference.**

---

## 2. The API surface, as it actually is

Nine routes. Two exist only when `create_app` is asked for the demo surface.

| Method | Path | Response | Notes |
|---|---|---|---|
| `GET` | `/api/health` | `{"status": "ok"}` | not a projection, so not a response model |
| `GET` | `/api/cases` | `CaseSections` | three arrays, not a flat list |
| `GET` | `/api/cases/{case_id}` | `CaseDetail` | facts, approvals, actions, timeline |
| `POST` | `/api/events` | `RunView`, 202 | `{event_id, case_id}`; opens or reopens |
| `POST` | `/api/cases/{id}/decisions/{approval_id}/approve` | `RunView`, 202 | requires `X-Caregiver` |
| `POST` | `/api/cases/{id}/decisions/{approval_id}/reject` | `RunView`, 202 | requires `X-Caregiver` |
| `GET` | `/api/runs/{run_id}` | `RunView` | in-process registry only |
| `POST` | `/api/demo/reset` | `{cases_cleared, clinic_reset, scenarios}` | demo build only |
| `POST` | `/api/demo/scenarios/{key}` | `RunView`, 202 | demo build only |

Every verb accepts `?wait=<seconds>`. **The UI must never use it.** It blocks until the run settles,
which is precisely what `careloop/api/runs.py` explains the 202 exists to avoid: a synchronous
endpoint makes the Working column unreachable, because everything appears at once.

Closed vocabularies the adapter maps against:

- `CaseState` — 10 members (`NEW`, `GATHERING_EVIDENCE`, `PLANNING`, `AWAITING_HUMAN`,
  `READY_TO_EXECUTE`, `EXECUTING`, `VERIFYING`, `WAITING_EXTERNAL`, `DONE`, `FAILED`)
- `TimelineKind` — 18 members
- `ReasonCode` — 7 members
- `TrustLevel` — `UNTRUSTED`, `CONTROLLED`, `TRUSTED`
- `ArgClass` — `VALUE`, `DESTINATION`, `AUTHORITY`
- `RunState` — `RUNNING`, `SETTLED`, `ERRORED`
- `TIMELINE_PAYLOAD_FIELDS` — a 33-key allowlist. A payload key not on it is dropped by the API.

### Sections are a total, disjoint partition — and `FAILED` lands in `completed`

```
needs_you  = {AWAITING_HUMAN}
completed  = AT_REST_STATES = {DONE, FAILED}
working    = every other CaseState
```

`working` is defined as *the rest*, so a new `CaseState` joins it automatically rather than vanishing
from the dashboard. The consequence the adapter must carry: **a `FAILED` case is filed under
`completed`**, deliberately, and is told apart only by the `state` field every card carries. The
frontend's `CaseSection` has no fourth value, so `FAILED` needs an explicit presentation decision —
see divergence 2.

---

## 3. Field-by-field

Legend: **direct** — one stored field, renamed at most. **derived** — a pure function of what the
store holds, computed in `services/`. **presentation** — authored copy or formatting, belongs in
`services/`, never in a component and never as a new backend field. **absent** — no backend source
exists.

### 3.1 `CareCase`

| UI field | Backend source | Kind |
|---|---|---|
| `id` | `CaseCard.case_id` / `CaseDetail.case_id` | direct |
| `title` | — | **absent.** `Case.summary` is triage's one-line classification, not a title. Either use `summary` or hold a per-scenario title in the adapter. |
| `section` | `CaseCard.section` (`needs_you` / `working` / `completed`) | direct, renamed to `needs` / `working` / `done` — **but see `FAILED` above** |
| `statusLabel` | `CaseCard.state` | presentation — a state→copy table |
| `statusText` | `CaseCard.state` | presentation |
| `headline` | `CaseCard.state`, plus outcome text at `DONE` | presentation |
| `updatedLabel` | `CaseCard.updated_at` | derived — relative-time formatter |
| `verified` | `state == DONE` ∧ the last `VERIFICATION_PASSED` entry carries `payload.read_back == "true"` | derived |
| `mismatch` | the last verification entry on the timeline is `VERIFICATION_FAILED` | derived |
| `runId` | — | **not a case field.** `RunView.run_id` is `run-{case_id}-{n}`, minted per run by an in-memory registry. The UI treats it as a stable case property; the adapter must retain it from the 202 that started the run. |
| `outcome.title` / `.body` | `state`, `failure_reason`, the recorded `ActionView.result` | presentation over derived inputs |
| `readback.authorized` | the `PLAN_PROPOSED` entry's `payload.args` | derived |
| `readback.observed` | — | **absent.** See divergence 4 and question 2. |
| `readback.result` | `VERIFICATION_PASSED` / `VERIFICATION_FAILED` | presentation |
| `readback.state` | passed → `MATCH`, failed → `MISMATCH`, neither → `PENDING` | derived |
| `decision` | `CaseCard.decision` / `CaseDetail.decision` | see 3.2 |
| `events[]` | `CaseDetail.timeline` | see 3.3 |
| `evidence[]` | `CaseDetail.facts` | see 3.4 |

Presentation belongs in `services/` because it *must*. `careloop/api/` decides nothing, and
`projections.py` runs two import-time checks: every response field either names the stored field it
comes from or is declared derived, and response fields carry no defaults. A `statusLabel` field could
not be added to `CaseCard` without declaring a stored source it does not have.

### 3.2 `DecisionView` (UI) against `DecisionView` (backend)

Same name, different shape.

| UI field | Backend source | Kind |
|---|---|---|
| `id` | `DecisionView.approval_id` | direct |
| `kind` | `DecisionView.reason_code` | direct — `REQUIRES_HUMAN_APPROVAL` maps to itself; **everything else must map to `NO_SAFE_ACTION_AVAILABLE`**, i.e. not approvable. Default closed. |
| `title` | — | **absent.** `tool_name` is the nearest thing. |
| `body` | `DecisionView.reason_text` | present, **wrong register.** `reason_text` is a literal from `REASON_TEXT`, written for the *planner*: "This action needs the caregiver's decision. Stop and await it." That is not caregiver copy. Substitute in `services/`. |
| `facts[]` | `DecisionView.args` (`dict[str, str]`) | derived — the `label`s are presentation, the values are the arguments the approval's digest covers |
| `after` | — | **absent**, pure copy |
| `approveLabel` / `rejectLabel` | — | **absent**, pure copy |
| `recheckable` | `DecisionView.recheckable` | direct — **server truth, never derived here.** It says the *stored case* is structurally eligible for a caregiver to request an external re-check: the right handoff shape, a recorded action identity that resolves, an independently observable action type, and a proposal whose derived key agrees with the handoff's. It promises nothing about the run that follows. |
| `recheckLabel` | — | **absent**, pure copy |

`args` is the load-bearing one: an `Approval` stores `(tool_name, args_digest)` rather than the
arguments themselves, so `graph.context.proposal_for(case, tool_name, digest)` walks back to the
`PLAN_PROPOSED` entry that pair came from — **both halves matched**, so a proposal belonging to a
different tool cannot supply them however its arguments hash. The card therefore names exactly the
call that will be made. When no such entry exists, `args` is `{}` and the card is still rendered.
Do not lose either half in the mapping, and do not reconstruct the proposal on the client.

### 3.3 `TimelineEvent`

| UI field | Backend source | Kind |
|---|---|---|
| `id` | `TimelineEventView.seq` | direct |
| `time` | `TimelineEventView.at` | derived — formatted |
| `text` | `TimelineEventView.summary` | direct |
| `tone` | `TimelineEventView.kind` | derived — an 18-member enum onto 5 tones |
| `source` (`EvidenceExcerpt`) | `payload.source_doc`, `payload.page`, `payload.key` on `EVIDENCE_RECORDED` | **partial** — see 3.4 |
| `blocked[]` | adjacent `POLICY_DENY` entries; `payload.reason_code` supplies `rule` | derived — the backend has them as separate top-level entries, the UI nests them under the event they interrupted, so the adapter folds |

`TimelineEventView.payload` is already allowlist-filtered by the API. `diagnostic` is withheld
wholesale and is not available at any price.

### 3.4 `EvidenceItem` and `EvidenceExcerpt`

| UI field | Backend source | Kind |
|---|---|---|
| `fact` | `FactView.key` + `FactView.value` | derived |
| `source` | `FactView.citation` — already formatted as `referral.pdf p.2` | direct |
| `trust` | `FactView.trust_level` (`UNTRUSTED` / `CONTROLLED` / `TRUSTED`) | presentation — the UI wants a caregiver-facing word, and `domain.ts` says so ("Raw trust tiers stay out of this view") |
| `excerpt.source` | `FactView.citation` | direct |
| `excerpt.before` / `.highlight` / `.after` | ~~absent~~ **`D2:` the `excerpt:` page fact** | derived |
| `excerpt.digest` | — | **absent** |

~~**`EvidenceLens` is the one component with no data path.** A `Fact` carries the extracted value and
its provenance, never the surrounding page text. The lens must degrade to citation and trust only, or
be left closed. This is a genuine absence, not a withholding — nothing in `WITHHELD` is hiding it.~~

**`D2:` that is wrong, and it is wrong in a way that mattered twice.** `careloop/graph/extract.py`
records **one fact per document page whose value is the whole page**, keyed
`excerpt:<document_id>:p<n>`, so the planner can read a document without any of it being lifted into
a key an action could be built from. Those facts reach `GET /api/cases/{id}` through `FactView` like
any other, so the surrounding page text *is* available:

- **The lens shows a real excerpt.** `realApi.excerptFor` finds the page by **provenance** —
  matching `source_doc` and `page`, never by parsing the excerpt key, so the two tables never have
  to agree on a format — and locates the extracted value inside it. It degrades to the value alone
  when the page is absent or the value does not appear in it verbatim.
- **The evidence grid needs a filter, and would have been broken without one.** Left in, the page
  facts render as evidence cards each holding an entire PDF page. `presentation.isExcerptFact` is
  what keeps them out of the grid and out of the timeline spine.

`excerpt.digest` stays absent, and that half of the original claim holds: there is no digest of
evidence anywhere in the backend.

### 3.5 `AgentRun` against `RunView`

**`AgentRun` is not `RunView`.** `GET /api/runs/{id}` carries five fields; most of the drawer comes
from the case timeline. The adapter composes.

| UI field | Backend source | Kind |
|---|---|---|
| `id` | `RunView.run_id` | direct |
| `scenario` | — | **absent** from the API, but known to the adapter, which pressed the button |
| `stage` | `RunView.executed_nodes[-1]` and `RunView.state` | derived — `triage`→`TRIAGE`, `evidence`→`EVIDENCE`, `plan`→`PLANNER`, `gate`→`GATE`, `execute`→`EXECUTE`, `verify`→`VERIFY`, `state == SETTLED`→`SETTLED`. **`dispatch` has no UI stage** and maps to `IDLE` or is skipped. |
| `stageLabel` | as above | presentation |
| `actionIntent` | `PLAN_PROPOSED` → `payload.tool_name` + `payload.args` | **reachable** — both allowlisted |
| `idempotencyKey` | `ActionView.idempotency_key`, or `payload.idempotency_key` | **reachable** — allowlisted |
| `policyVerdict` | `POLICY_*` → `payload.verdict` | **reachable** — allowlisted |
| `policyDetail` | `payload.verdict` + `payload.matched_rule_id` + `payload.reason_code` | **reachable** — all three allowlisted |
| `evidenceDigest` | — | **absent.** See divergence 5. |
| `verification` | `VERIFICATION_PASSED` / `VERIFICATION_FAILED` | derived |
| `verificationDetail` | the kind plus `payload.read_back` | derived — carries whether an external system was read, never what it said |

### 3.6 `CareLoopApi` methods

| Method | Endpoint | Notes |
|---|---|---|
| `getDashboard()` | `GET /api/cases` | flatten `CaseSections`' three arrays into `cases[]`, keeping `section` |
| `getCase(id)` | `GET /api/cases/{id}` | 404 → `undefined` |
| `getRun(runId)` | `GET /api/runs/{id}` + the case detail | see 3.5 |
| `approveAction({approved: true})` | `POST …/approve` | **needs `X-Caregiver`** — `ApprovalInput` has no field for it |
| `approveAction({approved: false})` | `POST …/reject` | as above |
| `runScenario(id)` | `POST /api/demo/scenarios/{key}` | demo build only |
| `resetDemo()` | `POST /api/demo/reset` | **409 while any run is in flight** |
| `subscribe(listener)` | poll `GET /api/cases` | see divergence 6 |

---

## 4. The six questions in `frontend/docs/API_INTEGRATION_MAP.md`

**1. Does the decision payload expose a kind discriminator?**
Yes. `DecisionView.reason_code`, a string from the closed `ReasonCode` enum — 7 members when this
was written, 9 now. `REQUIRES_HUMAN_APPROVAL` is the only one that ever reaches an `Approval`; the
three park codes (`NO_SAFE_ACTION_AVAILABLE`, `EXTERNAL_STATE_NOT_CONFIRMED`,
`NO_VALID_PROPOSAL_PRODUCED`) are reasons a *case* stopped and raise none. Map anything
unrecognised to not-approvable: defaulting closed is both correct and free, and it is what let the
eighth and ninth codes arrive needing only truthful copy rather than a new affordance.

**2. Does the verification result carry both the authorized and the observed values?**
**No.** Authorized is reachable — `PLAN_PROPOSED`'s `payload.args`, allowlisted. Observed exists only
inside `Verification.diagnostic`, which the API withholds wholesale and deliberately: read-back values
are text from a system CareLoop does not control, and
`tests/test_verify_readback.py::TestTheReadBackIsNotAnInjectionChannel` exists to keep them out of
anything a prompt or a page could render. The timeline payload carries `read_back: "true" | "false"`
and nothing else — enough to say *whether* the world was read, never *what it said*.

**3. Do timeline events carry a semantic type and human-facing payload, or only raw log text?**
Both, cleanly separated, and the separation is the design. `kind` is an 18-member `TimelineKind`;
`summary` is human-facing; `payload` is a 33-key allowlist that a shipped writer cannot quietly widen
(`tests/test_api_projections.py` fails when one emits an unlisted key). The raw text is `diagnostic`,
and it is the withheld field.

**4. Does evidence provenance include source document, page and excerpt?**
Source document and page yes, plus a ready-formatted `citation`. ~~**Excerpt no.**~~ **`D2:` excerpt
yes** — through the `excerpt:` page facts, which nobody had noticed are facts like any other. See
3.4.

**5. Are blocked actions first-class events with verdict and rule code, and is there a per-case
count?**
First-class yes: `TimelineKind.POLICY_DENY`, carrying `payload.verdict`, `payload.reason_code`,
`payload.matched_rule_id` and `payload.tool_name`, all allowlisted. `matched_rule_id` is on the
allowlist *for this panel* — invariant 6 keeps it from the planner because it maps the defence for
the component being tempted; a judge reading the drawer is who the named declarative rule was written
for. **No per-case count exists.** `CaseCard` carries `fact_count` and `action_count` only, so a
dashboard row cannot show "3 blocked" without fetching `CaseDetail`.

**6. What are the exact demo scenario keys, and do runs emit intermediate states?**
Keys: **`referral`, `injection`, `resume`** — see divergence 1. Intermediate states: **yes**, and this
is the half that saves the choreography. Every node commits its transition to the store before it
finishes, so a dashboard polling `GET /api/cases` genuinely observes `GATHERING_EVIDENCE` →
`PLANNING` → `EXECUTING` → `VERIFYING` as the run proceeds. `careloop/api/runs.py`'s own docstring
says that is exactly why the verbs return 202. The beats are real; only the timetable is fictional.

---

## 5. Divergences

### 1. Scenario keys — **decided: A (rename in the UI)**

The UI ships `referral | injection | mismatch`. `careloop/api/demo.py` ships
`referral | injection | resume`. The UI's third scenario has no backend trigger; the backend's third
has no UI.

**Resolution: A.** Rename `mismatch` → `resume` in the frontend. `ScenarioMeta.title`, `.blurb` and
the tone token change with it — **the only §16 copy exception in this work**, approved narrowly and
for this reason only. The mismatch claim becomes a video beat backed by
`tests/test_verify_readback.py::TestTheIndependenceProof`, which already moves the appointment behind
CareLoop's back and requires verification to fail. A test that proves it beats a button whose backend
state contradicts its own screen.

The two rejected options are recorded with their costs so a reconsideration does not have to
re-derive them.

**B — add a demo-only `mismatch` scenario. Rejected: it only looks demo-only.**
The divergence has to be created by something outside CareLoop. `Clinic.amend()` exists in
`demoworld/clinic.py` and is written for exactly this ("someone at the desk moved the appointment"),
but **it has no HTTP route** — only a test holding the `Clinic` object reaches it. So B needs:

- a new control-plane route on `demoworld/` (`POST /demo/amend`, beside `/demo/reset`);
- a CareLoop demo scenario that books, then amends, then re-verifies. `careloop/api/demo.py` is a
  `key → (case_id, event_id)` table that calls the one `deliver`; a scenario poking the clinic
  mid-flight does not fit that table, and would be **the first demo endpoint doing something the
  normal flow does not** — which that module's docstring says a demo must never be;
- **open decision 18**, because otherwise the case reaches `FAILED` by burning the replan cycle;
- **open decision 19** or an equivalent, because a `FAILED` case carries no pending approval and so
  cannot render a decision card at all.

The last two are authority-layer changes. B is not a demo-only change.

**C — drop the scenario, keep the test. Not chosen, but close to A.**
A is C plus the `resume` beat actually being pressable, which is strictly better.

### 2. `NO_SAFE_ACTION_AVAILABLE` — **confirmed, and the divergence is structural**

The backend emits this code in exactly one place: `_every_tool_has_been_refused()` in
`careloop/graph/nodes.py`, when the planner has proposed every tool in the registry and been denied
each time. It parks the case in `AWAITING_HUMAN` with an approval that authorises nothing.

**A verification mismatch does something else entirely — and what it does changed when open
decision 18 closed.** It used to route to **`PLANNING`**: the execute node found the effect already
recorded and suppressed it, verification read the same divergent state and failed again, and the
cycle ran to `MAX_NODE_EXECUTIONS`, after which `run_case` marked the case **`FAILED`**. `VerifyNode`
now parks the case instead — a `MANUAL_HANDOFF_REQUIRED` entry carrying
`EXTERNAL_STATE_NOT_CONFIRMED`, then `AWAITING_HUMAN`, with **no `Approval`**.

So the two now share a shape and are told apart by their reason code, which is what the divergence
below is really about:

| | `NO_SAFE_ACTION_AVAILABLE` | `EXTERNAL_STATE_NOT_CONFIRMED` |
|---|---|---|
| case state | `AWAITING_HUMAN` | `AWAITING_HUMAN` |
| section | `needs_you` | `needs_you` |
| `decision` | a card with `approval_id: ""` | a card with `approval_id: ""` |
| what happened | every action was refused; **nothing was done** | an action **was carried out** and could not be confirmed |
| approve affordance | none | none |
| re-check affordance | none — there is no external effect to observe | one, **when `recheckable` is true** |

`recheckable` is a second axis and not a second kind. `kind` says whether the card may offer
*authority*, and only `REQUIRES_HUMAN_APPROVAL` ever may; `recheckable` says whether it may offer
*another look*, which is not authority at all. The `EXTERNAL_STATE_NOT_CONFIRMED` card therefore
renders a button and still renders no Approve and no Reject. Rendering it off the reason code
instead would draw it on a handoff persisted before the action identity was recorded, on one whose
key names no recorded effect, and on an action type nothing can observe — all three of which the
backend refuses with a `409`.

The last two rows are why `decisionCopy` branches on the code rather than only on
`decisionKindFor`. The *kind* is the same and must be — neither may offer Approve — but telling a
caregiver "nothing has been done" when a booking is sitting unchecked in a clinic is the one
mistake this card must not make.

A read-back mismatch reaching this frontend before that repair produced:

| | Backend, before decision 18 closed | UI's scenario 03 |
|---|---|---|
| case state | `FAILED` | — |
| section | `completed` | `needs` |
| `decision` | `null` (no pending approval) | a `NO_SAFE_ACTION_AVAILABLE` card |
| what renders | a completed-column row | a decision card |

`DecisionCard` returns `null` when `decision` is null, so the UI's screen is not merely mislabelled —
it has no data path. The UI is right on the safety point (`NO_SAFE_ACTION_AVAILABLE` must render no
Approve affordance) and right on the product point (a mismatch must not read as complete). It is
wrong about which code fires and wrong about the state.

**Not fixable in `services/`.** Divergence 1 resolves to A precisely so this does not have to be.

What the adapter still owes, for a `FAILED` case arriving from any cause: it is in `completed` and
must not render as a success. `failure_reason` is projected and is the honest source for the row's
`statusText`.

### 3. `CareCase` is pre-formatted — **confirmed, mapped in 3.1**

Every authored string maps to a state, a timestamp or a timeline entry. All of it lands in
`services/`. None of it becomes a backend field, and the import-time checks in `projections.py` would
refuse it if anyone tried.

### 4. Under the Hood — **no invariant-6 decision is required**

Four of the five interesting rows are already reachable through the existing allowlist, which is not
an accident: `verdict`, `reason_code`, `matched_rule_id`, `idempotency_key` and `tool_name` were put
on `TIMELINE_PAYLOAD_FIELDS` deliberately, for this panel.

The exception is **`evidenceDigest`, and it is absent rather than withheld** — a different and
cleaner answer than a withholding would be. There is no digest of evidence anywhere in the backend.
`args_digest` on `ApprovalView` and `ActionView` digests *arguments*, not evidence. Widening the
allowlist would not produce it, because it does not exist to be allowed.

**Decision: drop the row.** Do not substitute `FactView.citation`. The citation already appears in the
timeline and in the evidence grid, and putting it in a slot labelled `EVIDENCE DIGEST` would have the
drawer imply a value that does not exist. An absent row is honest; a relabelled one is not.

`readback.observed` on the case page is the other absence, and it is a real withholding — see
question 2. The outcome panel's `OBSERVED AT CLINIC` column has no source. It shows `read_back:
true/false` or it shows nothing.

### 5. `subscribe` is push in the mock, polling in reality — **confirmed**

**Nothing in the UI hard-codes a duration.** Every timing (1100/2400/3700, 1200/2600/4100/5400,
1300/2900) lives inside `mockApi.ts` and dies with it.

What the UI **does** assume is **ordering**:

- `UnderTheHoodDrawer` computes `STAGE_ORDER.indexOf(run.stage)` and paints every earlier stage
  "done". A stage sequence that skips forward leaves stages marked done that never ran; one that goes
  backwards — which the replan cycle genuinely does, `gate → plan` — repaints them undone.
- `VerificationSeal` plays its 900ms closure once, on `verified`. It must not be re-triggered by a
  poll that re-emits the same state.
- `CaseRow` picks its rotating arc from `section === "working"`, which is state-driven and safe.

Polling constraints:

- Poll `GET /api/cases` rather than `GET /api/runs/{id}` — the case carries the state transitions, the
  run carries only the node trace.
- Stop on `RunState.SETTLED` or `ERRORED`, **or** when the case reaches `AWAITING_HUMAN`, `DONE` or
  `FAILED`. A run that settles with the case parked is the normal approval beat, not an end.
- Never pass `?wait=`.
- `GET /api/runs/{id}` is an in-memory, per-process registry. A restart 404s a live run id, and a
  second API instance would not see the first's runs at all — open decision 17.

**`D2:` the timings, measured.** "Only the timetable is fictional" was right, and the real one is
faster than the dashboard can sample:

| Beat | Measured |
|---|---|
| button press → case visible | ~1.3s |
| whole `referral` run | **6.5s** typical, one 21s outlier when a model call was slow |
| whole `injection` run | ~8.7s — three denials and three replans |
| `resume` to parked | ~7s |
| **approve → `DONE`** | **440ms** |

At a one-second poll that means `EXECUTING` and `VERIFYING` are never observed: those nodes take
about 200ms each and no sampling interval a browser should use will catch them. Two of six states
were visible, and the approval beat had none.

The adapter therefore **paces the display**, walking it forward one state at a time through the
states the case's own `STATE_TRANSITION` entries record — nothing invented, nothing reordered, and
bounded to three states behind the store. `section` is paced with the state so a row cannot read
"Making the arrangement…" under COMPLETED, and `mismatch` is never paced because it is a safety
claim rather than a beat. See `presentation.pacedState`.

### 6. `AUTHORITY_ESCALATION` is not a `ReasonCode`

`mockApi.ts:329` uses it as a blocked-action rule. The real enum has **`AUTHORITY_NOT_TRUSTED`**, and
the two are kept apart on purpose: an insufficiently trusted `VALUE` can be validated and reproposed,
an `AUTHORITY` argument can never be. Measured live in session C, all three poisoned payloads in the
`injection` scenario were denied `DESTINATION_NOT_TRUSTED`, so the mock's three-rule spread is also
optimistic about which rule fires.

### 7. `ApprovalInput` has no caregiver field

Approve and reject require the `X-Caregiver` header. It is an **audit identity, not authentication**:
nothing is verified, anyone reaching the port can claim any name, and a missing one is **400, never
401**. The adapter supplies it. Do not surface it as a login, and do not "upgrade" the 400.

### 8. `decisionId` formats differ

The mock mints `d-card-1`; the backend mints `apr-<seq>`, where `seq` is the timeline position of
the `APPROVAL_REQUESTED` entry. Opaque on both sides — pass it through, never parse it. It was
`apr-<args_digest[:16]>` until session K, which is exactly why the rule is "never parse it": an
identifier that looked like it named the *action* was being used to name the *request*, and two
requests for one action collided. Nothing in the frontend changed when the format did.

### 9. No CORS anywhere in `careloop/`

There is no `CORSMiddleware` in the codebase. The vite `/api` proxy covers development exactly as
`HANDOFF.md` §15 describes. Serving the production build from a different origin has no path today.
This is a deployment decision for whoever ships it, **not a D2 task**, and "do not weaken backend
CORS" remains the instruction.

**`D2:` and there was a trap in the proxy that only springs once the adapter is real.**
`vite.config.ts` took its target from `VITE_API_BASE_URL`, which `services/api.ts` also reads as the
client's base URL for every request. Vite exposes every `VITE_`-prefixed variable to the browser, so
setting it to move the *proxy* onto a different port also makes the browser call that port
**directly, cross-origin**, against a backend that mounts no CORS middleware — which fails in a way
that reads like the backend being down. Inert while the app ran on mocks, because nothing read the
value. The proxy target is now **`CARELOOP_API_TARGET`**, unprefixed and therefore server-only;
`VITE_API_BASE_URL` still exists for a deliberate cross-origin deployment, which would need the CORS
decision above.

### 10. `GET /api/cases` returns three arrays, not a flat list

`CaseSections` is `{needs_you, working, completed}`; `DashboardState.cases` is flat with a `section`
discriminator. Trivially flattened, but it is the inverse shape and worth noting before someone
writes the type by hand.

### 11. Fonts are fetched from Google at page load

See §1. A deployment requirement, scheduled for D2.

---

## 6. Open decisions this session touched and did not close

**18 — a failed read-back costs a node budget.** Confirmed by reading, not re-tested. Verification
failure routes to `PLANNING`; for a read-back mismatch the effect has already happened, is suppressed
as a duplicate, and re-verifies against the same divergent state until the SDK's bound fires. Loud,
terminal, bounded, and it books nothing twice — but it reaches `FAILED` the long way. **Stays open and
out of scope.** It is the reason divergence 1 cannot resolve to B.

> **Closed in the backend, 2026-08-15, and the cost was worse than a node budget.** "It books nothing
> twice" was true only because the reproposal was *identical*. `book_appointment.reason` was then
> planner-authored free text carrying no `trust_key` and named by no rule, so a reproposal changing
> only that keyed differently, defeated duplicate suppression in CareLoop and in Demo World, reached
> `POLICY_ALLOW` and dispatched — measured as two real bookings against a clinic that could not
> confirm the first. A verification failure now parks the case in `AWAITING_HUMAN` with an
> `EXTERNAL_STATE_NOT_CONFIRMED` handoff and no `Approval`; the `verify → plan` edge is gone and
> `VERIFYING → PLANNING` is an illegal transition.
>
> **This frontend needed one copy branch and nothing else.** `decisionKindFor` defaults closed, so
> the new code already rendered the non-approval card with no Approve button; what it would have
> rendered is `NO_SAFE_ACTION_AVAILABLE`'s body, which says nothing was done. `decisionCopy` now
> branches on the code. `statusCopy` was already correct — `mismatch` outranks the state, so the case
> reads "The clinic's record disagrees" whatever state it is in — and it now says that about a case
> in `needs_you` rather than one filed under `completed`.
>
> **Patch 2C added the way out, and one line of that copy became false.** The card said "CareLoop
> will not retry", which was true while the case was permanently parked and stopped being true the
> moment a caregiver could ask for another observation. It now says CareLoop will not repeat the
> *action* and that you can ask it to look again — the distinction the whole operation turns on, and
> the one sentence on this card that is dangerous in both directions. The affordance itself is
> `Check the clinic again`, gated on `recheckable`, deliberately not labelled Approve, Retry, Run
> again, Resume or Continue: it communicates observation, never action.
>
> **Divergence 1 is unblocked on this axis and still not resolved to B.** The two remaining
> obstacles are unchanged and are the ones that always mattered: `Clinic.amend()` has no HTTP route,
> and a demo endpoint that pokes the clinic mid-flight would be the first one doing something the
> normal flow does not.

**19 — approving a `NO_SAFE_ACTION_AVAILABLE` parking releases the case to execution.** Session C's
log says the frontend needs an answer, because a Needs You card renders an Approve button off the
reason code and this is the one value where pressing it is wrong.

**The answer this UI gives: it renders no Approve.** `DecisionCard` gates both buttons on
`kind === "REQUIRES_HUMAN_APPROVAL"`, and `HomePage` uses the other value to switch the hero line to
"One case needs a person." So **19 is unreachable through this frontend** — but it is **mitigated at
the surface, not fixed**. Any other client hitting `POST …/approve` with that approval id still
releases the case to execution and still ends `FAILED`. It stays open, and the mitigation should not
be mistaken for a resolution.

> **Closed in the backend, 2026-08-15.** A parking now raises no `Approval`, so there is no id for
> any client to post — a fabricated one is a `404` — and `_decide` refuses the records already on
> disk with a `409` before anything moves. This UI was right and needed no behavioural change: the
> card is still projected into `DecisionView`, now from the timeline's `MANUAL_HANDOFF_REQUIRED`
> entry rather than from an approval, with `approval_id` empty because there is genuinely nothing to
> name. The only frontend change was naming that timeline kind — `backend.ts`, `realApi.ts` and the
> tone table — so the spine says "CareLoop stopped — manual follow-up needed" in its own words
> instead of falling through to the backend's `summary`. **The surface mitigation is now a
> defence-in-depth layer over a backend that refuses independently**, which is what it should have
> been all along.

---

### 12. `D2:` the provider seeded itself with mock cases

Not a backend divergence and not visible until the adapter was real: `CareLoopProvider` initialised
its state from `data/mock/cases.ts` and `data/mock/runs.ts`, so two invented cases and a
plausible-looking idempotency key sat on screen until the first poll replaced them. `services/api.ts`
now exports `emptyDashboard()` and the seam has a test that fails if it points back at the mock.

## 7. Order for D2

Reads first, writes second, choreography last. **Nothing in this list requires a backend change.**

1. **The presentation layer in `services/`, with no network.** State→copy tables for `statusLabel`,
   `statusText`, `headline`; the relative-time formatter; the section rename; the `FAILED` decision.
   Testable on its own.
2. **Rename `mismatch` → `resume`** (divergence 1, resolution A), including `ScenarioMeta.title`,
   `.blurb` and the tone token. The narrowest possible edit.
3. **Self-host the three font families** and drop the `fonts.googleapis.com` links. Do it early —
   it changes `index.html` and the golden captures, and it is a filming risk until it is done.
4. **`getDashboard`** over `GET /api/cases`, flattened.
5. **`getCase`** over `GET /api/cases/{id}` — timeline mapping, `blocked[]` folding, evidence without
   excerpts, `EvidenceLens` degraded to citation and trust.
6. **`getRun`** composed from `RunView` plus the case timeline. **Drop the `EVIDENCE DIGEST` row.**
7. **`subscribe` as polling** — `POST` returns a run id, poll `GET /api/cases` at ~1s, stop on settle
   or at rest. No `?wait=`.
8. **`approveAction`**, both paths, with `X-Caregiver`. Handle 409 (run in flight) and 404.
9. **`runScenario` / `resetDemo`.** Reset 409s while a run is in flight; two scenarios pressed without
   a reset between them collide on the clinic's one seeded slot and produce a correct 409, a `FAILED`
   case and an `ERRORED` run — session C's finding. The mock already resets first, which is right.
10. **Swap `api.ts`**, then re-check the ten references in both themes at **909px**.

### Correct `DESIGN_REFERENCE.md` before step 10

Done in this session: the viewport is 909, and the goldens are JPEGs cropped to the fold. Anyone
comparing at 1280 will see the demo launcher's cards wrap differently and report a defect that is not
there.

---

## 8. Out of scope for D1, by instruction

`realApi.ts`, any UI change beyond a build fix (none were needed), tests, AgentCore, DeepTeam, and
backend changes of any kind. **A required backend change is a finding, not a task** — the two that
exist are open decisions 18 and 19, and both stay open.

---

## 9. `D2:` what shipped against this map

All ten steps of the order above, in that order, across nine commits. **No backend change was needed
or made**, which is the map's own claim about itself holding.

The adapter is `frontend/src/services/`:

| Module | What it owns |
|---|---|
| `backend.ts` | the wire shapes `careloop/api/models.py` returns, hand-written so the mapping is reviewable against that file |
| `presentation.ts` | every string the UI says that the backend does not: state→copy tables, relative time, trust words, tones, decision copy, the pacing rule |
| `realApi.ts` | the nine routes, named readers over the timeline, polling, and the composition of `CareCase` and `AgentRun` |
| `api.ts` | the one line, now pointing at `realApi` |

**Two §16 exceptions were used, both pre-approved.** Scenario 03's copy (divergence 1), and the
`UnderTheHoodDrawer` — its `EVIDENCE DIGEST` row removed and its "Running on the mock adapter"
paragraph corrected to "Live run record from `GET /api/runs/:id`. Policy verdicts and rule ids come
from the case's own audit trail." A drawer claiming to run on mocks while showing live backend data
would be a lie on the exact panel that exists to prove the data is real.

### The ten references, re-checked at 909px in both themes

Captured against the real backend and compared numerically. The references are JPEGs, so a per-pixel
diff has a compression floor rather than reaching zero; the **`/trust` page is the control**, since
it is entirely static and its content cannot have changed.

| Region | Mean abs. difference |
|---|---|
| `/trust`, both themes | **2.8 – 3.1** — the floor |
| the shared header, all ten states | **2.1 – 2.3** |
| `/demo`, both themes | 3.4 – 3.6 |
| the three data-bearing states | 4.6 – 8.9 |

**No divergence.** The header carries the logo geometry, the three nav links and their underline, the
separator, the theme toggle and "Maya", all set in the three now-self-hosted faces — and it sits at
the compression floor on every one of the ten, which is the measurement that says self-hosting the
type changed nothing. `/demo` is barely above it, explained entirely by scenario 03's approved copy.
Everything higher is the data being real: the reference captures were taken against `mockApi`, so
identical *content* was never possible and was never the thing being checked.
