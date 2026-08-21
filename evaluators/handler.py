"""The AWS Lambda entry point. It delegates, and it is total.

``CreateEvaluator`` with a ``codeBased.lambdaConfig`` points at a function; AgentCore invokes it
with the evaluation envelope and reads a result mapping back. This module is that function and
does nothing else -- the judging is :func:`evaluators.settlement.assess`, which is a pure function
of a dictionary and can be exercised without AWS, without a Lambda runtime, and without this file
existing.

**Total, deliberately.** An evaluator that raises returns nothing at all, and a code-based
evaluator that returns nothing tells a caregiver, a judge and an operator exactly as much as one
that was never invoked. So every anticipated malformed input is already an
:class:`~evaluators.contract.ErrorCode` by the time it gets here, and the one broad ``except``
below catches whatever is left -- a bug in this package -- and answers ``INTERNAL_ERROR``.

**The broad catch discards the exception rather than reporting it, and that is not laziness.**
``str(exception)`` on a payload-derived failure can carry span content -- a URL, a resource id, a
digest, an attribute value -- into a result that lands in CloudWatch and, at submission, in
public. This is the same reasoning that stopped ``ClinicRefused`` interpolating the clinic's
response body: the text belongs to something CareLoop does not control, so it does not get to
choose what a published record says. What the caller gets is a fixed sentence and a code, which is
what the whole result vocabulary is.

**Nothing is logged here either.** A Lambda's stdout goes to a log group, which is another
published surface, and there is nothing this function knows that the returned ``errorCode`` does
not already say.
"""

from __future__ import annotations

from typing import Any

from evaluators.contract import ErrorCode
from evaluators.settlement import assess, error_result


def lambda_handler(event: Any, context: Any = None) -> dict[str, Any]:
    """Judge one AgentCore evaluation envelope.

    Args:
        event: the evaluation envelope, as AWS sends it. Passed to :func:`assess` untouched --
            validating it is that function's job, and doing any of it here would put half the
            contract in a file that cannot be tested without a Lambda runtime.
        context: the AWS Lambda context object. **Deliberately unread.** Nothing in this evaluator
            depends on the invocation, the request id, the remaining time or the environment; a
            deterministic function of the envelope is the whole property.

    Returns:
        The evaluation result mapping. Never raises, for any input.
    """
    try:
        return assess(event)
    except Exception:
        # Broad on purpose; see the module docstring. The exception is discarded rather
        # than reported, because its text can carry span content into a published record.
        return error_result(ErrorCode.INTERNAL_ERROR)


__all__ = ["lambda_handler"]
