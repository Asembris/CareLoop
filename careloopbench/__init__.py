"""CareLoopBench — the consolidation layer over CareLoop's existing proofs.

It is a *reporter*, not a test framework and not an evaluator. It declares which properties
CareLoop claims, points each one at evidence that already exists, runs the declared evidence once,
ingests live proofs that were measured elsewhere, and renders one canonical result. It proves
nothing new, and if it ever starts to it has become the second, weaker copy of the thing it
reports on.

Repo-only, exactly as ``evals/`` and ``evaluators/`` are: ``pyproject.toml`` packages
``careloop*`` alone, so nothing here ships in the wheel or reaches a deployed process.
"""

from __future__ import annotations

from careloopbench.claims import CLAIMS, Claim, RegistryRefused
from careloopbench.levels import Domain, ExecutionLevel, Status

__all__ = ["CLAIMS", "Claim", "Domain", "ExecutionLevel", "RegistryRefused", "Status"]
