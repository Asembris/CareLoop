"""Demo World on a real loopback socket, for the tests that need one.

**A real server, not a test client, and the distinction is the session's whole claim.** Driving the
ASGI app in-process would exercise every line of Demo World and none of the thing being asserted,
which is that CareLoop reaches an external system over HTTP. A loopback socket is not a network
dependency — nothing outside the machine is contacted, no credential is needed, and CI's
zero-secret contract is untouched — but it is a real connection, made by the real client, to a
server running in its own event loop.

Bound to port 0 and the actual port read back off the socket, so two test sessions cannot collide
and nothing has to guess what is free. Startup is polled rather than slept through, with a deadline,
because a fixture that sleeps a fixed time is a fixture that is either slow or flaky.

Kept out of ``conftest.py`` for the reason ``helpers.py`` is: test modules import this, and pytest
loads ``conftest.py`` rather than exporting it. The fixtures that wrap it live there.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import uvicorn

from demoworld.app import create_clinic_app
from demoworld.clinic import Clinic

#: How long the server has to bind before the fixture gives up. Generous, because the cost of being
#: wrong is a confusing hang rather than a slow suite: nothing waits this long in practice.
STARTUP_TIMEOUT = 10.0

SHUTDOWN_TIMEOUT = 10.0


@contextmanager
def clinic_server(clinic: Clinic | None = None) -> Iterator[tuple[str, Clinic]]:
    """Run Demo World on a loopback port, yielding its base URL and its state.

    The :class:`~demoworld.clinic.Clinic` is yielded alongside the URL because a test that wants to
    prove the read-back is independent has to be able to move an appointment out from under the
    caller — and it can only do that by holding the same object the routes hold.
    """
    world = clinic if clinic is not None else Clinic()
    config = uvicorn.Config(create_clinic_app(world), host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="demoworld", daemon=True)
    thread.start()

    deadline = time.monotonic() + STARTUP_TIMEOUT
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            server.should_exit = True
            raise RuntimeError("Demo World did not start")
        time.sleep(0.01)

    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}", world
    finally:
        server.should_exit = True
        thread.join(timeout=SHUTDOWN_TIMEOUT)


__all__ = ["SHUTDOWN_TIMEOUT", "STARTUP_TIMEOUT", "clinic_server"]
