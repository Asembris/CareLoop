"""The production API accepts a Cognito **access** token and nothing else.

The behaviour was already right and nothing was holding it there. Measured before this file
existed: flipping ``cognito_verifier``'s ``token_use`` default from ``"access"`` to ``"id"`` --
which would make every deployment accept ID tokens as API credentials and refuse every access token
Cognito issues -- turned **0 of 100** tests red. ``TestTheProductionCompositionIsNotReachedByTests``
asserted the JWKS URI, the issuer, the laziness and the auth method, and never the token use.

So the composition now *names* it, and every assertion here goes **through the composed production
boundary** rather than through a directly constructed verifier. A verifier a test built itself can
be built any way at all; what a deployment gets is what
:func:`careloop.identity.config.auth_from_environment` composes.

**Why the two are genuinely different credentials.** A Cognito access token carries ``client_id``
and no ``aud`` at all; an ID token carries ``aud`` and ``token_use: "id"`` and is minted for a user
agent to read rather than for an API to be called with. A verifier that checked ``aud`` would refuse
every real access token; one that accepted whichever claim happened to match would accept an ID
token where an access token was meant, and the audience check would stop meaning one thing.

A lower-level :class:`~careloop.identity.oidc.OidcVerifier` can still be constructed for ID tokens
-- that is a real deployment shape and ``tests/test_oidc_verifier.py`` exercises it. What must not
happen is CareLoop's own production composition reaching one.
"""

from __future__ import annotations

import pytest
from identity_helpers import CAREGIVER_A, two_caregiver_directory
from oidc_helpers import (
    AUDIENCE,
    ISSUER,
    PUBLIC_KEY,
    StaticSigningKeys,
    symmetric_token,
    token,
    unsigned_token,
)

from careloop.api.auth import AuthMode
from careloop.identity.config import (
    AUDIENCE_VAR,
    AUTH_MODE_VAR,
    ISSUER_VAR,
    PRODUCTION_MODE,
    auth_from_environment,
)
from careloop.identity.oidc import ACCESS_TOKEN, AUDIENCE_CLAIM, ID_TOKEN, OidcVerifier
from careloop.identity.principal import AuthenticationFailed, AuthMethod

PRODUCTION_ENV = {
    AUTH_MODE_VAR: PRODUCTION_MODE,
    ISSUER_VAR: ISSUER,
    AUDIENCE_VAR: AUDIENCE,
}


def composed():
    """The production boundary a deployment gets, with only the key transport swapped.

    Everything that decides *whether a token is acceptable* -- the algorithm allowlist, the issuer,
    the audience claim, the token use, the lifetime -- is the composed object's own. What is
    replaced is where the public key comes from, because a JWKS endpoint is a network call and this
    suite runs offline at $0.
    """
    boundary = auth_from_environment(directory=two_caregiver_directory(), environ=PRODUCTION_ENV)
    verifier = boundary.verifier
    assert isinstance(verifier, OidcVerifier)
    return OidcVerifier(
        issuer=verifier.issuer,
        audience=verifier.audience,
        directory=verifier.directory,
        keys=StaticSigningKeys(PUBLIC_KEY),
        token_use=verifier.token_use,
        algorithms=verifier.algorithms,
        leeway_seconds=verifier.leeway_seconds,
    )


class TestTheCompositionNamesTheTokenUse:
    def test_production_composes_an_access_token_verifier(self) -> None:
        boundary = auth_from_environment(
            directory=two_caregiver_directory(), environ=PRODUCTION_ENV
        )

        assert boundary.mode is AuthMode.PRODUCTION
        assert boundary.verifier.token_use == ACCESS_TOKEN

    def test_the_audience_is_read_from_client_id(self) -> None:
        assert AUDIENCE_CLAIM[ACCESS_TOKEN] == "client_id"
        assert AUDIENCE_CLAIM[ID_TOKEN] == "aud"

    def test_the_two_token_uses_are_distinct_names(self) -> None:
        """So a mutant swapping them is a visible edit rather than a one-character one."""
        assert ACCESS_TOKEN != ID_TOKEN
        assert set(AUDIENCE_CLAIM) == {ACCESS_TOKEN, ID_TOKEN}

    def test_it_is_stated_rather_than_inherited(self) -> None:
        """The whole point of this file. ``auth_from_environment`` must pass it explicitly.

        A default is correct behaviour nobody is holding in place: the measured mutant that flipped
        it turned nothing red, because no test read it. This reads it, and the source below says it
        is not merely being inherited.
        """
        import inspect

        from careloop.identity import config

        source = inspect.getsource(config.auth_from_environment)
        assert "token_use=ACCESS_TOKEN" in source


class TestWhatTheComposedBoundaryAccepts:
    def test_a_valid_access_token_with_the_right_client_id(self) -> None:
        who = composed().verify(token())

        assert who.caregiver_id == CAREGIVER_A
        assert who.auth_method is AuthMethod.VERIFIED_OIDC


class TestWhatTheComposedBoundaryRefuses:
    @pytest.mark.parametrize(
        ("what", "presented"),
        [
            ("an id token", lambda: token(token_use=ID_TOKEN, audience_claim="aud")),
            ("an id token carrying client_id", lambda: token(token_use=ID_TOKEN)),
            ("the wrong client_id", lambda: token(audience="some-other-client")),
            ("aud instead of client_id", lambda: token(audience=None, extra={"aud": AUDIENCE})),
            ("no audience at all", lambda: token(audience=None)),
            ("no token_use", lambda: token(token_use=None)),
            ("an unknown token_use", lambda: token(token_use="refresh")),
            ("an unsigned token", unsigned_token),
            ("an hs256 token", lambda: symmetric_token("a-secret-long-enough-for-sha256!!")),
            ("the wrong issuer", lambda: token(issuer="https://elsewhere.example/pool")),
            ("an unmapped subject", lambda: token(subject="sub-stranger")),
            ("garbage", lambda: "not-a-token"),
            ("nothing", lambda: None),
        ],
        ids=lambda value: value if isinstance(value, str) else "",
    )
    def test_every_one_of_them_is_an_authentication_failure(self, what: str, presented) -> None:
        with pytest.raises(AuthenticationFailed):
            composed().verify(presented())


class TestTheBoundaryTurnsThoseIntoTheSameHttpAnswer:
    """Through the dependency, so the API's answer is asserted rather than the verifier's."""

    def _refuses(self, credential: str | None) -> int:
        from fastapi import HTTPException

        from careloop.api.auth import AuthBoundary, principal

        bound = AuthBoundary(
            mode=AuthMode.PRODUCTION,
            verifier=composed(),
            directory=two_caregiver_directory(),
        )
        try:
            principal(bound, credential)
        except HTTPException as refused:
            return refused.status_code
        return 200

    def test_an_id_token_is_a_401(self) -> None:
        assert self._refuses(f"Bearer {token(token_use=ID_TOKEN, audience_claim='aud')}") == 401

    def test_a_wrong_client_id_is_a_401(self) -> None:
        assert self._refuses(f"Bearer {token(audience='some-other-client')}") == 401

    def test_no_token_is_a_401(self) -> None:
        assert self._refuses(None) == 401

    def test_a_valid_access_token_is_accepted(self) -> None:
        """Vacuity: the 401s above are the credential, not the dependency refusing everything."""
        assert self._refuses(f"Bearer {token()}") == 200
