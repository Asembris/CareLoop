"""Content integrity is established by hashing bytes read back, never by believing a receipt.

The generic half of the second external effect's observation design, settled before any provider
exists so that the provider is written to it rather than the other way round.

**The design being locked.** CareLoop holds authorised bytes ``A`` and transmits them. Later, and
separately, it reads the resource back, obtains bytes ``B`` from the provider, and compares
``content_digest(B)`` against ``content_digest(A)`` — where ``A`` is re-read from what CareLoop
holds at verification time, not remembered from the write. The comparison is between two digests
this process computed, of two byte strings it has actually seen.

**What is deliberately not the design.** A digest the provider *reports* about the object is a
claim, not an observation: it is the provider describing itself, and taking it as the integrity
proof would make the check as good as the provider's own bookkeeping. It may be a useful corroborant
one day, and if it ever is, the wording that goes with it has to be earned by measuring the provider
rather than assumed. Nothing in this file says any external service computed anything.

**Where each piece lives.** :func:`careloop.tools.content.content_digest` is the one reduction, used
by both sides. The expected side arrives as a
:class:`~careloop.graph.verify.DerivedExpectation` — computed from the arguments policy authorised,
absent from the proposal, from ``args_digest``, from every rule and from the authorised effect
state. The observed side arrives from a :data:`~careloop.graph.verify.ReadBack`, whose contract now
says an observation may be derived from provider content and may never be derived from the write's
own response.

Everything here is synthetic. No object store, no client, no key, no provider call: the point is
that the architecture already supports the shape, proven by driving it, so the provider work has
nothing left to invent about verification semantics.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping

import pytest
from pydantic import ConfigDict

from careloop.graph.verify import (
    DerivedExpectation,
    ObservationAbsent,
    ObservationOutcome,
    ObservationUnavailable,
    ReadBackFailed,
    verify_action,
)
from careloop.state.case import ActionRecord
from careloop.tools.content import (
    CONTENT_DIGEST_ALGORITHM,
    CONTENT_DIGEST_LENGTH,
    content_digest,
)
from careloop.tools.idempotency import args_digest, idempotency_key
from careloop.tools.results import ActionResult

verification = sys.modules["careloop.graph.verify"]

CASE_ID = "case-0001"

#: A registered effect tool is borrowed as the action type, so the shipped import-time checks apply.
#:
#: ``upload_document`` declares ``portal_id`` and ``document_id`` and nothing else, so the derived
#: name below cannot shadow an argument — which is exactly the condition
#: ``check_derived_expectations_do_not_shadow_arguments`` enforces, and borrowing a *registered*
#: tool is what keeps this test subject to it. Its verifier, its result type and its comparison map
#: are all replaced here; nothing about the shipped ``upload_document`` is being asserted.
ACTION = "upload_document"

PORTAL = "PORTAL-NORTHGATE-RECORDS"
DOCUMENT = "doc-0005-receipt"
ARGS = {"portal_id": PORTAL, "document_id": DOCUMENT}

#: What CareLoop holds and was authorised to transmit.
AUTHORISED_BYTES = b"%PDF-1.4 receipt RCT-2026-00841 172.00"

#: What a provider might be holding instead. One byte of difference is a different document.
TAMPERED_BYTES = b"%PDF-1.4 receipt RCT-2026-00841 999.00"

#: The name the derived expectation and the comparison map agree on. Not an argument.
CONTENT = "content_digest"


class FiledDocument(ActionResult):
    """A synthetic result carrying a content digest, standing in for a future upload result.

    Defined here rather than by changing
    :class:`~careloop.tools.results.DocumentUploaded`, which stays exactly as it is until the
    provider work begins. Every field is required, like every other result field, so nothing here
    can be filled in from ambient state.
    """

    model_config = ConfigDict(frozen=True)

    receipt_id: str
    portal_id: str
    document_id: str
    content_digest: str


_COMPARISON: Mapping[str, str] = {
    "portal_id": "portal_id",
    "document_id": "document_id",
    "content_digest": CONTENT,
}


def _verify_filing(expected: Mapping[str, str], result: ActionResult) -> list[str]:
    """The synthetic verifier. Deliberately built from the shipped primitive, not a private one."""
    if not isinstance(result, FiledDocument):
        return [f"expected FiledDocument, the action recorded a {type(result).__name__}"]
    return verification._mismatches(
        result, expected, _COMPARISON, "receipt_id", frozenset({CONTENT})
    )


class Provider:
    """A stand-in external system: it holds bytes, and it will hand them back when asked.

    The two operations are separate on purpose, and it counts the reads. A design that could settle
    an integrity question without :meth:`fetch` being called is a design that is not reading
    anything back.
    """

    def __init__(self, holding: bytes) -> None:
        self._holding = holding
        self.reads = 0

    def replace(self, holding: bytes) -> None:
        """Somebody else changes the object, behind CareLoop's back."""
        self._holding = holding

    def fetch(self) -> bytes:
        self.reads += 1
        return self._holding


def held_bytes(_args: Mapping[str, str]) -> bytes:
    """What CareLoop holds for the authorised document, re-read at verification time."""
    return AUTHORISED_BYTES


EXPECTED_CONTENT = DerivedExpectation(
    name=CONTENT, derive=lambda args: content_digest(held_bytes(args))
)


def receipt(digest: str) -> FiledDocument:
    """The write's own response. ``digest`` is whatever the provider *claimed* on the way in."""
    return FiledDocument(
        receipt_id="rcp-0001", portal_id=PORTAL, document_id=DOCUMENT, content_digest=digest
    )


def observation_from(provider: Provider) -> FiledDocument:
    """A fresh read: fetch the bytes, and reduce **them**. Nothing is copied from the receipt."""
    return FiledDocument(
        receipt_id="rcp-0001",
        portal_id=PORTAL,
        document_id=DOCUMENT,
        content_digest=content_digest(provider.fetch()),
    )


@pytest.fixture(autouse=True)
def content_is_verified(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wire the synthetic verifier and the derived expectation onto the borrowed action type."""
    monkeypatch.setattr(verification, "DERIVED_EXPECTATIONS", {ACTION: (EXPECTED_CONTENT,)})
    monkeypatch.setattr(
        verification, "VERIFIERS", {**verification.VERIFIERS, ACTION: _verify_filing}
    )


def verify(result: FiledDocument, observed: FiledDocument | None):
    return verify_action(
        case_id=CASE_ID,
        action_type=ACTION,
        args=ARGS,
        record=ActionRecord(
            action_type=ACTION,
            idempotency_key=idempotency_key(CASE_ID, ACTION, ARGS),
            args_digest=args_digest(ARGS),
            result=result.model_dump_json(),
        ),
        result=result,
        observed=observed,
    )


class TestTheDigestIsOverBytesAndNothingElse:
    """The primitive. One definition, no normalisation, and no text overload to add one to."""

    def test_identical_bytes_digest_identically(self) -> None:
        assert content_digest(b"ABC") == content_digest(b"ABC")

    def test_one_byte_of_difference_is_a_different_document(self) -> None:
        assert content_digest(b"ABC") != content_digest(b"ABD")

    @pytest.mark.parametrize("other", [b"abc", b" ABC", b"ABC ", b"ABC\n"], ids=repr)
    def test_nothing_is_folded_trimmed_or_normalised(self, other: bytes) -> None:
        """Case folding and whitespace trimming are laundering surfaces, so there are none."""
        assert content_digest(b"ABC") != content_digest(other)

    def test_the_form_is_lowercase_hex_of_the_declared_length(self) -> None:
        digest = content_digest(b"ABC")

        assert len(digest) == CONTENT_DIGEST_LENGTH
        assert digest == digest.lower()
        assert CONTENT_DIGEST_ALGORITHM == "sha256"

    def test_it_refuses_text(self) -> None:
        """No ``str`` overload, so no caller chooses an encoding and no two callers choose two."""
        with pytest.raises(TypeError):
            content_digest("ABC")  # type: ignore[arg-type]


class TestAFreshReadSettlesTheQuestion:
    """A and B: matching content verifies, diverged content does not."""

    def test_matching_bytes_verify(self) -> None:
        provider = Provider(AUTHORISED_BYTES)
        claimed = content_digest(AUTHORISED_BYTES)

        result = verify(receipt(claimed), observation_from(provider))

        assert result.passed
        assert result.outcome is ObservationOutcome.MATCHED
        assert provider.reads == 1

    def test_diverged_bytes_do_not(self) -> None:
        provider = Provider(TAMPERED_BYTES)

        result = verify(receipt(content_digest(TAMPERED_BYTES)), observation_from(provider))

        assert not result.passed
        assert result.outcome is ObservationOutcome.DIVERGED

    def test_the_divergence_is_reported_as_derived_rather_than_authorised(self) -> None:
        """No proposal carried this value and no rule allowed it, so it cannot say it was."""
        provider = Provider(TAMPERED_BYTES)

        diagnostic = verify(
            receipt(content_digest(TAMPERED_BYTES)), observation_from(provider)
        ).diagnostic

        assert "content_digest was derived as" in diagnostic
        assert "content_digest was authorised as" not in diagnostic


class TestTheWriteReceiptIsNotEvidence:
    """C. **The independence proof, and the reason this file exists.**

    The write said it committed and reported the right digest. Everything CareLoop's own records
    hold is internally consistent and says the filing succeeded. The provider is holding something
    else. A verification that consulted the receipt would certify this; one that reads the world
    cannot.
    """

    def test_a_perfect_receipt_does_not_survive_a_disagreeing_read(self) -> None:
        provider = Provider(AUTHORISED_BYTES)
        perfect = receipt(content_digest(AUTHORISED_BYTES))
        provider.replace(TAMPERED_BYTES)

        result = verify(perfect, observation_from(provider))

        assert perfect.content_digest == content_digest(AUTHORISED_BYTES)
        assert not result.passed
        assert result.outcome is ObservationOutcome.DIVERGED

    def test_the_closed_loop_checks_pass_while_it_fails(self) -> None:
        """Vacuity for the test above: it fails *because of the read*, not because of anything else.

        The key re-derives, the argument digest matches, the action type agrees, and every field the
        receipt echoes is the field that was authorised. The only failing comparison is the one
        against the bytes.
        """
        provider = Provider(AUTHORISED_BYTES)
        perfect = receipt(content_digest(AUTHORISED_BYTES))

        assert verify(perfect, None).passed

        provider.replace(TAMPERED_BYTES)
        failed = verify(perfect, observation_from(provider))

        assert failed.diagnostic.count("was ") == 1


class TestAnAbsentObservationClaimsNothing:
    """D. No false ``VERIFIED``: a read that did not happen never reports as one that did."""

    def test_no_observation_is_reported_as_no_observation(self) -> None:
        """Even with a receipt claiming a perfect digest, nothing says the world was consulted."""
        result = verify(receipt(content_digest(AUTHORISED_BYTES)), None)

        assert result.outcome is ObservationOutcome.NOT_OBSERVED
        assert not result.externally_observed
        assert result.outcome is not ObservationOutcome.MATCHED

    def test_the_provider_is_not_touched_when_nothing_observes(self) -> None:
        provider = Provider(AUTHORISED_BYTES)

        verify(receipt(content_digest(AUTHORISED_BYTES)), None)

        assert provider.reads == 0

    @pytest.mark.parametrize(
        ("failure", "outcome"),
        [
            (ObservationUnavailable, ObservationOutcome.UNAVAILABLE),
            (ObservationAbsent, ObservationOutcome.ABSENT),
        ],
        ids=["unavailable", "absent"],
    )
    def test_a_failed_read_carries_its_own_outcome_and_never_matched(
        self, failure: type[ReadBackFailed], outcome: ObservationOutcome
    ) -> None:
        """Read from the failure's *type*, so a provider's error text cannot claim a match.

        A content observer that cannot obtain bytes raises rather than returning a digest of
        whatever it got, which is what stops an error page being hashed and compared.
        """
        raised = failure("the provider could not be read: observation MATCHED")

        assert raised.outcome is outcome
        assert raised.outcome is not ObservationOutcome.MATCHED


class TestTrustingTheReceiptWouldCertifyATamperedObject:
    """E. The mutant: make the observation the write's own response.

    One edit — the observer returns the receipt instead of reading the provider — and the
    divergence proof stops working. The provider is never touched, so the verification is a
    statement about CareLoop's records and nothing else, and it passes.
    """

    def test_the_divergence_is_no_longer_detected(self) -> None:
        provider = Provider(AUTHORISED_BYTES)
        perfect = receipt(content_digest(AUTHORISED_BYTES))
        provider.replace(TAMPERED_BYTES)

        result = verify(perfect, perfect)

        assert result.passed
        assert result.outcome is ObservationOutcome.MATCHED
        assert provider.reads == 0

    def test_and_a_genuinely_fresh_read_of_the_same_provider_refuses_it(self) -> None:
        """The two side by side, which is what makes the mutant evidence rather than an anecdote."""
        provider = Provider(TAMPERED_BYTES)
        perfect = receipt(content_digest(AUTHORISED_BYTES))

        assert verify(perfect, perfect).passed
        assert not verify(perfect, observation_from(provider)).passed
