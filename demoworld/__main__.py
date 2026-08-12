"""Run Demo World as its own process.

    python -m demoworld --port 8100

CareLoop then reaches it over real HTTP, which is the whole point: an external service whose state
actually changes, read back independently, is the difference between a verifier and a tautology.

``uvicorn`` is imported here and nowhere else in the package, so importing :mod:`demoworld` in a
test costs nothing and needs no server.
"""

from __future__ import annotations

import argparse

from demoworld.app import create_clinic_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Demo World clinic service.")
    parser.add_argument("--host", default="127.0.0.1", help="interface to bind (default loopback)")
    parser.add_argument("--port", type=int, default=8100, help="port to bind")
    arguments = parser.parse_args()

    import uvicorn

    uvicorn.run(create_clinic_app(), host=arguments.host, port=arguments.port, log_level="warning")


if __name__ == "__main__":  # pragma: no cover - a process entry point, not a call site
    main()
