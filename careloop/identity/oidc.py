"""Verifying an OIDC access token, and turning its subject into a caregiver CareLoop owns.

**No custom cryptography, and no custom parser.** Signature verification, base64url handling and
claim decoding are ``PyJWT``'s, with ``cryptography`` behind it for RSA. What this module adds is
the *policy*: which algorithms are acceptable, which issuer, which audience claim, which token use,
and what happens to the subject afterwards.

**The algorithm allowlist is positive.** ``algorithms=("RS256",)`` is passed to ``jwt.decode``, so
``alg: none`` and an HS256 token signed with a public key are refused by the library before any
claim is read -- the two classic confusions, closed by not offering the option rather than by
detecting it.

**Cognito's audience lives in different claims for different token uses, and that is not a detail.**
An *access* token carries ``client_id`` and no ``aud`` at all; an *ID* token carries ``aud`` and
``token_use: "id"``. So a verifier that checked ``aud`` on an access token would reject every real
one, and one that accepted "whichever of the two matches" would accept an ID token where an access
token was meant. :data:`AUDIENCE_CLAIM` names exactly one claim per token use, PyJWT's own ``aud``
check is turned off because it cannot express that, and the check is made here against the
configured value.

**The key source is injectable and the claim policy is not.** Production reads JWKS over HTTPS
through ``PyJWKClient``, which caches by ``kid`` and refetches when an unknown one appears -- key
rotation without a restart. A test supplies a locally generated key. Both go through *this*
:meth:`OidcVerifier.verify`, so the offline suite exercises the claim validation production uses
rather than a second implementation of it.

**The token stops here.** What leaves is a :class:`~careloop.identity.principal.Principal` carrying
a ``caregiver_id`` CareLoop's own directory produced, and nothing else -- no claims, no subject, no
email, no token. ``tests/test_identity_boundary.py`` sweeps the package to keep it that way.

**Every failure is the same failure.** :class:`~careloop.identity.principal.AuthenticationFailed`,
undifferentiated, for invariant 6's reason applied to a different consumer: a caller who can tell an
expired token from a wrong audience by probing has been handed a map of the verifier. The
distinguishing detail goes to a log line that never carries the token or the subject.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Final, Protocol, runtime_checkable

from careloop.identity.directory import CaregiverDirectory, UnknownCaregiver, UnknownSubject
from careloop.identity.principal import (
    AuthenticationFailed,
    AuthMethod,
    Principal,
    PrincipalVerifier,
)

logger = logging.getLogger(__name__)

#: The only signature algorithm this deployment accepts.
#:
#: A tuple rather than a set so the value passed to ``jwt.decode`` is ordered and obvious, and a
#: constant rather than a literal so there is one place to look. Cognito signs with RS256; adding a
#: second member is a deliberate act with its own reason, never a convenience.
ALLOWED_ALGORITHMS: Final[tuple[str, ...]] = ("RS256",)

#: Which claim carries the audience, per Cognito token use.
#:
#: See the module docstring: an access token has ``client_id`` and no ``aud``, an ID token has
#: ``aud``. A mapping rather than a branch, so a token use nobody decided about is a ``KeyError`` at
#: construction instead of an unchecked third case.
AUDIENCE_CLAIM: Final[dict[str, str]] = {"access": "client_id", "id": "aud"}

#: How much clock skew between CareLoop and the issuer is tolerated, in seconds.
#:
#: Applied to ``exp``, ``nbf`` and ``iat`` alike. Sixty seconds is the usual allowance for hosts
#: whose clocks are both NTP-disciplined; larger would extend the life of a revoked-by-expiry token
#: for no operational gain.
DEFAULT_LEEWAY_SECONDS: Final[int] = 60


@runtime_checkable
class SigningKeys(Protocol):
    """Where the public key for a token comes from."""

    def key_for(self, token: str) -> Any:
        """The verification key for this token's ``kid``.

        Raises:
            Exception: any failure to resolve one. The verifier translates it.
        """
        ...


@dataclass
class JwksSigningKeys:
    """Keys fetched from the issuer's JWKS endpoint, cached and refetched on an unknown ``kid``.

    ``PyJWKClient`` owns the caching and the rotation behaviour, which is the reason to use it
    rather than fetch the document here: a hand-rolled cache is a hand-rolled key-rotation bug.

    Constructed lazily, so importing this module -- or building an app in a test -- reaches no
    network. The first *verification* is what opens a connection.
    """

    jwks_uri: str
    cache_keys: bool = True
    lifespan_seconds: int = 300
    _client: Any = field(default=None, init=False, repr=False)

    def key_for(self, token: str) -> Any:
        if self._client is None:
            from jwt import PyJWKClient

            self._client = PyJWKClient(
                self.jwks_uri,
                cache_keys=self.cache_keys,
                lifespan=self.lifespan_seconds,
            )
        return self._client.get_signing_key_from_jwt(token).key


@dataclass(frozen=True)
class OidcVerifier:
    """Turns a bearer token into a principal, or refuses.

    Every field is required except the ones with a defensible default, and the defaults are the
    strict ones: one algorithm, access tokens, sixty seconds of leeway. There is deliberately no
    parameter that disables a check.
    """

    issuer: str
    audience: str
    directory: CaregiverDirectory
    keys: SigningKeys
    token_use: str = "access"
    algorithms: tuple[str, ...] = ALLOWED_ALGORITHMS
    leeway_seconds: int = DEFAULT_LEEWAY_SECONDS

    def __post_init__(self) -> None:
        if self.token_use not in AUDIENCE_CLAIM:
            raise ValueError(
                f"token_use {self.token_use!r} is not one this verifier knows how to check the "
                f"audience of; known: {sorted(AUDIENCE_CLAIM)}"
            )
        if not self.issuer or not self.audience:
            raise ValueError("an OIDC verifier needs both an issuer and an audience")

    @property
    def auth_method(self) -> AuthMethod:
        return AuthMethod.VERIFIED_OIDC

    def verify(self, credential: str | None) -> Principal:
        """Establish a principal from ``credential``, or raise.

        Raises:
            AuthenticationFailed: for every failure, undifferentiated to the caller.
        """
        if not credential:
            raise AuthenticationFailed("no token presented")

        claims = self._claims(credential)
        self._check_token_use(claims)
        self._check_audience(claims)
        return self._principal_for(claims)

    # -- the steps, named so a failure log can say which one ---------------------------------------

    def _claims(self, token: str) -> dict[str, Any]:
        """Signature, issuer, expiry, not-before and issued-at, all checked by the library."""
        import jwt

        try:
            key = self.keys.key_for(token)
        except Exception as unresolved:
            raise self._refused("the signing key could not be resolved", unresolved) from None

        try:
            return jwt.decode(
                token,
                key=key,
                algorithms=list(self.algorithms),
                issuer=self.issuer,
                leeway=self.leeway_seconds,
                options={
                    # Turned off because it cannot express the per-token-use claim name; checked
                    # explicitly in `_check_audience` instead. See the module docstring.
                    "verify_aud": False,
                    # Turned *on* explicitly rather than left to the library's defaults, so that a
                    # future release relaxing one of them cannot quietly relax CareLoop.
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_nbf": True,
                    "verify_iat": True,
                    "verify_iss": True,
                    "require": ["exp", "iat", "iss", "sub"],
                },
            )
        except Exception as refused:
            raise self._refused("the token did not verify", refused) from None

    def _check_token_use(self, claims: dict[str, Any]) -> None:
        """That this is the kind of token this deployment accepts.

        An ID token presented where an access token is expected is a different credential with a
        different lifetime and a different audience claim, and accepting either would make the
        audience check ambiguous.
        """
        presented = claims.get("token_use")
        if presented != self.token_use:
            raise self._refused(f"token_use was {presented!r}", None)

    def _check_audience(self, claims: dict[str, Any]) -> None:
        """That the token was issued for this application.

        Exact match against exactly one claim, chosen by token use. A list-valued ``aud`` is
        accepted only if the configured audience is a member -- which is what the specification
        allows and is still an exact match on the value.
        """
        claim = AUDIENCE_CLAIM[self.token_use]
        presented = claims.get(claim)
        allowed = presented if isinstance(presented, list) else [presented]
        if self.audience not in allowed:
            raise self._refused(f"{claim} did not match the configured audience", None)

    def _principal_for(self, claims: dict[str, Any]) -> Principal:
        """The subject, resolved through CareLoop's own directory and then dropped.

        The one place an identity provider's idea of who somebody is becomes CareLoop's. Nothing
        below this line has ever seen the subject, and nothing above it keeps one.
        """
        try:
            caregiver = self.directory.caregiver_for_subject(self.issuer, str(claims["sub"]))
        except (UnknownSubject, UnknownCaregiver) as unmapped:
            raise self._refused("the subject maps to no caregiver", unmapped) from None

        if not caregiver.active:
            raise self._refused("the caregiver record is not active", None)

        return Principal(caregiver_id=caregiver.caregiver_id, auth_method=AuthMethod.VERIFIED_OIDC)

    def _refused(self, because: str, cause: Exception | None) -> AuthenticationFailed:
        """One exception type for every refusal, with the detail logged rather than returned.

        ``cause`` is logged by **type** and never by message: a JWT library's message can quote
        claim values, and a claim value is somebody's identity.
        """
        logger.warning(
            "authentication refused: %s%s",
            because,
            f" ({type(cause).__name__})" if cause is not None else "",
        )
        return AuthenticationFailed(because)


def cognito_verifier(
    *,
    issuer: str,
    audience: str,
    directory: CaregiverDirectory,
    token_use: str = "access",
) -> PrincipalVerifier:
    """The production verifier, with the JWKS endpoint derived from the issuer.

    Cognito publishes its keys at ``<issuer>/.well-known/jwks.json``, which is the standard
    location rather than a Cognito-specific one -- so this composes for any OIDC provider that
    follows the discovery convention, and the name says which one it is configured for today.
    """
    return OidcVerifier(
        issuer=issuer.rstrip("/"),
        audience=audience,
        directory=directory,
        keys=JwksSigningKeys(jwks_uri=f"{issuer.rstrip('/')}/.well-known/jwks.json"),
        token_use=token_use,
    )


__all__ = [
    "ALLOWED_ALGORITHMS",
    "AUDIENCE_CLAIM",
    "DEFAULT_LEEWAY_SECONDS",
    "JwksSigningKeys",
    "OidcVerifier",
    "SigningKeys",
    "cognito_verifier",
]
