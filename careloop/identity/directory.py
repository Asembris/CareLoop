"""CareLoop's own record of who a caregiver is and which patients they may act for.

**Server-side, and that is the whole point.** Every value here is CareLoop's, not an identity
provider's: the mapping from an IdP subject to a ``caregiver_id``, the caregiver's own trusted
destinations, and the grants. A token establishes *which record to look up*; it never supplies the
contents of one. Without that split an identity provider -- or anybody who could get a claim into
one -- would be a second producer of ``TRUSTED``, and invariant 3 says there is exactly one.

**The smallest model that answers the question.** A caregiver, a grant, and a subject mapping.
There are deliberately no roles, groups, teams, organisations, scopes or hierarchy, and no policy
language over any of them: the only question production asks is *may this caregiver currently act
for this patient*, and a boolean relation answers it. Anything richer would be a second authority
system beside ``rules.yaml``, which is the shape this repository has spent several sessions
removing rather than adding.

**Grants are checked, never carried.** :meth:`CaregiverDirectory.may_act_for` is asked at the two
moments that matter -- when a case is opened, and before a caller-driven run that could issue a
*new* external effect -- and its answer is not cached on the case, in the run context or in a
token. A cached answer is a revocation that has not happened yet.

**This module holds no values.** The demo caregiver, its destinations and its grants are composed
in :mod:`careloop.graph.trusted`, which is the one file invariant 3 names as the source of trusted
configuration. Types here, literals there, so a second file cannot quietly become a second source.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


class UnknownCaregiver(Exception):
    """No caregiver record under that id.

    Raised rather than answered with ``None`` because every caller of
    :meth:`CaregiverDirectory.caregiver` is about to make an authority decision, and a ``None`` one
    of them forgets to check is the defect. The API renders it as an authentication failure rather
    than a 404: a token that verifies but names nobody has established no principal.
    """


class UnknownSubject(Exception):
    """That issuer and subject map to no caregiver here.

    A verified token for somebody CareLoop has never heard of. Distinct from
    :class:`UnknownCaregiver` so a *server log* can say which happened; both surface to a caller as
    the same opaque authentication failure.
    """


@dataclass(frozen=True)
class Caregiver:
    """One caregiver, and the destinations CareLoop may reach on their behalf.

    ``destinations`` is ``(fact key, value)`` pairs in exactly the shape
    :func:`careloop.graph.trusted.trusted_facts` seeds onto a case -- the caregiver's own email
    address, the provider they may book with, the portal they may upload to. A tuple rather than a
    mapping so the freeze is real rather than shallow, which is the reason
    :class:`~careloop.state.case.Case` uses tuples too.

    ``active`` is the revocation switch for the caregiver as a whole. A caregiver that is not
    active may act for nobody, whatever their grants say, so withdrawing one is a single write
    rather than a sweep over a relation.
    """

    caregiver_id: str
    destinations: tuple[tuple[str, str], ...] = ()
    active: bool = True


@dataclass(frozen=True)
class Grant:
    """That this caregiver may act for this patient.

    The whole authorisation model. ``active`` rather than a deletion so revoking is a state change
    the record survives -- an audit trail that loses the fact a grant ever existed cannot explain a
    case that was legitimately opened before it was withdrawn.
    """

    caregiver_id: str
    patient_id: str
    active: bool = True


@dataclass(frozen=True)
class SubjectMapping:
    """That this identity provider's subject is this caregiver.

    Keyed on ``(issuer, subject)`` and not on subject alone: a subject is unique within an issuer
    and nowhere else, so a directory serving two issuers that matched on the second half would let
    one of them name the other's people.
    """

    issuer: str
    subject: str
    caregiver_id: str


@runtime_checkable
class CaregiverDirectory(Protocol):
    """CareLoop's server-side answer to who a caller is and what they may do."""

    def caregiver(self, caregiver_id: str) -> Caregiver:
        """The caregiver record, or raise.

        Raises:
            UnknownCaregiver: if there is no such record.
        """
        ...

    def caregiver_for_subject(self, issuer: str, subject: str) -> Caregiver:
        """The caregiver an IdP subject resolves to, or raise.

        Raises:
            UnknownSubject: if that issuer and subject map to nobody.
            UnknownCaregiver: if the mapping names a caregiver that does not exist.
        """
        ...

    def may_act_for(self, caregiver_id: str, patient_id: str) -> bool:
        """Whether an active grant currently lets this caregiver act for this patient.

        Total and fail-closed: an unknown caregiver, an inactive one, a missing grant and an
        inactive grant are all ``False``. No exception, because every caller is asking one yes/no
        authority question and the safe answer to all four is the same one.
        """
        ...


@dataclass
class InMemoryCaregiverDirectory:
    """A directory held in the process. The demo's, and every test's.

    Revocation goes through :meth:`revoke_grant` and :meth:`deactivate` rather than through callers
    replacing records, so a test constructs a revocation the same way a real one would happen.

    A durable directory replaces this the way ``SqliteCaseStore`` replaces ``InMemoryCaseStore``:
    same protocol, no caller changes. Nothing here is a business fact the way a ``Case`` is, so it
    is deliberately not in the :class:`~careloop.state.store.CaseStore` -- invariant 8 is about
    state, evidence, approvals and action history, and a directory is none of those.
    """

    caregivers: dict[str, Caregiver] = field(default_factory=dict)
    grants: tuple[Grant, ...] = ()
    subjects: tuple[SubjectMapping, ...] = ()

    def caregiver(self, caregiver_id: str) -> Caregiver:
        found = self.caregivers.get(caregiver_id)
        if found is None:
            raise UnknownCaregiver(f"no caregiver record under {caregiver_id!r}")
        return found

    def caregiver_for_subject(self, issuer: str, subject: str) -> Caregiver:
        for mapping in self.subjects:
            if mapping.issuer == issuer and mapping.subject == subject:
                return self.caregiver(mapping.caregiver_id)
        # The subject is deliberately not interpolated: this message reaches a server log, and an
        # IdP subject is personal data with no business being in one.
        raise UnknownSubject(f"issuer {issuer!r} presented a subject this directory does not map")

    def may_act_for(self, caregiver_id: str, patient_id: str) -> bool:
        holder = self.caregivers.get(caregiver_id)
        if holder is None or not holder.active:
            return False
        return any(
            grant.active and grant.caregiver_id == caregiver_id and grant.patient_id == patient_id
            for grant in self.grants
        )

    def revoke_grant(self, caregiver_id: str, patient_id: str) -> None:
        """Withdraw one grant, keeping the record. See :class:`Grant`."""
        self.grants = tuple(
            Grant(grant.caregiver_id, grant.patient_id, active=False)
            if grant.caregiver_id == caregiver_id and grant.patient_id == patient_id
            else grant
            for grant in self.grants
        )

    def deactivate(self, caregiver_id: str) -> None:
        """Withdraw a caregiver entirely. Every grant they hold stops answering."""
        held = self.caregivers.get(caregiver_id)
        if held is not None:
            self.caregivers[caregiver_id] = Caregiver(
                caregiver_id=held.caregiver_id, destinations=held.destinations, active=False
            )


__all__ = [
    "Caregiver",
    "CaregiverDirectory",
    "Grant",
    "InMemoryCaregiverDirectory",
    "SubjectMapping",
    "UnknownCaregiver",
    "UnknownSubject",
]
