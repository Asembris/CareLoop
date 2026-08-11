"""The tool registry owns argument classes; these tests hold it to that."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from careloop.provenance import ArgClass
from careloop.tools import REGISTRY, ArgSpec, DuplicateToolName, ToolRegistry, ToolSpec


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
