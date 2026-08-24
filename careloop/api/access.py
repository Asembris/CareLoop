"""Which cases this caller may see and move.

**One predicate, several readers.** Every route that reads or mutates a case goes through
:meth:`CaseAccess.case`, and the dashboard goes through :meth:`CaseAccess.sections`. There is
deliberately not an ownership check per route: a check per route is a check somebody forgets, and
the class of defect this closes -- knowing a case id being sufficient to read it -- is exactly what
a forgotten one reintroduces.

**A case the caller does not own is a 404, not a 403.** The two answers differ in what they
disclose: ``403`` says *that case exists and is not yours*, which is an oracle a caller can sweep an
id space with. The message is byte-identical to the one a genuinely missing case gets, so the two
are indistinguishable from outside.

**An unowned case is refused in production.** ``Case.caregiver_id`` is optional so that a case
persisted before ownership existed stays *readable* -- reading is total on purpose. But a case
nobody owns is a case nobody may act on, so production treats ``None`` exactly as it treats somebody
else's. Demo mode does not check at all, because a demo world is isolated rather than authorised and
its one synthetic caregiver owns everything in it.

**Nothing here decides what may be done to a case.** That is still the policy engine's question,
unchanged and untouched. This decides only whether the caller may reach the case at all, which is a
question the policy engine has never asked and must not start asking: authorisation of a *caller*
and provenance of an *argument* are separate axes, and ``rules.yaml`` reads neither a principal nor
an owner.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Final

from fastapi import Depends, Header, HTTPException, status

from careloop.api.auth import Acting, AuthBoundary, Boundary, Caller
from careloop.api.deps import CAREGIVER_HEADER, ApiDeps, Deps, caregiver, sections_from
from careloop.graph.context import established_patients
from careloop.identity.directory import Caregiver, CaregiverDirectory
from careloop.identity.principal import Principal
from careloop.state.case import Case, CaseState
from careloop.state.store import CaseNotFound, CaseStore

#: What a caller is told when a grant no longer covers the work they are asking for.
#:
#: One sentence for "you hold no grants at all" and for "you may not act for this case's patient",
#: because both mean the same thing to the person reading it and telling them apart would let a
#: caller map the directory by probing.
NOT_AUTHORISED_FOR_THIS_PATIENT: Final[str] = (
    "you are not currently authorised to act for this patient; ask whoever administers CareLoop "
    "to review your access"
)


def _no_such_case(case_id: str) -> HTTPException:
    """The one answer for a case that is missing and for a case that is not the caller's.

    Byte-identical on purpose. See the module docstring.
    """
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"no case {case_id!r}")


@dataclass(frozen=True)
class CaseAccess:
    """This request's dependencies, together with who is asking and what they may reach."""

    deps: ApiDeps
    boundary: AuthBoundary
    principal: Principal
    caregiver: Caregiver

    @property
    def store(self) -> CaseStore:
        return self.deps.store

    @property
    def enforced(self) -> bool:
        """Whether ownership is checked at all. Production only -- see the module docstring."""
        return self.boundary.is_production

    def owns(self, case: Case) -> bool:
        """Whether this caller owns ``case``.

        ``None`` is not a match for anybody, which is what makes an unowned legacy case inert in
        production rather than reachable by everybody.
        """
        return case.caregiver_id is not None and case.caregiver_id == self.principal.caregiver_id

    def case(self, case_id: str) -> Case:
        """The case, if this caller may have it.

        Raises:
            HTTPException: 404, identically, whether the case is missing or belongs to somebody
                else.
        """
        try:
            case = self.store.get(case_id)
        except CaseNotFound:
            raise _no_such_case(case_id) from None

        if self.enforced and not self.owns(case):
            raise _no_such_case(case_id)
        return case

    def may_open(self, case_id: str) -> None:
        """Refuse a delivery that would land on a case this caller does not own.

        A delivery either opens a case or reopens one. Opening a case that does not exist is always
        allowed -- there is no owner yet, and this caller becomes it. Reopening one that exists is
        reaching an existing case, so it goes through the same predicate everything else does.

        Raises:
            HTTPException: 404 if the case exists and is not this caller's.
        """
        try:
            existing = self.store.get(case_id)
        except CaseNotFound:
            return
        if self.enforced and not self.owns(existing):
            raise _no_such_case(case_id)

    def may_open_a_case(self) -> None:
        """Refuse a caller who may act for nobody at all.

        The check available *at the moment a case is opened*, which is before any document has been
        read: there is no patient yet, so there is nothing patient-specific to ask. What can be
        asked is whether this caregiver is still active and still holds a grant for somebody, and a
        caregiver who holds none has no business opening work.

        The patient-specific question is :meth:`may_act_on`, asked once the case knows who it is
        about.

        Raises:
            HTTPException: 403 if this caller may act for nobody.
        """
        if not self.enforced:
            return
        if not self.directory.may_act_for_anybody(self.principal.caregiver_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail=NOT_AUTHORISED_FOR_THIS_PATIENT
            )

    def may_act_on(self, case: Case) -> None:
        """Refuse a caller who no longer holds a grant for this case's patient.

        Asked immediately before anything a caller drives that could reach a **new** external
        effect, and asked against the directory rather than against anything cached: a cached
        answer is a revocation that has not happened yet.

        ``403`` rather than ``404``. The caller owns this case -- ownership was checked one step
        earlier -- so its existence is not a secret, and a caregiver whose access was withdrawn is
        better told than left guessing.

        **This is not on the recovery path and must never be.** Reconciling an action that may
        already have committed is establishing what a provider did, not issuing new authority: an
        effect that is already out there does not become un-issued because a grant was withdrawn,
        and refusing to look would leave a real appointment unknowable. ``ApiDeps`` holds no
        directory at all, which is what makes that structural rather than remembered.

        Raises:
            HTTPException: 403 if any patient this case has established is one this caller may not
                currently act for.
        """
        if not self.enforced:
            return
        patients = established_patients(case)
        if not patients:
            # Nothing has been established yet, so there is nobody to be authorised for. An effect
            # still cannot name a patient this case has not established -- that is
            # `deny-booking-for-unverified-patient`, one layer down and unchanged.
            return
        if not all(
            self.directory.may_act_for(self.principal.caregiver_id, patient) for patient in patients
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail=NOT_AUTHORISED_FOR_THIS_PATIENT
            )

    @property
    def directory(self) -> CaregiverDirectory:
        return self.boundary.directory

    def sections(self, sections: dict[str, frozenset[CaseState]]) -> dict[str, list[Case]]:
        """The dashboard, holding only this caller's cases.

        A filter, and the only one in the system -- which is worth saying plainly, because this
        repository argues elsewhere that isolation by separate world beats isolation by remembering
        to filter. Both are true: an isolated demo world has nothing to filter, and a single durable
        production store holding several caregivers' cases has no world boundary to lean on. So the
        filter lives *here*, in the same object every other read goes through, rather than being
        spelled again in the route.
        """
        loaded = sections_from(self.store, sections)
        if not self.enforced:
            return loaded
        return {name: [case for case in cases if self.owns(case)] for name, cases in loaded.items()}


def decision_actor(
    bound: Boundary,
    who: Caller,
    x_caregiver: Annotated[str | None, Header()] = None,
) -> str:
    """Who a decision is recorded against. **Verified in production, typed in demo.**

    The audit trail's answer to *"who said yes"* used to be a claim by whoever said yes: any HTTP
    caller could put any string in ``X-Caregiver`` and it was written to ``Approval.decided_by`` and
    to ``TimelineEvent.actor`` unchecked. Authority never moved -- no rule ever read it -- so this
    was audit integrity rather than an authorisation bypass, in the one direction an append-only
    trail must not move.

    **Production refuses the header rather than ignoring it**, and that is the whole of the
    difference between a header with no authority and a header that is not there. Ignoring it would
    leave two spellings of caregiver identity in one request, and the next person to read the code
    has to work out which one won; refusing it means a client that sends one is told, once, that it
    means nothing here. ``400`` rather than ``401``: the caller authenticated perfectly well and
    sent a field that does not exist on this surface.

    Demo mode keeps the header, because the judge flow has no verified principal to draw a name from
    and a synthetic world's audit trail should still say who pressed the button. It is a name a
    caller typed, exactly as it always was, and the demo says so.
    """
    if not bound.is_production:
        return caregiver(x_caregiver)

    if x_caregiver is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"the {CAREGIVER_HEADER} header carries no authority here and is refused rather "
                f"than ignored: decisions are recorded against the verified caregiver this request "
                f"authenticated as. Remove the header."
            ),
        )
    return who.caregiver_id


Actor = Annotated[str, Depends(decision_actor)]


def case_access(
    deps: Deps, bound: Boundary, who: Caller, acting: Acting
) -> CaseAccess:  # pragma: no cover - exercised through every route
    return CaseAccess(deps=deps, boundary=bound, principal=who, caregiver=acting)


Access = Annotated[CaseAccess, Depends(case_access)]


__all__ = [
    "NOT_AUTHORISED_FOR_THIS_PATIENT",
    "Access",
    "Actor",
    "CaseAccess",
    "case_access",
    "decision_actor",
]
