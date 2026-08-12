"""The ``BeforeToolCall`` policy hook — the primary enforcement point.

**Read this before describing what it does.** CareLoop fires the SDK's real hook against a real
:class:`~strands.hooks.registry.HookRegistry` **from its own execution node**. An SDK event loop
does not fire it, because CareLoop has no event loop to fire it: ``careloop/agents/invoke.py``
drives the provider's structured-output generator directly, and neither agent holds a tool.
(The method is deliberately not named here in full — ``tests/test_agents.py`` sweeps the whole
package for it to prove there is one model call site, and a docstring mention would blunt the
sweep. The sweep is worth more than the phrasing.)

Everything here is genuine SDK machinery — the real
:class:`~strands.hooks.events.BeforeToolCallEvent`, the real registry, the real ``cancel_tool``
contract, and a provider that would plug unchanged into a ``strands.Agent`` the day one exists.
What is CareLoop's is the *firing*. Saying "Strands blocks the attack" would
overstate it, and this project cannot afford a claim someone can check and find inflated.

``docs/spike-findings.md`` §2 assumed the event loop that commit 5 removed, and is corrected there.
One consequence is worth stating rather than discovering: **a denial does not reach the planner as
an error tool result**, because no tool result reaches a model at all. The replan channel is
``prior_denials`` into :func:`careloop.agents.prompts.planner_user_prompt`, which carries a
``(tool_name, ReasonCode)`` pair and renders it through
:func:`careloop.policy.verdicts.reason_text`. That is strictly better for invariant 6: what
arrives at the planner is a literal from a closed enum, on a path with no SDK in it.

**What goes into ``cancel_tool``.** Exactly ``decision.model_visible_reason`` — one literal from
:data:`careloop.policy.verdicts.REASON_TEXT`, with nothing prepended, appended or interpolated.
The spike's own example wrote ``"DENY: policy rule comms.send_document.deny_untrusted_destination"``
into that field, which leaks a rule id to the model; invariant 6 forbids it and
``tests/test_policy_hook.py`` fails if any such token appears. The field is model-visible by
design, so it is treated as prompt content, not as a log line.

**The hook decides nothing itself.** It asks :class:`~careloop.policy.gate.PolicyGate`, which asks
the engine, which reads ``rules.yaml``. The gated tool wrapper in :mod:`careloop.policy.gated` asks
the same gate, so removing this hook does not remove the enforcement — that is what makes the two
layers layers rather than alternatives.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from strands.hooks import BeforeToolCallEvent
from strands.hooks.registry import HookRegistry

from careloop.policy.gate import PolicyGate
from careloop.policy.verdicts import Decision

#: Identifier given to a tool use CareLoop dispatches itself.
#:
#: A real event loop takes this from the model's ``toolUse`` block. Ours is derived from the
#: idempotency key by the caller, so it is deterministic and carries no clock — the same reason
#: nothing under ``careloop/tools/`` may read one.
TOOL_USE_ID_PREFIX: Final[str] = "careloop-"


class PolicyHook:
    """A :class:`~strands.hooks.registry.HookProvider` that cancels refused tool calls.

    Structurally a Strands hook provider: it implements ``register_hooks`` and is registered with
    ``registry.add_hook(...)`` exactly as the SDK expects. Nothing about it is CareLoop-shaped
    except who fires the event.
    """

    def __init__(self, gate: PolicyGate) -> None:
        self._gate = gate
        self._decisions: list[Decision] = []

    @property
    def gate(self) -> PolicyGate:
        """The gate every verdict comes from. Shared with the backstop."""
        return self._gate

    @property
    def decisions(self) -> tuple[Decision, ...]:
        """Every decision this hook has made, in order.

        Held so the calling node can write them to the timeline. The hook does not write them
        itself: it cannot reach a case, and a hook that could transition one would be an authority
        it is not supposed to hold.
        """
        return tuple(self._decisions)

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        """Subscribe to :class:`~strands.hooks.events.BeforeToolCallEvent`."""
        registry.add_callback(BeforeToolCallEvent, self.on_before_tool_call)

    def on_before_tool_call(self, event: BeforeToolCallEvent) -> None:
        """Refuse the call by setting ``cancel_tool``, or leave it alone.

        ``cancel_tool`` is ``bool | str``; a string cancels the call and becomes the text the
        caller sees. Only the closed-vocabulary reason goes in it.
        """
        tool_name = event.tool_use["name"]
        args: Mapping[str, object] = event.tool_use["input"] or {}

        decision = self._gate.decide(tool_name, args)
        self._decisions.append(decision)

        if decision.allowed:
            return

        reason = decision.model_visible_reason
        # Total over every non-ALLOW verdict by construction: Decision refuses to exist without a
        # reason code unless it is an ALLOW, which returned above.
        assert reason is not None
        event.cancel_tool = reason


def policy_registry(hook: PolicyHook) -> HookRegistry:
    """A registry with ``hook`` registered, and nothing else on it."""
    registry = HookRegistry()
    registry.add_hook(hook)
    return registry


async def fire_before_tool_call(
    registry: HookRegistry,
    tool_name: str,
    args: Mapping[str, object],
    *,
    tool_use_id: str,
) -> str | None:
    """Fire ``BeforeToolCallEvent`` on ``registry`` and return the cancellation message, if any.

    This is where CareLoop takes the SDK's place. In an agent run the tool executor builds this
    event and reads ``cancel_tool`` back; here the execute node does, with the same event class and
    the same registry type, so a hook written against the SDK works unmodified either way.

    ``selected_tool`` is ``None`` rather than a fabricated tool object. The field is declared
    ``AgentTool | None`` and documented as ``None`` when tool lookup failed, so ``None`` is a value
    the contract already admits — and inventing an ``AgentTool`` to look more like an agent run
    would be dressing rather than fidelity. ``agent`` is ``None`` for the same reason and because
    there is no agent: verified against ``strands-agents==1.51.0``, where
    ``HookRegistry.invoke_callbacks_async`` dispatches on the event's type and never reads it. That
    is a version-specific fact and belongs to the pin.

    Returns:
        The cancellation text if a callback refused the call, otherwise ``None``.
    """
    event = BeforeToolCallEvent(
        agent=None,  # type: ignore[arg-type]
        selected_tool=None,
        tool_use={"toolUseId": tool_use_id, "name": tool_name, "input": dict(args)},
        invocation_state={},
    )
    fired, _interrupts = await registry.invoke_callbacks_async(event)

    cancel = fired.cancel_tool
    if cancel is False:
        return None
    if cancel is True:
        # The SDK's documented "cancel with a default message" form. No CareLoop callback produces
        # it — ours always carries a reason code — but a third-party hook on the same registry
        # could, and silently reading it as "not cancelled" would be an enforcement hole.
        return "tool call cancelled"
    return cancel


def tool_use_id(idempotency_key: str) -> str:
    """A deterministic tool-use id derived from the idempotency key.

    No clock and no randomness, so two runs of the same case produce the same identifier and a
    replayed action is comparable byte for byte.
    """
    return f"{TOOL_USE_ID_PREFIX}{idempotency_key[:16]}"


__all__ = [
    "TOOL_USE_ID_PREFIX",
    "PolicyHook",
    "fire_before_tool_call",
    "policy_registry",
    "tool_use_id",
]
