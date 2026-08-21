"""The AgentCore code-based evaluators, deliberately outside the CareLoop package.

``CareLoopSettlementIntegrity`` asks one question of a production trace: *does what CareLoop
claims about an external effect agree with the transport evidence in the same trace?* An evaluator
that imported CareLoop's constants would be checking CareLoop against itself — the same category
error as verifying a booking by re-reading its own receipt — so the independence is enforced by
what this directory **is** rather than by a rule somebody has to remember.

Three consequences, each load-bearing:

- **Standard library only.** No ``careloop``, no ``evals``, no ``boto3``, no ``opentelemetry``,
  no ``httpx``, no ``strands``. No network, no filesystem, no environment, no clock, no model.
  ``tests/test_evaluator.py::TestTheEvaluatorImportsNothingItMustNot`` sweeps the source for it.
- **Its own expectations.** :data:`~evaluators.contract.OBSERVABLE_EFFECT_TYPES` and
  :data:`~evaluators.contract.EXPECTED_FIELDS` are declared here and never imported. Patch C adds
  the drift tests that compare them against CareLoop's registry; Patch B does not import CareLoop
  to make that comparison, because then there would be nothing left to compare.
- **Not in the wheel.** ``pyproject.toml`` packages ``careloop*`` and this directory does not
  match, exactly as ``demoworld/``, ``evals/``, ``bench/`` and ``scripts/`` do not. Nothing about
  application packaging changed to add this, and nothing should. The Lambda is zipped from this
  directory separately, which is only possible because it depends on none of the above.

The one entry point a Lambda needs is :func:`evaluators.handler.lambda_handler`; the one entry
point a test needs is :func:`evaluators.settlement.assess`. Nothing is re-exported from here, so
reaching either means naming the module it lives in.
"""
