"""The tool registry owns argument classes; these tests hold it to that."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from careloop.provenance import ArgClass
from careloop.tools import REGISTRY, ArgSpec, DuplicateToolName, ToolRegistry, ToolSpec
from careloop.tools.registry import (
    AUTHORED_VALUES,
    ArgAuthor,
    UndeclaredAuthorship,
    UnidentifiableAction,
    check_authorship_is_declared,
    check_identity_is_usable,
)


def _spec(name: str = "t", external_effect: bool = False) -> ToolSpec:
    return ToolSpec(
        name=name,
        external_effect=external_effect,
        args=(ArgSpec(name="a", arg_class=ArgClass.VALUE),),
    )


class TestToolSpec:
    def test_arg_class_returns_the_declared_class(self) -> None:
        spec = ToolSpec(
            name="send",
            external_effect=True,
            args=(
                ArgSpec(name="to", arg_class=ArgClass.DESTINATION),
                ArgSpec(name="body", arg_class=ArgClass.VALUE),
            ),
        )
        assert spec.arg_class("to") is ArgClass.DESTINATION
        assert spec.arg_class("body") is ArgClass.VALUE

    def test_arg_class_of_undeclared_argument_is_none(self) -> None:
        assert _spec().arg_class("nope") is None

    def test_arg_names_lists_every_declared_argument(self) -> None:
        assert REGISTRY.get("send_email").arg_names == {"to", "subject", "body"}

    def test_args_of_class_filters_by_class(self) -> None:
        grant = REGISTRY.get("grant_portal_access")
        assert grant.args_of_class(ArgClass.AUTHORITY) == {"role"}
        assert grant.args_of_class(ArgClass.DESTINATION) == {"grantee", "portal_id"}

    def test_duplicate_argument_names_are_rejected(self) -> None:
        with pytest.raises(ValidationError, match="declares argument 'a' twice"):
            ToolSpec(
                name="t",
                external_effect=False,
                args=(
                    ArgSpec(name="a", arg_class=ArgClass.VALUE),
                    ArgSpec(name="a", arg_class=ArgClass.DESTINATION),
                ),
            )

    def test_a_tool_must_declare_at_least_one_argument(self) -> None:
        with pytest.raises(ValidationError):
            ToolSpec(name="t", external_effect=False, args=())

    def test_specs_are_frozen(self) -> None:
        with pytest.raises(ValidationError):
            _spec().name = "other"


class TestToolRegistry:
    def test_duplicate_tool_names_are_rejected(self) -> None:
        with pytest.raises(DuplicateToolName, match="'dup' is declared twice"):
            ToolRegistry([_spec("dup"), _spec("dup")])

    def test_get_of_unknown_tool_is_none(self) -> None:
        assert REGISTRY.get("no_such_tool") is None

    def test_membership_and_length(self) -> None:
        registry = ToolRegistry([_spec("one"), _spec("two")])
        assert "one" in registry
        assert "three" not in registry
        assert len(registry) == 2
        assert {tool.name for tool in registry} == {"one", "two"}

    def test_external_effect_tools_selects_only_effectful_tools(self) -> None:
        registry = ToolRegistry([_spec("read", False), _spec("write", True)])
        assert registry.external_effect_tools == {"write"}

    def test_arg_class_lookup_tolerates_unknown_tool_and_argument(self) -> None:
        assert REGISTRY.arg_class("send_email", "to") is ArgClass.DESTINATION
        assert REGISTRY.arg_class("no_such_tool", "to") is None
        assert REGISTRY.arg_class("send_email", "no_such_arg") is None


class TestShippedRegistry:
    def test_every_destination_and_authority_argument_is_declared_deliberately(self) -> None:
        """Pin the classes that carry the trust model, so a silent reclassification fails here."""
        assert REGISTRY.arg_class("send_email", "to") is ArgClass.DESTINATION
        assert REGISTRY.arg_class("book_appointment", "provider_id") is ArgClass.DESTINATION
        assert REGISTRY.arg_class("upload_document", "portal_id") is ArgClass.DESTINATION
        assert REGISTRY.arg_class("grant_portal_access", "grantee") is ArgClass.DESTINATION
        assert REGISTRY.arg_class("grant_portal_access", "portal_id") is ArgClass.DESTINATION
        assert REGISTRY.arg_class("grant_portal_access", "role") is ArgClass.AUTHORITY

    def test_the_system_has_at_least_one_authority_argument(self) -> None:
        """``AUTHORITY`` must have a real user, or the third leg of the trust model is theory."""
        authority = {
            (tool.name, arg) for tool in REGISTRY for arg in tool.args_of_class(ArgClass.AUTHORITY)
        }
        assert authority == {("grant_portal_access", "role")}

    def test_external_effect_flags_are_as_declared(self) -> None:
        assert REGISTRY.external_effect_tools == {
            "send_email",
            "book_appointment",
            "upload_document",
            "grant_portal_access",
        }
        assert REGISTRY.get("fetch_document").external_effect is False


class TestAuthorshipMetadata:
    """Who composes a value is declared here, and it is not a trust level."""

    def test_exactly_one_shipped_argument_is_system_authored(self) -> None:
        """``book_appointment.reason`` and nothing else, which is the whole shipped claim."""
        assert REGISTRY.system_authored == {("book_appointment", "reason")}
        for tool in REGISTRY:
            expected = {"reason"} if tool.name == "book_appointment" else frozenset()
            assert tool.system_authored_args == expected, tool.name
            assert tool.model_authored_args == tool.arg_names - expected, tool.name

    def test_the_authored_text_makes_no_claim_careloop_cannot_prove(self) -> None:
        """A fixed literal, and one that asserts nothing clinical.

        Nothing in CareLoop produces a ``CONTROLLED`` fact for *why* an appointment is needed —
        there is no ``purpose`` key, no validator and no promotion — so a richer sentence would be
        inventing information. The interpolation check is the one that matters: a value assembled
        from case content would be a value a document could steer.
        """
        assert AUTHORED_VALUES == {
            ("book_appointment", "reason"): "CareLoop appointment scheduling request."
        }
        for text in AUTHORED_VALUES.values():
            assert "{" not in text and "}" not in text and "%" not in text

    def test_author_defaults_to_model_so_a_new_argument_behaves_as_an_old_one(self) -> None:
        assert ArgSpec(name="a", arg_class=ArgClass.VALUE).author is ArgAuthor.MODEL

    def test_author_reads_the_declaration(self) -> None:
        spec = ToolSpec(
            name="t",
            external_effect=True,
            args=(
                ArgSpec(name="a", arg_class=ArgClass.VALUE),
                ArgSpec(name="b", arg_class=ArgClass.VALUE, author=ArgAuthor.SYSTEM),
            ),
        )
        assert spec.author("a") is ArgAuthor.MODEL
        assert spec.author("b") is ArgAuthor.SYSTEM
        assert spec.author("nope") is None
        assert spec.system_authored_args == {"b"}
        assert spec.model_authored_args == {"a"}

    def test_a_system_authored_argument_with_no_declared_value_is_refused(self) -> None:
        registry = ToolRegistry(
            [
                ToolSpec(
                    name="t",
                    external_effect=True,
                    args=(ArgSpec(name="a", arg_class=ArgClass.VALUE, author=ArgAuthor.SYSTEM),),
                )
            ]
        )
        with pytest.raises(UndeclaredAuthorship, match="have no declared value"):
            check_authorship_is_declared(registry, {})

    def test_a_declared_value_for_a_model_authored_argument_is_refused(self) -> None:
        registry = ToolRegistry(
            [
                ToolSpec(
                    name="t",
                    external_effect=True,
                    args=(ArgSpec(name="a", arg_class=ArgClass.VALUE),),
                )
            ]
        )
        with pytest.raises(UndeclaredAuthorship, match="which no tool declares SYSTEM-authored"):
            check_authorship_is_declared(registry, {("t", "a"): "text"})

    @pytest.mark.parametrize("arg_class", [ArgClass.DESTINATION, ArgClass.AUTHORITY])
    def test_careloop_may_never_compose_a_destination_or_an_authority(
        self, arg_class: ArgClass
    ) -> None:
        registry = ToolRegistry(
            [
                ToolSpec(
                    name="t",
                    external_effect=True,
                    args=(ArgSpec(name="a", arg_class=arg_class, author=ArgAuthor.SYSTEM),),
                )
            ]
        )
        with pytest.raises(UndeclaredAuthorship, match="must come from trusted config"):
            check_authorship_is_declared(registry, {("t", "a"): "text"})

    def test_a_value_class_system_authored_argument_is_accepted(self) -> None:
        registry = ToolRegistry(
            [
                ToolSpec(
                    name="t",
                    external_effect=True,
                    args=(ArgSpec(name="a", arg_class=ArgClass.VALUE, author=ArgAuthor.SYSTEM),),
                )
            ]
        )
        check_authorship_is_declared(registry, {("t", "a"): "text"})

    def test_the_shipped_registry_satisfies_the_check(self) -> None:
        check_authorship_is_declared()


class TestIdentityMetadata:
    """Which values define the same logical action is declared here too, and separately."""

    def test_exactly_one_shipped_argument_does_not_identify(self) -> None:
        """``book_appointment.reason``, and the other twelve arguments are unchanged."""
        for tool in REGISTRY:
            excluded = {"reason"} if tool.name == "book_appointment" else frozenset()
            assert tool.identifying_args == tool.arg_names - excluded, tool.name

    def test_identifies_defaults_to_true_so_a_new_argument_behaves_as_an_old_one(self) -> None:
        assert ArgSpec(name="a", arg_class=ArgClass.VALUE).identifies is True

    def test_identifying_args_excludes_what_opted_out(self) -> None:
        spec = ToolSpec(
            name="t",
            external_effect=True,
            args=(
                ArgSpec(name="a", arg_class=ArgClass.VALUE),
                ArgSpec(name="b", arg_class=ArgClass.VALUE, identifies=False),
            ),
        )
        assert spec.identifying_args == {"a"}
        assert spec.arg_names == {"a", "b"}

    def test_the_registry_answers_for_a_named_tool_and_empties_for_an_unknown_one(self) -> None:
        assert REGISTRY.identifying_args("book_appointment") == {
            "provider_id",
            "patient_id",
            "slot_start",
        }
        assert REGISTRY.identifying_args("no_such_tool") == frozenset()

    def test_a_tool_that_identifies_nothing_is_refused(self) -> None:
        registry = ToolRegistry(
            [
                ToolSpec(
                    name="t",
                    external_effect=True,
                    args=(ArgSpec(name="a", arg_class=ArgClass.VALUE, identifies=False),),
                )
            ]
        )
        with pytest.raises(UnidentifiableAction, match="declares no identifying argument"):
            check_identity_is_usable(registry)

    @pytest.mark.parametrize("arg_class", [ArgClass.DESTINATION, ArgClass.AUTHORITY])
    def test_a_destination_or_authority_may_never_leave_identity(self, arg_class: ArgClass) -> None:
        registry = ToolRegistry(
            [
                ToolSpec(
                    name="t",
                    external_effect=True,
                    args=(
                        ArgSpec(name="keep", arg_class=ArgClass.VALUE),
                        ArgSpec(name="a", arg_class=arg_class, identifies=False),
                    ),
                )
            ]
        )
        with pytest.raises(UnidentifiableAction, match="not the same logical action"):
            check_identity_is_usable(registry)

    def test_a_value_class_argument_may_leave_identity(self) -> None:
        registry = ToolRegistry(
            [
                ToolSpec(
                    name="t",
                    external_effect=True,
                    args=(
                        ArgSpec(name="keep", arg_class=ArgClass.VALUE),
                        ArgSpec(name="a", arg_class=ArgClass.VALUE, identifies=False),
                    ),
                )
            ]
        )
        check_identity_is_usable(registry)

    def test_the_shipped_registry_satisfies_the_check(self) -> None:
        check_identity_is_usable()
