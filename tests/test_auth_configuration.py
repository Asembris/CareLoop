"""P8 -- production cannot be entered by accident, and cannot be left by accident either.

Patch 9A. Three environment variables decide which identity story a deployment tells, and every way
of getting them wrong stops the process rather than degrading it.

**A typo does not mean demo.** ``CARELOOP_AUTH_MODE=prod`` -- or ``Production``, or a stray space --
is refused, because *"anything I do not recognise means the safe-looking one"* is exactly how a
production deployment silently becomes anonymous. Only the two known values are accepted, and
``demo`` is what *absence* means, which is a different statement.

**Absent configuration is not a fallback.** An unset mode is a CareLoop nobody configured for
production -- a laptop, the offline suite, a judge's browser -- and the demo is the honest thing for
it to be. What cannot happen is a *failed verification* resolving to a demo identity, which is
settled one layer up by the boundary holding exactly one verifier and is proved in
``tests/test_principal_seam.py``.

**None of the three is a secret**, and there is deliberately no client-secret variable at all: the
browser client is a public PKCE client, and a secret in a page anybody can read is not a secret.
"""

from __future__ import annotations

import pytest
from identity_helpers import two_caregiver_directory
from oidc_helpers import AUDIENCE, ISSUER

from careloop.api.auth import AuthMode
from careloop.identity.config import (
    AUDIENCE_VAR,
    AUTH_MODE_VAR,
    DEMO_MODE,
    ISSUER_VAR,
    PRODUCTION_MODE,
    IncompleteAuthConfiguration,
    UnknownAuthMode,
    auth_from_environment,
    configured_mode,
    wants_production,
)

PRODUCTION_ENV = {
    AUTH_MODE_VAR: PRODUCTION_MODE,
    ISSUER_VAR: ISSUER,
    AUDIENCE_VAR: AUDIENCE,
}


class TestTheModeIsReadExactly:
    def test_absent_means_demo(self) -> None:
        assert configured_mode({}) == DEMO_MODE
        assert not wants_production({})

    def test_empty_means_demo(self) -> None:
        assert configured_mode({AUTH_MODE_VAR: ""}) == DEMO_MODE

    def test_production_means_production(self) -> None:
        assert wants_production({AUTH_MODE_VAR: PRODUCTION_MODE})

    def test_case_and_whitespace_are_tolerated(self) -> None:
        """Not laxity: these are the same word, and a deployment file is edited by hand."""
        assert wants_production({AUTH_MODE_VAR: "  PRODUCTION "})

    @pytest.mark.parametrize(
        "asked",
        ["prod", "production-mode", "live", "true", "1", "demo-mode", "off"],
        ids=lambda value: value,
    )
    def test_anything_else_is_refused_rather_than_treated_as_demo(self, asked: str) -> None:
        with pytest.raises(UnknownAuthMode):
            configured_mode({AUTH_MODE_VAR: asked})

    def test_the_refusal_names_both_known_values(self) -> None:
        """So an operator who typed one wrong is told what to type instead."""
        with pytest.raises(UnknownAuthMode) as raised:
            configured_mode({AUTH_MODE_VAR: "prod"})

        assert PRODUCTION_MODE in str(raised.value)
        assert DEMO_MODE in str(raised.value)


class TestProductionRefusesToStartHalfConfigured:
    @pytest.mark.parametrize("missing", [ISSUER_VAR, AUDIENCE_VAR], ids=["issuer", "audience"])
    def test_a_missing_variable_stops_the_process(self, missing: str) -> None:
        environ = {name: value for name, value in PRODUCTION_ENV.items() if name != missing}

        with pytest.raises(IncompleteAuthConfiguration, match=missing):
            auth_from_environment(directory=two_caregiver_directory(), environ=environ)

    def test_a_blank_variable_counts_as_missing(self) -> None:
        with pytest.raises(IncompleteAuthConfiguration, match=ISSUER_VAR):
            auth_from_environment(
                directory=two_caregiver_directory(),
                environ={**PRODUCTION_ENV, ISSUER_VAR: "   "},
            )

    def test_both_missing_are_both_reported(self) -> None:
        with pytest.raises(IncompleteAuthConfiguration) as raised:
            auth_from_environment(
                directory=two_caregiver_directory(), environ={AUTH_MODE_VAR: PRODUCTION_MODE}
            )

        assert ISSUER_VAR in str(raised.value)
        assert AUDIENCE_VAR in str(raised.value)

    def test_production_without_a_directory_stops_the_process(self) -> None:
        """A token names a subject; with no directory there is nothing to resolve it into."""
        with pytest.raises(IncompleteAuthConfiguration, match="directory"):
            auth_from_environment(directory=None, environ=PRODUCTION_ENV)

    def test_no_failure_here_produces_a_demo_boundary(self) -> None:
        """The property, stated as the thing that must not be returned."""
        for environ in (
            {AUTH_MODE_VAR: PRODUCTION_MODE},
            {**PRODUCTION_ENV, ISSUER_VAR: ""},
            {**PRODUCTION_ENV, AUDIENCE_VAR: ""},
        ):
            with pytest.raises(IncompleteAuthConfiguration):
                auth_from_environment(directory=two_caregiver_directory(), environ=environ)


class TestAFullyConfiguredProductionBoundaryIsBuildable:
    """Vacuity: the refusals above are about what is missing, not about production being broken."""

    def test_it_is_a_production_boundary(self) -> None:
        built = auth_from_environment(directory=two_caregiver_directory(), environ=PRODUCTION_ENV)

        assert built.mode is AuthMode.PRODUCTION
        assert built.is_production

    def test_it_holds_a_verifier_over_the_configured_issuer(self) -> None:
        built = auth_from_environment(directory=two_caregiver_directory(), environ=PRODUCTION_ENV)

        assert built.verifier.issuer == ISSUER
        assert built.verifier.audience == AUDIENCE

    def test_building_it_reaches_no_network(self) -> None:
        built = auth_from_environment(directory=two_caregiver_directory(), environ=PRODUCTION_ENV)

        assert built.verifier.keys._client is None

    def test_it_verifies_a_real_token_end_to_end(self) -> None:
        """The claim policy is the production one; only the key transport is swapped below."""
        from oidc_helpers import PUBLIC_KEY, StaticSigningKeys, token

        from careloop.identity.oidc import OidcVerifier

        built = auth_from_environment(directory=two_caregiver_directory(), environ=PRODUCTION_ENV)
        assert isinstance(built.verifier, OidcVerifier)

        offline = built.verifier.__class__(
            issuer=built.verifier.issuer,
            audience=built.verifier.audience,
            directory=built.verifier.directory,
            keys=StaticSigningKeys(PUBLIC_KEY),
        )
        assert offline.verify(token()).caregiver_id == "cg-alma"


class TestADemoEnvironmentIsUnchanged:
    def test_an_empty_environment_gives_the_demo_boundary(self) -> None:
        built = auth_from_environment(environ={})

        assert built.mode is AuthMode.DEMO
        assert not built.is_production

    def test_demo_needs_no_issuer_or_audience(self) -> None:
        assert auth_from_environment(environ={AUTH_MODE_VAR: DEMO_MODE}).mode is AuthMode.DEMO

    def test_demo_ignores_a_leftover_issuer(self) -> None:
        """A deployment switched back to demo does not half-authenticate."""
        built = auth_from_environment(environ={AUTH_MODE_VAR: DEMO_MODE, ISSUER_VAR: ISSUER})

        assert built.mode is AuthMode.DEMO


class TestTheDeploymentFileSaysAllOfThis:
    def test_every_variable_appears_in_careloop_env(self) -> None:
        from pathlib import Path

        text = Path("deploy/careloop.env").read_text(encoding="utf-8")
        for name in (AUTH_MODE_VAR, ISSUER_VAR, AUDIENCE_VAR):
            assert f"{name}=" in text, f"{name} is not in deploy/careloop.env"

    def test_no_client_secret_variable_exists_anywhere(self) -> None:
        """A public PKCE client has no secret, and a page anybody can read cannot keep one."""
        from pathlib import Path

        text = Path("deploy/careloop.env").read_text(encoding="utf-8").upper()
        for forbidden in ("CLIENT_SECRET", "OIDC_SECRET", "COGNITO_SECRET"):
            assert f"{forbidden}=" not in text

    def test_the_shipped_file_is_still_demo(self) -> None:
        """The committed deployment file must not be the thing that switches production on."""
        from pathlib import Path

        text = Path("deploy/careloop.env").read_text(encoding="utf-8")
        assert f"{AUTH_MODE_VAR}={DEMO_MODE}" in text
