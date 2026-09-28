"""FastAPI app: auth endpoints (/login, /logout, /session), the TX
activity log (/tx-log), /health, and one /ws endpoint carrying the whole
control/spectrum/audio protocol (see protocol.py). Which SessionBackend is
active is decided once, at process startup, by WEB_TRX_BACKEND (see
backend_for_name()) -- never per-connection: there is only ever one
physical session (see docs/PROJECT_PLAN.md section 3), matching the
single shared-password login below (see auth.py).
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import Cookie, FastAPI, HTTPException, Response, WebSocket
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth as auth_module
from .session import SessionBackend, SessionManager
from .sim_backend import SimBackend
from .txlog import TxLog


def backend_for_name(name: str) -> SessionBackend:
    if name == "sim":
        return SimBackend()
    if name == "gnuradio":
        # Deferred import: only touches vendor/pluto-tx (and therefore
        # GNU Radio/libiio) when actually selected -- keeps `sim` usable in
        # environments without those installed (this dev container, CI).
        from .radio_backend import GnuRadioBackend

        return GnuRadioBackend()
    raise ValueError(f"unknown WEB_TRX_BACKEND '{name}' (expected 'sim' or 'gnuradio')")


class LoginRequest(BaseModel):
    password: str


def create_app(
    backend: SessionBackend | None = None,
    *,
    auth: auth_module.AuthManager | None = None,
    tx_log_path: str | None = None,
) -> FastAPI:
    backend = backend or backend_for_name(os.environ.get("WEB_TRX_BACKEND", "sim"))
    auth = auth or auth_module.AuthManager(sessions_path=os.environ.get("WEB_TRX_SESSIONS_PATH"))
    tx_log = TxLog(tx_log_path or os.environ.get("WEB_TRX_TX_LOG_PATH", "web_trx_tx_log.sqlite3"))
    # False by default for frictionless local/dev use over plain http; set
    # WEB_TRX_COOKIE_SECURE=true once deployed behind TLS (see README).
    cookie_secure = os.environ.get("WEB_TRX_COOKIE_SECURE", "false").lower() == "true"
    manager = SessionManager(backend, tx_log=tx_log)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        start = getattr(manager.backend, "start_background_tasks", None)
        if start is not None:
            start()
        manager.start()
        yield
        await manager.backend.shutdown()
        tx_log.close()

    app = FastAPI(title="Web-TRX", lifespan=lifespan)
    app.state.manager = manager
    app.state.auth = auth
    app.state.tx_log = tx_log

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "backend": manager.backend_name}

    @app.post("/login")
    async def login(body: LoginRequest, response: Response) -> dict:
        if not auth.check_password(body.password):
            raise HTTPException(status_code=401, detail="wrong password")
        token = auth.issue_token()
        response.set_cookie(
            auth_module.COOKIE_NAME, token, max_age=int(auth.token_ttl_s),
            httponly=True, samesite="lax", secure=cookie_secure,
        )
        return {"ok": True}

    @app.post("/logout")
    async def logout(response: Response, web_trx_token: str | None = Cookie(default=None)) -> dict:
        auth.revoke(web_trx_token)
        response.delete_cookie(auth_module.COOKIE_NAME)
        return {"ok": True}

    @app.get("/session")
    async def session_status(web_trx_token: str | None = Cookie(default=None)) -> dict:
        return {"authenticated": auth.validate(web_trx_token)}

    @app.get("/tx-log")
    async def tx_log_endpoint(limit: int = 50, web_trx_token: str | None = Cookie(default=None)) -> list:
        if not auth.validate(web_trx_token):
            raise HTTPException(status_code=401, detail="not authenticated")
        return tx_log.recent(limit=limit)

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket) -> None:
        token = websocket.cookies.get(auth_module.COOKIE_NAME)
        if not auth.validate(token):
            # Rejecting before accept() -> the ASGI server turns this into
            # an HTTP 403 handshake response rather than an accepted-then-
            # immediately-closed connection (see RFC-of-record: the ASGI
            # websocket spec). Verified in test_server_ws.py.
            await websocket.close(code=4401)
            return
        await manager.handle_connection(websocket)

    # The built frontend (npm run build), so one process serves everything
    # -- see scripts/start.sh. Mounted last: API routes above take precedence.
    static_dir = os.environ.get("WEB_TRX_STATIC_DIR")
    if static_dir and os.path.isdir(static_dir):
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="frontend")

    return app


# Deliberately NO module-level `app = create_app()` here: that would run
# create_app()'s side effects (opening/creating the TX-log SQLite file,
# possibly generating and printing a password) on every import of this
# module -- including every test collection, and every `from
# web_trx.server import create_app` elsewhere. Run with uvicorn's factory
# mode instead: `uvicorn web_trx.server:create_app --factory`.
