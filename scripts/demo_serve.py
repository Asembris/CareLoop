"""Serve CareLoop for a live demo: real models, a real clinic, the control surface mounted.

Two processes, and the separation is the point::

    python -m demoworld --port 8100
    python scripts/demo_serve.py --clinic-url http://127.0.0.1:8100 --port 8000

Then press a button::

    curl -X POST "http://127.0.0.1:8000/api/demo/scenarios/referral?wait=60"

What that produces is a real appointment in a service CareLoop does not own, verified by reading
that service back — not by re-reading the confirmation it was handed. ``GET /clinic/slots`` on the
other process is the independent check a judge can run themselves.

**Nothing here decides anything.** The script chooses a store, resolves the model providers from
the environment the way every other entry point does, points a client at the clinic, and hands all
three to ``create_app``. Whether a case books, parks or is refused is the graph's business.

``--clinic-url`` is optional: without it every effect is simulated, exactly as it was before this
session, and ``read_back`` on a verification says ``false`` rather than lying.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from careloop.api.app import create_app
from careloop.state.sqlite_store import SqliteCaseStore
from careloop.tools.clinic import ClinicClient


def load_env_file(path: Path) -> int:
    """Read ``KEY=value`` lines into the environment, leaving anything already set alone.

    The same thing ``bench/run.py`` does and for the same reason: credentials live in ``.env`` on a
    developer's machine, and a demo entry point that could not read them would be a demo that only
    runs with a shell full of exports. Nothing under ``careloop/`` loads a file — configuration
    reaches the library as environment variables, which is what keeps CI's zero-secret contract
    checkable.
    """
    if not path.exists():
        return 0
    loaded = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if value and key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve CareLoop with the demo control surface.")
    parser.add_argument("--db", default="demo.sqlite3", help="case store path")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--clinic-url",
        default=os.environ.get("CARELOOP_CLINIC_URL", ""),
        help="Demo World's base URL; empty means every effect stays simulated",
    )
    parser.add_argument("--env-file", default=".env", help="key=value file to load first")
    arguments = parser.parse_args()

    load_env_file(Path(arguments.env_file))

    store = SqliteCaseStore(arguments.db)
    clinic = ClinicClient(arguments.clinic_url) if arguments.clinic_url else None
    app = create_app(store, clinic=clinic, demo=True)

    print(f"case store    {arguments.db}")
    print(f"clinic        {arguments.clinic_url or '(simulated)'}")
    print(f"triage model  {os.environ.get('CARELOOP_MODEL_TRIAGE', '(mock)')}")
    print(f"planner model {os.environ.get('CARELOOP_MODEL_PLANNER', '(mock)')}")

    import uvicorn

    uvicorn.run(app, host=arguments.host, port=arguments.port, log_level="warning")


if __name__ == "__main__":  # pragma: no cover - a process entry point, not a call site
    main()
