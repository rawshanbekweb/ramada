"""FastAPI ilovasi: mehmon sahifasi, ega paneli, webhooklar."""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.web import guest, panel, webhooks
from app.web.common import STATIC_DIR

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def create_app() -> FastAPI:
    app = FastAPI(title="Shaffof qabul", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(webhooks.router)
    app.include_router(guest.router)
    app.include_router(panel.router)

    @app.exception_handler(panel.NeedLogin)
    async def _need_login(request: Request, exc: panel.NeedLogin):
        return RedirectResponse("/panel/login", status_code=303)

    @app.get("/")
    def root():
        return RedirectResponse("/panel/")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        return resp

    return app


app = create_app()
