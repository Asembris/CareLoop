"""A create-once object store, speaking enough of S3's REST surface to stand in for it offline.

**A test substitute, and it is never described as anything else.** It is not "an S3-compatible
service", it is not a product, and no claim CareLoop makes about a real upload rests on it. What it
is for is running :mod:`scripts.objectstore_contract` — *the same assertions*, unmodified — on every
CI run, at zero cost and with no credential, so that the behaviours
[`docs/objectstore-contract.md`](../docs/objectstore-contract.md) records as measured against
Amazon S3 are behaviours CareLoop is continuously held to rather than ones it was held to once.

**The drift argument is the whole justification.** A substitute proves nothing about S3; the shared
contract module is what makes it useful. If this package's semantics wander from the provider's, an
operator's run of that module against real S3 goes red and says which check moved. A substitute with
its own private test suite would be a fake with a fan club.

**It authenticates nothing, deliberately.** boto3 signs every request and this package ignores the
signature entirely. Implementing SigV4 verification would be building a second security system in
order to test a first one, and it would establish nothing about CareLoop: the property under test is
*what the store does with a conditional write*, not who is allowed to make one. Access control is a
deployment concern and is asserted against the real provider by the deployment smoke test.

**It may never import ``careloop``**, for the reason ``demoworld`` may not: a provider that
could see the application's beliefs would be corroborating them rather than being read back.

Deliberately narrow. Five operations, one addressing style, no multipart, no versioning, no
lifecycle, no ACLs, no pagination beyond the one listing CareLoop's contract needs. Anything absent
here is absent because nothing in CareLoop's upload path uses it, and adding an operation "for
completeness" would widen the surface a future defect could hide in.
"""

from objectstore.app import create_object_store_app
from objectstore.store import ObjectStore, StoredObject

__all__ = ["ObjectStore", "StoredObject", "create_object_store_app"]
