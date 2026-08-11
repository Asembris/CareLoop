"""Triage — the first of CareLoop's two LLM agents.

Classifies one inbound event into typed case fields. It does not decide what happens next, does
not transition the case, and does not propose an action: :class:`~careloop.agents.schemas.
TriageResult` has no field through which it could.

**Triage does not extract document evidence, and that is an architecture decision.** Invariant 1
allows exactly two LLM agents, so a third that reads documents into facts is not an option; and
invariant 1's own second sentence says what the alternative is — everything else is deterministic
Python or a typed tool. Triage therefore reads the *inbound event* and copies the identifiers it
states; the evidence node reads documents deterministically. See the extraction entry in
``docs/session-log.md`` for the reasoning and for what would have to change to revisit it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from careloop.agents.invoke import structured_call
from careloop.agents.prompts import triage_system_prompt, triage_user_prompt
from careloop.agents.schemas import TriageResult
from careloop.llm.providers import ModelRole, model_for

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from strands.models import Model


class TriageAgent:
    """The triage step, bound to one model.

    Holds a model and nothing else. No toolset, no case store, no policy engine — there is no
    collaborator here through which a classification could become an effect.
    """

    def __init__(self, model: Model | None = None) -> None:
        self._model = model if model is not None else model_for(ModelRole.TRIAGE)

    @property
    def model(self) -> Model:
        """The model backing this agent. Defaults to whatever ``CARELOOP_MODEL_TRIAGE`` selects."""
        return self._model

    async def classify(self, event_text: str) -> TriageResult:
        """Classify one inbound event.

        ``event_text`` is untrusted: it is the body of a message or a document that arrived from
        outside. It reaches the model framed as evidence in the *user* message, never in the
        system prompt.
        """
        return await structured_call(
            self._model,
            TriageResult,
            system_prompt=triage_system_prompt(),
            user_prompt=triage_user_prompt(event_text),
        )


__all__ = ["TriageAgent"]
