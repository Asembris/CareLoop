"""Who is asking, and how CareLoop came to believe it.

**Authentication is not provenance, and this package is on the other side of that line.** Invariant
3 says ``TRUSTED`` comes only from :mod:`careloop.graph.trusted` and that validation cannot
manufacture it; the same sentence has to hold for a verified token. A :class:`Principal` says *who
may ask CareLoop to act*. A :class:`~careloop.provenance.facts.Fact` says *why an argument may be
sent somewhere*. They are read by different code for different questions, exactly as
``args_approved`` is read separately from ``arg_trust_at_least``, and nothing here mints a fact,
raises a trust level or reaches the policy engine.

**The IdP subject stops here.** It is resolved once, against
:class:`~careloop.identity.directory.CaregiverDirectory`, into a ``caregiver_id`` CareLoop owns —
and then dropped. Nothing below this boundary sees an issuer, a subject, a claim set, an email or a
token, which is what makes swapping identity providers a rewrite of one mapping table rather than
of every persisted case. :mod:`careloop.identity.boundary` is the import-time sweep that keeps it
true.

**A verifier answers one question and has one failure.** :class:`PrincipalVerifier` turns whatever
the transport carried into a :class:`Principal` or raises :class:`AuthenticationFailed`. There is
deliberately no third outcome: an "unauthenticated principal" would be an object every caller has
to remember to check, and the check somebody forgets is the whole defect class this package exists
to close.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable


class AuthMethod(StrEnum):
    """How CareLoop came to believe a principal.

    Two members and there is no third. ``VERIFIED_OIDC`` means a signature, an issuer, an audience,
    a token use and a lifetime were all checked. ``SYNTHETIC_DEMO`` means none of that happened and
    the world is synthetic.

    **Carried so that "a demo principal reached a production route" is assertable rather than
    absent.** A production route refuses anything that is not ``VERIFIED_OIDC``, which is a positive
    check on a closed vocabulary rather than a negative check on a flag somebody might not set.
    """

    VERIFIED_OIDC = "VERIFIED_OIDC"
    SYNTHETIC_DEMO = "SYNTHETIC_DEMO"


@dataclass(frozen=True)
class Principal:
    """A caller CareLoop is willing to act for.

    Two fields, and every omission is deliberate. There is no subject, because it is resolved at
    the boundary and dropped. No email, display name or role, because none is authority-bearing and
    each is personal data with no consumer — the caregiver's *own* address is trusted
    configuration read from CareLoop's directory, never a token claim, or an identity provider
    would have become a second producer of ``TRUSTED``. No raw claims and no token, because
    anything carried is something a later edit can persist, log or send to a provider.
    """

    caregiver_id: str
    auth_method: AuthMethod

    @property
    def is_verified(self) -> bool:
        """Whether this principal was established cryptographically."""
        return self.auth_method is AuthMethod.VERIFIED_OIDC


class AuthenticationFailed(Exception):
    """The credential presented did not establish a principal.

    One type for every way of failing — absent, malformed, unsigned, wrongly signed, wrong issuer,
    wrong audience, wrong token use, expired, not yet valid, unknown key. The API renders it as a
    bare ``401`` with a fixed message for invariant 6's reason applied to a different consumer: a
    caller who can tell the failures apart by probing has been handed a map of the verifier.

    The distinguishing detail is logged, never returned, and never carries the token.
    """


@runtime_checkable
class PrincipalVerifier(Protocol):
    """Turns whatever the transport carried into a :class:`Principal`.

    The seam :class:`~careloop.graph.witness.Witness` and
    :data:`~careloop.api.deps.ContextFactory` already are: a protocol, one implementation chosen at
    the composition root, and an offline suite that never touches AWS.

    ``credential`` is the raw ``Authorization`` header value or ``None``. Implementations must not
    return anything for ``None`` unless they are the synthetic demo source, and must never fall
    back to one another.
    """

    @property
    def auth_method(self) -> AuthMethod:
        """Which method this verifier establishes. Fixed per implementation."""
        ...

    def verify(self, credential: str | None) -> Principal:
        """Establish a principal, or raise.

        Raises:
            AuthenticationFailed: for every failure, without distinguishing them to the caller.
        """
        ...


__all__ = [
    "AuthMethod",
    "AuthenticationFailed",
    "Principal",
    "PrincipalVerifier",
]
