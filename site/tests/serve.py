"""Serve the dossier under a GitHub project path, the way Pages will.

``python site/tests/serve.py --port 8801`` publishes ``site/`` at ``/CareLoop/`` and nothing at
``/``. That asymmetry is the point: the deployed dossier lives at
``https://<owner>.github.io/CareLoop/``, and a gate that exercised it at the origin root would pass
happily on a page whose assets are all root-absolute. Serving it anywhere else would be testing a
deployment shape that will never exist.

Standard library only, and no staging copy — the prefix is stripped in ``translate_path`` so the
bytes served are the ones in the working tree.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import posixpath
from pathlib import Path
from typing import Final

SITE_ROOT: Final = Path(__file__).resolve().parents[1]
PROJECT_PATH: Final = "/CareLoop/"


class ProjectPathHandler(http.server.SimpleHTTPRequestHandler):
    """Serve ``site/`` beneath ``/CareLoop/`` and 404 everything outside it."""

    # HTTP/1.1, so the browser keeps one connection rather than reopening a socket per asset.
    # The page is a 1.9 MB document followed by several captures, and on a loaded machine the
    # HTTP/1.0 default's connection churn was enough to push the first navigation past its
    # budget. SimpleHTTPRequestHandler already sends Content-Length, which is what makes
    # keep-alive safe here.
    protocol_version = "HTTP/1.1"

    def translate_path(self, path: str) -> str:
        clean = posixpath.normpath(path.split("?", 1)[0].split("#", 1)[0])
        if clean == PROJECT_PATH.rstrip("/"):
            clean = PROJECT_PATH
        if not clean.startswith(PROJECT_PATH):
            return str(SITE_ROOT / "__outside_the_project_path__")
        relative = clean[len(PROJECT_PATH) :].lstrip("/")
        target = SITE_ROOT.joinpath(*[p for p in relative.split("/") if p not in ("", ".", "..")])
        if target.is_dir():
            target = target / "index.html"
        return str(target)

    def end_headers(self) -> None:
        # GitHub Pages does not cache aggressively enough to hide a stale artifact from a gate,
        # and neither should this.
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt: str, *args: object) -> None:
        return


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve the dossier under /CareLoop/.")
    parser.add_argument("--port", type=int, default=8801)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--root", type=Path, default=None, help="serve this directory instead")
    args = parser.parse_args()

    if args.root is not None:
        # One process, one root, and it is set before the server starts.
        global SITE_ROOT
        SITE_ROOT = args.root.resolve()

    handler = functools.partial(ProjectPathHandler)
    server = http.server.ThreadingHTTPServer((args.host, args.port), handler)
    print(f"serving {SITE_ROOT} at http://{args.host}:{args.port}{PROJECT_PATH}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
