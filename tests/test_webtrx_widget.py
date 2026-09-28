"""pluto_tx/webtrx_widget.py with a fake controller: no subprocess, no
server, no dialogs (every dialog hook is replaced)."""
import os
import subprocess
import threading
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt5 import QtWidgets
except ImportError:  # pragma: no cover
    QtWidgets = None

from pluto_tx import webtrx_control as wc


class FakeControl:
    def __init__(self, state=wc.STOPPED, devices=None, installed=True):
        self.state = state
        self.devices = devices
        self._installed = installed
        self.status_calls = 0
        self.started = threading.Event()
        self.stopped = threading.Event()
        self.start_rc = 0
        self.stop_rc = 0
        self.last_status = None
        self.holds_result = None

    def status(self):
        self.status_calls += 1
        self.last_status = wc.Status(self.state, url="https://localhost:8321", devices=self.devices)
        return self.last_status

    def installed(self):
        return self._installed, "" if self._installed else "missing"

    def holds(self, device_type, timeout=0.5):
        if isinstance(self.holds_result, Exception):
            raise self.holds_result
        return self.holds_result

    def start(self):
        self.started.set()
        self.state = wc.RUNNING
        return subprocess.CompletedProcess(["start.sh"], self.start_rc, "ok\n", "")

    def stop(self):
        self.stopped.set()
        self.state = wc.STOPPED
        return subprocess.CompletedProcess(["stop.sh"], self.stop_rc, "", "")

    def restore_devices(self):
        return [("tx", "pluto", "ip:pluto.local")]

    def url(self):
        return "https://localhost:8321"


@unittest.skipIf(QtWidgets is None, "PyQt5 not installed")
class WebTrxRowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {wc.DISABLE_ENV: "on"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.device_open = False
        self.released = 0
        self.messages = []

    def make_row(self, control):
        from pluto_tx.webtrx_widget import WebTrxRow

        row = WebTrxRow(has_device=lambda: self.device_open, release_device=self.release,
                        device_type=lambda: "pluto", show_message=self.messages.append, control=control)
        row.confirm_start_with_device = mock.Mock(return_value=None)
        row.confirm_stop = mock.Mock(return_value=True)
        row.confirm_connect_dialog = mock.Mock(return_value=False)
        row.notify_warning = mock.Mock()
        row.open_url = mock.Mock()
        from PyQt5 import sip

        self.addCleanup(lambda: None if sip.isdeleted(row) else row.deleteLater())
        return row

    def release(self):
        self.released += 1
        self.device_open = False

    def pump(self, until=lambda: False, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if until():
                return True
            time.sleep(0.01)
        return False

    def test_no_polling_until_shown(self):
        ctl = FakeControl()
        row = self.make_row(ctl)
        self.pump(timeout=0.3)
        self.assertEqual(ctl.status_calls, 0)
        row.show()
        self.assertTrue(self.pump(lambda: ctl.status_calls >= 1))
        self.assertTrue(self.pump(lambda: row.state_label.text() == "stopped"))
        self.assertTrue(row.start_button.isEnabled())
        self.assertFalse(row.stop_button.isEnabled())
        self.assertFalse(row.open_button.isEnabled())

    def test_disabled_by_environment(self):
        with mock.patch.dict(os.environ, {wc.DISABLE_ENV: "off"}):
            ctl = FakeControl()
            row = self.make_row(ctl)
            row.show()
            self.pump(timeout=0.3)
            self.assertEqual(ctl.status_calls, 0)
            self.assertEqual(row.state_label.text(), "disabled")
            self.assertTrue(row.confirm_connect("pluto"))
            self.assertFalse(row.startup_blocks("pluto"))

    def test_buttons_follow_state(self):
        cases = {
            wc.RUNNING: (False, True, True),
            wc.STARTING: (False, True, False),
            wc.UNRESPONSIVE: (False, True, True),
            wc.RUNNING_ELSEWHERE: (False, False, True),
            wc.NOT_INSTALLED: (False, False, False),
        }
        for state, (start, stop, open_) in cases.items():
            row = self.make_row(FakeControl(state))
            row._on_status(row.control.status())
            self.assertEqual((row.start_button.isEnabled(), row.stop_button.isEnabled(), row.open_button.isEnabled()),
                             (start, stop, open_), state)

    def test_state_text_names_held_devices(self):
        ctl = FakeControl(wc.RUNNING, devices={"rx": {"connected": True, "device_type": "rtlsdr"},
                                               "tx": {"connected": False, "device_type": None}})
        row = self.make_row(ctl)
        row._on_status(ctl.status())
        self.assertEqual(row.state_label.text(), "running (RX RTL-SDR)")

    def test_start_without_open_device(self):
        ctl = FakeControl()
        row = self.make_row(ctl)
        row._on_status(ctl.status())
        row.start_button.click()
        self.assertTrue(self.pump(lambda: any("started" in m for m in self.messages)))
        self.assertTrue(ctl.started.is_set())
        row.confirm_start_with_device.assert_not_called()

    def test_start_with_open_device_cancelled(self):
        self.device_open = True
        ctl = FakeControl()
        row = self.make_row(ctl)
        row._on_status(ctl.status())
        row.start_button.click()
        self.pump(timeout=0.3)
        row.confirm_start_with_device.assert_called_once_with("pluto", [("tx", "pluto", "ip:pluto.local")])
        self.assertFalse(ctl.started.is_set())
        self.assertEqual(self.released, 0)

    def test_start_with_open_device_release_first(self):
        self.device_open = True
        ctl = FakeControl()
        row = self.make_row(ctl)
        row.confirm_start_with_device.return_value = "release"
        row._on_status(ctl.status())
        row.start_button.click()
        self.assertTrue(self.pump(lambda: ctl.started.is_set()))
        self.assertEqual(self.released, 1)

    def test_start_with_open_device_anyway(self):
        self.device_open = True
        ctl = FakeControl()
        row = self.make_row(ctl)
        row.confirm_start_with_device.return_value = "anyway"
        row._on_status(ctl.status())
        row.start_button.click()
        self.assertTrue(self.pump(lambda: ctl.started.is_set()))
        self.assertEqual(self.released, 0)

    def test_start_failure_is_reported(self):
        ctl = FakeControl()
        ctl.start_rc = 1
        row = self.make_row(ctl)
        row._on_status(ctl.status())
        row.start_button.click()
        self.assertTrue(self.pump(lambda: any("failed (exit 1)" in m for m in self.messages)))

    def test_stop_needs_confirmation(self):
        ctl = FakeControl(wc.RUNNING)
        row = self.make_row(ctl)
        row._on_status(ctl.status())
        row.confirm_stop.return_value = False
        row.stop_button.click()
        self.pump(timeout=0.3)
        self.assertFalse(ctl.stopped.is_set())
        row.confirm_stop.return_value = True
        row.stop_button.click()
        self.assertTrue(self.pump(lambda: any("stopped" in m for m in self.messages)))
        row.notify_warning.assert_not_called()

    def test_forced_kill_warns(self):
        ctl = FakeControl(wc.RUNNING)
        ctl.stop_rc = 2
        row = self.make_row(ctl)
        row._on_status(ctl.status())
        row.stop_button.click()
        self.assertTrue(self.pump(lambda: row.notify_warning.called))
        self.assertIn("safe state", row.notify_warning.call_args.args[0])

    def test_buttons_locked_while_a_script_runs(self):
        ctl = FakeControl()
        gate = threading.Event()
        original_start = ctl.start
        ctl.start = lambda: (gate.wait(5), original_start())[1]
        row = self.make_row(ctl)
        row._on_status(ctl.status())
        row.start_button.click()
        self.pump(timeout=0.1)
        self.assertEqual(row.state_label.text(), "starting...")
        self.assertFalse(row.start_button.isEnabled() or row.stop_button.isEnabled())
        row._on_start_clicked()                     # a second click does nothing
        gate.set()
        self.assertTrue(self.pump(lambda: row._busy is None))

    def test_open_uses_url(self):
        ctl = FakeControl(wc.RUNNING)
        row = self.make_row(ctl)
        row._on_status(ctl.status())
        row.open_button.click()
        row.open_url.assert_called_once_with("https://localhost:8321")

    def test_confirm_connect(self):
        ctl = FakeControl(wc.RUNNING, devices={"rx": {"connected": False, "device_type": None},
                                               "tx": {"connected": True, "device_type": "pluto"}})
        row = self.make_row(ctl)
        self.assertTrue(row.confirm_connect("pluto"))            # status unknown: no dialog, no I/O
        row.confirm_connect_dialog.assert_not_called()
        self.assertEqual(ctl.status_calls, 0)
        ctl.status()
        self.assertTrue(row.confirm_connect("hackrf"))           # other type: no dialog
        row.confirm_connect_dialog.assert_not_called()
        self.assertFalse(row.confirm_connect("pluto"))           # held: dialog, answered "No"
        row.confirm_connect_dialog.return_value = True
        self.assertTrue(row.confirm_connect("pluto"))

    def test_startup_blocks(self):
        ctl = FakeControl()
        row = self.make_row(ctl)
        for result, blocks in ((True, True), (False, False), (None, False), (RuntimeError("x"), False)):
            ctl.holds_result = result
            self.assertEqual(row.startup_blocks("pluto"), blocks, result)
        ctl._installed = False
        ctl.holds_result = True
        self.assertFalse(row.startup_blocks("pluto"))
        self.assertIn("Pluto", row.startup_message("pluto"))

    def test_result_after_the_widget_is_gone_is_dropped(self):
        ctl = FakeControl()
        gate = threading.Event()
        original_status = ctl.status
        ctl.status = lambda: (gate.wait(5), original_status())[1]
        row = self.make_row(ctl)
        row.show()
        self.pump(timeout=0.1)
        from PyQt5 import sip
        sip.delete(row)
        gate.set()
        time.sleep(0.2)
        self.pump(timeout=0.2)                      # no crash, nothing to assert beyond that


if __name__ == "__main__":
    unittest.main()
