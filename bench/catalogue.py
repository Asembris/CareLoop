"""Rendering the tool catalogue the planner is shown.

FROZEN, like ``bench.schemas``: this is the rendering the 2026-08-11 benchmark measured against.
CareLoop's live renderer is :func:`careloop.agents.prompts.render_catalogue`, which sorts arguments
by name where this one keeps declaration order. Kept separate for the same reason the schemas are —
the rendered catalogue is part of the system prompt and therefore part of every cache key, so
editing it here discards the recorded responses rather than updating them.

Built from :data:`careloop.tools.registry.REGISTRY` rather than written out by hand. A hand-written
catalogue drifts from the registry, and the moment it does the benchmark is scoring the model
against tools the policy engine does not govern. The live registry is also why the cache is only
replayable while the registry is unchanged: add a tool and every key moves, whatever this module
does.

Argument **classes** are deliberately not rendered. The planner is told what arguments exist, not
which of them are ``DESTINATION`` or ``AUTHORITY``: invariant 6 keeps the trust vocabulary out of
model-visible text, and a model told which arguments are policed would be measured on its ability
to read the policy rather than on its ability to plan.
"""

from __future__ import annotations

from careloop.tools.registry import REGISTRY, ToolRegistry


def render_catalogue(registry: ToolRegistry = REGISTRY) -> str:
    """A stable, human-readable listing of every tool and its arguments."""
    lines: list[str] = []
    for tool in sorted(registry, key=lambda t: t.name):
        lines.append(f"- {tool.name}: {tool.description}")
        for arg in tool.args:
            suffix = f" — {arg.description}" if arg.description else ""
            lines.append(f"    {arg.name}{suffix}")
    return "\n".join(lines)


__all__ = ["render_catalogue"]
