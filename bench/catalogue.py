"""Rendering the tool catalogue the planner is shown.

Built from :data:`careloop.tools.registry.REGISTRY` rather than written out by hand. A hand-written
catalogue drifts from the registry, and the moment it does the benchmark is scoring the model
against tools the policy engine does not govern.

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
