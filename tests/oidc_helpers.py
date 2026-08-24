"""A deterministic local OIDC issuer, so token verification is tested offline at $0.

An RSA key pair generated in-process, tokens signed with it, and a
:class:`~careloop.identity.oidc.SigningKeys` that hands the public half back. **Only the key
transport is local**: the tokens are real RS256 JWTs and the thing under test is
:meth:`careloop.identity.oidc.OidcVerifier.verify` -- the same method production runs, with the same
algorithm allowlist, the same claim requirements and the same audience rule. Nothing here weakens a
check; swapping ``PyJWKClient`` for a dictionary is the whole of the difference.

No AWS, no network, no credentials, no clock the test does not control.

Key generation is the slow part (~100 ms), so one module-level pair is shared. A test that needs a
*different* key -- to prove a token signed by a stranger is refused -- calls :func:`another_issuer`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

#: Read from :mod:`identity_helpers` so the two fixtures cannot drift: a verifier configured for one
#: issuer over a directory that maps another would refuse every token for the wrong reason, and the
#: test would still be green about something else.
from identity_helpers import ISSUER

AUDIENCE = "client-abc123"
SUBJECT_A = "sub-alma"
SUBJECT_B = "sub-bo"


def _generate() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


#: One pair for the whole module. Deterministic in the sense that matters -- every test gets the
#: same key -- without pinning a private key into the repository.
PRIVATE_KEY = _generate()
PUBLIC_KEY = PRIVATE_KEY.public_key()


@dataclass(frozen=True)
class StaticSigningKeys:
    """Hands back one public key, whatever the token's ``kid`` says.

    Deliberately ignores ``kid``: what these tests exercise is the claim policy, and a key-selection
    stub that also modelled rotation would be a second implementation of ``PyJWKClient`` for the
    tests to agree with.
    """

    key: Any

    def key_for(self, token: str) -> Any:
        return self.key


@dataclass(frozen=True)
class UnavailableSigningKeys:
    """Cannot resolve a key at all, the way an unreachable or ``kid``-less JWKS behaves."""

    def key_for(self, token: str) -> Any:
        raise LookupError("no signing key for that kid")


def another_issuer() -> tuple[rsa.RSAPrivateKey, Any]:
    """A second key pair, for proving a token signed by somebody else is refused."""
    private = _generate()
    return private, private.public_key()


def token(
    *,
    private_key: Any = None,
    issuer: str = ISSUER,
    audience: str = AUDIENCE,
    subject: str = SUBJECT_A,
    token_use: str | None = "access",
    algorithm: str = "RS256",
    audience_claim: str = "client_id",
    expires_in: int = 3600,
    not_before: int | None = None,
    issued_at: int | None = None,
    now: int | None = None,
    omit: tuple[str, ...] = (),
    extra: dict[str, Any] | None = None,
) -> str:
    """Mint one signed token. Every parameter exists so a test can make exactly one thing wrong."""
    at = now if now is not None else int(time.time())
    claims: dict[str, Any] = {
        "iss": issuer,
        "sub": subject,
        "iat": issued_at if issued_at is not None else at,
        "exp": at + expires_in,
    }
    if token_use is not None:
        claims["token_use"] = token_use
    if audience is not None:
        claims[audience_claim] = audience
    if not_before is not None:
        claims["nbf"] = not_before
    claims.update(extra or {})
    for name in omit:
        claims.pop(name, None)

    return jwt.encode(claims, private_key or PRIVATE_KEY, algorithm=algorithm)


def unsigned_token(**kwargs: Any) -> str:
    """A token with ``alg: none`` and no signature. The classic confusion, in one call."""
    at = int(time.time())
    claims = {
        "iss": ISSUER,
        "sub": SUBJECT_A,
        "iat": at,
        "exp": at + 3600,
        "token_use": "access",
        "client_id": AUDIENCE,
        **kwargs,
    }
    return jwt.encode(claims, key=None, algorithm="none")


def handmade_hs256_token(secret: bytes, **kwargs: Any) -> str:
    """An HS256 token assembled by hand, because PyJWT refuses to *encode* one with a PEM key.

    That refusal is the library protecting the signing side. What has to be proven here is the
    *verification* side: a token whose header says HS256 and whose signature is an HMAC over the
    RSA public key must be refused by ``jwt.decode`` because RS256 is the only algorithm offered,
    not because the attacker could not build it.
    """
    import base64
    import hashlib
    import hmac
    import json

    at = int(time.time())
    claims = {
        "iss": ISSUER,
        "sub": SUBJECT_A,
        "iat": at,
        "exp": at + 3600,
        "token_use": "access",
        "client_id": AUDIENCE,
        **kwargs,
    }

    def segment(payload: dict[str, Any]) -> bytes:
        return base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=")

    signing_input = segment({"alg": "HS256", "typ": "JWT"}) + b"." + segment(claims)
    signature = hmac.new(secret, signing_input, hashlib.sha256).digest()
    return (signing_input + b"." + base64.urlsafe_b64encode(signature).rstrip(b"=")).decode()


def symmetric_token(secret: str, **kwargs: Any) -> str:
    """An HS256 token, for the RS/HS confusion: signed with a string a caller could guess."""
    at = int(time.time())
    claims = {
        "iss": ISSUER,
        "sub": SUBJECT_A,
        "iat": at,
        "exp": at + 3600,
        "token_use": "access",
        "client_id": AUDIENCE,
        **kwargs,
    }
    return jwt.encode(claims, secret, algorithm="HS256")


def verifier(directory=None, **overrides: Any):
    """The production verifier, over the local key."""
    from identity_helpers import two_caregiver_directory

    from careloop.identity.oidc import OidcVerifier

    return OidcVerifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        directory=directory if directory is not None else two_caregiver_directory(),
        keys=StaticSigningKeys(PUBLIC_KEY),
        **overrides,
    )
