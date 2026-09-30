import pytest


@pytest.fixture(autouse=True)
def _js8_inbox_in_tmp(tmp_path, monkeypatch):
    """GnuRadioBackend.start_background_tasks() opens the JS8 inbox (web_trx/js8_automation.py); without this a
    test would create it in the user's ~/.local/share/web-trx."""
    monkeypatch.setenv("WEB_TRX_JS8_INBOX_PATH", str(tmp_path / "js8_inbox.db3"))


@pytest.fixture(autouse=True)
def _no_heartbeat_by_default(monkeypatch):
    """The browser-liveness heartbeat (session.py) would interleave 'hb' events with the events tests read in
    order; tests of the heartbeat itself set these small again."""
    from web_trx import session

    monkeypatch.setattr(session, "HB_INTERVAL_S", 3600.0)
    monkeypatch.setattr(session, "CLIENT_TIMEOUT_S", 3600.0)
