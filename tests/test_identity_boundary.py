"""Where identity stops, and the two things that must never cross it.

**Authentication is not provenance.** A verified principal says *who may ask CareLoop to act*; a
:class:`~careloop.provenance.facts.Fact` says *why an argument may be sent somewhere*. Invariant 3
gives ``TRUSTED`` exactly one producer, and the reason this file exists is that an identity provider
is the most plausible second one anybody would build: a token carries an email, an email is a
destination, and one convenience later a claim would be deciding where an effect lands. So the
sweeps here refuse it structurally rather than by review.

**A credential stops at the API boundary.** The token, its claims, its issuer and its IdP subject
are read in :mod:`careloop.api.auth` and the OIDC verifier, and nowhere else. The graph, the tools,
the policy engine, the providers and the telemetry adapter may not name them. That is the same
arrangement ``tests/test_witness_seam.py`` uses to keep OpenTelemetry out of the domain, and it is
here for a sharper reason: a token that reached a provider payload or a span would be a credential
in somebody else's logs.

Both are AST sweeps over the shipped package rather than assertions about lines, so a module added
later is covered without this file being widened.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import careloop

#: Names that mean a credential, a claim set or an identity provider is being handled.
#:
#: Deliberately coarse. ``sub`` is not here because it is three letters and collides with too much
#: ordinary code; the subject is kept out by the narrower rule that nothing below the boundary may
#: name :func:`careloop.identity.directory.CaregiverDirectory.caregiver_for_subject`, which is the
#: only way a subject becomes anything.
CREDENTIAL_NAMES = frozenset(
    {
        "jwt",
        "jwks",
        "decode_jwt",
        "id_token",
        "access_token",
        "bearer_token",
        "_bearer_token",
        "AUTHORIZATION_HEADER",
        "caregiver_for_subject",
        "PyJWKClient",
        "OidcVerifier",
    }
)

#: The only modules allowed to name any of them.
#:
#: ``api/auth.py`` parses the scheme and hands the token on; ``identity/oidc.py`` verifies it;
#: ``identity/directory.py`` declares the subject mapping and is where ``caregiver_for_subject``
#: lives. Nothing else, and in particular nothing the graph, a tool or a provider can reach.
CREDENTIAL_HANDLERS = frozenset({"api/auth.py", "identity/oidc.py", "identity/directory.py"})

#: Layers that must not name a principal, a caregiver record or a grant at all.
#:
#: The graph is where ``open_case`` legitimately takes a :class:`Caregiver` to freeze ownership and
#: seed that caregiver's trusted configuration, so ``graph/`` is not swept wholesale -- the two
#: modules that may are listed in :data:`CAREGIVER_AWARE_GRAPH_MODULES` and the rest are swept
#: through :data:`IDENTITY_FREE_LAYERS`.
IDENTITY_FREE_LAYERS = ("policy", "tools", "agents", "llm", "provenance", "state", "events")

#: The graph modules that may know a caregiver exists.
#:
#: ``runner.py`` opens a case and freezes its owner; ``trusted.py`` composes the demo caregiver and
#: turns a caregiver's destinations into trusted facts. Every other graph module -- the nodes, the
#: builder, the conditions, extraction, verification, recovery, the witness seam -- must not, which
#: is what keeps *"recovery asks no model anything and consults no current authorisation"* and
#: *"the policy engine reads no principal"* structural.
CAREGIVER_AWARE_GRAPH_MODULES = frozenset({"graph/runner.py", "graph/trusted.py"})

IDENTITY_NAMES = frozenset({"Principal", "AuthMethod", "AuthBoundary", "may_act_for", "Grant"})


def package_root() -> Path:
    return Path(careloop.__file__).parent


def modules_under(*parts: str) -> list[Path]:
    root = package_root()
    found: list[Path] = []
    for part in parts:
        target = root / part
        found.extend(sorted(target.rglob("*.py")) if target.is_dir() else [target])
    return found


def names_used(source: str) -> set[str]:
    """Every bare name, attribute and imported alias, so an alias does not defeat the sweep."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            found.update(alias.name.split(".")[-1] for alias in node.names)
    return found


def relative(path: Path) -> str:
    return path.relative_to(package_root()).as_posix()


def all_modules() -> list[Path]:
    return sorted(package_root().rglob("*.py"))


class TestACredentialStopsAtTheBoundary:
    @pytest.mark.parametrize("module", all_modules(), ids=relative)
    def test_only_the_boundary_names_a_credential(self, module: Path) -> None:
        named = names_used(module.read_text(encoding="utf-8")) & CREDENTIAL_NAMES
        if not named:
            return
        assert relative(module) in CREDENTIAL_HANDLERS, (
            f"{relative(module)} names {sorted(named)}. A token, its claims and its "
            f"issuer are read at the API boundary and nowhere else; below it there is only a "
            f"caregiver_id"
        )

    def test_the_sweep_runs_over_the_whole_package(self) -> None:
        """Vacuity: an empty sweep passes every parametrised assertion over it."""
        assert len(all_modules()) > 40

    def test_the_matcher_catches_every_spelling(self) -> None:
        assert "jwt" in names_used("import jwt")
        assert "jwt" in names_used("from x import jwt")
        assert "decode_jwt" in names_used("helpers.decode_jwt(t)")


class TestIdentityDoesNotReachTheDecidingLayers:
    @pytest.mark.parametrize("module", modules_under(*IDENTITY_FREE_LAYERS), ids=relative)
    def test_no_deciding_layer_names_a_principal_or_a_grant(self, module: Path) -> None:
        named = names_used(module.read_text(encoding="utf-8")) & IDENTITY_NAMES
        assert not named, (
            f"{relative(module)} names {sorted(named)}. Caller authorisation and evidence "
            f"provenance are separate axes: the policy engine, the tools and the agents decide "
            f"what may happen to a case, never who asked"
        )

    @pytest.mark.parametrize("module", modules_under("graph"), ids=relative)
    def test_only_two_graph_modules_know_a_caregiver_exists(self, module: Path) -> None:
        if relative(module) in CAREGIVER_AWARE_GRAPH_MODULES:
            return
        named = names_used(module.read_text(encoding="utf-8")) & (
            IDENTITY_NAMES | {"Caregiver", "caregiver_id"}
        )
        assert not named, (
            f"{relative(module)} names {sorted(named)}. Ownership is frozen by runner.open_case "
            f"and read by the API; a node, an edge, verification or recovery that could see it "
            f"would be a lifecycle decision made on who is asking"
        )

    def test_the_graph_sweep_covers_the_nodes_and_the_recovery_engine(self) -> None:
        """Vacuity: the two modules that matter most are actually in the swept set."""
        swept = {relative(path) for path in modules_under("graph")} - CAREGIVER_AWARE_GRAPH_MODULES
        assert {"graph/nodes.py", "graph/recovery.py", "graph/verify.py"} <= swept


class TestIdentityCannotMintTrust:
    """The hard boundary. A verified token may never become a ``TRUSTED`` fact."""

    def test_trusted_facts_is_still_the_only_producer(self) -> None:
        producing = sorted(
            relative(path)
            for path in all_modules()
            if "TRUSTED" in names_used(path.read_text(encoding="utf-8"))
            and builds_a_trusted_fact(path.read_text(encoding="utf-8"))
        )

        assert producing == ["graph/trusted.py"], (
            f"{producing} construct a Fact at TRUSTED. Invariant 3 gives that exactly one "
            f"producer, and an identity provider is the most plausible second one anybody adds"
        )

    def test_the_trusted_config_module_reads_no_credential(self) -> None:
        source = (package_root() / "graph" / "trusted.py").read_text(encoding="utf-8")
        assert not (names_used(source) & CREDENTIAL_NAMES)

    def test_the_policy_rules_name_no_caregiver_predicate(self) -> None:
        """``rules.yaml`` reads neither a principal nor an owner, and must not learn to."""
        rules = (package_root() / "policy" / "rules.yaml").read_text(encoding="utf-8")
        for forbidden in ("caregiver_id:", "principal:", "auth_method:", "grant:"):
            assert forbidden not in rules, f"rules.yaml names {forbidden}"

    def test_the_policy_condition_vocabulary_did_not_grow_an_identity_clause(self) -> None:
        from careloop.policy.schema import Condition

        assert not (set(Condition.model_fields) & {"caregiver", "caregiver_id", "principal"})


def builds_a_trusted_fact(source: str) -> bool:
    """Whether this module calls ``Fact(...)`` with ``trust_level`` naming ``TRUSTED``.

    Narrower than mentioning the name, because reading a trust level, comparing one or annotating
    one is not producing one -- the resolver, the projections and the engine all legitimately say
    the word.
    """
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        callee = (
            node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        )
        if callee != "Fact":
            continue
        for keyword in node.keywords:
            if keyword.arg != "trust_level":
                continue
            value = keyword.value
            if isinstance(value, ast.Attribute) and value.attr == "TRUSTED":
                return True
            if isinstance(value, ast.Name) and value.id == "TRUSTED":
                return True
    return False
