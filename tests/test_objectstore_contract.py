"""The measured S3 contract, re-run offline against the substitute every time the suite runs.

This file asserts **nothing of its own**. Every check comes from
:mod:`scripts.objectstore_contract`, the same module an operator points at real Amazon S3, and the
only difference between the two runs is which endpoint the boto3 client was built for. A second
list of expectations here would be a second answer to the question
[`docs/objectstore-contract.md`](../docs/objectstore-contract.md) exists to record one answer to.

**What this does and does not establish.** It does not prove anything about S3 — nothing offline
can. What it proves is that the substitute the rest of the suite runs against reproduces the
behaviours that *were* measured, so a test elsewhere asserting "a replay is refused" is asserting it
against a store that refuses the way the provider does. When the substitute drifts, an operator's
run of the same module goes red and names the check.

Each contract check is a separate test, so a drift reports as *which behaviour moved* rather than as
one opaque failure.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from objectstore_helpers import BUCKET, object_store_client, object_store_server
from test_demoworld import imports_matching

import careloop
import objectstore
import scripts.objectstore_contract
from objectstore.store import ObjectStore
from scripts.objectstore_contract import Check, run_contract

PREFIX = "careloop-contract"


@pytest.fixture(scope="module")
def contract_report() -> list[Check]:
    """Run the whole contract once, on a real socket, through a real boto3 client.

    Module-scoped because the sequence is deliberately destructive and ordered — the overwrite check
    really overwrites and the absence check really deletes — so it is one run inspected many times
    rather than many runs of one check.
    """
    with object_store_server(ObjectStore()) as (endpoint, _store):
        client = object_store_client(endpoint)
        return run_contract(client, BUCKET, PREFIX).checks


def _named(report: list[Check], name: str) -> Check:
    for check in report:
        if check.name == name:
            return check
    raise AssertionError(f"the contract module no longer runs a check called {name!r}")


CONTRACT_CHECKS = (
    "create-once-on-absent-key",
    "create-returns-a-checksum",
    "same-key-same-bytes-is-refused",
    "same-key-divergent-bytes-is-refused",
    "the-refused-write-changed-nothing",
    "fresh-read-returns-the-stored-bytes",
    "metadata-round-trips",
    "reported-checksum-corroborates",
    "exactly-one-object-under-the-key",
    "without-the-precondition-it-overwrites",
    "delete-is-accepted",
    "absent-key-answers-no-such-key",
)


class TestTheSubstituteReproducesTheMeasuredContract:
    @pytest.mark.parametrize("name", CONTRACT_CHECKS)
    def test_the_check_is_satisfied(self, contract_report: list[Check], name: str) -> None:
        check = _named(contract_report, name)
        assert check.satisfied, f"{name}: {check.observed}"

    def test_every_check_the_module_runs_is_named_here(self, contract_report: list[Check]) -> None:
        """Vacuity, in the direction that matters.

        Parametrising over a hand-written list means a check *added* to the contract module would be
        run and then ignored. This is what fails instead — and it is why the list is spelled out
        rather than derived: a derived list would make the assertions self-satisfying.
        """
        assert tuple(check.name for check in contract_report) == CONTRACT_CHECKS


class TestTheSubstituteIsNotVacuous:
    """A store that accepted everything, or refused everything, would satisfy nothing above."""

    def test_the_conditional_create_is_what_refuses_the_second_write(self) -> None:
        """Remove the precondition and the same second write succeeds. The measured asymmetry."""
        with object_store_server(ObjectStore()) as (endpoint, _store):
            client = object_store_client(endpoint)
            key = f"{PREFIX}/asymmetry"

            client.put_object(Bucket=BUCKET, Key=key, Body=b"first", IfNoneMatch="*")

            with pytest.raises(Exception) as refused:
                client.put_object(Bucket=BUCKET, Key=key, Body=b"second", IfNoneMatch="*")
            assert _code(refused.value) == "PreconditionFailed"

            unconditional = client.put_object(Bucket=BUCKET, Key=key, Body=b"second")
            assert unconditional["ResponseMetadata"]["HTTPStatusCode"] == 200
            assert client.get_object(Bucket=BUCKET, Key=key)["Body"].read() == b"second"

    def test_a_read_of_a_key_it_holds_is_not_refused(self) -> None:
        """So ``absent-key-answers-no-such-key`` is about absence rather than about every read."""
        with object_store_server(ObjectStore()) as (endpoint, _store):
            client = object_store_client(endpoint)
            key = f"{PREFIX}/present"
            client.put_object(Bucket=BUCKET, Key=key, Body=b"here", IfNoneMatch="*")

            assert client.get_object(Bucket=BUCKET, Key=key)["Body"].read() == b"here"

    def test_the_test_can_change_what_the_provider_holds(self) -> None:
        """The capability every independence proof downstream depends on.

        The store yielded by the fixture is the object the routes hold, so a test can replace an
        object behind CareLoop's back and require a read-back to notice.
        """
        store = ObjectStore()
        with object_store_server(store) as (endpoint, held):
            client = object_store_client(endpoint)
            key = f"{PREFIX}/tampered"
            client.put_object(Bucket=BUCKET, Key=key, Body=b"authorised", IfNoneMatch="*")

            assert held is store
            held.put(key, held.get(key).__class__(body=b"tampered"), only_if_absent=False)

            assert client.get_object(Bucket=BUCKET, Key=key)["Body"].read() == b"tampered"


def _code(failure: Exception) -> str:
    response = getattr(failure, "response", None)
    return response.get("Error", {}).get("Code", "") if isinstance(response, dict) else ""


class TestTheSubstituteIsNotPartOfCareLoop:
    """The store may not import the agent, and it may not read a clock.

    Both rules are ``demoworld``'s, applied here for the same two reasons. A provider that could see
    the application's beliefs would be corroborating them rather than being read back — and a
    provider whose answers drift between two identical requests would make every replay assertion in
    the suite approximate, since a suppressed duplicate is asserted to return a byte-identical
    result.
    """

    FORBIDDEN_CLOCKS = ("datetime", "time", "random", "secrets", "uuid")

    def test_the_package_has_the_modules_this_sweep_expects(self) -> None:
        assert {path.name for path in _substitute_modules()} == {
            "__init__.py",
            "app.py",
            "store.py",
        }

    def test_no_module_imports_careloop(self) -> None:
        offenders = {
            path.name: sorted(found)
            for path in _substitute_modules()
            if (found := imports_matching(path, ("careloop",)))
        }
        assert offenders == {}

    def test_the_import_detector_finds_one_where_it_exists(self) -> None:
        """Vacuity: a module that does import CareLoop must be flagged, or this proves nothing."""
        verify = Path(careloop.__file__).parent / "graph" / "verify.py"

        assert imports_matching(verify, ("careloop",))

    def test_no_module_reads_a_clock_or_a_random_source(self) -> None:
        offenders = {
            path.name: sorted(found)
            for path in _substitute_modules()
            if (found := imports_matching(path, self.FORBIDDEN_CLOCKS))
        }
        assert offenders == {}

    def test_the_contract_module_imports_no_careloop_either(self) -> None:
        """It establishes a fact about a provider; reaching into the application would not."""
        contract = Path(scripts.objectstore_contract.__file__)

        assert imports_matching(contract, ("careloop", "objectstore")) == set()


def _substitute_modules() -> list[Path]:
    return sorted(Path(objectstore.__file__).parent.glob("*.py"))
