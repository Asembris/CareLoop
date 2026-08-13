"""The built frontend served from the API's own origin, and the cache headers that go with it.

Two things are being defended. The frontend must be reachable at ``/`` and on client-side routes
without any CORS middleware existing anywhere — that is what keeps the "no CORS in ``careloop/``"
instruction true for a real deployment rather than only for the dev proxy. And ``/api`` responses
must be uncacheable, because the dashboard polls them to watch a run and a stale one reads exactly
like a hung backend.

Every guard here has a test that fails when the guard is removed; the ones where that is not
obvious say so by name.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from careloop.api.app import create_app
from careloop.api.static import BOUNDED, IMMUTABLE, REVALIDATE, cache_control_for, resolve_within
from careloop.state import InMemoryCaseStore

INDEX = (
    "<!doctype html><html><head><title>CareLoop</title></head>"
    "<body><div id=root></div></body></html>"
)


@pytest.fixture
def built(tmp_path: Path) -> Path:
    """A directory shaped like ``frontend/dist`` after ``npm run build``."""
    (tmp_path / "index.html").write_text(INDEX, encoding="utf-8")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "index-abc123.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path / "fonts").mkdir()
    (tmp_path / "fonts" / "newsreader-latin.woff2").write_bytes(b"wOF2fake")
    (tmp_path / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    return tmp_path


@pytest.fixture
def client(built: Path) -> TestClient:
    return TestClient(create_app(InMemoryCaseStore(), demo=True, static_dir=built))


class TestTheAppIsServedFromTheApiOrigin:
    def test_root_returns_the_shell(self, client: TestClient) -> None:
        response = client.get("/")
        assert response.status_code == 200
        assert "CareLoop" in response.text

    def test_a_client_side_route_returns_the_shell(self, client: TestClient) -> None:
        """``/case/case-0001`` is a React route, not a file. The server must not 404 it."""
        response = client.get("/case/case-0001")
        assert response.status_code == 200
        assert "id=root" in response.text

    def test_a_real_asset_is_served_as_itself(self, client: TestClient) -> None:
        response = client.get("/assets/index-abc123.js")
        assert response.status_code == 200
        assert response.text == "console.log(1)"

    def test_fonts_are_served(self, client: TestClient) -> None:
        """The three families are self-hosted; a 404 here films the demo in fallback faces."""
        assert client.get("/fonts/newsreader-latin.woff2").status_code == 200


class TestTheApiStillWins:
    def test_a_real_api_route_is_not_shadowed(self, client: TestClient) -> None:
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_an_unknown_api_path_is_json_404_not_the_shell(self, client: TestClient) -> None:
        """The catch-all must not answer for ``/api``.

        A mistyped endpoint returning ``200 text/html`` turns a one-line fix into an afternoon:
        the client parses the SPA shell as JSON and reports something unrelated.
        """
        response = client.get("/api/nonesuch")
        assert response.status_code == 404
        assert "text/html" not in response.headers.get("content-type", "")

    def test_the_dashboard_route_is_reachable_with_static_mounted(self, client: TestClient) -> None:
        assert client.get("/api/cases").status_code == 200


class TestNoCacheOnTheApi:
    """The dashboard polls these. A cached one looks like a hung backend."""

    @pytest.mark.parametrize("path", ["/api/health", "/api/cases"])
    def test_api_responses_are_no_store(self, client: TestClient, path: str) -> None:
        assert client.get(path).headers["cache-control"] == "no-store"

    def test_the_shell_is_revalidated_rather_than_cached(self, client: TestClient) -> None:
        """The shell names the fingerprinted bundle, so caching it pins the app to an old deploy."""
        assert client.get("/").headers["cache-control"] == REVALIDATE

    def test_fingerprinted_assets_are_immutable(self, client: TestClient) -> None:
        assert client.get("/assets/index-abc123.js").headers["cache-control"] == IMMUTABLE

    def test_static_files_are_not_marked_no_store(self, client: TestClient) -> None:
        """The vacuity proof for the middleware's path check.

        A middleware that stamped ``no-store`` on everything would pass every assertion above and
        make the CDN useless — every font and bundle refetched on every page load. This is the
        assertion that tells the two apart.
        """
        assert client.get("/assets/index-abc123.js").headers["cache-control"] != "no-store"
        assert client.get("/fonts/newsreader-latin.woff2").headers["cache-control"] == BOUNDED


class TestItIsOffUnlessAskedFor:
    def test_no_static_dir_means_no_catch_all(self) -> None:
        """Without the flag the app is exactly what it was, so the suite tests what ships."""
        client = TestClient(create_app(InMemoryCaseStore()))
        assert client.get("/").status_code == 404
        assert client.get("/case/case-0001").status_code == 404

    def test_the_api_is_still_no_store_without_static(self) -> None:
        """The cache header belongs to the response, not to the deployment that serves files."""
        client = TestClient(create_app(InMemoryCaseStore()))
        assert client.get("/api/health").headers["cache-control"] == "no-store"

    def test_an_unbuilt_frontend_fails_at_startup(self, tmp_path: Path) -> None:
        """Loudly, at build time. A deployment pointed at nothing must not serve 404s to a judge."""
        with pytest.raises(FileNotFoundError, match="npm run build"):
            create_app(InMemoryCaseStore(), static_dir=tmp_path)


class TestTheStaticRootIsNotAFileServer:
    """Serving a directory to the internet is a path-traversal surface; this is the proof."""

    @pytest.mark.parametrize(
        "attempt",
        [
            "../secrets.txt",
            "../../etc/passwd",
            "assets/../../secrets.txt",
            "%2e%2e/secrets.txt",
        ],
    )
    def test_escaping_the_root_never_serves_the_file(self, built: Path, attempt: str) -> None:
        (built.parent / "secrets.txt").write_text("SENSITIVE", encoding="utf-8")
        client = TestClient(create_app(InMemoryCaseStore(), static_dir=built))
        response = client.get(f"/{attempt}")
        assert "SENSITIVE" not in response.text

    def test_resolve_within_rejects_an_escape_and_accepts_a_child(self, built: Path) -> None:
        """Asserted on the function too: the client normalises some of these before they arrive.

        A test that only drove the HTTP surface would pass with the check removed for any attempt
        the test client happened to collapse first, which is the vacuous version of the test above.
        """
        root = built.resolve()
        assert resolve_within(root, "../secrets.txt") is None
        assert resolve_within(root, "assets/../../secrets.txt") is None
        assert resolve_within(root, "assets/index-abc123.js") == root / "assets" / "index-abc123.js"


class TestCacheControlForIsTotal:
    @pytest.mark.parametrize(
        ("relative", "expected"),
        [
            ("assets/index-abc123.js", IMMUTABLE),
            ("fonts/newsreader-latin.woff2", BOUNDED),
            ("index.html", REVALIDATE),
            ("favicon.svg", REVALIDATE),
            ("", REVALIDATE),
        ],
    )
    def test_every_path_gets_a_policy(self, relative: str, expected: str) -> None:
        assert cache_control_for(relative) == expected
