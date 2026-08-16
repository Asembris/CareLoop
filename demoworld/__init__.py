"""Demo World — a synthetic external service CareLoop books appointments with.

**Not part of CareLoop, and it never imports it.** Verification of an external effect is worth
something only if the system being read back is not the system doing the reading; a world that
imported the agent could not be used to prove the agent reached outside itself. The direction is
swept in ``tests/test_demoworld.py``.

Shipped with the repository and not with the distribution, the same way ``bench/`` is: not an
installed package, reachable because the root ``conftest.py`` puts the repository root on
``sys.path``.

Run it::

    python -m demoworld --port 8100
"""

from __future__ import annotations

from demoworld.app import (
    CLINIC_PREFIX,
    IDEMPOTENCY_HEADER,
    SESSION_HEADER,
    SESSION_PARAM,
    create_clinic_app,
)
from demoworld.clinic import (
    DEFAULT_WORLD_LIMIT,
    SEEDED_SLOTS,
    Appointment,
    BookingRequest,
    Clinic,
    Clinics,
    Slot,
)

__all__ = [
    "CLINIC_PREFIX",
    "DEFAULT_WORLD_LIMIT",
    "IDEMPOTENCY_HEADER",
    "SEEDED_SLOTS",
    "SESSION_HEADER",
    "SESSION_PARAM",
    "Appointment",
    "BookingRequest",
    "Clinic",
    "Clinics",
    "Slot",
    "create_clinic_app",
]
