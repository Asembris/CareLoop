"""Root conftest.

Its only job is to exist. ``bench/`` is not an installed package — it is a measuring instrument
that ships with the repo but not with the distribution — so ``import bench`` resolves only if the
repository root is on ``sys.path``. pytest inserts the directory of every ``conftest.py`` it loads,
and this file is what puts the root there.

The path is inserted explicitly rather than left to that behaviour alone, because the suite is
required to pass identically under ``pytest`` and ``python -m pytest``, and the two differ in
exactly this area — session 1 lost time to a test module that resolved under one and not the
other.
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).parent.resolve()

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
