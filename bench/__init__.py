"""The model-provider benchmark.

Not part of the CareLoop package. It lives outside ``careloop/`` because it is a measuring
instrument, not part of the system being measured, and because it is allowed to do things the
library is not — read a clock, read ``.env``, and talk to a paid endpoint.

In particular, **invariant 9 (no clock, no randomness) does not apply here**. It constrains
``careloop/tools/`` so that a replayed action is byte-identical; this package measures latency,
which requires exactly the clock that invariant forbids. Keeping the benchmark out of the package
is what lets both statements be true at once.
"""
