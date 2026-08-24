"""Where a request stops being anonymous.

**One boundary, two modes, and no way between them.** An :class:`AuthBoundary` holds the mode this
app runs in and the one verifier that establishes principals for it. The two are checked against
each other at construction: a production boundary must hold a verifier that establishes
``VERIFIED_OIDC`` principals and a demo boundary one that establishes ``SYNTHETIC_DEMO`` ones. So
*"a failed production verification falls back to a demo identity"* is not a branch somebody has to
remember not to write -- there is no second verifier in the object for it to fall back to.

**A production app cannot be constructed without a verifier.** :class:`AuthBoundary` gives
``verifier`` no default, so the failure is a ``TypeError`` at the call site rather than a runtime
check, and :func:`production_auth` is the only way to name the production mode. An app that reached
a request handler and *then* discovered it had nobody to verify with would already be an app
serving anonymous production traffic.

**Held beside the app's dependencies, not among them.** An isolated app deliberately holds no
app-scoped dependency that resolves to a world -- the only way to a store is through the session a
request presents, which is what makes cross-visitor access something no route can express rather
than something every query must remember to filter. The boundary is not one of those: it reaches no
store, holds no case and answers one question about the caller. It has to be app-scoped, because
authentication happens *before* a workspace is resolved -- a workspace whose owner is checked
against a principal cannot be the thing that produces the principal.

**Nothing below this line sees a credential.** :meth:`AuthBoundary.principal` takes the raw
``Authorization`` header and returns a :class:`~careloop.identity.principal.Principal` carrying a
``caregiver_id`` and how it was established. The token, its claims, its issuer and its subject stop
here; ``tests/test_identity_boundary.py`` is the sweep that keeps them from reappearing further in.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Final

from fastapi import Depends, Header, HTTPException, Request, status

from careloop.identity.directory import Caregiver, CaregiverDirectory, UnknownCaregiver
from careloop.identity.principal import (
    AuthenticationFailed,
    AuthMethod,
    Principal,
    PrincipalVerifier,
)

logger = logging.getLogger(__name__)

#: The header a production caller presents a bearer token in.
#:
#: Named once so the dependency, the OpenAPI declaration and the tests read one spelling, which is
#: the arrangement :data:`careloop.api.deps.CAREGIVER_HEADER` already has.
AUTHORIZATION_HEADER: Final[str] = "Authorization"

#: The scheme, matched case-insensitively as RFC 6750 requires.
BEARER_SCHEME: Final[str] = "bearer"

#: What a caller is told when authentication fails, whatever actually went wrong.
#:
#: One sentence for an absent header, a malformed token, a bad signature, the wrong issuer, the
#: wrong audience, the wrong token use, an expired token, an unknown key and a subject the
#: directory does not map. Invariant 6's reasoning applied to a different consumer: a caller who
#: can tell the failures apart by probing has been handed a map of the verifier. Which one it was
#: is logged, never returned, and the token itself is neither.
AUTHENTICATION_FAILED_DETAIL: Final[str] = (
    "a valid bearer token is required; present one as Authorization: Bearer <token>"
)


class AuthMode(StrEnum):
    """Which identity story this app tells.

    ``DEMO`` is a synthetic caregiver over a synthetic world holding no real records. ``PRODUCTION``
    is a cryptographically verified principal mapped to a caregiver CareLoop's own directory holds.
    There is no third mode and no mixture: an app is one or the other, decided at construction.
    """

    DEMO = "DEMO"
    PRODUCTION = "PRODUCTION"


class AuthModeMismatch(Exception):
    """Raised at construction when a boundary's mode and its verifier disagree.

    The structural half of *"no fallback"*. A ``PRODUCTION`` boundary holding
    :class:`~careloop.identity.demo.DemoPrincipals` would be an app that answers every request with
    a synthetic identity while claiming to be production, and it is refused here rather than
    detected later -- there is no later, because by the time a request arrives the answer would
    already have been given.
    """


#: Which principal each mode's verifier must establish.
#:
#: A mapping rather than an ``if``, so adding a mode without deciding what it verifies is a
#: ``KeyError`` at construction instead of an unchecked third case.
REQUIRED_METHOD: Final[dict[AuthMode, AuthMethod]] = {
    AuthMode.DEMO: AuthMethod.SYNTHETIC_DEMO,
    AuthMode.PRODUCTION: AuthMethod.VERIFIED_OIDC,
}


@dataclass(frozen=True)
class AuthBoundary:
    """The mode this app runs in, and the one thing that establishes principals for it.

    ``directory`` is CareLoop's server-side record of caregivers and grants. It is here rather than
    on :class:`~careloop.api.deps.ApiDeps` because it answers a question about the *caller*, which
    is settled before a workspace is resolved -- and because in an isolated app there is no
    app-scoped ``ApiDeps`` to put it on.

    Raises:
        AuthModeMismatch: if the verifier does not establish the mode's required auth method.
    """

    mode: AuthMode
    verifier: PrincipalVerifier
    directory: CaregiverDirectory

    def __post_init__(self) -> None:
        required = REQUIRED_METHOD[self.mode]
        if self.verifier.auth_method is not required:
            raise AuthModeMismatch(
                f"a {self.mode} boundary must hold a verifier that establishes {required}; this "
                f"one establishes {self.verifier.auth_method}. There is no fallback between the "
                f"two modes and this is where that is enforced"
            )

    @property
    def is_production(self) -> bool:
        """Whether this app authenticates rather than synthesises."""
        return self.mode is AuthMode.PRODUCTION

    def principal(self, credential: str | None) -> Principal:
        """Establish who is asking, or raise.

        ``credential`` is the raw ``Authorization`` header value, scheme and all. Parsing the
        scheme is done here rather than in a verifier because it is a fact about HTTP and a
        verifier's subject is a token -- the same separation that keeps HTTP inside
        ``_clinic_readback`` and out of everything above it.

        Raises:
            AuthenticationFailed: for every failure, undifferentiated to the caller.
        """
        if not self.is_production:
            return self.verifier.verify(None)

        token = _bearer_token(credential)
        if token is None:
            raise AuthenticationFailed("no bearer credential was presented")
        return self.verifier.verify(token)


def _bearer_token(credential: str | None) -> str | None:
    """The token out of an ``Authorization`` header, or ``None`` if there is not one.

    Scheme comparison is case-insensitive because RFC 6750 says so; the token itself is taken
    verbatim, because a token is not a value to normalise and trimming one is how a subtly
    different credential comes to be accepted.
    """
    if credential is None:
        return None
    scheme, _, rest = credential.strip().partition(" ")
    if scheme.lower() != BEARER_SCHEME or not rest.strip():
        return None
    return rest.strip()


def demo_auth(caregiver_id: str | None = None) -> AuthBoundary:
    """The judge-facing demo's boundary: one synthetic caregiver, verified by nothing.

    Composed from :mod:`careloop.graph.trusted`, which is where the demo caregiver's id, their
    destinations and their one grant live. Imported inside the function so importing this module
    does not drag the graph package in.
    """
    from careloop.graph.trusted import DEMO_CAREGIVER_ID, demo_directory
    from careloop.identity.demo import DemoPrincipals

    return AuthBoundary(
        mode=AuthMode.DEMO,
        verifier=DemoPrincipals(caregiver_id or DEMO_CAREGIVER_ID),
        directory=demo_directory(),
    )


def production_auth(*, verifier: PrincipalVerifier, directory: CaregiverDirectory) -> AuthBoundary:
    """A production boundary. **The only way to name that mode.**

    Both arguments are required and keyword-only, so a production app with nothing to verify with
    or nobody to resolve a subject against cannot be built at all. There is no parameter here that
    relaxes either, and adding one would recreate exactly the anonymous-production shape the mode
    exists to prevent.

    Raises:
        AuthModeMismatch: if ``verifier`` does not establish ``VERIFIED_OIDC`` principals.
    """
    return AuthBoundary(mode=AuthMode.PRODUCTION, verifier=verifier, directory=directory)


def boundary(request: Request) -> AuthBoundary:
    """This app's identity boundary.

    Held on ``app.state`` rather than among the dependencies -- see the module docstring for why
    that separation is deliberate rather than incidental.
    """
    held = request.app.state.careloop_auth
    if not isinstance(held, AuthBoundary):  # pragma: no cover - set by create_app, never a request
        raise RuntimeError("this app was built without an identity boundary")
    return held


Boundary = Annotated[AuthBoundary, Depends(boundary)]


def principal(
    bound: Boundary,
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    """Who is asking. **401 on any failure, and never a demo identity.**

    ``401`` rather than the ``400`` :func:`careloop.api.deps.caregiver` returns, and the difference
    is that something is now actually being checked. ``400`` was the honest answer while
    ``X-Caregiver`` was an unverified audit field -- returning ``401`` would have claimed a check
    that was not happening. Here the check happens, so ``401`` says what is true.
    """
    try:
        established = bound.principal(authorization)
    except AuthenticationFailed:
        raise _unauthenticated() from None

    # **The second enforcement point, and it is independent of the first.** The boundary already
    # refuses at construction to hold a verifier that establishes the wrong kind of principal, so
    # this cannot fire today -- which is exactly why it is here. It is the policy layer's
    # arrangement applied to identity: a hook and a decorator, either of which alone would do, so
    # that a future edit has to defeat both. What it refuses is the one shape this whole boundary
    # exists to make unreachable: a synthetic identity answering a production request.
    if established.auth_method is not REQUIRED_METHOD[bound.mode]:
        logger.error(
            "a %s boundary established a %s principal; refusing",
            bound.mode,
            established.auth_method,
        )
        raise _unauthenticated()
    return established


def _unauthenticated() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=AUTHENTICATION_FAILED_DETAIL,
        headers={"WWW-Authenticate": "Bearer"},
    )


Caller = Annotated[Principal, Depends(principal)]


def acting_caregiver(bound: Boundary, who: Caller) -> Caregiver:
    """The caregiver record this request acts as. **Resolved server-side, never sent.**

    The one place an authenticated subject becomes a domain identity with destinations and grants
    attached. A caller supplies a token; CareLoop supplies everything the token turns into.

    A principal naming a caregiver the directory does not hold is an authentication failure and not
    a 404: a token that verifies but resolves to nobody has established no principal, and telling
    the caller which of the two happened would let them enumerate the directory.
    """
    try:
        return bound.directory.caregiver(who.caregiver_id)
    except UnknownCaregiver:
        logger.warning("a verified principal named a caregiver this directory does not hold")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=AUTHENTICATION_FAILED_DETAIL,
            headers={"WWW-Authenticate": "Bearer"},
        ) from None


Acting = Annotated[Caregiver, Depends(acting_caregiver)]


__all__ = [
    "AUTHENTICATION_FAILED_DETAIL",
    "AUTHORIZATION_HEADER",
    "BEARER_SCHEME",
    "REQUIRED_METHOD",
    "Acting",
    "AuthBoundary",
    "AuthMode",
    "AuthModeMismatch",
    "Boundary",
    "Caller",
    "acting_caregiver",
    "boundary",
    "demo_auth",
    "principal",
    "production_auth",
]
