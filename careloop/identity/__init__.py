"""Who may ask CareLoop to act, and for whom.

Four things and no more: a :class:`~careloop.identity.principal.Principal`, the
:class:`~careloop.identity.principal.PrincipalVerifier` that establishes one, the
:class:`~careloop.identity.directory.CaregiverDirectory` that says what a caregiver is and which
patients they may act for, and the two verifiers -- synthetic for the demo, OIDC for production.

**This package is one side of a line the rest of CareLoop must not cross.** Caller authorisation
answers *who may request an action for this patient*. Evidence provenance answers *why a tool
argument may be sent somewhere*. Nothing here mints a :class:`~careloop.provenance.facts.Fact`,
raises a trust level, appears in ``rules.yaml`` or reaches the policy engine, and
:mod:`careloop.identity.boundary` is the import-time check that keeps it so.

The OIDC verifier is deliberately **not** re-exported: it is the only thing here that needs a
third-party JWT library, and importing this package must not drag one in. Reach it by module name,
which is the arrangement ``careloop.policy``'s three loader steps already use.
"""

from careloop.identity.demo import DemoPrincipals
from careloop.identity.directory import (
    Caregiver,
    CaregiverDirectory,
    Grant,
    InMemoryCaregiverDirectory,
    SubjectMapping,
    UnknownCaregiver,
    UnknownSubject,
)
from careloop.identity.principal import (
    AuthenticationFailed,
    AuthMethod,
    Principal,
    PrincipalVerifier,
)

__all__ = [
    "AuthMethod",
    "AuthenticationFailed",
    "Caregiver",
    "CaregiverDirectory",
    "DemoPrincipals",
    "Grant",
    "InMemoryCaregiverDirectory",
    "Principal",
    "PrincipalVerifier",
    "SubjectMapping",
    "UnknownCaregiver",
    "UnknownSubject",
]
