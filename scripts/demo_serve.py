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

It also chooses whether runs record what they did, which is the *only* place that choice is made.
Both telemetry lines below are composition and neither is a feature flag a route or a node can
read: with no instrumentation the process installs no redaction and hands runs a witness that
records nothing, and the cases it produces are identical either way.

``--clinic-url`` is optional: without it every effect is simulated, exactly as it was before this
session, and ``read_back`` on a verification says ``false`` rather than lying.

``--isolate-sessions`` gives **every visitor their own world** — their own case store, their own
run registry, their own clinic — which is what a public URL two judges may open at the same moment
needs. It is off by default, because a developer wants one world they can point ``sqlite3`` at, and
on in the deployed unit. With it on ``--db`` is not used at all: a world lives in memory and lasts
as long as somebody is using it. That is a real trade and it is stated where a judge will meet it —
durable, cross-process ownership of a case is demonstrated by ``scripts/resume_demo.py``, not by
the public URL.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from careloop.api.app import create_app
from careloop.api.deps import simulated_context_factory
from careloop.api.faults import booking_response_loss
from careloop.api.sessions import DEFAULT_SESSION_LIMIT, demo_session_worlds
from careloop.graph.witness import NullWitness
from careloop.state.sqlite_store import SqliteCaseStore
from careloop.tools.clinic import ClinicClient
from scripts.observability import install_telemetry_redaction
from scripts.witness_span import span_witness


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
    parser.add_argument(
        "--isolate-sessions",
        action="store_true",
        help="give every visitor their own world; --db is unused when this is on",
    )
    parser.add_argument(
        "--static-dir",
        default=os.environ.get("CARELOOP_STATIC_DIR", ""),
        help="a built frontend to serve from this same origin; empty means API only",
    )
    arguments = parser.parse_args()

    load_env_file(Path(arguments.env_file))

    # Telemetry only, and only ever subtractive. Installed here because it must be registered
    # before the first AWS call and this is the earliest point that exists; returns False when the
    # process is not running under `opentelemetry-instrument`, which is the ordinary uninstrumented
    # case rather than a fault. Nothing below reads the result except the line that prints it.
    redacting = install_telemetry_redaction()

    # Telemetry only, and only ever *additive* — the mirror of the line above, and a separate
    # decision from it. `span_witness()` asks its own question rather than reusing `redacting`,
    # which answers "was the redaction processor registered?" and carries a side effect with it;
    # one switch for two unrelated features is how a later change to one silently turns off the
    # other. A run is handed the result and never asks which one it got.
    witness = span_witness()

    if arguments.isolate_sessions:
        app = create_app(
            sessions=demo_session_worlds(clinic_url=arguments.clinic_url, witness=witness),
            demo=True,
            static_dir=arguments.static_dir or None,
        )
        where = f"per visitor, in memory (up to {DEFAULT_SESSION_LIMIT} worlds)"
    else:
        clinic = ClinicClient(arguments.clinic_url) if arguments.clinic_url else None
        # The single-world path gets the recovery scenario too, wired exactly as an isolated world
        # is: a second client into the same clinic, handed only to runs for the case a recovery
        # scenario armed. Leaving it out here would make the demo surface mean two different things
        # depending on a flag, and `scripts/e2e_serve.py` drives the isolated shape anyway.
        faults = booking_response_loss(arguments.clinic_url) if clinic is not None else None
        app = create_app(
            SqliteCaseStore(arguments.db),
            clinic=clinic,
            context_factory=(
                faults.context_factory(clinic, witness)
                if faults is not None and clinic is not None
                else simulated_context_factory(witness)
            ),
            demo=True,
            static_dir=arguments.static_dir or None,
            faults=faults,
        )
        where = arguments.db

    print(f"case store    {where}")
    print(f"clinic        {arguments.clinic_url or '(simulated)'}")
    print(f"frontend      {arguments.static_dir or '(API only)'}")
    print(f"triage model  {os.environ.get('CARELOOP_MODEL_TRIAGE', '(mock)')}")
    print(f"planner model {os.environ.get('CARELOOP_MODEL_PLANNER', '(mock)')}")
    print(f"telemetry     {'ADOT, access key id redacted' if redacting else '(not instrumented)'}")
    print(f"witness       {'(none)' if isinstance(witness, NullWitness) else 'span attributes'}")

    import uvicorn

    uvicorn.run(app, host=arguments.host, port=arguments.port, log_level="warning")


if __name__ == "__main__":  # pragma: no cover - a process entry point, not a call site
    main()
