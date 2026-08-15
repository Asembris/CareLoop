"""A policy verdict is only meaningful for a complete tool call.

:meth:`careloop.policy.engine.PolicyEngine.evaluate` has always refused arguments the selected tool
does not declare, in a dedicated branch that runs before trust is resolved: an argument the registry
has never classified has no argument class, so nothing establishes that it is not a destination.
There was no symmetric branch for the arguments the tool *does* declare and the call leaves out, and
this module is where that half is now refused.

**What the missing half cost, measured against the shipped registry and the shipped rules.** Ten of
the thirteen declared arguments were denied when omitted, but by accident rather than by design —
each of them happens to be named by a rule, so the omission surfaced as ``UNTRUSTED`` through
``trust.get(arg, UNTRUSTED)`` and the rule refused it. The other three are named by no rule at all,
because they carry no ``trust_key`` and by design cannot:

- ``book_appointment`` without ``reason`` reached ``ALLOW`` under
  ``allow-booking-on-validated-slot``, **autonomously**, on the one tool whose effect is genuinely
  external;
- ``send_email`` without ``subject`` and ``send_email`` without ``body`` reached ``REQUIRE_HUMAN``,
  and reached ``ALLOW`` under ``allow-approved-outbound-email`` once a caregiver's approval covered
  the incomplete argument map — so the policy layer considered a malformed capability approved.

**None of that was an authorisation bypass and none of it was production-reachable.**
:meth:`careloop.agents.schemas.PlannerIntentBase._bind_arguments_to_the_chosen_tool` rebuilds a
proposal's arguments as exactly ``spec.arg_names``, filling omissions with ``NOT_SUPPLIED``, so a
validated intent cannot be incomplete and ``PlanNode`` is the only production writer of
``PLAN_PROPOSED``. And nothing incomplete could execute: ``ToolSet.<tool>(**args)`` raises
``TypeError`` before the executor is reached, so the ledger stays empty. The defect was that the
authority layer issued verdicts about objects that are not tool calls, with an argument-binding
accident in another layer as the only thing between one of those verdicts and an effect — session
N's shape, where the safety came from somewhere else and was stated in no rule.

**Key presence, and nothing else.** ``{"body": ""}`` is a *complete* ``send_email`` argument map.
The model named the argument and put nothing in it, which is a question about the value and belongs
to the trust rules and to ``args_present``, both of which already answer it.
:class:`TestAnEmptyValueIsASuppliedValue` pins every one of the thirteen against today's behaviour
so this patch cannot quietly become an empty-string policy.

**The refusal is deliberately indistinguishable from the other structural ones.** ``DENY`` /
``NO_RULE_MATCH`` / no matched rule, exactly as an undeclared argument and an unregistered tool are
refused. No new :class:`~careloop.policy.verdicts.ReasonCode`: the vocabulary is kept coarse on
purpose so a planner cannot map the policy surface by probing it, and *"you omitted an argument"* is
precisely the hint that would help one construct a well-formed call.

The empty ``arg_classes`` and ``arg_trust`` on the returned decision are not incidental. They are
the assertion that the branch runs **before** trust resolution rather than a proxy for it: a
decision that had reached the rule matcher would carry both.
"""

from __future__ import annotations

from typing import Final

import pytest
from helpers import make_case, make_fact, trusted_fact

from careloop.policy.engine import PolicyEngine, default_engine
from careloop.policy.schema import Condition, Rule, RuleSet
from careloop.policy.verdicts import Decision, ReasonCode, Verdict
from careloop.provenance.trust import ArgClass, TrustLevel
from careloop.state.case import Approval, ApprovalStatus, Case, CaseState
from careloop.tools.idempotency import args_digest
from careloop.tools.registry import REGISTRY, ArgSpec, ToolRegistry, ToolSpec

CAREGIVER: Final[str] = "rosa.marlowe@carer.example"
PROVIDER: Final[str] = "PROV-NORTHGATE-CARDIOLOGY"
PORTAL: Final[str] = "PORTAL-NORTHGATE-RECORDS"
PATIENT: Final[str] = "PT-4417"
SLOT: Final[str] = "2026-09-01T10:00:00+00:00"
DOCUMENT: Final[str] = "doc-0001-referral"
ROLE: Final[str] = "records_viewer"

#: The phrase the completeness refusal writes into its timeline diagnostic.
#:
#: Deliberately not a substring of :data:`UNDECLARED_MARKER` and vice versa, so the two structural
#: refusals stay distinguishable by assertion rather than by reading.
COMPLETENESS_MARKER: Final[str] = "without declared argument"

#: The phrase the pre-existing undeclared-argument refusal writes.
UNDECLARED_MARKER: Final[str] = "undeclared argument"


def favourable_facts() -> tuple:
    """Every fact each shipped rule asks for, at the level it asks for it.

    Maximally favourable on purpose: an unrelated trust failure that denied the call anyway would
    hide the branch under test, and a table that passes because everything is denied is a table that
    proves nothing.

    ``role`` is synthesised, and that is worth stating rather than hiding. **No producer anywhere in
    CareLoop emits a fact under that key** — not ``TRUSTED_CONFIG``, not extraction, not promotion —
    so ``grant_portal_access`` can never reach ``ALLOW`` in production and
    ``tests/test_typed_trust.py::TestTheGrantPathHasNoProductionAuthority`` pins that in both
    directions. It is synthesised here for the reason ``tests/test_grant_authority.py`` synthesises
    it: to ask whether completeness holds on the grant path *if* the role barrier were satisfied.
    """
    return (
        trusted_fact("caregiver_email", CAREGIVER),
        trusted_fact("provider_id", PROVIDER),
        trusted_fact("portal_id", PORTAL),
        trusted_fact("role", ROLE),
        make_fact("patient_id", PATIENT, TrustLevel.CONTROLLED),
        make_fact("slot_start", SLOT, TrustLevel.CONTROLLED),
        make_fact("document_id", DOCUMENT, TrustLevel.CONTROLLED),
    )


#: One complete, legitimate call per shipped tool.
COMPLETE: Final[dict[str, dict[str, str]]] = {
    "send_email": {"to": CAREGIVER, "subject": "Appointment confirmed", "body": "Booked."},
    "book_appointment": {
        "provider_id": PROVIDER,
        "patient_id": PATIENT,
        "slot_start": SLOT,
        "reason": "cardiology follow-up",
    },
    "upload_document": {"portal_id": PORTAL, "document_id": DOCUMENT},
    "grant_portal_access": {"grantee": CAREGIVER, "role": ROLE, "portal_id": PORTAL},
    "fetch_document": {"document_id": DOCUMENT},
}

#: The state each tool's own rules permit it from. ``fetch_document`` is the deterministic evidence
#: node's read path and is permitted only while the case is ``GATHERING_EVIDENCE``; every external
#: effect is proposed from ``PLANNING``.
PERMITTED_STATE: Final[dict[str, CaseState]] = {"fetch_document": CaseState.GATHERING_EVIDENCE}


def permitted_state(tool_name: str) -> CaseState:
    return PERMITTED_STATE.get(tool_name, CaseState.PLANNING)


def favourable_case(tool_name: str, *, approving: dict[str, str] | None = None) -> Case:
    """A case in which ``tool_name`` has every advantage the rules can give it.

    ``approving`` records a granted caregiver decision covering exactly that argument map — which
    may be an *incomplete* one, since proving that an approval cannot buy completeness requires
    building the approval the defect would have honoured.
    """
    approvals: tuple[Approval, ...] = ()
    if approving is not None:
        approvals = (
            Approval(
                approval_id="apr-1",
                tool_name=tool_name,
                args_digest=args_digest(approving),
                reason_code=str(ReasonCode.REQUIRES_HUMAN_APPROVAL),
                status=ApprovalStatus.APPROVED,
                decided_by="rosa",
            ),
        )
    case = make_case(state=permitted_state(tool_name), facts=favourable_facts())
    return case.model_copy(update={"approvals": approvals})


def without(tool_name: str, arg_name: str) -> dict[str, str]:
    """The complete call for ``tool_name`` with exactly one declared key removed."""
    args = dict(COMPLETE[tool_name])
    del args[arg_name]
    return args


def emptied(tool_name: str, arg_name: str) -> dict[str, str]:
    """The complete call for ``tool_name`` with one declared value blanked, key intact."""
    return {**COMPLETE[tool_name], arg_name: ""}


#: ``(tool, argument)`` for every declared argument of every registered tool, derived rather than
#: written down so a new tool or argument joins the table without anyone remembering to add it.
EVERY_DECLARED_ARGUMENT: Final[tuple[tuple[str, str], ...]] = tuple(
    (tool.name, arg_name)
    for tool in sorted(REGISTRY, key=lambda t: t.name)
    for arg_name in sorted(tool.arg_names)
)

ARGUMENT_IDS: Final[tuple[str, ...]] = tuple(
    f"{tool}-{arg}" for tool, arg in EVERY_DECLARED_ARGUMENT
)


@pytest.fixture
def engine() -> PolicyEngine:
    return default_engine()


def assert_refused_as_incomplete(decision: Decision, *missing: str) -> None:
    """The full shape of a completeness refusal, asserted in one place.

    ``arg_classes`` and ``arg_trust`` being empty is the load-bearing half: it is what proves the
    branch returned before trust resolution rather than after it.
    """
    assert decision.verdict is Verdict.DENY
    assert decision.reason_code is ReasonCode.NO_RULE_MATCH
    assert decision.matched_rule_id is None
    assert decision.arg_classes == {}
    assert decision.arg_trust == {}
    assert COMPLETENESS_MARKER in decision.diagnostic
    assert UNDECLARED_MARKER not in decision.diagnostic
    for name in missing:
        assert name in decision.diagnostic


class TestTheTableCoversTheShippedRegistry:
    """Otherwise the tables below could drift into covering less than they claim."""

    def test_every_registered_tool_has_a_complete_call(self) -> None:
        assert set(COMPLETE) == set(REGISTRY.names)

    def test_every_complete_call_supplies_exactly_the_declared_arguments(self) -> None:
        for tool_name, args in COMPLETE.items():
            spec = REGISTRY.get(tool_name)
            assert spec is not None
            assert set(args) == set(spec.arg_names), tool_name

    def test_the_argument_table_is_not_empty(self) -> None:
        """A parametrisation over an empty list passes vacuously and reports nothing."""
        assert len(EVERY_DECLARED_ARGUMENT) == sum(len(tool.arg_names) for tool in REGISTRY)
        assert len(EVERY_DECLARED_ARGUMENT) > 0


class TestAnIncompleteCallCannotBeAuthorisedAutonomously:
    """The autonomous half of the defect, on the one tool whose effect is genuinely external.

    ``reason`` is ``VALUE``-class, declares no ``trust_key``, and no rule names it — deliberately,
    because a rule requiring its trust would fail at load. So nothing in ``rules.yaml`` had any
    opinion about its absence, and ``allow-booking-on-validated-slot`` was satisfied by the three
    arguments that were supplied. A rule that simply does not care about the missing argument can no
    longer authorise an incomplete capability.
    """

    def test_a_booking_without_its_reason_is_refused(self, engine: PolicyEngine) -> None:
        args = without("book_appointment", "reason")

        decision = engine.evaluate("book_appointment", args, favourable_case("book_appointment"))

        assert_refused_as_incomplete(decision, "reason")

    def test_the_same_booking_with_its_reason_is_allowed(self, engine: PolicyEngine) -> None:
        """The vacuity floor. Without this the test above could pass on a case that denies
        everything."""
        decision = engine.evaluate(
            "book_appointment", COMPLETE["book_appointment"], favourable_case("book_appointment")
        )

        assert decision.verdict is Verdict.ALLOW
        assert decision.matched_rule_id == "allow-booking-on-validated-slot"


class TestAnIncompleteCallIsNeverPutToAHuman:
    """``REQUIRE_HUMAN`` is refused for the same reason ``ALLOW`` is.

    CareLoop must not ask a caregiver *"do you approve this incomplete email?"* Completeness is a
    question about whether there is a call at all, and it precedes the question of who may authorise
    one. The ordering falls out of the branch's position rather than being enforced separately: it
    returns before ``_MATCH_ORDER`` is entered, so no ``REQUIRE_HUMAN`` rule is ever considered.
    """

    @pytest.mark.parametrize("omitted", ["subject", "body"])
    def test_an_incomplete_email_does_not_reach_the_caregiver(
        self, engine: PolicyEngine, omitted: str
    ) -> None:
        args = without("send_email", omitted)

        decision = engine.evaluate("send_email", args, favourable_case("send_email"))

        assert_refused_as_incomplete(decision, omitted)

    def test_the_complete_email_still_reaches_the_caregiver(self, engine: PolicyEngine) -> None:
        """The vacuity floor: ``REQUIRE_HUMAN`` is still reachable for a well-formed call."""
        decision = engine.evaluate(
            "send_email", COMPLETE["send_email"], favourable_case("send_email")
        )

        assert decision.verdict is Verdict.REQUIRE_HUMAN
        assert decision.matched_rule_id == "require-human-for-outbound-email"


class TestApprovalCannotBuyCompleteness:
    """The strongest of these, because it is where the old behaviour was least defensible.

    ``approved()`` digests whatever argument map it is handed, and
    :meth:`careloop.state.case.Case.is_approved` matches ``(tool_name, args_digest)`` — neither has
    any notion of a tool's declared argument set, and neither should: completeness is a property of
    the call against the registry, and the registry is what the *engine* reasons over. Teaching an
    identity primitive about schemas would give the registry a second consumer.

    So the approval subsystem is unchanged and correct, and the fix is that the engine never reaches
    it. A caregiver cannot authorise something that is not a call.
    """

    @pytest.mark.parametrize("omitted", ["subject", "body"])
    def test_an_exact_approval_over_an_incomplete_map_does_not_authorise_it(
        self, engine: PolicyEngine, omitted: str
    ) -> None:
        args = without("send_email", omitted)
        case = favourable_case("send_email", approving=args)

        decision = engine.evaluate("send_email", args, case)

        assert_refused_as_incomplete(decision, omitted)

    @pytest.mark.parametrize("omitted", ["subject", "body"])
    def test_the_approval_really_does_cover_the_call_it_was_given(self, omitted: str) -> None:
        """Non-vacuity for the test above.

        If the approval did not match, the refusal would prove nothing — the call would be denied
        for want of a decision rather than for being incomplete. This asserts the capability lookup
        answers ``True`` for exactly the map the engine was handed.
        """
        args = without("send_email", omitted)
        case = favourable_case("send_email", approving=args)

        assert case.is_approved("send_email", args_digest(args))

    def test_the_same_approval_over_the_complete_map_still_authorises_it(
        self, engine: PolicyEngine
    ) -> None:
        """The vacuity floor: an approval has not stopped working, it has stopped being consulted
        for malformed calls."""
        args = COMPLETE["send_email"]
        case = favourable_case("send_email", approving=args)

        decision = engine.evaluate("send_email", args, case)

        assert decision.verdict is Verdict.ALLOW
        assert decision.matched_rule_id == "allow-approved-outbound-email"


class TestEveryDeclaredArgumentIsRequired:
    """Coverage across the whole registry: five tools, thirteen declared arguments.

    **This table is coverage, not the causal proof.** Ten of the thirteen omissions already produced
    a ``DENY`` before the completeness branch existed, because a rule happened to name the omitted
    argument and read its absence as ``UNTRUSTED``. What changes for those ten is the *cause* — an
    earlier refusal, no rule, no trust resolution — and what changes for the other three is the
    verdict itself. The causal weight sits in the three classes above and in the mutant.

    Derived from :data:`careloop.tools.registry.REGISTRY` rather than written out, so a new argument
    on any tool is covered without anyone remembering to add it here.
    """

    @pytest.mark.parametrize(("tool_name", "arg_name"), EVERY_DECLARED_ARGUMENT, ids=ARGUMENT_IDS)
    def test_omitting_it_refuses_the_call(
        self, engine: PolicyEngine, tool_name: str, arg_name: str
    ) -> None:
        args = without(tool_name, arg_name)

        decision = engine.evaluate(tool_name, args, favourable_case(tool_name))

        assert_refused_as_incomplete(decision, arg_name)

    @pytest.mark.parametrize(("tool_name", "arg_name"), EVERY_DECLARED_ARGUMENT, ids=ARGUMENT_IDS)
    def test_omitting_it_is_refused_even_with_a_covering_approval(
        self, engine: PolicyEngine, tool_name: str, arg_name: str
    ) -> None:
        args = without(tool_name, arg_name)
        case = favourable_case(tool_name, approving=args)

        assert_refused_as_incomplete(engine.evaluate(tool_name, args, case), arg_name)

    def test_an_empty_argument_map_names_every_declared_argument(
        self, engine: PolicyEngine
    ) -> None:
        """The degenerate case, and the one where the diagnostic has the most to say."""
        decision = engine.evaluate("book_appointment", {}, favourable_case("book_appointment"))

        assert_refused_as_incomplete(decision, "provider_id", "patient_id", "slot_start", "reason")

    def test_the_missing_names_are_ordered_deterministically(self, engine: PolicyEngine) -> None:
        """No clock, no set iteration order in the audit trail: the same call reads the same way
        every time it is replayed."""
        decision = engine.evaluate("book_appointment", {}, favourable_case("book_appointment"))
        again = engine.evaluate("book_appointment", {}, favourable_case("book_appointment"))

        assert decision.diagnostic == again.diagnostic
        positions = [
            decision.diagnostic.index(name)
            for name in ("patient_id", "provider_id", "reason", "slot_start")
        ]
        assert positions == sorted(positions), "missing names are rendered sorted"


class TestCompletenessPrecedesEverythingElse:
    """Ordering, asserted through what the decision does *not* carry.

    A refusal that had reached the rule matcher would carry an argument class and a resolved trust
    level for every supplied argument, because ``evaluate`` computes both before entering
    ``_MATCH_ORDER``. Empty maps are therefore evidence the branch returned first — stronger than
    asserting the reason code, which the default deny shares.
    """

    def test_trust_is_not_resolved_for_an_incomplete_call(self, engine: PolicyEngine) -> None:
        args = without("send_email", "body")

        decision = engine.evaluate("send_email", args, favourable_case("send_email"))

        assert decision.arg_trust == {}
        assert decision.arg_classes == {}

    def test_trust_is_resolved_for_a_complete_one(self, engine: PolicyEngine) -> None:
        """The other direction, or the assertion above would pass on an engine that resolved
        nothing."""
        decision = engine.evaluate(
            "send_email", COMPLETE["send_email"], favourable_case("send_email")
        )

        assert decision.arg_trust == {
            "to": TrustLevel.TRUSTED,
            "subject": TrustLevel.UNTRUSTED,
            "body": TrustLevel.UNTRUSTED,
        }
        assert decision.arg_classes["to"] is ArgClass.DESTINATION

    def test_a_state_denial_does_not_precede_completeness(self, engine: PolicyEngine) -> None:
        """``deny-external-effect-outside-execution-states`` is the first rule in the file and the
        most fundamental objection any *rule* can make. Completeness is more fundamental still: from
        a state that permits nothing, an incomplete call is still refused as incomplete rather than
        as mistimed, because there is no call to be mistimed."""
        case = make_case(state=CaseState.DONE, facts=favourable_facts())

        decision = engine.evaluate("send_email", without("send_email", "body"), case)

        assert_refused_as_incomplete(decision, "body")
        assert decision.reason_code is not ReasonCode.WRONG_CASE_STATE

    def test_the_arguments_are_not_mutated(self, engine: PolicyEngine) -> None:
        """Purity holds on the new branch too: ``evaluate`` reads and never writes."""
        args = without("book_appointment", "reason")
        before = dict(args)

        engine.evaluate("book_appointment", args, favourable_case("book_appointment"))

        assert args == before

    def test_the_decision_carries_the_supplied_arguments_for_the_timeline(
        self, engine: PolicyEngine
    ) -> None:
        args = without("book_appointment", "reason")

        decision = engine.evaluate("book_appointment", args, favourable_case("book_appointment"))

        assert decision.args == args


class TestTheUndeclaredBranchStillComesFirst:
    """A map that is wrong in both directions reports the extra argument.

    The two refusals mean different things — *"this argument has no class, so nothing says it is not
    a destination"* against *"this is not a complete call"* — and collapsing them into one opaque
    set-mismatch check would lose the distinction in the audit trail. The undeclared branch stays
    first because it is the older and more specific statement.
    """

    def test_an_undeclared_argument_is_reported_over_a_missing_one(
        self, engine: PolicyEngine
    ) -> None:
        args = {**without("send_email", "body"), "bcc": "attacker@evil.example"}

        decision = engine.evaluate("send_email", args, favourable_case("send_email"))

        assert decision.verdict is Verdict.DENY
        assert decision.reason_code is ReasonCode.NO_RULE_MATCH
        assert decision.matched_rule_id is None
        assert UNDECLARED_MARKER in decision.diagnostic
        assert COMPLETENESS_MARKER not in decision.diagnostic
        assert "bcc" in decision.diagnostic

    def test_the_undeclared_refusal_never_leaks_a_value(self, engine: PolicyEngine) -> None:
        args = {**COMPLETE["send_email"], "bcc": "attacker@evil.example"}

        decision = engine.evaluate("send_email", args, favourable_case("send_email"))

        assert "attacker@evil.example" not in decision.diagnostic

    def test_the_completeness_refusal_never_leaks_a_value(self, engine: PolicyEngine) -> None:
        """Argument *names* are in the diagnostic; argument *values* never are. The diagnostic is
        timeline-only and the API withholds it wholesale, but invariant 6's habit is kept anyway."""
        args = without("send_email", "body")

        decision = engine.evaluate("send_email", args, favourable_case("send_email"))

        assert CAREGIVER not in decision.diagnostic
        assert COMPLETE["send_email"]["subject"] not in decision.diagnostic

    def test_an_unregistered_tool_is_still_refused_before_either(
        self, engine: PolicyEngine
    ) -> None:
        """A tool with no spec has no declared argument set to compare against, so the registry
        lookup must keep coming first."""
        decision = engine.evaluate("exfiltrate_everything", {}, favourable_case("send_email"))

        assert decision.verdict is Verdict.DENY
        assert decision.reason_code is ReasonCode.NO_RULE_MATCH
        assert COMPLETENESS_MARKER not in decision.diagnostic
        assert "not in the registry" in decision.diagnostic


class TestCompleteCallsAreUnaffected:
    """Every shipped tool's legitimate verdict, so the branch cannot be swallowing normal calls.

    Not a second copy of the policy suite — ``tests/test_policy_engine.py`` owns the verdict matrix.
    These are the seven outcomes this module's own fixture can reach, pinned by matched rule id so a
    change of *cause* would fail here even if the verdict happened to survive.
    """

    def test_a_complete_booking_is_allowed(self, engine: PolicyEngine) -> None:
        decision = engine.evaluate(
            "book_appointment", COMPLETE["book_appointment"], favourable_case("book_appointment")
        )

        assert decision.verdict is Verdict.ALLOW
        assert decision.matched_rule_id == "allow-booking-on-validated-slot"

    def test_a_complete_upload_is_allowed(self, engine: PolicyEngine) -> None:
        decision = engine.evaluate(
            "upload_document", COMPLETE["upload_document"], favourable_case("upload_document")
        )

        assert decision.verdict is Verdict.ALLOW
        assert decision.matched_rule_id == "allow-upload-to-trusted-portal"

    def test_a_complete_fetch_is_allowed_while_evidence_is_gathered(
        self, engine: PolicyEngine
    ) -> None:
        decision = engine.evaluate(
            "fetch_document", COMPLETE["fetch_document"], favourable_case("fetch_document")
        )

        assert decision.verdict is Verdict.ALLOW
        assert decision.matched_rule_id == "allow-document-fetch-while-gathering-evidence"

    def test_a_complete_email_asks_the_caregiver(self, engine: PolicyEngine) -> None:
        decision = engine.evaluate(
            "send_email", COMPLETE["send_email"], favourable_case("send_email")
        )

        assert decision.verdict is Verdict.REQUIRE_HUMAN
        assert decision.matched_rule_id == "require-human-for-outbound-email"

    def test_an_approved_complete_email_is_allowed(self, engine: PolicyEngine) -> None:
        args = COMPLETE["send_email"]

        decision = engine.evaluate(
            "send_email", args, favourable_case("send_email", approving=args)
        )

        assert decision.verdict is Verdict.ALLOW
        assert decision.matched_rule_id == "allow-approved-outbound-email"

    def test_a_complete_grant_asks_the_caregiver(self, engine: PolicyEngine) -> None:
        decision = engine.evaluate(
            "grant_portal_access",
            COMPLETE["grant_portal_access"],
            favourable_case("grant_portal_access"),
        )

        assert decision.verdict is Verdict.REQUIRE_HUMAN
        assert decision.matched_rule_id == "require-human-for-authority-grant"

    def test_an_approved_complete_grant_is_allowed(self, engine: PolicyEngine) -> None:
        """Reachable only because this module synthesises a ``TRUSTED`` role. Production emits no
        fact under that key and this changes nothing about that."""
        args = COMPLETE["grant_portal_access"]

        decision = engine.evaluate(
            "grant_portal_access",
            args,
            favourable_case("grant_portal_access", approving=args),
        )

        assert decision.verdict is Verdict.ALLOW
        assert decision.matched_rule_id == "allow-approved-portal-access-grant"


#: What every declared argument's verdict is when its value is blanked and its **key is kept**.
#:
#: Captured from the tree before the completeness branch existed and asserted verbatim afterwards.
#: This is the hard non-regression: an empty value is a *supplied* value, judged by the trust rules
#: and by ``args_present`` exactly as it was, and this patch is not an empty-string policy. Note
#: ``book_appointment.reason``, whose empty value still reaches ``ALLOW`` — whether it should is the
#: open reason-content question, which this patch deliberately leaves untouched.
EMPTY_VALUE_VERDICTS: Final[dict[tuple[str, str], tuple[Verdict, str | None]]] = {
    ("book_appointment", "provider_id"): (Verdict.DENY, None),
    ("book_appointment", "patient_id"): (Verdict.DENY, "deny-booking-for-unverified-patient"),
    ("book_appointment", "slot_start"): (Verdict.DENY, "deny-booking-on-unvalidated-slot"),
    ("book_appointment", "reason"): (Verdict.ALLOW, "allow-booking-on-validated-slot"),
    ("send_email", "to"): (Verdict.DENY, "deny-email-to-untrusted-recipient"),
    ("send_email", "subject"): (Verdict.REQUIRE_HUMAN, "require-human-for-outbound-email"),
    ("send_email", "body"): (Verdict.REQUIRE_HUMAN, "require-human-for-outbound-email"),
    ("upload_document", "portal_id"): (Verdict.DENY, "deny-upload-to-untrusted-portal"),
    ("upload_document", "document_id"): (Verdict.DENY, None),
    ("grant_portal_access", "grantee"): (
        Verdict.DENY,
        "deny-portal-access-to-untrusted-grantee",
    ),
    ("grant_portal_access", "role"): (Verdict.DENY, "deny-authority-grant-below-trusted"),
    ("grant_portal_access", "portal_id"): (Verdict.DENY, "deny-portal-access-to-untrusted-portal"),
    ("fetch_document", "document_id"): (Verdict.DENY, None),
}


class TestAnEmptyValueIsASuppliedValue:
    """The hard non-regression. ``{"body": ""}`` is a complete ``send_email`` call.

    Completeness is **key presence and nothing else**. It does not read truthiness, does not
    consult :func:`careloop.provenance.canonical.canonical_value`, and does not overlap
    ``args_present``. The model named the argument and put nothing in it, which is a statement about
    the value, and the trust rules and ``args_present`` already answer statements about values.

    Every one of the thirteen is pinned against the verdict it produced before this branch existed.
    """

    def test_the_table_covers_every_declared_argument(self) -> None:
        assert set(EMPTY_VALUE_VERDICTS) == set(EVERY_DECLARED_ARGUMENT)

    @pytest.mark.parametrize(("tool_name", "arg_name"), EVERY_DECLARED_ARGUMENT, ids=ARGUMENT_IDS)
    def test_an_empty_value_is_never_treated_as_missing(
        self, engine: PolicyEngine, tool_name: str, arg_name: str
    ) -> None:
        decision = engine.evaluate(
            tool_name, emptied(tool_name, arg_name), favourable_case(tool_name)
        )

        assert COMPLETENESS_MARKER not in decision.diagnostic

    @pytest.mark.parametrize(("tool_name", "arg_name"), EVERY_DECLARED_ARGUMENT, ids=ARGUMENT_IDS)
    def test_an_empty_value_keeps_the_verdict_it_has_always_had(
        self, engine: PolicyEngine, tool_name: str, arg_name: str
    ) -> None:
        verdict, rule_id = EMPTY_VALUE_VERDICTS[(tool_name, arg_name)]

        decision = engine.evaluate(
            tool_name, emptied(tool_name, arg_name), favourable_case(tool_name)
        )

        assert decision.verdict is verdict
        assert decision.matched_rule_id == rule_id

    def test_an_empty_booking_reason_still_books(self, engine: PolicyEngine) -> None:
        """Called out explicitly because it is the one empty value that still reaches ``ALLOW``.

        Whether an empty, planner-authored reason should book at all is the open content and
        provenance question — separate from this patch and deliberately untouched by it. What is
        settled here is only that the call is *complete*.
        """
        decision = engine.evaluate(
            "book_appointment",
            emptied("book_appointment", "reason"),
            favourable_case("book_appointment"),
        )

        assert decision.verdict is Verdict.ALLOW
        assert decision.matched_rule_id == "allow-booking-on-validated-slot"

    def test_an_empty_email_body_still_asks_the_caregiver(self, engine: PolicyEngine) -> None:
        decision = engine.evaluate(
            "send_email", emptied("send_email", "body"), favourable_case("send_email")
        )

        assert decision.verdict is Verdict.REQUIRE_HUMAN

    def test_an_approved_empty_email_body_is_still_allowed(self, engine: PolicyEngine) -> None:
        """The exact counterpart of :class:`TestApprovalCannotBuyCompleteness`: an approval still
        authorises a complete call whose values happen to be empty."""
        args = emptied("send_email", "body")

        decision = engine.evaluate(
            "send_email", args, favourable_case("send_email", approving=args)
        )

        assert decision.verdict is Verdict.ALLOW
        assert decision.matched_rule_id == "allow-approved-outbound-email"

    def test_an_empty_document_id_is_still_refused_by_args_present(
        self, engine: PolicyEngine
    ) -> None:
        """``args_present`` keeps a live job, narrowed rather than removed.

        It meant three things at once — key exists, value canonicalisable, value non-empty — and
        completeness subsumes only the first. This is the second and third still working.
        """
        decision = engine.evaluate(
            "fetch_document",
            emptied("fetch_document", "document_id"),
            favourable_case("fetch_document"),
        )

        assert decision.verdict is Verdict.DENY
        assert decision.reason_code is ReasonCode.NO_RULE_MATCH
        assert COMPLETENESS_MARKER not in decision.diagnostic

    def test_an_uncanonicalisable_value_is_still_refused_by_args_present(
        self, engine: PolicyEngine
    ) -> None:
        """The other half of what ``args_present`` still owns. A float has no canonical form, so it
        is supplied, present, and still not usable."""
        decision = engine.evaluate(
            "fetch_document", {"document_id": 1.5}, favourable_case("fetch_document")
        )

        assert decision.verdict is Verdict.DENY
        assert COMPLETENESS_MARKER not in decision.diagnostic


# -- the generic proof ---------------------------------------------------------------------------
#
# The shipped registry and the shipped rules are one arrangement of tools and one set of policy
# opinions. What follows asserts the property itself, through the real engine over a synthetic
# registry, so it reads as an invariant rather than as a spelling of ``rules.yaml``.

SYNTHETIC_A: Final[str] = "tool_a"


def _synthetic_registry() -> ToolRegistry:
    """One tool, two ``VALUE``-class arguments.

    ``VALUE`` deliberately, and for a reason worth stating: an unconditional ``ALLOW`` over a tool
    declaring a ``DESTINATION`` or an ``AUTHORITY`` argument is exactly what
    :func:`careloop.policy.loader.validate_authority_coverage` refuses, so a synthetic registry with
    a destination would drag the loader invariant into a test about something else. These two
    properties are orthogonal and are kept that way.
    """
    return ToolRegistry(
        [
            ToolSpec(
                name=SYNTHETIC_A,
                external_effect=False,
                args=(
                    ArgSpec(name="a", arg_class=ArgClass.VALUE),
                    ArgSpec(name="b", arg_class=ArgClass.VALUE),
                ),
            )
        ]
    )


def _synthetic_rules() -> RuleSet:
    """An unconditional ``ALLOW``, so nothing but completeness can refuse anything."""
    return RuleSet(
        version=1,
        rules=(
            Rule(
                id="allow-anything",
                effect=Verdict.ALLOW,
                tools=(SYNTHETIC_A,),
                when=Condition(),
                diagnostic="The synthetic tool is permitted unconditionally.",
            ),
        ),
    )


class TestTheInvariantIsGeneric:
    """``tool_a(a, b)`` behind a rule that permits everything.

    Independent of trust, of approvals, of case state and of every CareLoop-specific rule. The
    ``{a, b} → ALLOW`` row is what stops the rest passing because the synthetic ruleset denies
    everything, and the ``{a, b, c}`` row is what proves the *old* branch still owns undeclared
    arguments rather than the new one absorbing them.
    """

    @pytest.fixture
    def engine(self) -> PolicyEngine:
        return PolicyEngine(_synthetic_rules(), _synthetic_registry())

    @pytest.fixture
    def case(self) -> Case:
        return make_case(state=CaseState.PLANNING)

    def test_the_complete_call_is_allowed(self, engine: PolicyEngine, case: Case) -> None:
        decision = engine.evaluate(SYNTHETIC_A, {"a": "1", "b": "2"}, case)

        assert decision.verdict is Verdict.ALLOW
        assert decision.matched_rule_id == "allow-anything"

    def test_omitting_b_refuses_the_call(self, engine: PolicyEngine, case: Case) -> None:
        assert_refused_as_incomplete(engine.evaluate(SYNTHETIC_A, {"a": "1"}, case), "b")

    def test_omitting_a_refuses_the_call(self, engine: PolicyEngine, case: Case) -> None:
        assert_refused_as_incomplete(engine.evaluate(SYNTHETIC_A, {"b": "2"}, case), "a")

    def test_omitting_both_refuses_the_call(self, engine: PolicyEngine, case: Case) -> None:
        assert_refused_as_incomplete(engine.evaluate(SYNTHETIC_A, {}, case), "a", "b")

    def test_an_undeclared_argument_is_still_the_old_branch(
        self, engine: PolicyEngine, case: Case
    ) -> None:
        decision = engine.evaluate(SYNTHETIC_A, {"a": "1", "b": "2", "c": "3"}, case)

        assert decision.verdict is Verdict.DENY
        assert UNDECLARED_MARKER in decision.diagnostic
        assert COMPLETENESS_MARKER not in decision.diagnostic

    def test_an_empty_value_is_complete_here_too(self, engine: PolicyEngine, case: Case) -> None:
        """The synthetic ruleset has no opinion about values, so an empty one is simply allowed —
        which is the cleanest possible statement that completeness does not read values."""
        decision = engine.evaluate(SYNTHETIC_A, {"a": "", "b": ""}, case)

        assert decision.verdict is Verdict.ALLOW
