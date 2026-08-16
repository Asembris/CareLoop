"""Property-based schema testing over the HTTP API, in-process against the ASGI app.

Offline and $0, like the rest of the suite: ``schemathesis.openapi.from_asgi`` drives the FastAPI
app over ASGI, so there is no server, no port, no socket and no model call. The CLI is deliberately
not used — ``st run --app`` was removed in Schemathesis 4 and the CLI now requires a live server, so
in-process testing is the pytest API and nothing else.

Skipped rather than excluded when the ``schema`` extra is absent. A visible skip in the run summary
is the treatment ``pyproject.toml`` already argues for over an invisible ``-m`` exclusion: a test
nobody can see being skipped is a test nobody notices has stopped existing.

**What this suite is for.** Two things, in order of value.

The custom checks are the point: every response of every operation is swept for a withheld field and
for a payload key outside the allowlist. ``tests/schema_checks.py`` carries the property and the
argument for why it earns its keep on top of the import-time projection tables — briefly, because
four surfaces pass through no response model at all, and an exception message somebody widens is the
realistic leak, not a modelled field.

The built-in checks are the secondary prize, and they are what caught the eleven operations that
used to declare nothing but their success code: an undocumented status, a response violating its own
schema, schema-violating input being accepted, a missing required header not being rejected, an
undocumented method not answering 405, and any 5xx at all.

**What this suite cannot do, stated so nobody reads more into a green tick.**

* It cannot test the case lifecycle. The stateful phase is off — see ``schemathesis.toml`` for why —
  and even on, it is a state machine over CRUD resource data-flow whose only checks are
  ``use_after_free`` and ``ensure_resource_availability``. "``DONE`` is reachable only from
  ``VERIFYING``" is structural, lives in the transition table, and is tested by
  ``tests/test_post_effect_lifecycle.py``. This suite neither replaces nor duplicates that.
* It has no concept of byte-identical replay, so it says nothing about idempotency-key semantics.
* It cannot see inside Demo World's process, the policy engine, or trust promotion and scoping.
  Neither target binds a clinic, so no booking here leaves the process.
* ``analysis.constants`` is on by default and reads handler source to harvest literals for
  generation. Harmless, and noted because it means the generator inspects code as well as schema.

Where it does earn its keep on this surface: 409 on illegal approve, reject and re-check sequences,
400 on a missing required header, ``NoSuchApproval`` and ``AmbiguousApproval`` reached by generated
identifiers, and 404 on unknown case, run, event and scenario keys.
"""

from __future__ import annotations

from typing import Any

import pytest

_MISSING = "the `schema` extra is not installed; `pip install -e '.[dev,schema]'`"

# Both the package and the submodule, deliberately. `importorskip` on the top-level name alone is
# not enough: an interrupted or partially removed install leaves an empty `schemathesis/` directory,
# which Python imports happily as a *namespace package* — so the guard passes and the submodule
# import below becomes a collection **error** that takes the whole file down instead of a skip.
# Measured, by uninstalling. Naming every module actually reached keeps the failure a skip.
schemathesis = pytest.importorskip("schemathesis", reason=_MISSING)
_failures = pytest.importorskip("schemathesis.core.failures", reason=_MISSING)
FailureGroup = _failures.FailureGroup

from fastapi import FastAPI  # noqa: E402

from careloop.api.app import create_app  # noqa: E402
from careloop.api.deps import ApiDeps  # noqa: E402
from careloop.api.sessions import SESSION_HEADER, SessionWorlds  # noqa: E402
from careloop.state.memory_store import InMemoryCaseStore  # noqa: E402
from tests.schema_checks import (  # noqa: E402
    check_withheld_names_are_distinct,
    payload_keys_outside_allowlist,
    withheld_keys_in,
)

# Before either sweep is trusted. `withheld_keys_in` matches on field name, which is only a leak
# detector while the withheld and projected vocabularies are disjoint; if they ever collide this
# fails at import with the name in it rather than silently becoming a false-positive generator.
check_withheld_names_are_distinct()


# -- the custom checks ----------------------------------------------------------------------------


def _json_body(response: Any) -> object | None:
    """The parsed body, or ``None`` when there is not one.

    A response with no body, or one that is not JSON, carries no keys to sweep. Returning ``None``
    would make both checks vacuous if this were wrong about every response, which is exactly what
    the negative controls below are for: they require a real failure through this same path.
    """
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return None


@schemathesis.check
def no_withheld_field_in_response(ctx: Any, response: Any, case: Any) -> None:
    """No response may carry a field the API decided to withhold, at any depth.

    Reads :data:`careloop.api.projections.WITHHELD` through ``tests.schema_checks``, so a second
    withheld field is covered the day it is declared.
    """
    if (body := _json_body(response)) is None:
        return
    if leaked := sorted(withheld_keys_in(body)):
        raise AssertionError(
            f"{case.operation.label} answered {response.status_code} carrying withheld "
            f"field(s) {', '.join(leaked)}. These are withheld wholesale — "
            f"TimelineEvent.diagnostic is unbounded free text its own docstring says may hold raw "
            f"argument values and rule internals. Withhold it here too, rather than filtering it."
        )


@schemathesis.check
def timeline_payload_is_allowlisted(ctx: Any, response: Any, case: Any) -> None:
    """Every timeline payload key must be one the allowlist names, at any depth."""
    if (body := _json_body(response)) is None:
        return
    if extra := sorted(payload_keys_outside_allowlist(body)):
        raise AssertionError(
            f"{case.operation.label} answered {response.status_code} carrying timeline payload "
            f"key(s) {', '.join(extra)} that TIMELINE_PAYLOAD_FIELDS does not name. The allowlist "
            f"exists so a new payload key is invisible until somebody decides about it; decide, "
            f"then add it there."
        )


# -- the two app shapes ---------------------------------------------------------------------------
#
# Both are tested because they answer different statuses and require different headers. A
# single-world app is what local development and the offline suite use; an isolated app is what the
# deployed demo uses, and it is the only shape in which X-CareLoop-Session is required at all — so
# it is the only shape where a missing-required-header check has that header to fire on.
#
# **A fresh world per example, not per test.** Measured: rebuilding the store, the run registry and
# the event simulator costs 0.002 ms, so 2000 of them is three milliseconds. Sharing one store
# across examples would have been faster by nothing and would have cost the thing
# property-based testing is for — a failing example that depends on state accumulated by earlier
# examples may not reproduce when Hypothesis replays it alone, and a red CI nobody can reproduce
# locally teaches people to press the button again.


def _single_world() -> ApiDeps:
    return ApiDeps(store=InMemoryCaseStore())


def _isolated_worlds() -> SessionWorlds:
    return SessionWorlds(lambda session_id: ApiDeps(store=InMemoryCaseStore()))


single_world_app = create_app(_single_world().store, demo=True)
isolated_app = create_app(sessions=_isolated_worlds(), demo=True)

single_world_schema = schemathesis.openapi.from_asgi("/openapi.json", single_world_app)
isolated_schema = schemathesis.openapi.from_asgi("/openapi.json", isolated_app)


@single_world_schema.parametrize()
def test_the_single_world_api(case: Any) -> None:
    """Every operation, over an app serving one world to everybody."""
    single_world_app.state.careloop = _single_world()
    case.call_and_validate()


@isolated_schema.parametrize()
def test_the_isolated_api(case: Any) -> None:
    """Every operation, over an app giving each visitor a world of their own.

    The session is minted per example and supplied explicitly, because it cannot be a constant: a
    made-up session id is answered 410, and a target where every response is 410 would exercise
    nothing behind session resolution. Measured: a supplied header overrides the generated one — 108
    of 216 responses were 410 without this and none with it.

    The cost is stated rather than hidden. Because the session header is put back on every request,
    this target does **not** exercise a *missing* session header; the 400 for that is covered by
    ``tests/test_session_isolation.py``. What it buys is that everything behind session resolution —
    the store, the projections, the verbs — is reached at all in the shape the demo deploys.
    """
    worlds = _isolated_worlds()
    isolated_app.state.careloop = worlds
    case.call_and_validate(headers={SESSION_HEADER: worlds.mint()})


# -- the negative controls ------------------------------------------------------------------------


def _leaking_app() -> FastAPI:
    """An app that leaks on purpose, for the checks to catch.

    A fixture app rather than a temporary mutation of CareLoop's own routes, so the proof is
    permanent: these controls fail the moment either check stops working, whereas a mutation proves
    something once and then stops existing. The shapes are the realistic ones — nested inside an
    array, and inside an error body — not a top-level key.
    """
    app = FastAPI()

    @app.get("/leaks-a-withheld-field-in-an-array")
    def in_an_array() -> dict:
        return {"timeline": [{"seq": 1}, {"seq": 2, "diagnostic": "rule deny-x, arg to=a@b.com"}]}

    @app.get("/leaks-a-withheld-field-in-an-error-body")
    def in_an_error() -> dict:
        # The realistic channel: an exception message somebody widened.
        return {"detail": {"diagnostic": "matched deny-email-to-untrusted-recipient"}}

    @app.get("/leaks-a-payload-key-outside-the-allowlist")
    def outside_allowlist() -> dict:
        return {"timeline": [{"payload": {"tool_name": "send_email", "raw_args": "..."}}]}

    @app.get("/leaks-nothing")
    def clean() -> dict:
        return {"timeline": [{"seq": 1, "payload": {"tool_name": "send_email"}}]}

    return app


@pytest.fixture(scope="module")
def leaking_schema() -> Any:
    """The deliberately leaking app, loaded once. Module scope: nothing here mutates it."""
    return schemathesis.openapi.from_asgi("/openapi.json", _leaking_app())


class TestTheChecksAreNotVacuous:
    """A check that has never failed proves nothing. These are what make them checks.

    Driven through the real ``call_and_validate`` dispatch rather than by calling the predicates,
    which ``tests/test_schema_checks.py`` already does. What is proven here is the wiring: that the
    checks are registered, that Schemathesis invokes them on every response, and that a failure
    surfaces as a failure rather than being swallowed by ``_json_body`` returning ``None``.
    """

    def test_the_clean_control_passes(self, leaking_schema: Any) -> None:
        # Without this, the three below would prove only that something failed, not that the checks
        # discriminate. The same app, the same dispatch, an allowlisted payload: no failure.
        leaking_schema["/leaks-nothing"]["GET"].Case().call_and_validate()

    @pytest.mark.parametrize(
        ("path", "expected_check", "expected_detail"),
        [
            (
                "/leaks-a-withheld-field-in-an-array",
                "no_withheld_field_in_response",
                "diagnostic",
            ),
            (
                "/leaks-a-withheld-field-in-an-error-body",
                "no_withheld_field_in_response",
                "diagnostic",
            ),
            (
                "/leaks-a-payload-key-outside-the-allowlist",
                "timeline_payload_is_allowlisted",
                "raw_args",
            ),
        ],
    )
    def test_a_planted_leak_fails_the_run(
        self, leaking_schema: Any, path: str, expected_check: str, expected_detail: str
    ) -> None:
        with pytest.raises(FailureGroup) as raised:
            leaking_schema[path]["GET"].Case().call_and_validate()
        reported = str(raised.value)
        # Named, so the failure is attributed to the custom check and not to a built-in that
        # happened to fire on the same response.
        assert expected_check in reported
        assert expected_detail in reported

    def test_the_withheld_sweep_reaches_through_an_array(self, leaking_schema: Any) -> None:
        # The load-bearing one. In CareLoop's real responses a withheld field can only ever appear
        # nested, so a top-level-only check would have been vacuously green on every endpoint. This
        # requires the wired check to find one two levels down.
        with pytest.raises(FailureGroup):
            leaking_schema["/leaks-a-withheld-field-in-an-array"]["GET"].Case().call_and_validate()
