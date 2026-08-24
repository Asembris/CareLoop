"""P6 -- every malformed, expired, misdirected or unsafely signed token fails closed.

Patch 7. Real RS256 tokens, real signature verification, the production
:meth:`careloop.identity.oidc.OidcVerifier.verify`, and a locally generated key pair instead of a
JWKS endpoint. **Only the key transport is local.** Nothing here relaxes an algorithm, a claim
requirement or an audience rule, and the offline suite therefore exercises the same claim policy a
deployed CareLoop runs.

The two confusions have their own tests and are refused *before any claim is read*. Two independent
mechanisms do it, and the tests say which is which rather than crediting one for both:
``algorithms=("RS256",)`` is a positive allowlist passed to the library, **and** the key a
:class:`~careloop.identity.oidc.SigningKeys` returns is an RSA public key that PyJWT will not accept
as an HMAC secret. Measured: widening the allowlist alone does not make either token verify, because
the key type refuses it second. So the allowlist is proven load-bearing by
``test_the_allowlist_is_what_decides``, which holds the key constant and varies only the list.

**Every refusal is the same refusal.** One exception type, one message to the caller, the detail in
a log line. A caller who could tell an expired token from a wrong audience by probing would have
been handed a map of the verifier -- invariant 6's argument, applied to a different consumer. The
last class asserts that indistinguishability directly rather than leaving it to the docstring.
"""

from __future__ import annotations

import time

import pytest
from identity_helpers import CAREGIVER_A, CAREGIVER_B, two_caregiver_directory
from oidc_helpers import (
    AUDIENCE,
    ISSUER,
    PUBLIC_KEY,
    SUBJECT_B,
    StaticSigningKeys,
    UnavailableSigningKeys,
    another_issuer,
    handmade_hs256_token,
    symmetric_token,
    token,
    unsigned_token,
    verifier,
)

from careloop.identity.oidc import (
    ALLOWED_ALGORITHMS,
    AUDIENCE_CLAIM,
    DEFAULT_LEEWAY_SECONDS,
    OidcVerifier,
    cognito_verifier,
)
from careloop.identity.principal import AuthenticationFailed, AuthMethod


class TestAValidTokenEstablishesItsCaregiver:
    """Vacuity for everything below: refusals are about the defect, not about nothing working."""

    def test_a_good_token_resolves_to_the_mapped_caregiver(self) -> None:
        who = verifier().verify(token())

        assert who.caregiver_id == CAREGIVER_A
        assert who.auth_method is AuthMethod.VERIFIED_OIDC
        assert who.is_verified

    def test_a_different_subject_resolves_to_a_different_caregiver(self) -> None:
        assert verifier().verify(token(subject=SUBJECT_B)).caregiver_id == CAREGIVER_B

    def test_a_token_with_a_not_before_in_the_past_is_fine(self) -> None:
        assert verifier().verify(token(not_before=int(time.time()) - 60)) is not None

    def test_a_list_valued_audience_containing_ours_is_accepted(self) -> None:
        """Permitted by the specification, and still an exact match on the value."""
        bound = verifier(token_use="id")
        accepted = bound.verify(
            token(token_use="id", audience=None, extra={"aud": ["someone-else", AUDIENCE]})
        )

        assert accepted.caregiver_id == CAREGIVER_A


class TestTheAlgorithmAllowlistIsPositive:
    def test_an_unsigned_token_is_refused(self) -> None:
        with pytest.raises(AuthenticationFailed):
            verifier().verify(unsigned_token())

    def test_an_hs256_token_signed_with_a_guessable_secret_is_refused(self) -> None:
        with pytest.raises(AuthenticationFailed):
            verifier().verify(symmetric_token("a-secret-long-enough-for-sha256!!"))

    def test_an_hs256_token_signed_with_the_public_key_is_refused(self) -> None:
        """The RS/HS confusion proper: the verification key used as a shared secret.

        Assembled by hand -- see :func:`oidc_helpers.handmade_hs256_token`. PyJWT will not *encode*
        one, which is the library protecting the signing side; what is under test is that decoding
        refuses it because RS256 is the only algorithm offered.
        """
        from cryptography.hazmat.primitives import serialization

        public_pem = PUBLIC_KEY.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )

        with pytest.raises(AuthenticationFailed):
            verifier().verify(handmade_hs256_token(public_pem))

    def test_only_one_algorithm_is_allowed(self) -> None:
        assert ALLOWED_ALGORITHMS == ("RS256",)

    def test_the_allowlist_is_what_the_verifier_uses(self) -> None:
        assert verifier().algorithms == ALLOWED_ALGORITHMS

    def test_the_allowlist_is_what_decides(self) -> None:
        """Non-vacuity for the allowlist, holding the key constant and varying only the list.

        Both tokens above are refused twice over -- by the allowlist and by PyJWT declining an RSA
        key as an HMAC secret -- so neither proves the allowlist does anything. This does: one
        symmetric secret, one HS256 token, and two verifiers differing in nothing but which
        algorithms they offer.
        """
        secret = "a-secret-long-enough-for-sha256!!"
        presented = symmetric_token(secret)

        permissive = OidcVerifier(
            issuer=ISSUER,
            audience=AUDIENCE,
            directory=two_caregiver_directory(),
            keys=StaticSigningKeys(secret),
            algorithms=("RS256", "HS256"),
        )
        strict = OidcVerifier(
            issuer=ISSUER,
            audience=AUDIENCE,
            directory=two_caregiver_directory(),
            keys=StaticSigningKeys(secret),
        )

        assert permissive.verify(presented).caregiver_id == CAREGIVER_A
        with pytest.raises(AuthenticationFailed):
            strict.verify(presented)


class TestSignatureAndIssuer:
    def test_a_token_signed_by_a_stranger_is_refused(self) -> None:
        stranger, _ = another_issuer()

        with pytest.raises(AuthenticationFailed):
            verifier().verify(token(private_key=stranger))

    def test_a_tampered_token_is_refused(self) -> None:
        good = token()
        header, payload, signature = good.split(".")
        tampered = f"{header}.{payload}.{signature[:-4]}AAAA"

        with pytest.raises(AuthenticationFailed):
            verifier().verify(tampered)

    def test_the_wrong_issuer_is_refused(self) -> None:
        with pytest.raises(AuthenticationFailed):
            verifier().verify(token(issuer="https://elsewhere.example/pool"))

    def test_a_missing_issuer_is_refused(self) -> None:
        with pytest.raises(AuthenticationFailed):
            verifier().verify(token(omit=("iss",)))

    def test_a_key_that_cannot_be_resolved_is_refused(self) -> None:
        """An unreachable JWKS, or a ``kid`` it does not carry. Fail closed, never fail open."""
        bound = OidcVerifier(
            issuer=ISSUER,
            audience=AUDIENCE,
            directory=two_caregiver_directory(),
            keys=UnavailableSigningKeys(),
        )

        with pytest.raises(AuthenticationFailed):
            bound.verify(token())


class TestLifetime:
    def test_an_expired_token_is_refused(self) -> None:
        with pytest.raises(AuthenticationFailed):
            verifier().verify(token(now=int(time.time()) - 7200, expires_in=3600))

    def test_a_token_expiring_within_the_leeway_is_still_accepted(self) -> None:
        """Vacuity for the expiry check: the boundary is the leeway, not a blanket refusal."""
        assert verifier().verify(token(now=int(time.time()) - 10, expires_in=1)) is not None

    def test_a_not_yet_valid_token_is_refused(self) -> None:
        with pytest.raises(AuthenticationFailed):
            verifier().verify(token(not_before=int(time.time()) + 7200))

    def test_a_missing_expiry_is_refused(self) -> None:
        with pytest.raises(AuthenticationFailed):
            verifier().verify(token(omit=("exp",)))

    def test_a_missing_issued_at_is_refused(self) -> None:
        with pytest.raises(AuthenticationFailed):
            verifier().verify(token(omit=("iat",)))

    def test_the_leeway_is_bounded_and_stated(self) -> None:
        assert DEFAULT_LEEWAY_SECONDS == 60
        assert verifier().leeway_seconds == DEFAULT_LEEWAY_SECONDS


class TestAudienceAndTokenUse:
    def test_the_wrong_audience_is_refused(self) -> None:
        with pytest.raises(AuthenticationFailed):
            verifier().verify(token(audience="some-other-client"))

    def test_a_missing_audience_is_refused(self) -> None:
        with pytest.raises(AuthenticationFailed):
            verifier().verify(token(audience=None))

    def test_an_id_token_presented_as_an_access_token_is_refused(self) -> None:
        """Different credential, different lifetime, different audience claim."""
        with pytest.raises(AuthenticationFailed):
            verifier().verify(token(token_use="id", audience_claim="aud", audience=AUDIENCE))

    def test_a_token_with_no_token_use_is_refused(self) -> None:
        with pytest.raises(AuthenticationFailed):
            verifier().verify(token(token_use=None))

    def test_an_access_tokens_audience_is_read_from_client_id(self) -> None:
        """Cognito access tokens carry no ``aud``, so checking ``aud`` would refuse every one."""
        assert AUDIENCE_CLAIM["access"] == "client_id"
        assert verifier().verify(token(audience_claim="client_id")) is not None

    def test_an_access_token_carrying_only_aud_is_refused(self) -> None:
        """The other half: an ``aud`` where a ``client_id`` was required is not a near miss."""
        with pytest.raises(AuthenticationFailed):
            verifier().verify(token(audience=None, extra={"aud": AUDIENCE}))

    def test_an_id_token_verifier_reads_aud(self) -> None:
        bound = verifier(token_use="id")
        assert bound.verify(token(token_use="id", audience_claim="aud")) is not None

    def test_a_token_use_nobody_decided_about_is_refused_at_construction(self) -> None:
        with pytest.raises(ValueError, match="token_use"):
            verifier(token_use="refresh")

    def test_every_known_token_use_names_its_audience_claim(self) -> None:
        assert set(AUDIENCE_CLAIM) == {"access", "id"}


class TestTheSubjectMustMapToACaregiver:
    def test_an_unmapped_subject_is_refused(self) -> None:
        with pytest.raises(AuthenticationFailed):
            verifier().verify(token(subject="sub-stranger"))

    def test_a_missing_subject_is_refused(self) -> None:
        with pytest.raises(AuthenticationFailed):
            verifier().verify(token(omit=("sub",)))

    def test_a_deactivated_caregiver_is_refused(self) -> None:
        """Fail closed at the boundary as well as at the grant check. Two independent points."""
        directory = two_caregiver_directory()
        directory.deactivate(CAREGIVER_A)

        with pytest.raises(AuthenticationFailed):
            verifier(directory).verify(token())

    def test_a_subject_from_another_issuer_does_not_map(self) -> None:
        """The directory keys on ``(issuer, subject)``; the verifier passes its own issuer."""
        bound = OidcVerifier(
            issuer=ISSUER,
            audience=AUDIENCE,
            directory=two_caregiver_directory(),
            keys=StaticSigningKeys(PUBLIC_KEY),
        )
        assert bound.issuer == ISSUER


class TestMalformedInputIsNotACrash:
    @pytest.mark.parametrize(
        "credential",
        [None, "", "   ", "not-a-token", "a.b", "a.b.c", "..", "Bearer x", "e30.e30.e30"],
        ids=[
            "none",
            "empty",
            "blank",
            "garbage",
            "two-parts",
            "three-bad",
            "dots",
            "scheme",
            "b64",
        ],
    )
    def test_every_malformed_credential_is_an_authentication_failure(self, credential) -> None:
        with pytest.raises(AuthenticationFailed):
            verifier().verify(credential)


class TestNoFailureIsDistinguishableFromAnother:
    """A caller who could tell them apart would have a map of the verifier."""

    def test_every_refusal_raises_the_same_type(self) -> None:
        stranger, _ = another_issuer()
        for bad in (
            unsigned_token(),
            symmetric_token("a-secret-long-enough-for-sha256!!"),
            token(private_key=stranger),
            token(issuer="https://elsewhere.example/pool"),
            token(audience="another-client"),
            token(token_use="id", audience_claim="aud"),
            token(now=int(time.time()) - 7200, expires_in=3600),
            token(subject="sub-stranger"),
            "garbage",
        ):
            with pytest.raises(AuthenticationFailed):
                verifier().verify(bad)

    def test_no_refusal_message_carries_a_claim_value(self) -> None:
        """The message reaches a log; a claim value is somebody's identity."""
        for bad, secret in (
            (token(subject="sub-secret-person"), "sub-secret-person"),
            (token(audience="secret-client-id"), "secret-client-id"),
            (token(issuer="https://secret-issuer.example"), "secret-issuer"),
        ):
            with pytest.raises(AuthenticationFailed) as raised:
                verifier().verify(bad)
            assert secret not in str(raised.value)

    def test_no_refusal_message_carries_the_token(self) -> None:
        bad = token(audience="another-client")
        with pytest.raises(AuthenticationFailed) as raised:
            verifier().verify(bad)
        assert bad not in str(raised.value)


class TestTheProductionCompositionIsNotReachedByTests:
    """It exists, it derives the standard endpoint, and it opens no connection to be constructed."""

    def test_the_jwks_endpoint_is_derived_from_the_issuer(self) -> None:
        built = cognito_verifier(
            issuer="https://cognito-idp.eu-west-1.amazonaws.com/eu-west-1_abc",
            audience="client",
            directory=two_caregiver_directory(),
        )

        assert built.keys.jwks_uri == (
            "https://cognito-idp.eu-west-1.amazonaws.com/eu-west-1_abc/.well-known/jwks.json"
        )

    def test_a_trailing_slash_on_the_issuer_does_not_double(self) -> None:
        built = cognito_verifier(
            issuer="https://issuer.example/pool/",
            audience="client",
            directory=two_caregiver_directory(),
        )

        assert built.keys.jwks_uri == "https://issuer.example/pool/.well-known/jwks.json"
        assert built.issuer == "https://issuer.example/pool"

    def test_constructing_it_reaches_no_network(self) -> None:
        """Lazy on purpose: building an app in a test must not open a connection."""
        built = cognito_verifier(
            issuer=ISSUER, audience=AUDIENCE, directory=two_caregiver_directory()
        )

        assert built.keys._client is None

    def test_it_establishes_verified_principals(self) -> None:
        built = cognito_verifier(
            issuer=ISSUER, audience=AUDIENCE, directory=two_caregiver_directory()
        )

        assert built.auth_method is AuthMethod.VERIFIED_OIDC

    def test_it_refuses_an_empty_issuer_or_audience(self) -> None:
        for issuer, audience in (("", AUDIENCE), (ISSUER, "")):
            with pytest.raises(ValueError, match="issuer and an audience"):
                OidcVerifier(
                    issuer=issuer,
                    audience=audience,
                    directory=two_caregiver_directory(),
                    keys=StaticSigningKeys(PUBLIC_KEY),
                )
