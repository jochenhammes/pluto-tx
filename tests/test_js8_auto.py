"""J9: JS8 automation -- received commands (pluto_advanced_rx/js8_commands.py), replies/heartbeat/relay/inbox
(pluto_tx/js8_auto.py, js8_inbox.py) against JS8Call's rules (docs/js8/SPEC.md 7). Every command arrives as
real frames built by the bit-exact TX builder; every reply must itself build into frames."""
import dataclasses
import json
import os
import random
import sqlite3
import tempfile
import unittest

from pluto_advanced_rx.js8_commands import Js8CommandParser
from pluto_tx import js8, js8_auto as A, js8_message as M
from pluto_tx.js8_inbox import Js8Inbox, base_callsign

T0 = 1_790_000_010.0        # a NORMAL period start + 0 (slot grid of 15 s)


@dataclasses.dataclass
class D:
    frame: str
    flags: int
    submode: int
    snr_db: float
    freq_hz: float


def receive(parser, text, call="DL1ABC", grid="JO62", t0=T0, submode=js8.NORMAL, freq=1200.0, snr=-12.0):
    period = js8.speed_info(submode)["period_s"]
    out = []
    for i, (frame, flags) in enumerate(M.build_frames(call, grid, text, submode)):
        out += parser.feed(t0 + period * i, submode, [D(frame, flags, submode, snr, freq + i)])
    return out


class ParserTests(unittest.TestCase):
    def test_commands(self):
        p = Js8CommandParser()
        cases = {
            "DA2JH SNR?": ("DA2JH", " SNR?", ""),
            "DA2JH MSG HELLO THERE HOW ARE YOU": ("DA2JH", " MSG", "HELLO THERE HOW ARE YOU"),
            "DA2JH MSG TO:DK2XY TEST 123": ("DA2JH", " MSG TO:", "DK2XY TEST 123"),
            "DA2JH>DK2XY HELLO": ("DA2JH", ">", "DK2XY HELLO"),
            "DA2JH QUERY MSGS": ("DA2JH", " QUERY MSGS", ""),
            "DA2JH QUERY MSG 3": ("DA2JH", " QUERY", "MSG 3"),
            "@HB HEARTBEAT JO62": ("@HB", " HEARTBEAT", ""),
        }
        for text, (to, cmd, body) in cases.items():
            (c,) = receive(p, text)
            self.assertEqual((c.from_call, c.to, c.cmd, c.text), ("DL1ABC", to, cmd, body), text)
            self.assertTrue(c.is_last)
        (c,) = receive(p, "@ALLCALL CQ CQ CQ")
        self.assertEqual((c.to, c.cmd), ("@ALLCALL", " CQ"))
        self.assertEqual(p.open_buffer_offsets(), [])

    def test_compound_sender(self):
        (c,) = receive(Js8CommandParser(), "DA2JH SNR?", call="EA8/DL1ABC")
        self.assertEqual((c.from_call, c.to, c.cmd), ("EA8/DL1ABC", "DA2JH", " SNR?"))

    def test_bad_checksum_is_dropped(self):
        from unittest import mock
        from pluto_advanced_rx import js8_commands
        p = Js8CommandParser()
        with mock.patch.object(js8_commands.M, "checksum16_valid", return_value=False) as check:
            self.assertEqual(receive(p, "DA2JH MSG HELLO THERE"), [])
        check.assert_called_once_with("J8F", "HELLO THERE")      # MSG: 16-bit checksum, last 3 characters
        self.assertEqual(p.open_buffer_offsets(), [])

    def test_buffer_times_out(self):
        p = Js8CommandParser()
        frames = M.build_frames("DL1ABC", "", "DA2JH MSG ONE TWO THREE FOUR FIVE SIX SEVEN", js8.NORMAL)
        self.assertGreater(len(frames), 2)
        for i, (f, b) in enumerate(frames[:-1]):                  # the LAST frame never arrives
            self.assertEqual(p.feed(T0 + 15 * i, js8.NORMAL, [D(f, b, js8.NORMAL, -10, 1200)]), [])
        last = T0 + 15 * (len(frames) - 1)
        self.assertEqual(p.expire(last + 60), [])                  # not yet 60 s quiet
        self.assertEqual(p.expire(last + 15 + 61), [])             # ends, but the checksum is missing
        self.assertEqual(p.open_buffer_offsets(), [])

    def test_first_frame_drops_an_old_buffer(self):
        p = Js8CommandParser()
        f1 = M.build_frames("DL1ABC", "", "DA2JH MSG AAAA BBBB CCCC DDDD", js8.NORMAL)
        p.feed(T0, js8.NORMAL, [D(*f1[0], js8.NORMAL, -10, 1200)])
        (c,) = receive(p, "DA2JH SNR?", t0=T0 + 15)                # a new FIRST frame on the same offset
        self.assertEqual(c.cmd, " SNR?")
        self.assertEqual(p.open_buffer_offsets(), [])


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


class AutoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.inbox = Js8Inbox(os.path.join(self.tmp.name, "inbox.db3"))
        self.addCleanup(self.inbox.close)
        self.clock = Clock()
        self.parser = Js8CommandParser()

    def auto(self, **cfg):
        cfg = {"mycall": "DA2JH", "grid": "JO31", "confirm": False, **cfg}
        return A.Js8Auto(A.Js8AutoConfig(**cfg), self.inbox, clock=self.clock, rng=random.Random(1))

    def rx(self, auto, text, **kw):
        cmds = receive(self.parser, text, t0=self.clock.t, **kw)
        return auto.process_commands(cmds, now=self.clock.t)

    def sent(self, auto, busy=False, own=1500):
        nxt = auto.next_transmission(busy, own, now=self.clock.t)
        if nxt is None:
            return None
        msg, offset, is_auto = nxt
        self.assertTrue(M.build_frames("DA2JH", "JO31", msg.text, js8.NORMAL))    # every reply is sendable
        auto.transmitted(msg, msg.text, is_auto, now=self.clock.t)
        return msg.text, offset, is_auto

    def test_snr_query(self):
        a = self.auto(autoreply=True)
        ev = self.rx(a, "DA2JH SNR?", snr=-12)
        self.assertEqual(ev[0]["type"], "queued")
        self.assertEqual(self.sent(a), ("DL1ABC SNR -12", 1500, True))

    def test_without_autoreply_the_reply_is_only_suggested(self):
        a = self.auto()
        self.rx(a, "DA2JH SNR?")
        text, _off, is_auto = self.sent(a)
        self.assertEqual(text, "DL1ABC SNR -12")
        self.assertFalse(is_auto)                          # processTxQueue: into the message box only
        self.assertEqual(a.auto_count(self.clock.t), 0)

    def test_allcall_query_gets_no_reply_without_autoreply(self):
        a = self.auto()
        self.assertEqual(self.rx(a, "@ALLCALL SNR?"), [])

    def test_confirmation(self):
        a = self.auto(autoreply=True, confirm=True)
        (ev,) = self.rx(a, "DA2JH SNR?")
        self.assertEqual(ev["type"], "confirm")
        self.assertIsNone(self.sent(a))
        self.assertEqual(a.confirm(ev["id"], True, now=self.clock.t)[0]["type"], "queued")
        self.assertEqual(self.sent(a)[0], "DL1ABC SNR -12")
        (ev,) = self.rx(a, "DA2JH SNR?")
        self.clock.t += 91
        self.assertEqual(a.tick(1500)[0]["type"], "declined")      # 90 s without an answer
        self.assertIsNone(self.sent(a))

    def test_grid_info_status_hearing_agn(self):
        a = self.auto(autoreply=True, info="PLUTO+ 40 DB DOWN")
        for call in ("DK1AA", "DK2BB", "DK3CC", "DK4DD", "DK5EE"):
            a.process_commands(receive(self.parser, "@HB HEARTBEAT JO40", call=call, t0=self.clock.t))
            self.clock.t += 15
        self.rx(a, "DA2JH GRID?")
        self.assertEqual(self.sent(a)[0], "DL1ABC GRID JO31")
        self.rx(a, "DA2JH INFO?")
        self.assertEqual(self.sent(a)[0], "DL1ABC INFO PLUTO+ 40 DB DOWN")
        a.last_activity = self.clock.t - 5 * 60
        self.rx(a, "DA2JH STATUS?")
        self.assertEqual(self.sent(a)[0], "DL1ABC STATUS IDLE 5M VERSION PLUTO-TX")
        self.rx(a, "DA2JH HEARING?")
        self.assertEqual(self.sent(a)[0], "DL1ABC HEARING DK5EE DK4DD DK3CC DK2BB")   # 4, newest first
        self.rx(a, "DA2JH AGN?")
        self.assertEqual(self.sent(a)[0], "DL1ABC HEARING DK5EE DK4DD DK3CC DK2BB")

    def test_no_info_no_reply(self):
        a = self.auto(autoreply=True)
        self.assertEqual(self.rx(a, "DA2JH INFO?"), [])

    def test_allcall_cache_15_min(self):
        a = self.auto(autoreply=True)
        self.rx(a, "@ALLCALL GRID?")
        self.assertEqual(a.queue, [])                                  # GRID? to allcall: no reply
        self.rx(a, "@ALLCALL QUERY MSGS")
        self.assertEqual(a.queue, [])                                  # allcall and nothing stored: silent
        self.inbox.append("STORE", {"TO": "DL1ABC", "FROM": "DK2XY", "TEXT": "HI", "UTC": "2026-09-30 10:00:00"})
        self.rx(a, "@ALLCALL QUERY MSGS")
        self.assertEqual(self.sent(a)[0], "DL1ABC YES MSG ID 1")
        self.clock.t += 14 * 60
        self.rx(a, "@ALLCALL QUERY MSGS")
        self.assertEqual(a.queue, [])                                  # within 15 min
        self.clock.t += 61
        self.rx(a, "@ALLCALL QUERY MSGS")
        self.assertEqual(len(a.queue), 1)

    def test_white_and_blacklist(self):
        a = self.auto(autoreply=True, blacklist=("DL1ABC",))
        self.assertEqual(self.rx(a, "DA2JH SNR?"), [])
        a = self.auto(autoreply=True, whitelist=("DK2XY",))
        self.assertEqual(self.rx(a, "DA2JH SNR?"), [])
        a = self.auto(autoreply=True, whitelist=("DL1ABC",))
        self.assertTrue(self.rx(a, "DA2JH SNR?", call="DL1ABC/P"))     # base callsign counts

    def test_msg_to_me_goes_to_the_inbox(self):
        a = self.auto(autoreply=True)
        ev = self.rx(a, "DA2JH MSG HELLO FROM THE TEST")
        self.assertEqual(ev[0]["type"], "inbox")
        msg = self.inbox.value(ev[0]["id"])
        self.assertEqual((msg["type"], msg["params"]["FROM"], msg["params"]["TEXT"], msg["params"]["CMD"]),
                         ("UNREAD", "DL1ABC", "HELLO FROM THE TEST", " MSG "))
        self.assertEqual(self.sent(a)[0], "DL1ABC ACK")

    def test_store_and_forward(self):
        a = self.auto(autoreply=True)
        self.assertEqual(self.rx(a, "DA2JH MSG TO:DK2XY SEE YOU AT SIX"), [])   # relay off: nothing stored
        self.assertEqual(self.inbox.all(), [])
        a.configure(relay=True)
        self.rx(a, "DA2JH MSG TO:DK2XY/P SEE YOU AT SIX")
        self.assertEqual(self.sent(a)[0], "DL1ABC ACK")
        (mid, msg), = self.inbox.all()
        self.assertEqual((msg["type"], msg["params"]["TO"], msg["params"]["TEXT"]), ("STORE", "DK2XY", "SEE YOU AT SIX"))
        # DK2XY asks, then fetches it
        self.rx(a, "DA2JH QUERY MSGS", call="DK2XY")
        self.assertEqual(self.sent(a)[0], f"DK2XY YES MSG ID {mid}")
        self.rx(a, f"DA2JH QUERY MSG {mid}", call="DK2XY")
        self.assertEqual(self.inbox.value(mid)["type"], "STORE")
        self.assertEqual(self.sent(a)[0], "DK2XY MSG SEE YOU AT SIX FROM DL1ABC")
        self.assertEqual(self.inbox.value(mid)["type"], "DELIVERED")    # after it went out
        self.rx(a, "DA2JH QUERY MSGS", call="DK2XY")
        self.assertEqual(self.sent(a)[0], "DK2XY NO")
        self.rx(a, f"DA2JH QUERY MSG {mid}", call="DK9ZZ")               # not addressed to DK9ZZ
        self.assertIsNone(self.sent(a))

    def test_relay(self):
        a = self.auto(autoreply=True)
        self.assertEqual(self.rx(a, "DA2JH>DK2XY HELLO"), [])          # relay off
        a.configure(relay=True)
        self.rx(a, "DA2JH>DK2XY HELLO")
        self.assertEqual(self.sent(a)[0], "DK2XY>HELLO *DE* DL1ABC")   # forwarded
        self.rx(a, "DA2JH>HELLO *DE* DK2XY")
        self.assertEqual(self.sent(a)[0], "DL1ABC>DK2XY ACK")          # for me via DL1ABC
        self.rx(a, "DA2JH>SNR? *DE* DK2XY", snr=-7)
        self.assertEqual(self.sent(a)[0], "DL1ABC>DK2XY SNR -07")      # relayed autoreply command

    def test_heartbeat_ack(self):
        a = self.auto(autoreply=True)
        self.assertEqual(self.rx(a, "@HB HEARTBEAT JO62"), [])          # HB mode/HB ACK off
        a.configure(hb_mode=True, hb_ack=True)
        self.inbox.append("STORE", {"TO": "DL1ABC", "FROM": "DK2XY", "TEXT": "HI", "UTC": "2026-09-30 10:00:00"})
        self.rx(a, "@HB HEARTBEAT JO62", snr=-3)
        text, offset, is_auto = self.sent(a)
        self.assertEqual(text, "DL1ABC HEARTBEAT SNR -03 MSG ID 1")
        self.assertTrue(500 <= offset < 1000 and offset % 50 == 0 and is_auto)
        self.rx(a, "@HB HEARTBEAT JO62")
        self.assertEqual(a.queue, [])                                   # allcall cache
        a.set_submode(js8.TURBO)
        self.clock.t += 16 * 60
        self.assertEqual(self.rx(a, "@HB HEARTBEAT JO62", submode=js8.TURBO), [])   # no HB in TURBO

    def test_heartbeat_timer(self):
        a = self.auto()
        a.configure(hb_mode=True, hb_interval_min=10)
        self.assertEqual(a.hb_next % 15, 0)
        self.assertGreaterEqual(a.hb_next - self.clock.t, 600)
        self.clock.t = a.hb_next - 5
        self.assertEqual(a.tick(1500), [])
        self.clock.t = a.hb_next - A.HB_LEAD_S
        first = a.hb_next
        (ev,) = a.tick(1500)
        self.assertEqual(ev["text"], "DA2JH: HEARTBEAT JO31")
        self.assertEqual(a.hb_next, first + 600)
        text, offset, is_auto = self.sent(a, own=1500)
        self.assertTrue(500 <= offset < 1000 and is_auto)               # heartbeat goes out without autoreply
        (ev,) = a.heartbeat_now(800)
        self.assertEqual(a.queue[0].offset_hz, 800)                     # own offset <= 1000 Hz is kept

    def test_low_priority_waits_30_s(self):
        a = self.auto()
        a.queue.append(A.QueuedMessage(99, self.clock.t, A.PRIORITY_LOW, "DA2JH: HEARTBEAT JO31", 600, "x"))
        a.last_tx_start = self.clock.t - 10
        self.assertIsNone(a.next_transmission(False, 1500))
        self.clock.t += 21
        self.assertIsNotNone(a.next_transmission(False, 1500))

    def test_watchdog(self):
        a = self.auto(autoreply=True, idle_watchdog_min=60)
        a.configure(hb_mode=True, hb_interval_min=10)
        self.rx(a, "DA2JH SNR?")
        self.clock.t += 60 * 60
        (ev,) = a.tick(1500)
        self.assertEqual((ev["type"], ev["dropped"]), ("watchdog", 1))
        self.assertFalse(a.config.autoreply or a.config.hb_mode)
        self.assertIsNone(a.hb_next)
        self.assertEqual(self.rx(a, "DA2JH SNR?"), [])
        self.assertIsNone(a.next_transmission(False, 1500))
        a.operator_active()
        self.assertFalse(a.watchdog)
        self.assertTrue(self.rx(a, "DA2JH SNR?"))                        # the reply is only suggested again
        a.operator_gone()
        self.assertTrue(a.watchdog and not a.queue)

    def test_hourly_limit(self):
        a = self.auto(autoreply=True, max_auto_per_hour=3)
        for i in range(5):
            self.rx(a, "DA2JH SNR?", call=f"DK{i}AA")
        self.assertEqual([self.sent(a) is not None for _ in range(5)], [True, True, True, False, False])
        self.clock.t += 3600
        self.assertIsNotNone(self.sent(a))

    def test_operator_draft_blocks(self):
        a = self.auto(autoreply=True)
        a.operator_draft = True
        self.assertEqual(self.rx(a, "DA2JH SNR?"), [])


class InboxTests(unittest.TestCase):
    def test_schema_is_js8calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "inbox.db3")
            box = Js8Inbox(path)
            mid = box.append("STORE", {"TO": "DK2XY", "FROM": "DL1ABC", "TEXT": "HI", "UTC": "2026-09-30 10:00:00"})
            box.close()
            db = sqlite3.connect(path)
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertLessEqual({"inbox_v1", "inbox_group_recip_v1"}, tables)
            (blob,) = db.execute("SELECT blob FROM inbox_v1 WHERE id = ?", (mid,)).fetchone()
            self.assertEqual(set(json.loads(blob)), {"type", "value", "params"})
            self.assertEqual(db.execute("SELECT json_extract(blob, '$.params.TO') FROM inbox_v1").fetchone()[0], "DK2XY")
            db.close()

    def test_lookahead_and_groups(self):
        with tempfile.TemporaryDirectory() as tmp:
            box = Js8Inbox(os.path.join(tmp, "inbox.db3"))
            a = box.append("STORE", {"TO": "DK2XY", "TEXT": "ONE", "UTC": "2026-09-30 10:00:00"})
            b = box.append("STORE", {"TO": "DK2XY", "TEXT": "TWO", "UTC": "2026-09-30 10:01:00"})
            self.assertEqual(box.next_message_id_for("DK2XY/P"), a)
            self.assertEqual(box.lookahead_message_id_for("DK2XY", a), b)
            self.assertEqual(box.lookahead_message_id_for("DK2XY", b), -1)
            g = box.append("STORE", {"TO": "@NET", "TEXT": "G", "UTC": "2026-09-30 10:00:00"})
            now = 1_790_766_000.0                                       # 2026-09-30 11:00 UTC
            self.assertEqual(box.next_group_message_id_for("@NET", "DK2XY", now=now), g)
            box.mark_group_delivered(g, "DK2XY")
            self.assertEqual(box.next_group_message_id_for("@NET", "DK2XY", now=now), -1)
            self.assertEqual(box.next_group_message_id_for("@NET", "DL1ABC", now=now), g)
            self.assertEqual(box.next_group_message_id_for("@NET", "DL1ABC", now=now + 3 * 86400), -1)
            box.close()

    def test_base_callsign(self):
        for call, base in (("DA2JH", "DA2JH"), ("DA2JH/P", "DA2JH"), ("EA8/DL1ABC", "DL1ABC"),
                           ("VK/DL1ABC/P", "DL1ABC/P")):
            self.assertEqual(base_callsign(call), base)


if __name__ == "__main__":
    unittest.main()
