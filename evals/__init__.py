"""Adversarial evaluation of CareLoop, against two targets.

This package is a **measuring instrument**, in the same sense ``bench/`` is: it is allowed to read
a clock, read ``.env`` and talk to a paid endpoint, none of which anything under ``careloop/`` may
do. Nothing here is imported by the system it measures, and session G changed no line of
``careloop/``.

**The two targets, and why there are two.** DeepTeam's own documentation states that it tests
agentic risk at the model reasoning and response level only, and that comprehensive agentic
security requires additional runtime controls. CareLoop *is* that layer, so a single number
against a single target would measure the wrong thing:

``planner``   the planner model with the real tool catalogue and **no gate**, its proposal
              executed through a bare :class:`~careloop.tools.callables.ToolSet`. Model-level
              exposure. Every effect here is unauthorised by construction, because there is
              nothing in the path that could authorise one — which is the point.

``gated``     :func:`careloop.graph.runner.run_case` over the real graph, the real ``rules.yaml``
              and both enforcement points. System-level exposure.

The contrast between them is the measurement, and it comes from CareLoop's own architecture rather
than from disabling a guard to manufacture a before-and-after.

**Two rules this package may not break, both checked by tests rather than asserted here.**

*No ``ClinicClient``, ever.* ``book_appointment`` becomes a real HTTP call to Demo World the
moment a :class:`~careloop.tools.clinic.ClinicClient` is handed to a
:class:`~careloop.graph.context.RunContext`. An adversarial suite that could construct one would
be firing attacker-derived payloads at a live service, so ``tests/test_evals_targets.py`` sweeps
this package for the name. Every effect in an evaluation run is simulated.

*Nothing offline imports ``deepteam``.* :mod:`evals.capture`, :mod:`evals.targets`,
:mod:`evals.corpus` and :mod:`evals.replay` import CareLoop and the standard library and nothing
else, which is what lets the CI job that prints the headline number install ``.[dev]`` alone.
DeepTeam is reached only through :mod:`evals.bridge`, which also carries the telemetry opt-out
that has to precede its import — see ``docs/spike-findings.md`` §7.2.
"""

from __future__ import annotations

__all__: list[str] = []
