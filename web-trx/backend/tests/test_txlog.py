from web_trx.txlog import TxLog


def make_log() -> TxLog:
    return TxLog(":memory:")  # sqlite3 supports ":memory:" as a real path -- no files touch disk


def test_empty_log_has_no_entries():
    log = make_log()
    assert log.recent() == []


def test_record_keyed_then_unkeyed_closes_the_entry():
    log = make_log()
    log.record_keyed(mode="fm", freq_hz=145_500_000.0, device_type="sim",
                      connection="sim:0", params={"ctcss_hz": 88.5})
    entries = log.recent()
    assert len(entries) == 1
    assert entries[0]["ended_at"] is None  # still open
    assert entries[0]["mode"] == "fm"
    assert entries[0]["params"] == {"ctcss_hz": 88.5}

    log.record_unkeyed()
    entries = log.recent()
    assert entries[0]["ended_at"] is not None


def test_record_unkeyed_without_open_entry_is_a_no_op():
    log = make_log()
    log.record_unkeyed()  # must not raise
    assert log.recent() == []


def test_recent_orders_newest_first_and_respects_limit():
    log = make_log()
    for i in range(5):
        log.record_keyed(mode="pocsag", freq_hz=433_000_000.0, device_type="sim",
                          connection="sim:0", params={"ric": i})
        log.record_unkeyed()
    entries = log.recent(limit=3)
    assert len(entries) == 3
    assert [e["params"]["ric"] for e in entries] == [4, 3, 2]


def test_second_keyed_reassigns_the_open_entry():
    """Mirrors what actually happens over a session: each new keyed
    transition tracks its OWN open entry, not the previous one -- an
    unkeyed after a second keyed must close the second, not silently
    re-touch the first (already-closed) one."""
    log = make_log()
    log.record_keyed(mode="fm", freq_hz=1.0, device_type="sim", connection="sim:0", params={})
    log.record_unkeyed()
    log.record_keyed(mode="fm", freq_hz=2.0, device_type="sim", connection="sim:0", params={})
    log.record_unkeyed()
    entries = log.recent()
    assert len(entries) == 2
    assert all(e["ended_at"] is not None for e in entries)
