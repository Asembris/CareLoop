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
from typing import Final

from careloop.identity.directory import CaregiverDirectory

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
            f"names a subject, and without a directory there is nothing to resolve it into"
        )

    from careloop.identity.oidc import cognito_verifier

    return production_auth(
        verifier=cognito_verifier(issuer=issuer, audience=audience, directory=directory),
        directory=directory,
    )


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
    "DEFAULT_SCOPES",
    "DEMO_MODE",
    "ISSUER_VAR",
    "LOGIN_DOMAIN_VAR",
    "PRODUCTION_MODE",
    "IncompleteAuthConfiguration",
    "UnknownAuthMode",
    "auth_from_environment",
    "browser_login",
    "configured_mode",
    "wants_production",
]
