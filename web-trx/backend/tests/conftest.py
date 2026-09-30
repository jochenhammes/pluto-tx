import pytest


@pytest.fixture(autouse=True)
def _js8_inbox_in_tmp(tmp_path, monkeypatch):
    """GnuRadioBackend.start_background_tasks() opens the JS8 inbox (web_trx/js8_automation.py); without this a
    test would create it in the user's ~/.local/share/web-trx."""
    monkeypatch.setenv("WEB_TRX_JS8_INBOX_PATH", str(tmp_path / "js8_inbox.db3"))
