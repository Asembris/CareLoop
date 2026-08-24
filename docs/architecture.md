# Architecture

The diagram is the deliverable; this page is its caption. Depth lives in the ADRs and
[`session-log.md`](session-log.md).

[![CareLoop architecture: three trust zones, one policy engine, an independent read-back](architecture.svg)](architecture.svg)

*Click to open full size. Hand-authored SVG — it is text, so it diffs and it is edited rather than
re-exported.*

[`architecture.png`](architecture.png) (3720 × 2850) and [`architecture.pdf`](architecture.pdf)
are **derived** from that SVG by [`scripts/export_diagram.py`](../scripts/export_diagram.py), which
renders it through a headless Chromium — the same engine the SVG was drawn against, so the system
font stacks and text anchoring land where they were placed. They exist because Devpost's
architecture field takes PDF, PPT, PPTX, PNG or JPG and not SVG. Edit the SVG, re-run the script,
commit all three; never edit an export.

## The three zones

**UNTRUSTED — what the world sends, and what the models say.** Inbound email, its PDF attachments,
the text pdfplumber pulls out of them, and — deliberately in the same zone — `TriageResult` and
`PlannerIntent`. A model's output is data: typed, read, and gated exactly like an attacker's
document. Instructions found in either never gain authority, however they are phrased. This is the
zone that makes the central claim testable: a continuity notice telling the agent to email records
to an outside address is *read*, and it changes nothing about what may happen.

**CONTROLLED — what validation has earned the right to promote.** A `Fact` carries
`{source_doc, page, trust_level}` and may be promoted `UNTRUSTED → CONTROLLED` by deterministic
validation. Only `VALUE`-class arguments are promotable at all; `DESTINATION` and `AUTHORITY` —
recipients, addresses, URLs, permission changes — never are, whatever a validator says. Trust
resolves to the *lowest* level among facts sharing the argument's declared fact key **and** its
canonical value, so a poisoned duplicate cannot be laundered by a legitimate twin — and a
configured literal that happens to appear in a document under some other key cannot be dragged
down by it either.

**TRUSTED — application code, the policy engine, `rules.yaml`.** Trust is never manufactured by
validation. It comes from configuration a person wrote — `careloop/graph/trusted.py` and nothing
else. **A caregiver pressing approve does not produce it**, and this page said otherwise until
session W: an approval is a capability for one exact `(tool_name, args_digest)`, it writes no
fact, and no fact's trust level moves when a person decides. Deterministic trust and human
approval are separate dimensions, read by separate predicates, which is exactly why an approval
can never satisfy a rule about *where* an effect lands.

Zone colours are claims about *authority*, not about code quality. UNTRUSTED does not mean badly
written; it means nothing there may decide anything, whoever wrote it.

## Two agents, and why not three

**Triage** classifies an inbound event into typed `Case` fields. **Planner** proposes one next
action as a typed tool intent. That is every model call CareLoop makes. Everything else —
extraction, validation, promotion, gating, dispatch, verification, state — is deterministic Python
or a typed tool.

The constraint is load-bearing rather than minimalist. Neither agent owns a tool, so neither can
act — Triage is a `Model.structured_output` call against a schema, and the planner offers the
provider one closed `propose_*` schema per registered action and reads back the one it called.
Those are specifications handed to a provider, not a tool the model may run: no `strands.Agent` is
constructed, no event loop runs, nothing is dispatched, and the `BeforeToolCall` hook is never
reached because nothing there calls a tool. **"Two agents" is a count of agents, not of model
calls** — a denial sends the planner back for another proposal, and one proposal may cost a second
provider call when the first answer does not satisfy its schema, so the number of invocations per
case is variable by design. Adding a third agent is an
architecture change, argued as one. Three fields are deliberately *not* extracted at all — a
referral's clinical reason as prose, whether a letter is informational or is asking for something,
and a provider's identity from letterhead — because they cannot be done deterministically, and
reaching for a model to read them would be that same architecture change slipped in as an
improvement to extraction.

Whether the proposal agents should be `strands.Agent` instances was evaluated by execution and
rejected: [ADR 0001](adr/0001-strands-agent-for-the-proposal-agents.md).

## Where the gate sits

One engine, one `rules.yaml`, three call sites — the `gate` node before execution, the Strands
`BeforeToolCall` hook at dispatch, and the `@gated_tool` wrapper on every external-effect tool. The
two at the tool boundary are independent: `tests/test_case_run.py` runs the attack with only the
hook, then with only the wrapper, then with neither — and the last one lands, which is what makes
the first two mean anything.

Argument classes belong to the **tool registry**, not to `rules.yaml`; rules may reference a class
but never define one, and load-time validation fails startup if a rule names a tool or argument the
registry does not have. What comes back to the planner after a refusal is a closed `ReasonCode` and
nothing else — no argument values, no trust levels, no rule ids — because denial text re-enters the
planner's prompt, and an interpolated reason would be a self-inflicted injection channel.

**One disclosure, stated because the imprecise version would be an overclaim.** CareLoop fires the
SDK's real `BeforeToolCallEvent` **itself, from its own execute node, against a real
`HookRegistry`**. An SDK event loop does not fire it, because there is no event loop to fire it:
both agents drive the model provider directly and neither holds a tool. The event type, the
registry and the `cancel_tool` contract are genuine SDK machinery, and the hook provider would plug
unchanged into a `strands.Agent` the day one exists. What is CareLoop's is the *firing*.

**Naming an argument in a rule is not the same as guarding it.** Every booking argument already
declared the fact key its trust comes from, and resolution already returned the right answer for
each — but the booking rule tested `patient_id` with `args_present`, which asks only that the value
be non-empty. The trust was computed and nothing read it, so a booking with a provider from trusted
configuration and a validated slot was allowed for an invented patient, one borrowed from an
unrelated fact key, one that never validated, and the real one misquoted. The requirement is now the
same as the slot's: **the exact patient identifier must be established by the case's own evidence at
`CONTROLLED` or higher**, refused by `deny-booking-for-unverified-patient` otherwise. Legitimate
bookings are unaffected — five of the six fixture documents print the identifier and the
cross-document agreement check promotes it — so the autonomous path stays autonomous and no
caregiver is asked anything new.

Why there is no probabilistic guardrail in front of any of this:
[ADR 0003](adr/0003-no-runtime-guardrail-layer.md).

## The read-back

`DONE` is reachable only from `VERIFYING`. Verification means one specific thing: **the effect
performed is the effect authorised** — the idempotency key re-derived from the approved arguments
equals the key the action was recorded under, and every field the typed result echoes back equals
the argument policy allowed.

For the **two effects with a real provider** it means more, and the diagram draws that as a separate
arrow on purpose. Verification performs its **own read** and compares what the external system holds
now against what policy authorised — never the effect's own response, which would be re-reading
CareLoop's receipt. Move the record behind CareLoop's back and every closed-loop check still passes
while verification fails; `tests/test_verify_readback.py::TestTheIndependenceProof` is that sentence
as a test for the clinic and `tests/test_upload_readback.py::TestTheIndependenceProof` for the object
store.

| effect | provider | the independent read | evaluated in AWS |
| --- | --- | --- | --- |
| `book_appointment` | Demo World clinic, over HTTP | `GET /clinic/appointments/{confirmation_id}` | yes |
| `upload_document` | Amazon S3, create-once | `GetObject`, digest computed here from the bytes | no — `NOT_APPLICABLE` |
| `send_email` | none, simulated | — | — |
| `grant_portal_access` | none, simulated | — | — |

**The two providers offer CareLoop nothing in common, and that is the interesting part.** The clinic
keys writes by an idempotency header and answers a duplicate with the original record; S3 has no
notion of an idempotency key at all, refuses a duplicate `PutObject(IfNoneMatch="*")` with `412`, and
hands back nothing — so the adapter fetches what is there and compares it against the bytes CareLoop
was about to write. The measured contract is in [`objectstore-contract.md`](objectstore-contract.md).
The saga above them consumes only whether a frozen identity may be re-issued and whether an
independent observer is bound; `tests/test_third_provider_seam.py` carries a third, synthetic
provider with a contract neither of them has through the same recovery lifecycle, and
`tests/test_provider_seams.py` is the structural proof that no provider mechanics reach the engine.

That read compares **every declared booking argument** — the provider, the patient, the slot and
the reason — which closes the patient contract at both ends: the exact identifier must be
`CONTROLLED` before the effect, and the clinic must still hold it afterwards. A front desk that
files the booking against somebody else is caught by the second half —
`TestTheAuthorisedPatientIsCheckedOnTheWayOutToo` — and by nothing else, because the booking response
echoed the right patient and every closed-loop check still passes.

The fourth was missing at both ends until session O, and this page said so: a clinic that kept the
appointment and rewrote only *why* it was made reached `VERIFICATION_PASSED` and `DONE` with the
timeline reporting `read_back: true`. It is compared now, against the exact value CareLoop executed
rather than against a value policy authorised — `reason` carries no `trust_key` and by design cannot,
since a rule requiring its trust fails at load. So this comparison is fidelity to an external record
and **not** provenance.

Provenance is answered one layer over, and separately. `reason` is `SYSTEM`-authored: CareLoop
composes it from a fixed literal in the tool registry, it is absent from the model-facing
`propose_book_appointment` schema, and `allow-booking-on-validated-slot` requires exactly that value
through `args_system_authored`. Authorship mints no `Fact` and moves no trust level, so the two
questions stay apart — and both are now closed rather than one of them being open. `reason` is also
declared **non-identifying**, so rewording it is not a second appointment; it remains in the payload,
in `args_digest`, in this comparison and in both settlement state digests. See *Four questions about
one argument* in `CLAUDE.md`.

**What it does not prove.** It checks the effect performed against the effect authorised. It does
not prove the outside world did the right thing, that the appointment suits the patient, or that
the clinic will honour it. The other two effects — `send_email` and `grant_portal_access` — are
simulated, and for those verification closes the loop between decision and execution rather than
between execution and the world. `read_back` on a `VERIFICATION_PASSED` entry is `true` only when an
external system was actually read, so the weaker claim can never be read as the stronger one.

**Nor does it widen what AWS evaluates.** `CareLoopSettlementIntegrity` is booking-scoped,
deliberately: a trace claiming an `upload_document` effect returns `NOT_APPLICABLE`, and
`tests/test_evaluator_upload_scope.py` states that as a named exclusion rather than as an equality
that happened to hold while there was one real provider.

## Deployed

One `t4g.small` behind CloudFront, serving `/` and `/api/*` from uvicorn on `:8000` and `/clinic/*`
from `demoworld` on `:8100` — **one distribution over two backend origins**, which is what lets one
hostname reach two ports. **The browser sees a single origin, so no `CORSMiddleware` exists anywhere
in `careloop/`.**
Two processes, because Demo World is a system CareLoop does not own and `demoworld/` may never
import `careloop` — routing both through one CDN does not couple them. Bedrock is reached through
an instance profile; there are no keys on the box.

Every graph node commits its transition to the store before it finishes, which is what lets a
browser polling `GET /api/cases` watch a case move. Moving graph execution onto Bedrock AgentCore
Runtime was planned, costed and measured against a deployed runtime — 6.47 s cold, 0.72 s warm —
and rejected because a single terminal commit takes that away:
[ADR 0002](adr/0002-graph-execution-stays-on-the-host.md). The `GraphRunner` seam is built anyway,
so the rejection is falsifiable rather than rhetorical.

---

| | |
|---|---|
| the invariants, in full | [`../CLAUDE.md`](../CLAUDE.md) |
| what was measured and when | [`session-log.md`](session-log.md) |
| SDK behaviour verified by execution | [`spike-findings.md`](spike-findings.md) |
| the adversarial evidence | [`security-evaluation.md`](security-evaluation.md) |
| which model, and why | [`model-benchmark.md`](model-benchmark.md) |
