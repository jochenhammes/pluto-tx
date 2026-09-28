import subprocess

import pytest

from web_trx import clock


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    monkeypatch.setattr(clock, "_cache", None)


def _fake_timedatectl(monkeypatch, stdout=None, exc=None):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if exc is not None:
            raise exc
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(clock.subprocess, "run", run)
    return calls


def test_synchronized(monkeypatch):
    _fake_timedatectl(monkeypatch, "yes\n")
    assert clock.ntp_synchronized() is True


def test_not_synchronized(monkeypatch):
    _fake_timedatectl(monkeypatch, "no\n")
    assert clock.ntp_synchronized() is False


def test_unknown_counts_as_synchronized_like_pluto_tx(monkeypatch):
    _fake_timedatectl(monkeypatch, exc=FileNotFoundError("timedatectl"))
    assert clock.ntp_synchronized() is True


def test_result_is_cached(monkeypatch):
    calls = _fake_timedatectl(monkeypatch, "yes\n")
    clock.ntp_synchronized()
    clock.ntp_synchronized()
    assert len(calls) == 1
