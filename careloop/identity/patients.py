"""Binding the graph's patient boundary to a caregiver directory.

The one place the two vocabularies meet. :mod:`careloop.graph.authority` asks *may these patients be
acted for* and knows nothing else; :mod:`careloop.identity.directory` answers *may this caregiver
act for this patient*. This closes over the second and satisfies the first, so the graph never
learns that a caregiver exists and the directory never learns that a graph does.

**The grant is read on every call.** Nothing about authorisation is captured, cached or
precomputed: a grant withdrawn between a run starting and its execute node reaching dispatch must
take effect, and a set of authorised patients computed at run start would also have been computed
before the case had established a patient at all -- which is the first-run defect this closes.

**The caregiver *is* captured, and that is a different thing.** ``Case.caregiver_id`` is frozen when
the case is opened and never rewritten, so holding it is holding a constant rather than a stale
answer. Taking it from the case rather than from the current request is also deliberate: a caller
who could say which caregiver a run is authorised as could grant themselves somebody else's case.

**An unowned case is refused.** ``caregiver_id`` is ``None`` for a case persisted before ownership
existed, and a case nobody owns is a case nobody may act for -- the same reading
:meth:`careloop.api.access.CaseAccess.owns` gives it, arrived at independently so neither layer's
answer depends on the other having been consulted.

**An empty patient set asks a different question, and gets one.** It means the case has established
nobody -- no document named a patient, which is the ordinary shape of an ``upload_document`` case --
so there is no *patient* boundary to enforce and this is not the boundary that should refuse. What
is asked instead is the one that still means something: does this caregiver currently hold any
active grant at all? A caregiver deactivated mid-run is caught by it; a legitimate action on a case
whose documents named nobody is not.

Refusing the empty set was the first shape of this and it was wrong in a way worth recording:
measured, it turned an ``upload_document`` first attempt into a park, because that tool names no
patient and its case establishes none. A patient boundary that refuses an action *about no patient*
is not being strict, it is answering a question it was not asked -- and the hole this closes is
specifically a case establishing a patient the caregiver is **not** granted, which is never the
empty set.
"""

from __future__ import annotations

from dataclasses import dataclass

from careloop.identity.directory import CaregiverDirectory


@dataclass(frozen=True)
class CaregiverPatientAuthority:
    """A :class:`~careloop.graph.authority.PatientAuthority` over one caregiver's grants."""

    directory: CaregiverDirectory
    caregiver_id: str | None

    def allows(self, patient_ids: frozenset[str]) -> bool:
        if self.caregiver_id is None:
            return False
        if not patient_ids:
            return self.directory.may_act_for_anybody(self.caregiver_id)
        return all(
            self.directory.may_act_for(self.caregiver_id, patient) for patient in patient_ids
        )


__all__ = ["CaregiverPatientAuthority"]
