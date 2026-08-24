"""Choosing the identity mode from configuration, and refusing to guess.

**Three variables and no fallback.** ``CARELOOP_AUTH_MODE`` picks the mode; production additionally
requires ``CARELOOP_OIDC_ISSUER`` and ``CARELOOP_OIDC_AUDIENCE``. A production mode with either
missing raises :class:`IncompleteAuthConfiguration` **at startup** -- the arrangement
``validate_authority_coverage`` already has for an unguarded destination, and for the same reason: a
gap that stops the process is one somebody fixes, and a gap that degrades quietly is one nobody
finds.

**Absent means demo, and that is not a fallback.** An unset ``CARELOOP_AUTH_MODE`` is a CareLoop
nobody has configured for production -- a developer's laptop, the offline suite, a judge's browser
-- and the honest thing for it to be is the synthetic demo it has always been. What must never
happen, and cannot, is a *failed production verification* resolving to a demo identity: that is a
different question, settled one layer up by :class:`~careloop.api.auth.AuthBoundary` holding exactly
one verifier.

**None of these is a secret.** An issuer URL and a public client id are published in every token
this deployment will ever see. There is no client secret, because the browser client is a public
PKCE client and giving one a secret would be storing a credential in a page anybody can read.

Nothing here reads a file. Configuration reaches the library as environment variables, which keeps
CI's zero-secret contract checkable -- ``scripts/demo_serve.py`` is the one place a ``.env`` is
read, and it does that before this is asked anything.
"""

from __future__ import annotations

import os
import pathlib
from typing import Final

from careloop.identity.directory import (
    Caregiver,
    CaregiverDirectory,
    Grant,
    InMemoryCaregiverDirectory,
    SubjectMapping,
)

#: Which mode to run in. ``production`` or ``demo``; unset means demo.
AUTH_MODE_VAR: Final[str] = "CARELOOP_AUTH_MODE"

#: The OIDC issuer, exactly as it appears in a token's ``iss``. For Cognito,
#: ``https://cognito-idp.<region>.amazonaws.com/<pool-id>``.
ISSUER_VAR: Final[str] = "CARELOOP_OIDC_ISSUER"

#: The app client id, matched against ``client_id`` on an access token.
AUDIENCE_VAR: Final[str] = "CARELOOP_OIDC_AUDIENCE"

#: Where the browser is sent to sign in. The hosted-UI domain, origin only.
#:
#: A separate variable rather than derived from the issuer, because they are genuinely different
#: hosts: a Cognito user pool's issuer is ``cognito-idp.<region>.amazonaws.com/<pool>`` and its
#: hosted UI is ``<prefix>.auth.<region>.amazoncognito.com``. Deriving one from the other would be
#: a guess about AWS's naming, and the failure would be a login page that 404s.
#:
#: Optional. Without it the API reports production mode with no login endpoint, and a browser is
#: told plainly that it must obtain a token some other way -- which is the truthful answer for a
#: deployment whose clients are machines rather than people.
LOGIN_DOMAIN_VAR: Final[str] = "CARELOOP_OIDC_LOGIN_DOMAIN"

#: The scopes the browser asks for. Space-separated, OAuth's own form.
#:
#: ``openid`` because this is OIDC; nothing else, because CareLoop reads no claim but ``sub`` and
#: asking for ``email`` or ``profile`` would collect personal data with no consumer.
DEFAULT_SCOPES: Final[str] = "openid"

#: A JSON file holding the caregiver directory production resolves subjects against.
#:
#: A file rather than a table because the durable store this eventually wants is a decision of its
#: own, and a deployment that cannot start until somebody picks one is a deployment nobody starts.
#: :class:`~careloop.identity.directory.InMemoryCaregiverDirectory` is replaced the way
#: ``SqliteCaseStore`` replaces ``InMemoryCaseStore``: same protocol, no caller changes.
#:
#: **Not a secret, and it must not become one.** It holds caregiver ids, their trusted destinations,
#: their grants and the ``(issuer, subject)`` pairs those map from. A subject is an opaque IdP
#: identifier and an email address here is *configuration* -- the address CareLoop may write to --
#: rather than a credential. Nothing in it is a token, a password or a key, and if a future field
#: would be one it belongs somewhere else entirely.
CAREGIVERS_FILE_VAR: Final[str] = "CARELOOP_CAREGIVERS_FILE"

#: The value of :data:`AUTH_MODE_VAR` that means "verify a real token".
PRODUCTION_MODE: Final[str] = "production"

#: What an unset or explicitly-demo mode means.
DEMO_MODE: Final[str] = "demo"


class IncompleteAuthConfiguration(Exception):
    """Raised at startup when production is asked for and cannot be built.

    Not a warning and not a degradation. An app that started anyway would be an app serving
    production traffic with nobody verifying it, which is the one shape this whole boundary exists
    to make unreachable.
    """


class UnknownAuthMode(Exception):
    """Raised when ``CARELOOP_AUTH_MODE`` is set to something that is neither mode.

    Refused rather than treated as demo. A typo in a deployment variable must not be the difference
    between authenticating and not, and *"anything I do not recognise means the safe-looking one"*
    is how a production deployment silently becomes a demo.
    """


def configured_mode(environ: dict[str, str] | None = None) -> str:
    """Which mode this environment asks for.

    Raises:
        UnknownAuthMode: if the variable is set to anything but the two known values.
    """
    source = os.environ if environ is None else environ
    asked = (source.get(AUTH_MODE_VAR) or DEMO_MODE).strip().lower()
    if asked not in (PRODUCTION_MODE, DEMO_MODE):
        raise UnknownAuthMode(
            f"{AUTH_MODE_VAR}={asked!r} is neither {PRODUCTION_MODE!r} nor {DEMO_MODE!r}; a typo "
            f"here must not be the difference between authenticating and not"
        )
    return asked


def wants_production(environ: dict[str, str] | None = None) -> bool:
    """Whether this environment asks for a verified principal."""
    return configured_mode(environ) == PRODUCTION_MODE


def auth_from_environment(
    directory: CaregiverDirectory | None = None,
    environ: dict[str, str] | None = None,
):
    """The identity boundary this environment describes.

    Demo needs nothing and gets the synthetic caregiver. Production needs an issuer, an audience and
    a directory, and gets a real verifier over the issuer's JWKS -- constructed lazily, so building
    the app reaches no network.

    Raises:
        IncompleteAuthConfiguration: if production is asked for without an issuer, an audience or a
            directory.
        UnknownAuthMode: if the mode is neither.
    """
    from careloop.api.auth import demo_auth, production_auth

    source = os.environ if environ is None else environ
    if not wants_production(source):
        return demo_auth()

    if directory is None:
        directory = directory_from_environment(source)

    issuer = (source.get(ISSUER_VAR) or "").strip()
    audience = (source.get(AUDIENCE_VAR) or "").strip()
    required = ((ISSUER_VAR, issuer), (AUDIENCE_VAR, audience))
    missing = [name for name, value in required if not value]
    if missing:
        raise IncompleteAuthConfiguration(
            f"{AUTH_MODE_VAR}={PRODUCTION_MODE} needs {', '.join(missing)}. Production identity is "
            f"not something to start without and then discover"
        )
    if directory is None:
        raise IncompleteAuthConfiguration(
            f"{AUTH_MODE_VAR}={PRODUCTION_MODE} needs a caregiver directory: a verified token "
            f"names a subject, and without a directory there is nothing to resolve it into. "
            f"Set {CAREGIVERS_FILE_VAR}, or pass one in"
        )

    from careloop.identity.oidc import ACCESS_TOKEN, cognito_verifier

    # **``token_use`` is named here rather than left to a default**, and the difference is what a
    # test can pin. Measured before it was: flipping ``cognito_verifier``'s default to ``"id"`` --
    # which would make every deployment accept ID tokens as API credentials and refuse every access
    # token -- turned 0 of 100 tests red. A default is correct behaviour nobody is holding in place.
    #
    # Access and not ID, because these are genuinely different credentials. A Cognito access token
    # carries ``client_id`` and no ``aud`` at all; an ID token carries ``aud`` and is minted for a
    # user agent to read, not for an API to be called with. Accepting either would make the audience
    # check ambiguous -- see :data:`careloop.identity.oidc.AUDIENCE_CLAIM`.
    return production_auth(
        verifier=cognito_verifier(
            issuer=issuer, audience=audience, directory=directory, token_use=ACCESS_TOKEN
        ),
        directory=directory,
    )


class MalformedDirectory(Exception):
    """Raised when the caregivers file cannot be read as a directory.

    Refused at startup rather than skipped. A production CareLoop whose directory failed to load
    would authenticate every caller and then resolve none of them -- every request a 401, for a
    reason nothing on the surface explains.
    """


def directory_from_file(path: str) -> InMemoryCaregiverDirectory:
    """Read the caregiver directory from a JSON file.

    The shape, in full:

    .. code-block:: json

        {
          "caregivers": [
            {"caregiver_id": "cg-rosa",
             "active": true,
             "destinations": {"caregiver_email": "rosa@carer.example",
                              "provider_id": "PROV-NORTHGATE-CARDIOLOGY",
                              "portal_id": "PORTAL-NORTHGATE-RECORDS"}}
          ],
          "grants": [{"caregiver_id": "cg-rosa", "patient_id": "PT-4417", "active": true}],
          "subjects": [
            {"issuer": "https://cognito-idp...", "subject": "...", "caregiver_id": "cg-rosa"}
          ]
        }

    ``active`` defaults to ``true`` on both records, because the ordinary entry is a live one and a
    file that had to say so everywhere is a file where a missing ``false`` is invisible.

    Raises:
        MalformedDirectory: if the file is missing, is not JSON, or a record lacks its identifiers.
    """
    import json

    try:
        raw = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise MalformedDirectory(
            f"{CAREGIVERS_FILE_VAR} names {path!r}, which does not exist"
        ) from None
    except (OSError, ValueError) as unreadable:
        # The message is deliberately not interpolated with the file's contents: a parse error can
        # quote the line it failed on, and that line is somebody's identifier.
        raise MalformedDirectory(
            f"{CAREGIVERS_FILE_VAR} names {path!r}, which could not be read as JSON "
            f"({type(unreadable).__name__})"
        ) from None

    if not isinstance(raw, dict):
        raise MalformedDirectory(f"{path!r} is not a JSON object")

    try:
        caregivers = {
            str(entry["caregiver_id"]): Caregiver(
                caregiver_id=str(entry["caregiver_id"]),
                destinations=tuple(
                    (str(key), str(value))
                    for key, value in dict(entry.get("destinations") or {}).items()
                ),
                active=bool(entry.get("active", True)),
            )
            for entry in raw.get("caregivers") or ()
        }
        grants = tuple(
            Grant(
                caregiver_id=str(entry["caregiver_id"]),
                patient_id=str(entry["patient_id"]),
                active=bool(entry.get("active", True)),
            )
            for entry in raw.get("grants") or ()
        )
        subjects = tuple(
            SubjectMapping(
                issuer=str(entry["issuer"]),
                subject=str(entry["subject"]),
                caregiver_id=str(entry["caregiver_id"]),
            )
            for entry in raw.get("subjects") or ()
        )
    except (KeyError, TypeError, AttributeError) as incomplete:
        raise MalformedDirectory(
            f"a record in {path!r} is missing a required field ({type(incomplete).__name__})"
        ) from None

    named = {grant.caregiver_id for grant in grants} | {
        mapping.caregiver_id for mapping in subjects
    }
    unknown = sorted(named - set(caregivers))
    if unknown:
        # A grant or a mapping naming nobody is a silent no-op that reads as working configuration,
        # which is exactly the shape that gets deployed and discovered by somebody being refused.
        raise MalformedDirectory(f"{path!r} names caregiver(s) it does not declare: {unknown}")

    return InMemoryCaregiverDirectory(caregivers=caregivers, grants=grants, subjects=subjects)


def directory_from_environment(environ: dict[str, str] | None = None):
    """The directory this environment describes, or ``None`` if it describes none.

    ``None`` is only ever a demo answer: :func:`auth_from_environment` refuses production without
    one, so an unset file in production stops the process rather than starting it directoryless.
    """
    source = os.environ if environ is None else environ
    path = (source.get(CAREGIVERS_FILE_VAR) or "").strip()
    return directory_from_file(path) if path else None


def browser_login(environ: dict[str, str] | None = None) -> dict[str, str]:
    """What a browser needs to obtain a token, or ``{}`` if this deployment offers no login.

    Public configuration in full: an authorization endpoint, a token endpoint, a logout endpoint,
    a public client id and the scopes to ask for. **No secret**, because the browser client is a
    public PKCE client and one that had a secret would be storing it in a page anybody can read.

    Endpoints are derived from the hosted-UI origin using Cognito's documented paths, so there is
    one variable to set rather than four to keep in step.
    """
    source = os.environ if environ is None else environ
    domain = (source.get(LOGIN_DOMAIN_VAR) or "").strip().rstrip("/")
    audience = (source.get(AUDIENCE_VAR) or "").strip()
    if not domain or not audience:
        return {}
    return {
        "authorization_endpoint": f"{domain}/oauth2/authorize",
        "token_endpoint": f"{domain}/oauth2/token",
        "end_session_endpoint": f"{domain}/logout",
        "client_id": audience,
        "scopes": DEFAULT_SCOPES,
    }


__all__ = [
    "AUDIENCE_VAR",
    "AUTH_MODE_VAR",
    "CAREGIVERS_FILE_VAR",
    "DEFAULT_SCOPES",
    "DEMO_MODE",
    "ISSUER_VAR",
    "LOGIN_DOMAIN_VAR",
    "PRODUCTION_MODE",
    "IncompleteAuthConfiguration",
    "MalformedDirectory",
    "UnknownAuthMode",
    "auth_from_environment",
    "browser_login",
    "configured_mode",
    "directory_from_environment",
    "directory_from_file",
    "wants_production",
]
