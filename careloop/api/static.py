"""Serving the built frontend from the same origin as the API.

**This exists so that no CORS middleware ever has to.** In development the Vite proxy keeps the
browser same-origin against the backend; a production build served from a different host would
not be, and `careloop/` mounts no `CORSMiddleware` anywhere. One origin serving both the app and
`/api` keeps the frontend's relative `/api` paths working unchanged and keeps that instruction
intact rather than weakened.

Mounted only when :func:`careloop.api.app.create_app` is asked for it, the same treatment the demo
control surface gets and for a related reason: an app that serves files nobody asked it to serve
is one whose test suite is exercising a different application than the deployment does.

The catch-all is registered last so every API route wins. A path under ``/api`` that reaches here
has already failed to match a real route, and it gets a JSON 404 rather than the SPA shell — a
mistyped endpoint answering ``200 text/html`` is a debugging afternoon nobody needs.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import FileResponse

from careloop.api.app import API_PREFIX

#: Vite fingerprints everything under ``assets/``, so those bytes can never change under a name.
IMMUTABLE = "public, max-age=31536000, immutable"

#: The shell names the fingerprinted bundle, so a cached one pins the whole app to an old deploy.
REVALIDATE = "no-cache"

#: Fonts are not fingerprinted — they keep their own names across builds — so they get a bounded
#: lifetime rather than an immutable one. A day is long enough that a judge never refetches them
#: mid-demo and short enough that replacing one is not a support call.
BOUNDED = "public, max-age=86400"


def cache_control_for(relative: str) -> str:
    if relative.startswith("assets/"):
        return IMMUTABLE
    if relative.startswith("fonts/"):
        return BOUNDED
    return REVALIDATE


def resolve_within(root: Path, relative: str) -> Path | None:
    """Resolve ``relative`` under ``root``, or ``None`` if it escapes.

    The check is on the *resolved* paths rather than on the text of the request. Rejecting ``..``
    by inspection is the kind of filter this project refuses everywhere else: it has to be right
    about every encoding, and symlinks make the string and the destination different questions.
    """
    try:
        candidate = (root / relative).resolve()
    except (OSError, ValueError):
        return None
    if candidate == root or root in candidate.parents:
        return candidate
    return None


def mount_static(app: FastAPI, directory: Path) -> None:
    """Serve the built frontend at ``/``, with an SPA fallback to ``index.html``.

    Raises:
        FileNotFoundError: if the directory holds no ``index.html``. Failing at build time beats
            serving a 404 to a judge: a deployment pointed at an unbuilt frontend is a mistake
            worth making loudly at startup.
    """
    root = directory.resolve()
    index = root / "index.html"
    if not index.is_file():
        raise FileNotFoundError(
            f"no index.html under {root} — build the frontend (`npm run build`) before serving it"
        )

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str) -> FileResponse:
        """A real file if there is one, otherwise the shell so client-side routing can take it."""
        if full_path == "api" or full_path.startswith(f"{API_PREFIX.lstrip('/')}/"):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such endpoint")

        candidate = resolve_within(root, full_path) if full_path else None
        if candidate is not None and candidate.is_file():
            return FileResponse(candidate, headers={"Cache-Control": cache_control_for(full_path)})

        return FileResponse(index, headers={"Cache-Control": REVALIDATE})


__all__ = [
    "BOUNDED",
    "IMMUTABLE",
    "REVALIDATE",
    "cache_control_for",
    "mount_static",
    "resolve_within",
]
