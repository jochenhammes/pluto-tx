"""The "Web-TRX" row of the TX and RX apps: server state plus Start / Stop /
Open buttons. A thin Qt layer over webtrx_control.py, which holds all logic.

Rules this widget follows so it can never get in the way of the app:
  - It never starts or stops the server by itself, only on a button click.
    Closing the app leaves the server running (the operator's choice).
  - Polling starts at the first showEvent -- a window that is only
    constructed (every GUI unit test) never polls, never runs a subprocess.
  - Status queries and start/stop run in worker threads and report back via
    queued signals; a result arriving after the window is gone is dropped.
  - Every dialog goes through a replaceable hook attribute
    (confirm_start_with_device, confirm_stop, confirm_connect_dialog,
    notify_warning, open_url), so tests never block on a modal dialog.
  - With PLUTO_WEBTRX_CONTROL=off the row only shows a note.

Device conflicts: Web-TRX and a Qt app must never open the same SDR at the
same time (a second libiio/IIOD client could change the attenuation, LO or
sample rate under a running transmission). The app asks this widget at three
points -- see confirm_connect(), startup_blocks() and the start dialog.
"""
from __future__ import annotations

import threading

from PyQt5 import QtCore, QtGui, QtWidgets, sip

from . import webtrx_control
from .webtrx_control import (NOT_INSTALLED, RUNNING, RUNNING_ELSEWHERE, STARTING, STOPPED, UNRESPONSIVE,
                             Status, WebTrxControl, integration_disabled)

POLL_INTERVAL_MS = 3000

_DEVICE_NAMES = {"pluto": "Pluto", "hackrf": "HackRF", "rtlsdr": "RTL-SDR", "aioc": "AIOC"}

_STATE_TEXT = {
    NOT_INSTALLED: "not installed",
    STOPPED: "stopped",
    STARTING: "starting...",
    RUNNING: "running",
    UNRESPONSIVE: "not responding",
    RUNNING_ELSEWHERE: "running (other checkout)",
}


def device_name(device_type: str | None) -> str:
    return _DEVICE_NAMES.get(device_type or "", device_type or "?")


class WebTrxRow(QtWidgets.QWidget):
    _status_ready = QtCore.pyqtSignal(object)        # Status
    _action_done = QtCore.pyqtSignal(str, object)    # "start"/"stop", subprocess.CompletedProcess

    def __init__(self, *, has_device, release_device, device_type, show_message,
                 control: WebTrxControl | None = None, parent=None):
        """has_device() -> bool: the app has an SDR open right now.
        release_device(): close it (safe state) before Web-TRX takes over.
        device_type() -> str: the app's currently selected device type.
        show_message(text): the app's status line."""
        super().__init__(parent)
        self.control = control or WebTrxControl()
        self._has_device = has_device
        self._release_device = release_device
        self._device_type = device_type
        self._show_message = show_message
        self._disabled = integration_disabled()
        self._polling_started = False
        self._poll_running = False
        self._busy: str | None = None   # "start"/"stop" while a script runs
        self._status: Status | None = None

        # Dialog hooks -- replaced in tests.
        self.confirm_start_with_device = self._dialog_start_with_device
        self.confirm_stop = self._dialog_stop
        self.confirm_connect_dialog = self._dialog_connect
        self.notify_warning = self._dialog_warning
        self.open_url = lambda url: QtGui.QDesktopServices.openUrl(QtCore.QUrl(url))

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QtWidgets.QLabel("Web-TRX:"))
        self.state_label = QtWidgets.QLabel("...")
        # never widen the app's column for a long state text -- details are in the tooltip
        self.state_label.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        self.state_label.setMinimumWidth(120)
        layout.addWidget(self.state_label, 1)
        self.start_button = QtWidgets.QPushButton("Start")
        self.stop_button = QtWidgets.QPushButton("Stop")
        self.open_button = QtWidgets.QPushButton("Open")
        for button in (self.start_button, self.stop_button, self.open_button):
            button.setSizePolicy(QtWidgets.QSizePolicy.Maximum, QtWidgets.QSizePolicy.Fixed)
            button.setEnabled(False)
            layout.addWidget(button)
        self.start_button.setToolTip("Start the Web-TRX server (browser UI). It keeps running when this app closes.")
        self.stop_button.setToolTip("Stop the Web-TRX server. Ends any transmission it is making.")
        self.open_button.setToolTip("Open the Web-TRX page in the browser.")
        self.start_button.clicked.connect(self._on_start_clicked)
        self.stop_button.clicked.connect(self._on_stop_clicked)
        self.open_button.clicked.connect(self._on_open_clicked)

        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(POLL_INTERVAL_MS)
        self._timer.timeout.connect(self.refresh)
        self._status_ready.connect(self._on_status)
        self._action_done.connect(self._on_action_done)

        if self._disabled:
            self.state_label.setText("disabled")
            self.state_label.setToolTip(f"{webtrx_control.DISABLE_ENV}=off is set.")

    # -- polling ---------------------------------------------------------

    def showEvent(self, event):
        super().showEvent(event)
        if not self._polling_started and not self._disabled:
            self._polling_started = True
            self.refresh()
            self._timer.start()

    def refresh(self):
        """Query the state in a worker thread (at most one at a time)."""
        if self._poll_running or self._disabled:
            return
        self._poll_running = True
        threading.Thread(target=self._poll_worker, name="webtrx-status", daemon=True).start()

    def _poll_worker(self):
        try:
            st = self.control.status()
        except Exception as e:  # never let a status problem surface as an app error
            st = Status(STOPPED, detail=f"status query failed: {e}")
        self._emit("_status_ready", st)

    def _emit(self, signal_name, *args):
        """Emit from a worker thread (queued to the GUI thread); dropped if the
        window is gone by now."""
        try:
            if not sip.isdeleted(self):
                getattr(self, signal_name).emit(*args)
        except RuntimeError:  # deleted between the check and the emit
            pass

    def _on_status(self, st: Status):
        self._poll_running = False
        self._status = st
        self._update_view()

    def _update_view(self):
        st = self._status
        if self._busy is not None:
            self.state_label.setText("starting..." if self._busy == "start" else "stopping...")
            for button in (self.start_button, self.stop_button, self.open_button):
                button.setEnabled(False)
            return
        if st is None:
            return
        text = _STATE_TEXT.get(st.state, st.state)
        held = st.held_devices()
        if held:
            text += " (" + ", ".join(f"{d.upper()} {device_name(t)}" for d, t in held) + ")"
        self.state_label.setText(text)
        tip = [st.url or "", st.detail, f"PID {st.pid}" if st.pid else "", f"Backend {st.backend}" if st.backend else ""]
        self.state_label.setToolTip("\n".join(t for t in tip if t))
        self.start_button.setEnabled(st.state == STOPPED)
        self.stop_button.setEnabled(st.state in (RUNNING, STARTING, UNRESPONSIVE))
        self.open_button.setEnabled(st.state in (RUNNING, UNRESPONSIVE, RUNNING_ELSEWHERE))

    # -- buttons ---------------------------------------------------------

    def _on_start_clicked(self):
        if self._busy is not None:
            return
        if self._has_device():
            choice = self.confirm_start_with_device(self._device_type(), self.control.restore_devices())
            if choice not in ("release", "anyway"):
                return
            if choice == "release":
                self._release_device()
        self._run_action("start", self.control.start)

    def _on_stop_clicked(self):
        if self._busy is not None or not self.confirm_stop():
            return
        self._run_action("stop", self.control.stop)

    def _on_open_clicked(self):
        self.open_url(self.control.url())

    def _run_action(self, action, fn):
        self._busy = action
        self._update_view()
        self._show_message(f"Web-TRX: {'starting' if action == 'start' else 'stopping'} the server...")

        def work():
            try:
                result = fn()
            except Exception as e:
                result = e
            self._emit("_action_done", action, result)

        threading.Thread(target=work, name=f"webtrx-{action}", daemon=True).start()

    def _on_action_done(self, action, result):
        self._busy = None
        if isinstance(result, Exception):
            self._show_message(f"Web-TRX {action} failed: {result}")
        else:
            rc = result.returncode
            last = _last_line(result.stdout) or _last_line(result.stderr)
            if action == "start":
                if rc == 4:
                    log = self.control.run_dir / "web-trx.log"
                    self._show_message(f"Web-TRX process is up but not confirmed yet -- see {log}.")
                else:
                    messages = {0: f"Web-TRX started: {self.control.url()}",
                                3: "Web-TRX was already running."}
                    self._show_message(messages.get(rc, f"Web-TRX start failed (exit {rc}): {last}"))
            else:
                if rc == 2:
                    self.notify_warning(
                        "Web-TRX did not shut down cleanly and had to be killed.\n\n"
                        "Check the transmitter's safe state (e.g. with pluto-cli or iio_attr) before using it.")
                messages = {0: "Web-TRX stopped.", 2: "Web-TRX killed -- check the transmitter's safe state!"}
                self._show_message(messages.get(rc, f"Web-TRX stop failed (exit {rc}): {last}"))
        self.refresh()

    # -- guards the app calls ----------------------------------------------

    def confirm_connect(self, device_type: str | None) -> bool:
        """Before a manual Connect: True = go ahead. Asks only if the LAST
        known status says Web-TRX holds a device of this type; unknown state
        means no dialog and no I/O."""
        if self._disabled:
            return True
        st = self.control.last_status
        if st is None or st.holds(device_type) is not True:
            return True
        return bool(self.confirm_connect_dialog(device_type, st))

    def startup_blocks(self, device_type: str | None) -> bool:
        """Before the app's automatic connect at startup: True = skip it,
        because Web-TRX is known to hold a device of this type. One short
        query; any doubt means False (connect as before)."""
        if self._disabled:
            return False
        try:
            if not self.control.installed()[0]:
                return False
            return self.control.holds(device_type, timeout=0.5) is True
        except Exception:
            return False

    def startup_message(self, device_type: str | None) -> str:
        return (f"Web-TRX has the {device_name(device_type)} open -- not connecting automatically. "
                "Stop Web-TRX first, or pick another device and press Connect.")

    # -- default dialogs ---------------------------------------------------

    def _dialog_start_with_device(self, device_type, restore):
        box = QtWidgets.QMessageBox(self.window())
        box.setIcon(QtWidgets.QMessageBox.Warning)
        box.setWindowTitle("Start Web-TRX")
        if restore:
            reopen = ", ".join(f"{d.upper()} {device_name(t)} ({c or 'auto'})" for d, t, c in restore)
        else:
            reopen = "none (the operator connects in the browser)"
        box.setText(f"This app has the {device_name(device_type)} open.\n\n"
                    f"Web-TRX re-opens at start: {reopen}.\n\n"
                    "Two programs must never use the same SDR at the same time.")
        release = box.addButton("Disconnect and start", QtWidgets.QMessageBox.AcceptRole)
        anyway = box.addButton("Start anyway (other device)", QtWidgets.QMessageBox.DestructiveRole)
        cancel = box.addButton(QtWidgets.QMessageBox.Cancel)
        box.setDefaultButton(cancel)
        box.exec_()
        clicked = box.clickedButton()
        return "release" if clicked is release else "anyway" if clicked is anyway else None

    def _dialog_stop(self):
        answer = QtWidgets.QMessageBox.question(
            self.window(), "Stop Web-TRX",
            "Stop the Web-TRX server? Any transmission it is making ends, browser users are disconnected.",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No, QtWidgets.QMessageBox.No)
        return answer == QtWidgets.QMessageBox.Yes

    def _dialog_connect(self, device_type, st):
        answer = QtWidgets.QMessageBox.warning(
            self.window(), "Device in use by Web-TRX",
            f"Web-TRX has a {device_name(device_type)} open. If it is the same device, both programs would "
            "control it at once.\n\nConnect anyway?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No, QtWidgets.QMessageBox.No)
        return answer == QtWidgets.QMessageBox.Yes

    def _dialog_warning(self, text):
        QtWidgets.QMessageBox.warning(self.window(), "Web-TRX", text)


def _last_line(text) -> str:
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1].strip() if lines else ""
