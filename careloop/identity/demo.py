"""The synthetic principal the judge-facing demo runs on.

**One writer of** :attr:`~careloop.identity.principal.AuthMethod.SYNTHETIC_DEMO`, and
``tests/test_principal_seam.py`` fails if a second appears. That sweep is the arrangement
``CaseStore.clear`` and ``arm_the_lost_booking_response`` already have, and it is what makes
*"a failed production verification cannot resolve to a demo identity"* a property of the package
rather than of one ``if`` somebody remembered to write: production and demo choose a verifier at
the composition root, the two are mutually exclusive there, and there is exactly one place in the
codebase where a synthetic principal can come into existence.

**It verifies nothing and says so in its name.** No credential is read, none is required, and the
world it authorises is a synthetic one holding fixture documents and no real records. That is the
same statement ``X-CareLoop-Session`` makes about isolation: what it buys is that visitors cannot
collide, never that they cannot impersonate.
"""

from __future__ import annotations

from dataclasses import dataclass

from careloop.identity.principal import AuthMethod, Principal


@dataclass(frozen=True)
class DemoPrincipals:
    """Mints one synthetic caregiver for every caller. The demo's verifier seam.

    ``caregiver_id`` is injected rather than reached for, so this module depends on nothing and
    the demo's identity is composed where the rest of the demo is. It is the id of a real record in
    the directory the demo world is built with, which is what lets the demo exercise the same
    ownership and grant code production does instead of a bypass around it.
    """

    caregiver_id: str

    @property
    def auth_method(self) -> AuthMethod:
        return AuthMethod.SYNTHETIC_DEMO

    def verify(self, credential: str | None = None) -> Principal:
        """The synthetic principal. ``credential`` is ignored, deliberately and visibly.

        Never raises. A demo that could fail to establish an identity would be a demo with a login
        screen in front of it, which is the one thing this mode exists to avoid.
        """
        return Principal(caregiver_id=self.caregiver_id, auth_method=AuthMethod.SYNTHETIC_DEMO)


__all__ = ["DemoPrincipals"]
