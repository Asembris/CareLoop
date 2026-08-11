"""Planner — the second and last of CareLoop's two LLM agents.

Proposes one next action as a typed tool intent. It proposes; it does not act, does not evaluate
its own proposal, and cannot execute one. That holds three times over: the agent has no toolset,
:class:`~careloop.agents.schemas.PlannerIntent` has no field that could ask for execution, and a
deterministic policy engine it never sees decides the outcome.

**The planner cannot say "stop".** There is no ``NO_ACTION`` intent and no completion flag,
because a planner that could declare the case finished would be deciding case state — which
invariant 4 reserves for :func:`careloop.state.machine.transition`, and which is precisely the
authority invariant 2 keeps away from the model. Termination belongs to the graph, bounded by the
Strands primitives named in invariant 5.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from careloop.agents.invoke import structured_call
from careloop.agents.prompts import planner_system_prompt, planner_user_prompt
from careloop.agents.schemas import PlannerIntent, PlannerIntentBase
from careloop.llm.providers import ModelRole, model_for
from careloop.policy.verdicts import ReasonCode
from careloop.state.case import Case

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from strands.models import Model


class PlannerAgent:
    """The planning step, bound to one model.

    Holds a model and nothing else — see :class:`~careloop.agents.triage.TriageAgent` for why that
    matters.
    """

    def __init__(self, model: Model | None = None) -> None:
        self._model = model if model is not None else model_for(ModelRole.PLANNER)

    @property
    def model(self) -> Model:
        """The model backing this agent. Defaults to what ``CARELOOP_MODEL_PLANNER`` selects."""
        return self._model

    async def propose(
        self,
        case: Case,
        prior_denials: Sequence[tuple[str, ReasonCode]] = (),
    ) -> PlannerIntentBase:
        """Propose the single next action for ``case``.

        ``prior_denials`` are proposals the policy engine already refused, as the tool that was
        proposed and the closed-vocabulary reason it was refused for. Only the
        :class:`~careloop.policy.verdicts.ReasonCode` travels: the rule that matched, the argument
        that failed and the trust it resolved to stay in the timeline, per invariant 6.

        The returned intent is a proposal. Nothing here has evaluated it, and nothing here can
        carry it out.
        """
        return await structured_call(
            self._model,
            PlannerIntent,
            system_prompt=planner_system_prompt(),
            user_prompt=planner_user_prompt(case, prior_denials),
        )


__all__ = ["PlannerAgent"]
