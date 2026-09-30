"""FastAPI app: auth endpoints (/login, /logout, /session), the TX
activity log (/tx-log), /health, and one /ws endpoint carrying the whole
control/spectrum/audio protocol (see protocol.py). Which SessionBackend is
active is decided once, at process startup, by WEB_TRX_BACKEND (see
backend_for_name()) -- never per-connection: there is only ever one
physical session (see docs/PROJECT_PLAN.md section 3), matching the
single shared-password login below (see auth.py).
"""
from __future__ import annotations

import ipaddress
import logging
import os
from contextlib import asynccontextmanager

from fastapi import Cookie, FastAPI, HTTPException, Request, Response, WebSocket
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
        # Deferred import: only touches the pluto-tx packages (and therefore
        # GNU Radio/libiio) when actually selected -- keeps `sim` usable in
        # environments without those installed (this dev container, CI).
        from .radio_backend import GnuRadioBackend

        return GnuRadioBackend()
    raise ValueError(f"unknown WEB_TRX_BACKEND '{name}' (expected 'sim' or 'gnuradio')")


class LoginRequest(BaseModel):
    password: str


class _HealthAccessFilter(logging.Filter):
    """Keeps GET /health out of uvicorn's access log: the TX/RX apps poll it
    every few seconds (pluto_tx/webtrx_control.py)."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3:
            return str(args[2]).split("?", 1)[0] != "/health"
        return True


def _install_health_access_filter() -> None:
    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _HealthAccessFilter) for f in access_logger.filters):
        access_logger.addFilter(_HealthAccessFilter())


def _is_loopback(host: str | None) -> bool:
    try:
        return host is not None and ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def device_summary(backend: SessionBackend) -> dict:
    """{"rx": {"connected", "device_type"}, "tx": {...}} for /health. A
    direction the server is still re-opening after a restart counts as
    connected already."""
    snapshot = backend.snapshot()
    restoring = getattr(backend, "restoring", lambda: set())()
    out = {}
    for direction in ("rx", "tx"):
        d = snapshot.get(direction) or {}
        out[direction] = {
            "connected": d.get("connection") is not None or direction in restoring,
            "device_type": d.get("device_type"),
        }
    return out


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
        await manager.stop()
        await manager.backend.shutdown()
        tx_log.close()

    _install_health_access_filter()
    app = FastAPI(title="Web-TRX", lifespan=lifespan)
    app.state.manager = manager
    app.state.auth = auth
    app.state.tx_log = tx_log

    @app.get("/health")
    async def health(request: Request) -> dict:
        out = {"status": "ok", "backend": manager.backend_name}
        # Which SDRs the server holds: only for the local TX/RX apps, which must
        # never open a device Web-TRX has open (pluto_tx/webtrx_control.py) --
        # not something to tell the network without a login.
        if _is_loopback(request.client.host if request.client else None):
            out["devices"] = device_summary(manager.backend)
        return out

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
