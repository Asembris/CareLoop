"""The vocabularies CareLoopBench reports in, and nothing that decides anything.

Three closed enumerations and one formatting constant. Every one of them is a *label* — a way of
saying which question a piece of evidence answers and what happened when it was asked. None of
them is a judgement about CareLoop, and none may grow a meaning that requires reading CareLoop's
source to understand.

**Execution levels are classifications, not a ranking.** A property proof and a live-provider proof
answer different questions: the first says *this cannot happen for any input*, the second says
*this was done, once, against a system nobody here controls*. Neither dominates the other, and a
report that sorted them into better and worse would be inviting a reader to discount the half that
is actually harder to obtain.

**Statuses are six and are deliberately not five.** The temptation is to fold ``NOT_APPLICABLE``
into ``NOT_RUN``, or ``STALE`` into ``FAILED``. Both folds destroy a distinction a reader needs:
nothing failed when a live proof goes stale — it stopped being about this code — and a claim that
declares itself inapplicable is not a claim nobody got round to.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class Domain(StrEnum):
    """The seven scored property groups.

    Deployment and operational facts are deliberately absent. They are *posture* rather than
    properties, and putting them in a numerator would let a redeploy raise a safety count.
    """

    AUTHORITY_INTEGRITY = "AUTHORITY_INTEGRITY"
    EXTERNAL_EFFECT_SAFETY = "EXTERNAL_EFFECT_SAFETY"
    SETTLEMENT_INTEGRITY = "SETTLEMENT_INTEGRITY"
    IDENTITY_AND_ISOLATION = "IDENTITY_AND_ISOLATION"
    ADVERSARIAL_ROBUSTNESS = "ADVERSARIAL_ROBUSTNESS"
    RECOVERY_AND_LIVENESS = "RECOVERY_AND_LIVENESS"
    BOUNDARY_FIDELITY = "BOUNDARY_FIDELITY"


class ExecutionLevel(StrEnum):
    """Where a claim's evidence runs. A classification, never a quality ordering."""

    #: Deterministic, in-process, no socket, no clock, no subprocess.
    PROPERTY = "PROPERTY"
    #: The FastAPI app over ASGI, or SQLite, or the store — still no socket.
    LOCAL_INTEGRATION = "LOCAL_INTEGRATION"
    #: A loopback socket, a real child process, or a real provider substitute over HTTP.
    REAL_PROTOCOL = "REAL_PROTOCOL"
    #: The adversarial corpus replayed through the real graph and the real rules.
    EVALUATION = "EVALUATION"
    #: Measured once against a real third-party provider. Ingested, never re-run.
    LIVE_PROVIDER = "LIVE_PROVIDER"
    #: Measured once against the deployed build. Ingested, never re-run.
    LIVE_DEPLOYMENT = "LIVE_DEPLOYMENT"


#: The levels whose evidence CareLoopBench executes. Everything else is ingested.
EXECUTED_LEVELS: Final[frozenset[ExecutionLevel]] = frozenset(
    {
        ExecutionLevel.PROPERTY,
        ExecutionLevel.LOCAL_INTEGRATION,
        ExecutionLevel.REAL_PROTOCOL,
        ExecutionLevel.EVALUATION,
    }
)

#: The levels whose evidence is a stored artifact produced by somebody running a tool.
INGESTED_LEVELS: Final[frozenset[ExecutionLevel]] = frozenset(
    {ExecutionLevel.LIVE_PROVIDER, ExecutionLevel.LIVE_DEPLOYMENT}
)

#: Which levels each tier executes. ``core`` is a superset of ``quick`` by construction rather
#: than by two lists somebody has to keep in step. There is no third tier: "everything" is
#: ``python -m pytest -q``, which CI already runs on every push, and a tier that duplicated it
#: would be the whole-repository rerun this layer exists not to be.
TIERS: Final[dict[str, frozenset[ExecutionLevel]]] = {
    "quick": frozenset(
        {
            ExecutionLevel.PROPERTY,
            ExecutionLevel.LOCAL_INTEGRATION,
            ExecutionLevel.EVALUATION,
        }
    ),
    "core": EXECUTED_LEVELS,
}


class Status(StrEnum):
    """What CareLoopBench is entitled to say about one claim after one invocation."""

    #: Every declared evidence node — and every counter-evidence node — ran in this invocation
    #: and passed. Nothing weaker earns this.
    PROVEN = "PROVEN"
    #: A stored live proof whose declared dependency surface still matches the commit. It
    #: certifies *its own* commit and is always rendered with it.
    PROVEN_AT = "PROVEN_AT"
    #: A stored live proof whose dependency surface has moved since it was produced. Nothing
    #: failed; it stopped being about this code.
    STALE = "STALE"
    #: Outside the selected tier, or a live proof with no stored artifact.
    NOT_RUN = "NOT_RUN"
    #: Evidence ran and something failed, errored, was skipped, or did not exist.
    FAILED = "FAILED"
    #: The claim declares itself inapplicable in this environment. Never a pass.
    NOT_APPLICABLE = "NOT_APPLICABLE"


#: The one status that counts toward a domain numerator when evidence was executed.
PROVEN_STATUSES: Final[frozenset[Status]] = frozenset({Status.PROVEN})

#: How many characters of a commit hash a human-readable row shows. A formatting width, kept
#: here rather than in the renderer so that ``report.py`` can be held to "no integer literals".
SHORT_COMMIT: Final[int] = 12
