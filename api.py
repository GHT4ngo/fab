"""
FaB Store API — FastAPI app wiring. Run with: python api.py

Endpoints live in fab_api/routers/* (cards, admin, scan, auth, cardlists);
shared runtime (env, pg pool, scan log) in fab_api/core.py; the OCR/visual
scan engine in fab_api/scan_engine.py.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from fab_api.core import HERE
from fab_api.routers import admin, auth, cardlists, cards, scan, tools

app = FastAPI(title="FaB Store API", version="1.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Compress JSON responses — /cards with page_size=300 carries full rules text
# for every row; gzip cuts that ~5-10× through the tunnel.
app.add_middleware(GZipMiddleware, minimum_size=1000)


app.include_router(cards.router)
app.include_router(admin.router)
app.include_router(scan.router)
app.include_router(auth.router)
app.include_router(cardlists.router)
app.include_router(tools.router)

# ── Serve the built frontend ────────────────────────────────────────────────
# Mounted last so it only catches paths not handled by an API route above.
# Same-origin means the frontend uses relative URLs (VITE_API_BASE_URL=""),
# so the public tunnel URL can change without ever rebuilding the frontend.


class SpaStaticFiles(StaticFiles):
    """StaticFiles with SPA fallback: unknown paths (React Router routes like
    /account or /tools — e.g. the emailed magic link) serve index.html instead
    of 404. Only paths NOT matched by an API route ever reach this mount, so
    API 404s stay JSON."""

    async def get_response(self, path: str, scope):
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as e:
            if e.status_code == 404:
                return await super().get_response("index.html", scope)
            raise
        if response.status_code == 404:
            response = await super().get_response("index.html", scope)
        return response


_FRONTEND_DIST = HERE / "retro-data-display" / "dist"
if _FRONTEND_DIST.is_dir():
    app.mount("/", SpaStaticFiles(directory=str(_FRONTEND_DIST), html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
