"""Whether the people this case is about may be acted for, asked at the moment of dispatch.

**A patient boundary, expressed without an identity.** The graph must not learn what a caregiver
is, what a directory is or what a token is -- ``tests/test_identity_boundary.py`` sweeps for it --
so this protocol takes patient identifiers and answers yes or no. Whoever binds it may close over a
caregiver id and a live directory; nothing below this line can see that it did.

**Asked live, never snapshotted.** :meth:`PatientAuthority.allows` is called at the execute node,
after evidence has established who the case is about, and its answer is not cached anywhere. A set
of authorised patients computed when the run started would be a revocation that had not happened
yet -- and the whole first-run defect this closes is precisely a decision taken before the patient
was known.

**Fail closed by construction: there is no allow-all.** The only implementation here is
:data:`AUTHORISES_NOBODY`, and a run that is bound to nothing therefore dispatches nothing. That is
deliberately inconvenient: an "allow everything" default is a default that ends up in production,
and the alternative -- a required constructor argument -- is the same discipline
``open_case(caregiver=...)`` already carries. ``tests/test_patient_authority.py`` sweeps the package
to keep a permissive implementation from appearing.

**Not the recovery path.** Reconciling an action that may already have committed is settling an
already-issued effect, not issuing a new one, so ``ReconcileNode`` does not ask this question and
structurally cannot -- an appointment that may exist does not stop existing because a grant was
withdrawn, and refusing to look would leave a real one permanently unknowable.

**Not the policy engine, either.** ``rules.yaml`` answers *may this action happen, with these
arguments, on this case*; this answers *may this case's people be acted for at all*. Two axes, two
readers, and neither learns the other's vocabulary.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class PatientAuthority(Protocol):
    """Whether a run may cause an external effect for the people a case is about."""

    def allows(self, patient_ids: frozenset[str]) -> bool:
        """Whether **every** identifier in ``patient_ids`` may currently be acted for.

        All of them, not any: a case that has established two patients and is authorised for one of
        them is a case whose effects would land partly outside the caller's reach, and there is no
        argument-level plumbing here to tell which patient a given effect is about.

        An **empty** set is the interesting case, and implementations must decide it deliberately.
        It means the case has established nobody -- no document has been read yet, or none named a
        patient, which is the ordinary shape of an ``upload_document`` case. There is then no
        *patient* boundary to enforce, and an implementation that refused it would be answering a
        question it was not asked; what stops an effect naming somebody is the policy engine, which
        requires a ``CONTROLLED`` ``patient_id`` for a booking and refuses one the case never
        established. :class:`~careloop.identity.patients.CaregiverPatientAuthority` answers it with
        the question that does still mean something -- may this caregiver act for anybody at all --
        and :data:`AUTHORISES_NOBODY` refuses it along with everything else, because "no authority"
        and "no patients" are different states.

        Called live at the point of dispatch. Implementations must read current state rather than a
        value captured earlier -- see the module docstring.
        """
        ...


class _AuthorisesNobody:
    """The fail-closed authority. Every question answers no, including the empty one.

    What a run gets when nothing bound one. A run holding this can prepare and plan, and can
    dispatch nothing at all -- which is the correct behaviour for a run whose caller was never
    established, and is why there is no allow-all sibling for somebody to reach for instead.
    """

    def allows(self, patient_ids: frozenset[str]) -> bool:
        return False

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return "AUTHORISES_NOBODY"


#: The fail-closed authority. A module-level singleton, so identity comparison is meaningful and a
#: test can assert *which* authority a run holds rather than only what it answers.
AUTHORISES_NOBODY: PatientAuthority = _AuthorisesNobody()


__all__ = ["AUTHORISES_NOBODY", "PatientAuthority"]
