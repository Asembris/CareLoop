"""Assert the deployed demo's properties through the CDN, from outside.

Run by ``scripts/deploy.py`` at the end of every deploy, and runnable by hand at any time::

    python scripts/verify_deploy.py https://d2heuwvhvrdyig.cloudfront.net

**Why this exists at all.** ``bootstrap.sh`` already checks that both units answer on the instance.
That is liveness, and liveness is not the thing that breaks. What breaks is the class of defect CI
structurally cannot see, because CI runs the code and not the deployment: a CDN that strips the
session header, a cache policy that serves one visitor's case list to another, a frontend bundle
that is not the one that was built, a unit that lost its ``--isolate-sessions`` flag. Every one of
those leaves a green test suite and a broken demo.

**Through the distribution, never the origin.** A check against ``127.0.0.1:8000`` on the instance
would pass in exactly the situations this exists to catch — the CDN is half of what is deployed.

**Read-only except for its own two worlds.** It mints sessions and works inside them, so it can be
run against the live demo while a judge is using it: the isolation this asserts is the same
isolation that makes running it safe. It never touches the default clinic world and never resets
anything it did not create.

**No credentials.** Public HTTP only, so this is also what the scheduled uptime check runs.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Final

DEFAULT_BASE: Final[str] = "https://d2heuwvhvrdyig.cloudfront.net"

#: How long to allow one graph run. Generous on purpose: the slowest run observed against this
#: deployment is 14.6s, minutes after a redeploy — which is exactly when this runs.
RUN_TIMEOUT: Final[float] = 90.0

SESSION_HEADER: Final[str] = "X-CareLoop-Session"


class Failed(Exception):
    """One asserted property did not hold."""


def call(
    base: str,
    path: str,
    *,
    method: str = "GET",
    session: str | None = None,
    caregiver: str | None = None,
    timeout: float = 30.0,
) -> tuple[int, Any, dict[str, str]]:
    """One request. Returns (status, decoded body or None, headers). Never raises for a status."""
    request = urllib.request.Request(f"{base}{path}", method=method)
    request.add_header("Accept", "application/json")
    if session:
        request.add_header(SESSION_HEADER, session)
    if caregiver:
        request.add_header("X-Caregiver", caregiver)

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            headers = {key.lower(): value for key, value in response.headers.items()}
            return response.status, decoded(response.read()), headers
    except urllib.error.HTTPError as refused:
        headers = {key.lower(): value for key, value in refused.headers.items()}
        return refused.code, decoded(refused.read()), headers


def decoded(raw: bytes) -> Any:
    """A JSON body, or ``None`` when there is not one.

    Not every route here answers JSON — ``/`` is the frontend shell — and a refusal may answer
    HTML from the CDN rather than from the origin. A checker that fell over on that would report a
    broken deployment when what is broken is the checker.
    """
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def check(description: str, condition: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if condition else 'FAIL'}  {description}")
    if not condition:
        raise Failed(f"{description}{': ' + detail if detail else ''}")


def mint(base: str) -> str:
    status, body, _ = call(base, "/api/sessions", method="POST")
    if status != 201 or not isinstance(body, dict) or not body.get("session"):
        raise Failed(f"POST /api/sessions answered {status}, so no world could be obtained")
    return str(body["session"])


def settled_case(base: str, session: str, case_id: str) -> dict[str, Any]:
    status, body, _ = call(base, f"/api/cases/{case_id}", session=session)
    if status != 200 or not isinstance(body, dict):
        raise Failed(f"case {case_id} answered {status} in this world")
    return body


def verify(base: str) -> None:
    """Assert what this deployment claims to be, and then assert that claim.

    **The mode is read rather than assumed, and it decides which properties are askable at all.**
    A demo deployment can be driven end to end by an anonymous checker, which is what
    :func:`verify_demo` does. A production one cannot, and must not: everything past
    ``GET /api/auth`` needs a credential this script deliberately does not hold. So the two are
    separate bodies rather than one body with skips in it -- a skipped assertion reads as a
    property that held.

    What the production half checks is exactly what is checkable without authenticating: that the
    deployment says it verifies identity, that it publishes a login a browser can complete, and
    that every route touching a case refuses an unauthenticated caller. The authenticated half --
    a real token, a real booking, a real read-back -- is a proof run by hand with a token obtained
    from the hosted UI, and is recorded in ``docs/identity-bootstrap.md`` rather than pretended to
    here.
    """
    print(f"\nverifying {base}\n")

    print("the API is there and says what it is")
    status, _, headers = call(base, "/api/health")
    check("GET /api/health is 200", status == 200)
    check(
        "/api/* is uncacheable at the edge",
        headers.get("cache-control") == "no-store",
        headers.get("cache-control", "<none>"),
    )
    status, raw, _ = call(base, "/api/auth")
    # `call` decodes already; this is the guard against a non-JSON body, not a second decode.
    published = raw if status == 200 and isinstance(raw, dict) else {}
    check("GET /api/auth is 200", status == 200, f"got {status}")
    mode = published.get("mode")
    check("it reports one of the two identity modes", mode in {"DEMO", "PRODUCTION"}, f"{mode!r}")

    if mode == "PRODUCTION":
        verify_production(base, published)
    else:
        verify_demo(base, published)

    verify_the_shipped_frontend(base)


def verify_production(base: str, published: dict[str, Any]) -> None:
    """A deployment that verifies real tokens, checked by something holding none.

    Every assertion here is about a *refusal*, and that is the honest shape: an anonymous checker
    can establish that nothing is reachable without a credential, and can establish nothing
    whatsoever about what happens once one is presented.
    """
    print("\nthis deployment verifies identity and publishes a login")
    for field in ("authorization_endpoint", "token_endpoint", "end_session_endpoint", "client_id"):
        check(f"it publishes {field}", bool(published.get(field)), "absent")
    check(
        "it asks for openid and nothing else",
        published.get("scopes") == "openid",
        f"got {published.get('scopes', '<none>')!r}",
    )
    # Both endpoints are derived from one configured hosted-UI origin, so a deployment whose login
    # and token exchange came from different pools is the failure worth catching -- and both are in
    # the same answer, which is what lets an anonymous checker see it. Nothing here is secret: the
    # public client id is published in every authorization request this deployment will ever send.
    authorize = str(published.get("authorization_endpoint", ""))
    token = str(published.get("token_endpoint", ""))
    check(
        "its authorize and token endpoints are the same hosted UI",
        authorize.endswith("/oauth2/authorize")
        and token.endswith("/oauth2/token")
        and authorize[: -len("/oauth2/authorize")] == token[: -len("/oauth2/token")],
        json.dumps(published),
    )

    print("\nnothing that touches a case is reachable without a token")
    unauthenticated: tuple[tuple[str, str, dict[str, Any]], ...] = (
        ("POST", "/api/sessions", {}),
        ("GET", "/api/cases", {}),
        ("GET", "/api/cases", {"session": "not-a-world-this-process-minted"}),
        ("GET", "/api/cases/demo-referral", {"session": "not-a-world-this-process-minted"}),
        ("POST", "/api/demo/reset", {}),
        ("POST", "/api/demo/scenarios/referral", {}),
    )
    for method, path, extra in unauthenticated:
        status, _, _ = call(base, path, method=method, **extra)
        check(f"{method} {path} is 401 with no token", status == 401, f"got {status}")

    # The header that used to be the whole of a caller's identity. Presenting one authenticates
    # nobody, so it is refused exactly as sending nothing is -- which is the property, rather than
    # the 400 a production route answers once a caller has authenticated and sent one anyway.
    status, _, _ = call(base, "/api/cases", caregiver="somebody-who-never-signed-in")
    check("a caller-supplied X-Caregiver authenticates nobody", status == 401, f"got {status}")

    status, _, _ = call(base, "/api/health")
    check("the unauthenticated health check still answers", status == 200, f"got {status}")


def verify_demo(base: str, published: dict[str, Any]) -> None:
    """The judge-facing synthetic world, driven end to end by an anonymous checker."""
    check(
        "a demo deployment publishes no login endpoint",
        "authorization_endpoint" not in published,
        "a demo mode that offered a login would be claiming to authenticate somebody",
    )

    print("\nthe API knows about worlds")
    status, _, _ = call(base, "/api/cases")
    check("a request naming no world is 400", status == 400, f"got {status}")
    status, _, _ = call(base, "/api/cases", session="not-a-world-this-process-minted")
    check("a request naming an unknown world is 410", status == 410, f"got {status}")

    print("\ntwo visitors, two worlds")
    first, second = mint(base), mint(base)
    check("two mints produce different ids", first != second)
    status, _, _ = call(base, "/api/cases", session=first)
    check("a minted world survives the CDN and answers 200", status == 200, f"got {status}")

    started = time.monotonic()
    status, _, _ = call(
        base,
        f"/api/demo/scenarios/referral?wait={RUN_TIMEOUT:.0f}",
        method="POST",
        session=first,
        timeout=RUN_TIMEOUT + 15,
    )
    check("visitor one runs the referral scenario", status == 202, f"got {status}")
    elapsed = time.monotonic() - started

    booked = settled_case(base, first, "demo-referral")
    check("their case reaches DONE", booked["state"] == "DONE", booked["state"])
    verifications = [
        event for event in booked["timeline"] if event["kind"].startswith("VERIFICATION")
    ]
    check("it was verified", bool(verifications))
    payload = verifications[-1]["payload"] if verifications else {}
    check(
        "the clinic was read back independently and matched",
        payload.get("read_back") == "true" and payload.get("observation") == "MATCHED",
        json.dumps(payload),
    )
    print(f"        (run settled in {elapsed:.1f}s)")

    status, _, _ = call(base, "/api/cases/demo-referral", session=second)
    check("the other visitor cannot see that case", status == 404, f"got {status}")

    print("\nthe clinic is per visitor too")
    _, mine, headers = call(base, f"/clinic/slots?session={first}")
    _, theirs, _ = call(base, f"/clinic/slots?session={second}")
    _, untouched, _ = call(base, "/clinic/slots")
    check(
        "/clinic/* is uncacheable at the edge",
        headers.get("cache-control") == "no-store",
        headers.get("cache-control", "<none>"),
    )
    check("the booking consumed a slot in their world", len(mine["slots"]) == 2, str(mine))
    check("the other visitor's world is untouched", len(theirs["slots"]) == 3, str(theirs))
    check("so is the world nobody named", len(untouched["slots"]) == 3, str(untouched))

    print("\na reset reaches one world only")
    status, cleared, _ = call(base, "/api/demo/reset", method="POST", session=second)
    check("the second visitor resets their own world", status == 200, f"got {status}")
    check(
        "and it had nothing of the first visitor's to clear",
        isinstance(cleared, dict) and cleared.get("cases_cleared") == 0,
        json.dumps(cleared),
    )
    still = settled_case(base, first, "demo-referral")
    check("the first visitor's case is still there", still["state"] == "DONE", still["state"])

    print("\nCareLoop owns the lifetime of a visitor's clinic")
    # The defect this closes: CareLoop expired a world by idle time and Demo World evicted by order
    # of *clinic* access, so a visitor polling the dashboard was alive on one side and the eviction
    # victim on the other — their appointment gone from the external system, and a read-back that
    # should have been MATCHED coming back ABSENT. Equal capacities never helped; the two disagreed
    # about order. Now CareLoop ends the clinic of every world it lets go, and Demo World removes a
    # world only when told to.
    #
    # **This covers the coupling half only, and says so.** The crossed-order reproduction needs a
    # world to age past the idle window, which is half an hour on this deployment — so it runs
    # locally in `tests/test_session_lifecycle.py`, against a real Demo World on a socket with a
    # clock a test can drive. What is asserted here is the thing that cannot be checked anywhere
    # else: that the *deployed pair* really are coupled. A Demo World deployed without the
    # lifecycle verb, or a CareLoop that stopped calling it, is a silent return to two owners, and
    # every unit test in the repository would stay green through it.
    #
    # Read off `POST /api/demo/reset`'s own answer rather than by calling Demo World's DELETE
    # through the distribution: CareLoop reaches the clinic over loopback, so whether the CDN
    # forwards that method is a fact about the CDN and not about the property.
    status, ended, _ = call(base, "/api/demo/reset", method="POST", session=first)
    check("the first visitor resets the world they booked in", status == 200, f"got {status}")
    check(
        "and CareLoop ended their clinic world rather than reseeding it",
        isinstance(ended, dict) and ended.get("clinic_world_ended") is True,
        json.dumps(ended),
    )

    # Non-vacuous in both directions: the field can say either thing, and it says the other one for
    # a visitor whose world has already gone. Without this, a route that answered `true`
    # unconditionally would pass the check above.
    status, again, _ = call(base, "/api/demo/reset", method="POST", session=first)
    check(
        "and a second reset reports there was nothing left to end",
        isinstance(again, dict) and again.get("clinic_world_ended") is False,
        json.dumps(again),
    )


def verify_the_shipped_frontend(base: str) -> None:
    """The shell and the bundle it names, which need no credential in either mode."""
    print("\nthe frontend that is served is the one that carries a session")
    status, _, headers = call(base, "/")
    check("the shell is served", status == 200, f"got {status}")
    check(
        "and it is revalidated rather than cached hard",
        headers.get("cache-control") == "no-cache",
        headers.get("cache-control", "<none>"),
    )
    shell = urllib.request.urlopen(f"{base}/", timeout=30).read().decode("utf-8", "replace")
    bundle = next(
        (
            chunk.split('"')[0]
            for chunk in shell.split('src="/')[1:]
            if chunk.startswith("assets/") and '.js"' in chunk[:80]
        ),
        "",
    )
    check("the shell names a fingerprinted bundle", bool(bundle), shell[:200])
    code = urllib.request.urlopen(f"{base}/{bundle}", timeout=30).read().decode("utf-8", "replace")
    check(
        "the shipped bundle mints and carries a world",
        "/api/sessions" in code and SESSION_HEADER in code,
        bundle,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Assert the deployed demo's properties.")
    parser.add_argument("base", nargs="?", default=DEFAULT_BASE, help="the distribution's URL")
    arguments = parser.parse_args()
    base = arguments.base.rstrip("/")

    try:
        verify(base)
    except Failed as broken:
        print(f"\nDEPLOY VERIFICATION FAILED: {broken}\n", file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError, OSError) as unreachable:
        print(
            f"\nDEPLOY VERIFICATION FAILED: {base} could not be reached: {unreachable}\n",
            file=sys.stderr,
        )
        return 2

    print("\nall deployed properties hold\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - a tool, not a call site
    sys.exit(main())
