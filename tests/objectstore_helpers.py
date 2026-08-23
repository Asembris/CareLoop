"""The object-store substitute on a real loopback socket, driven by a real boto3 client.

The sibling of :mod:`tests.clinic_helpers`, and it exists for the same reason. Driving the ASGI app
in-process would exercise every line of the substitute and none of the thing being asserted, which
is that **boto3 itself** — its signing, its header construction, its XML error parsing, its checksum
handling — behaves against this store the way it behaves against S3. A stub that returned Python
objects would let a client bug pass unseen; a loopback socket cannot.

Nothing outside the machine is contacted and no real credential is used: the client is built
with obviously fake static keys, and the substitute ignores signatures entirely. CI's zero-secret
contract is untouched.

Path-style addressing is forced rather than left to botocore's own inference. It happens to choose
path style for an IP endpoint today, and a client that silently switched to virtual-host addressing
would start asking a hostname that does not exist — a confusing failure for something this file can
simply state.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import boto3
import uvicorn
from botocore.config import Config

from objectstore.app import create_object_store_app
from objectstore.store import ObjectStore

STARTUP_TIMEOUT = 10.0
SHUTDOWN_TIMEOUT = 10.0

#: The bucket the substitute answers for. It has no bucket registry — every bucket exists — so this
#: is a name the tests agree on rather than a resource anybody creates.
BUCKET = "careloop-test-bucket"

#: Obviously fake, and deliberately not shaped like a real key.
#:
#: botocore refuses to sign without credentials and does not care what they look like, so these are
#: chosen to be unmistakable rather than realistic. AWS's own published example key was the obvious
#: candidate and is the wrong one: ``tests/test_nothing_secret_is_tracked.py`` sweeps every tracked
#: file for credential *shapes*, and a fixture that trips it teaches the next reader that the sweep
#: cries wolf. A string that could not be an AWS key needs no allowlist entry — and note that
#: spelling the example key in this comment tripped the sweep too, which is the same lesson.
FAKE_ACCESS_KEY = "careloop-substitute-not-a-real-key"
FAKE_SECRET_KEY = "careloop-substitute-not-a-real-secret"


def object_store_client(endpoint_url: str) -> Any:
    """A boto3 S3 client pointed at the substitute."""
    return boto3.session.Session(
        aws_access_key_id=FAKE_ACCESS_KEY,
        aws_secret_access_key=FAKE_SECRET_KEY,
        region_name="us-east-1",
    ).client(
        "s3",
        endpoint_url=endpoint_url,
        config=Config(
            s3={"addressing_style": "path"},
            retries={"max_attempts": 1, "mode": "standard"},
            signature_version="s3v4",
        ),
    )


@contextmanager
def object_store_server(store: ObjectStore | None = None) -> Iterator[tuple[str, ObjectStore]]:
    """Run the substitute on a loopback port, yielding its base URL and its state.

    The :class:`~objectstore.store.ObjectStore` is yielded alongside the URL for
    :func:`tests.clinic_helpers.clinic_server`'s reason: a test that means to prove the read-back is
    independent has to be able to change what the provider holds behind CareLoop's back, and it can
    only do that by holding the object the routes hold.
    """
    held = store if store is not None else ObjectStore()
    config = uvicorn.Config(
        create_object_store_app(held), host="127.0.0.1", port=0, log_level="warning"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="objectstore", daemon=True)
    thread.start()

    deadline = time.monotonic() + STARTUP_TIMEOUT
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("the object store substitute did not start")
        time.sleep(0.01)

    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}", held
    finally:
        server.should_exit = True
        thread.join(timeout=SHUTDOWN_TIMEOUT)


__all__ = [
    "BUCKET",
    "object_store_client",
    "object_store_server",
]
