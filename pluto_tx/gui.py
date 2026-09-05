"""PyQt5 GUI for the PlutoSDR TX app (stage 3+).

Deliberately PyQt5, not PyQt6: libgnuradio-qtgui on this system is linked
against Qt5, and mixing two Qt runtimes in one process is a crash risk.
"""
import os
import signal
import sys

from PyQt5 import QtCore, QtWidgets, sip

from . import config
from .flowgraph import PlutoTxFlowgraph, M17_AVAILABLE, _gr_atten
from .netutil import probe_uri_with_timeout, scan_devices_with_timeout


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, tb: PlutoTxFlowgraph):
        super().__init__()
        self.tb = tb
        self.setWindowTitle("PlutoSDR TX")
        self._armed = True  # False after emergency stop, until re-armed
        self._atten_ceiling_db = tb.atten_ceiling_db  # fixed for the session, carried across reconnects
        self._wav_path = tb.wav_path  # carried across reconnects; updated on a file pick

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        layout = QtWidgets.QVBoxLayout(central)

        # --- Device (network/USB URI) -----------------------------------
        device_row = QtWidgets.QHBoxLayout()
        device_row.addWidget(QtWidgets.QLabel("Device (hostname or IP):"))
        self.uri_combo = QtWidgets.QComboBox()
        self.uri_combo.setEditable(True)
        self.uri_combo.setInsertPolicy(QtWidgets.QComboBox.NoInsert)
        self.uri_combo.addItem(tb.uri)
        self.uri_combo.setEnabled(False)  # editable only while disconnected
        self.uri_combo.setToolTip(
            "libiio context URI, e.g. plutoplus.local, 192.168.1.50, or a full "
            "URI like usb:1.5.5 to pick a specific device when more than one "
            "Pluto is reachable. A bare hostname/IP gets 'ip:' prefixed "
            "automatically. Use Scan to discover devices on the network/USB."
        )
        device_row.addWidget(self.uri_combo, 1)
        self.scan_button = QtWidgets.QPushButton("Scan")
        self.scan_button.clicked.connect(self._on_scan_clicked)
        device_row.addWidget(self.scan_button)
        self.connect_button = QtWidgets.QPushButton("Disconnect")
        self.connect_button.clicked.connect(self._on_connect_clicked)
        device_row.addWidget(self.connect_button)
        layout.addLayout(device_row)

        # --- Frequency + fine tune ---------------------------------
        freq_row = QtWidgets.QHBoxLayout()
        freq_row.addWidget(QtWidgets.QLabel("Frequency (MHz):"))
        self.freq_spin = QtWidgets.QDoubleSpinBox()
        self.freq_spin.setDecimals(4)
        self.freq_spin.setRange(47.0, 6000.0)
        self.freq_spin.setSingleStep(0.001)
        self.freq_spin.setValue(tb.nominal_freq_hz / 1e6)
        self.freq_spin.valueChanged.connect(self._on_freq_changed)
        freq_font = self.freq_spin.font()
        freq_font.setPointSize(freq_font.pointSize() + 6)
        self.freq_spin.setFont(freq_font)
        freq_row.addWidget(self.freq_spin)

        freq_row.addWidget(QtWidgets.QLabel("Fine tune (Hz):"))
        self.fine_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.fine_slider.setRange(-config.FINE_TUNE_RANGE_HZ, config.FINE_TUNE_RANGE_HZ)
        self.fine_slider.setValue(0)
        self.fine_slider.valueChanged.connect(self._on_fine_changed)
        freq_row.addWidget(self.fine_slider)
        self.fine_label = QtWidgets.QLabel("0 Hz")
        self.fine_label.setMinimumWidth(70)
        freq_row.addWidget(self.fine_label)
        layout.addLayout(freq_row)

        # --- Mode + source -------------------------------------------
        mode_row = QtWidgets.QHBoxLayout()
        mode_row.addWidget(QtWidgets.QLabel("Mode:"))
        self.mode_combo = QtWidgets.QComboBox()
        self.mode_combo.addItem("FM", PlutoTxFlowgraph.MODE_FM)
        self.mode_combo.addItem("SSB (USB)", PlutoTxFlowgraph.MODE_SSB)
        self.mode_combo.addItem("M17", PlutoTxFlowgraph.MODE_M17)
        if not M17_AVAILABLE:
            # gr-m17 is an optional, from-source dependency (see
            # install-m17.sh) -- grey out rather than hide, so the operator
            # can see the mode exists and why it's unavailable.
            m17_item_idx = self.mode_combo.findData(PlutoTxFlowgraph.MODE_M17)
            item = self.mode_combo.model().item(m17_item_idx)
            item.setEnabled(False)
            self.mode_combo.setItemData(
                m17_item_idx, "gr-m17 is not installed -- see install-m17.sh / README", QtCore.Qt.ToolTipRole
            )
        # Sync to the flowgraph's ACTUAL mode before wiring the change
        # signal -- otherwise the combo always shows "FM" regardless of
        # what mode tb was actually constructed with (e.g. --mode ssb, or
        # M17 passed in directly), and connecting the signal first would
        # fire _on_mode_changed() -> tb.set_mode() before tb.start() has
        # run, which raises (blocks.selector's ninputs isn't known yet).
        initial_idx = self.mode_combo.findData(tb.mode)
        if initial_idx >= 0:
            self.mode_combo.setCurrentIndex(initial_idx)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        mode_row.addWidget(self.mode_combo)

        mode_row.addWidget(QtWidgets.QLabel("Source:"))
        self.source_combo = QtWidgets.QComboBox()
        self.source_combo.addItem("Microphone", PlutoTxFlowgraph.SRC_MIC)
        self.source_combo.addItem("Audio File", PlutoTxFlowgraph.SRC_FILE)
        self.source_combo.currentIndexChanged.connect(self._on_source_changed)
        mode_row.addWidget(self.source_combo)

        self.file_button = QtWidgets.QPushButton(self._file_button_text(tb.wav_path))
        self.file_button.setEnabled(False)
        self.file_button.clicked.connect(self._on_pick_file)
        mode_row.addWidget(self.file_button)
        layout.addLayout(mode_row)

        # --- M17 callsigns (only meaningful/enabled in M17 mode) -----------
        m17_row = QtWidgets.QHBoxLayout()
        m17_row.addWidget(QtWidgets.QLabel("M17 Src Callsign:"))
        self.m17_src_edit = QtWidgets.QLineEdit(tb.m17_src_callsign)
        self.m17_src_edit.setMaxLength(config.M17_CALLSIGN_MAX_LEN)
        self.m17_src_edit.setPlaceholderText("e.g. DA2JH")
        self.m17_src_edit.textChanged.connect(self._on_m17_src_callsign_changed)
        m17_row.addWidget(self.m17_src_edit)
        m17_row.addWidget(QtWidgets.QLabel("Dst Callsign:"))
        self.m17_dst_edit = QtWidgets.QLineEdit(tb.m17_dst_callsign)
        self.m17_dst_edit.setMaxLength(config.M17_CALLSIGN_MAX_LEN)
        self.m17_dst_edit.textChanged.connect(self._on_m17_dst_callsign_changed)
        m17_row.addWidget(self.m17_dst_edit)
        m17_row.addStretch(1)
        layout.addLayout(m17_row)
        self._update_m17_controls_enabled()

        # --- Power / attenuation ---------------------------------------
        power_row = QtWidgets.QHBoxLayout()
        power_row.addWidget(QtWidgets.QLabel("TX Power (Attenuation, dB):"))
        self.power_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.unlock_full_power = QtWidgets.QCheckBox("Unlock full power")
        self._refresh_power_slider_range()
        self.power_slider.valueChanged.connect(self._on_power_changed)
        self.unlock_full_power.stateChanged.connect(self._on_unlock_changed)
        power_row.addWidget(self.power_slider)
        self.power_label = QtWidgets.QLabel()
        power_row.addWidget(self.power_label)
        power_row.addWidget(self.unlock_full_power)
        layout.addLayout(power_row)
        self.power_slider.setValue(int(round(tb.target_atten_db)))

        # --- NF (audio) gain -------------------------------------------
        nf_row = QtWidgets.QHBoxLayout()
        nf_row.addWidget(QtWidgets.QLabel("Audio Gain:"))
        self.nf_gain_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.nf_gain_slider.setRange(0, 300)  # percent
        self.nf_gain_slider.setValue(int(config.DEFAULT_NF_GAIN * 100))
        self.nf_gain_slider.valueChanged.connect(self._on_nf_gain_changed)
        nf_row.addWidget(self.nf_gain_slider)
        self.nf_gain_label = QtWidgets.QLabel(f"{int(config.DEFAULT_NF_GAIN * 100)} %")
        self.nf_gain_label.setMinimumWidth(50)
        nf_row.addWidget(self.nf_gain_label)
        layout.addLayout(nf_row)

        # --- NF dynamics processing: noise gate, compressor, limiter -------
        # Attack/release/knee stay fixed (config.py) at all three stages --
        # exposing threshold (+ ratio for the compressor) is the "medium"
        # control depth the operator asked for; the limiter is enable-only
        # (a fixed, tuned safety/quality stage, not a session knob).
        gate_row = QtWidgets.QHBoxLayout()
        self.gate_enable = QtWidgets.QCheckBox("Noise Gate")
        self.gate_enable.setChecked(True)
        self.gate_enable.toggled.connect(self._on_gate_enabled_changed)
        gate_row.addWidget(self.gate_enable)
        gate_row.addWidget(QtWidgets.QLabel("Threshold (dB):"))
        self.gate_threshold_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.gate_threshold_slider.setRange(-80, -20)
        self.gate_threshold_slider.setValue(int(config.GATE_THRESHOLD_DB))
        self.gate_threshold_slider.valueChanged.connect(self._on_gate_threshold_changed)
        gate_row.addWidget(self.gate_threshold_slider)
        self.gate_threshold_label = QtWidgets.QLabel(f"{int(config.GATE_THRESHOLD_DB)} dB")
        self.gate_threshold_label.setMinimumWidth(50)
        gate_row.addWidget(self.gate_threshold_label)
        layout.addLayout(gate_row)

        comp_row = QtWidgets.QHBoxLayout()
        self.compressor_enable = QtWidgets.QCheckBox("Compressor")
        self.compressor_enable.setChecked(True)
        self.compressor_enable.toggled.connect(self._on_compressor_enabled_changed)
        comp_row.addWidget(self.compressor_enable)
        comp_row.addWidget(QtWidgets.QLabel("Threshold (dB):"))
        self.compressor_threshold_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.compressor_threshold_slider.setRange(-40, 0)
        self.compressor_threshold_slider.setValue(int(config.COMPRESSOR_THRESHOLD_DB))
        self.compressor_threshold_slider.valueChanged.connect(self._on_compressor_threshold_changed)
        comp_row.addWidget(self.compressor_threshold_slider)
        self.compressor_threshold_label = QtWidgets.QLabel(f"{int(config.COMPRESSOR_THRESHOLD_DB)} dB")
        self.compressor_threshold_label.setMinimumWidth(50)
        comp_row.addWidget(self.compressor_threshold_label)
        comp_row.addWidget(QtWidgets.QLabel("Ratio:"))
        self.compressor_ratio_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.compressor_ratio_slider.setRange(1, 10)
        self.compressor_ratio_slider.setValue(int(config.COMPRESSOR_RATIO))
        self.compressor_ratio_slider.valueChanged.connect(self._on_compressor_ratio_changed)
        comp_row.addWidget(self.compressor_ratio_slider)
        self.compressor_ratio_label = QtWidgets.QLabel(f"{int(config.COMPRESSOR_RATIO)}:1")
        self.compressor_ratio_label.setMinimumWidth(40)
        comp_row.addWidget(self.compressor_ratio_label)
        self.compressor_gr_label = QtWidgets.QLabel("GR: 0.0 dB")
        self.compressor_gr_label.setMinimumWidth(70)
        comp_row.addWidget(self.compressor_gr_label)
        layout.addLayout(comp_row)

        limiter_row = QtWidgets.QHBoxLayout()
        self.limiter_enable = QtWidgets.QCheckBox("Smooth Limiter (before the hard safety clip)")
        self.limiter_enable.setChecked(True)
        self.limiter_enable.toggled.connect(self._on_limiter_enabled_changed)
        limiter_row.addWidget(self.limiter_enable)
        limiter_row.addStretch(1)
        layout.addLayout(limiter_row)
        layout.addSpacing(16)

        # --- PTT + emergency stop + status -------------------------------
        btn_row = QtWidgets.QHBoxLayout()
        self.ptt_button = QtWidgets.QPushButton()
        self.ptt_button.setMinimumHeight(60)
        self.ptt_button.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        btn_row.addWidget(self.ptt_button, 3)

        btn_row.addStretch(1)

        # Plain action button, not a persistent toggle -- the PTT button's
        # own label already reflects the current mode (see
        # _reset_ptt_button_visual), so this one doesn't need to.
        self.ptt_mode_button = QtWidgets.QPushButton("Toggle PTT Mode")
        self.ptt_mode_button.setMinimumHeight(60)
        self.ptt_mode_button.clicked.connect(self._on_ptt_mode_toggle_clicked)
        btn_row.addWidget(self.ptt_mode_button)

        # Toggle: one button covers both E-STOP (unchecked -> checked) and
        # re-arm (checked -> unchecked) -- see _on_estop_toggled.
        self.estop_button = QtWidgets.QPushButton("E-STOP")
        self.estop_button.setMinimumHeight(60)
        self.estop_button.setCheckable(True)
        self.estop_button.toggled.connect(self._on_estop_toggled)
        self._style_estop_button(locked=False)
        btn_row.addWidget(self.estop_button)
        layout.addLayout(btn_row)

        # Wires up the PTT button's signals for the default mode (click-toggle).
        self._configure_ptt_button(hold_mode=False)

        self.tx_indicator = QtWidgets.QLabel("READY")
        self.tx_indicator.setAlignment(QtCore.Qt.AlignCenter)
        self.tx_indicator.setMinimumHeight(40)
        self._set_indicator_idle()
        layout.addWidget(self.tx_indicator)

        self.status_label = QtWidgets.QLabel()
        layout.addWidget(self.status_label)

        # Separate from status_label on purpose: this is overwritten every
        # 500ms by _tick()'s HW readback, which would otherwise clobber a
        # connect/disconnect/scan message before the operator ever sees it
        # (that's exactly what happened -- the message was there, just
        # invisible, making Connect look like it silently did nothing).
        self.hw_status_label = QtWidgets.QLabel()
        layout.addWidget(self.hw_status_label)

        # --- Live TX waterfall (own sub-layout so it can be swapped out on
        # a device reconnect, which rebuilds the flowgraph) -----------------
        self.waterfall_container = QtWidgets.QVBoxLayout()
        layout.addLayout(self.waterfall_container)
        self._embed_waterfall(tb)

        # --- Lifecycle: periodic tick doubles as (a) Ctrl-C responsiveness
        # for Qt's event loop and (b) a live hardware-state readback, so the
        # GUI can never silently disagree with what's actually on the AD9361
        # (the exact failure mode we hit with SDRangel earlier).
        self._timer = QtCore.QTimer()
        self._timer.timeout.connect(self._tick)
        self._timer.start(500)

    # --- helpers ------------------------------------------------------
    @staticmethod
    def _file_button_text(wav_path):
        from .flowgraph import _PLACEHOLDER_WAV
        if os.path.abspath(wav_path) == os.path.abspath(_PLACEHOLDER_WAV):
            return "Choose File"
        return os.path.basename(wav_path)

    def _embed_waterfall(self, tb):
        # Only detach the old widget from our layout, never delete it: the
        # underlying Qt widget is owned by its gr-qtgui sink block (part of
        # the OLD flowgraph object), not by this sip.wrapinstance() wrapper --
        # deleteLater() here would race the old flowgraph's own C++ teardown
        # (see pluto_rx/gui.py's identical note, verified by a real crash there).
        while self.waterfall_container.count():
            item = self.waterfall_container.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
        if tb is not None and tb.waterfall is not None:
            widget = sip.wrapinstance(tb.waterfall.qwidget(), QtWidgets.QWidget)
            widget.setMinimumHeight(300)
            self.waterfall_container.addWidget(widget)

    def _set_connected_controls_enabled(self, enabled: bool):
        for w in (self.freq_spin, self.fine_slider, self.mode_combo, self.source_combo,
                  self.power_slider, self.unlock_full_power, self.nf_gain_slider,
                  self.gate_enable, self.compressor_enable, self.limiter_enable,
                  self.ptt_button, self.ptt_mode_button, self.estop_button):
            w.setEnabled(enabled)
        # Threshold/ratio sliders additionally depend on their own stage's
        # checkbox -- no point leaving them interactive while that stage is
        # bypassed.
        self.gate_threshold_slider.setEnabled(enabled and self.gate_enable.isChecked())
        self.compressor_threshold_slider.setEnabled(enabled and self.compressor_enable.isChecked())
        self.compressor_ratio_slider.setEnabled(enabled and self.compressor_enable.isChecked())
        self.file_button.setEnabled(enabled and self.source_combo.currentData() == PlutoTxFlowgraph.SRC_FILE)
        self._m17_connected = enabled
        self._update_m17_controls_enabled()

    def _update_m17_controls_enabled(self):
        enabled = getattr(self, "_m17_connected", True) and self.mode_combo.currentData() == PlutoTxFlowgraph.MODE_M17
        self.m17_src_edit.setEnabled(enabled)
        self.m17_dst_edit.setEnabled(enabled)

    def _style_estop_button(self, locked: bool):
        self.estop_button.setText("Re-arm" if locked else "E-STOP")
        color = "#7f8c8d" if locked else "#c0392b"
        self.estop_button.setStyleSheet(f"background-color: {color}; color: white; font-weight: bold;")

    def _refresh_power_slider_range(self):
        ceiling = 0 if self.unlock_full_power.isChecked() else config.DEFAULT_ATTEN_CEILING
        self.power_slider.setRange(int(round(config.MIN_ATTEN)), int(round(ceiling)))

    def _set_indicator_idle(self):
        self.tx_indicator.setText("READY" if self._armed else "E-STOP - LOCKED")
        color = "#27ae60" if self._armed else "#7f8c8d"
        self.tx_indicator.setStyleSheet(f"background-color: {color}; color: white; font-size: 18pt; font-weight: bold;")

    def _set_indicator_on_air(self):
        self.tx_indicator.setText("ON AIR")
        self.tx_indicator.setStyleSheet("background-color: #c0392b; color: white; font-size: 18pt; font-weight: bold;")

    def _set_indicator_ending(self):
        # M17 only: shown during the brief EOT tail after PTT release, while
        # RF is intentionally still up so the receiver gets a clean end-of-
        # stream instead of a hard cutoff.
        self.tx_indicator.setText("ENDING...")
        self.tx_indicator.setStyleSheet("background-color: #e67e22; color: white; font-size: 18pt; font-weight: bold;")

    # --- slots ------------------------------------------------------
    def _on_freq_changed(self, mhz):
        self.tb.set_frequency(mhz * 1e6)

    def _on_fine_changed(self, value):
        self.tb.set_fine_offset(float(value))
        self.fine_label.setText(f"{value} Hz")

    def _on_nf_gain_changed(self, value):
        gain = value / 100.0
        self.tb.set_nf_gain(gain)
        self.nf_gain_label.setText(f"{value} %")

    def _on_gate_enabled_changed(self, checked):
        self.tb.set_gate_enabled(checked)
        self.gate_threshold_slider.setEnabled(checked and self.gate_enable.isEnabled())

    def _on_gate_threshold_changed(self, value):
        self.tb.set_gate_threshold(float(value))
        self.gate_threshold_label.setText(f"{value} dB")

    def _on_compressor_enabled_changed(self, checked):
        self.tb.set_compressor_enabled(checked)
        enabled = checked and self.compressor_enable.isEnabled()
        self.compressor_threshold_slider.setEnabled(enabled)
        self.compressor_ratio_slider.setEnabled(enabled)

    def _on_compressor_threshold_changed(self, value):
        self.tb.set_compressor_threshold(float(value))
        self.compressor_threshold_label.setText(f"{value} dB")

    def _on_compressor_ratio_changed(self, value):
        self.tb.set_compressor_ratio(float(value))
        self.compressor_ratio_label.setText(f"{value}:1")

    def _on_limiter_enabled_changed(self, checked):
        self.tb.set_limiter_enabled(checked)

    def _on_mode_changed(self, idx):
        self.tb.set_mode(self.mode_combo.currentData())
        self._update_m17_controls_enabled()

    def _on_m17_src_callsign_changed(self, text):
        if self.tb is not None:
            self.tb.set_m17_src_callsign(text)

    def _on_m17_dst_callsign_changed(self, text):
        if self.tb is not None:
            self.tb.set_m17_dst_callsign(text)

    def _on_source_changed(self, idx):
        source = self.source_combo.currentData()
        self.tb.set_source(source)
        self.file_button.setEnabled(source == PlutoTxFlowgraph.SRC_FILE)

    def _on_pick_file(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Choose Audio File", "", "WAV files (*.wav)")
        if not path or self.tb is None:
            return
        # blocks.wavfile_source has no runtime file-swap API in this GNU
        # Radio build, so loading a different file needs a full flowgraph
        # rebuild -- exactly the same rebuild _rebuild() already does for a
        # device reconnect, just keeping the current uri and swapping the
        # wav_path instead.
        self._rebuild(self.tb.uri, path)

    def _on_power_changed(self, value):
        self.tb.set_target_power(float(value))
        self.power_label.setText(f"{value} dB")

    def _on_unlock_changed(self, _state):
        self._refresh_power_slider_range()

    def _on_ptt_mode_toggle_clicked(self):
        self._configure_ptt_button(hold_mode=not self._ptt_hold_mode)

    def _configure_ptt_button(self, hold_mode: bool):
        """Rewire the PTT button between click-toggle and press-and-hold
        semantics. Switching modes while keyed would leave the RF on with no
        way to release it under the new mode's signals -- always unkey first."""
        if self.tb.keyed:
            self._release_ptt()

        for signal in (self.ptt_button.toggled, self.ptt_button.pressed, self.ptt_button.released):
            try:
                signal.disconnect()
            except TypeError:
                pass  # nothing was connected yet

        self._ptt_hold_mode = hold_mode
        self.ptt_button.setCheckable(not hold_mode)
        self._reset_ptt_button_visual()

        if hold_mode:
            self.ptt_button.pressed.connect(self._on_ptt_pressed)
            self.ptt_button.released.connect(self._on_ptt_released)
        else:
            self.ptt_button.toggled.connect(self._on_ptt_toggled)

    def _reset_ptt_button_visual(self):
        self.ptt_button.blockSignals(True)
        if self._ptt_hold_mode:
            self.ptt_button.setText("PTT (hold to send)")
        else:
            self.ptt_button.setChecked(False)
            self.ptt_button.setText("PTT (click to send)")
        self.ptt_button.blockSignals(False)

    def _on_ptt_toggled(self, checked):
        if checked and not self._armed:
            self.ptt_button.blockSignals(True)
            self.ptt_button.setChecked(False)
            self.ptt_button.blockSignals(False)
            return
        if checked:
            self.tb.key_ptt()
            self.ptt_button.setText("PTT (click to stop)")
            self._set_indicator_on_air()
        else:
            self.ptt_button.setText("PTT (click to send)")
            self._release_ptt()

    def _on_ptt_pressed(self):
        if not self._armed:
            return
        self.tb.key_ptt()
        self._set_indicator_on_air()

    def _on_ptt_released(self):
        if not self.tb.keyed:
            return
        self._release_ptt()

    def _release_ptt(self):
        """Shared PTT-release handling (both PTT interaction modes, plus the
        safety-unkey when switching between them). M17 can't cut RF
        instantly: unkey_ptt() sends EOT but deliberately leaves attenuation
        up for its tail (see flowgraph.py's unkey_ptt() docstring), so the
        indicator shows an intermediate state instead of jumping straight
        back to READY, and a bounded timer finishes the job. E-STOP is
        NOT routed through here -- it calls tb.unkey_ptt() + force_safe_state()
        directly, an unconditional override of any pending M17 tail."""
        self.tb.unkey_ptt()
        if self.mode_combo.currentData() == PlutoTxFlowgraph.MODE_M17:
            self._set_indicator_ending()
            QtCore.QTimer.singleShot(int(config.M17_EOT_HOLD_S * 1000), self._finish_m17_unkey)
        else:
            self._set_indicator_idle()

    def _finish_m17_unkey(self):
        if self.tb is not None:
            self.tb.finish_unkey_m17()
        self._set_indicator_idle()

    def _on_connect_clicked(self):
        if self.tb is not None:
            self._disconnect()
        else:
            self._connect(self.uri_combo.currentText())

    def _on_scan_clicked(self):
        self.status_label.setText("Scanning for devices...")
        QtWidgets.QApplication.processEvents()
        devices, error = scan_devices_with_timeout()
        if error is not None:
            self.status_label.setText(f"Scan failed: {error}")
            return
        devices.pop("local:", None)  # this machine's own sensors, never a Pluto
        current = self.uri_combo.currentText()
        self.uri_combo.blockSignals(True)
        self.uri_combo.clear()
        for uri, desc in devices.items():
            idx = self.uri_combo.count()
            self.uri_combo.addItem(uri)
            self.uri_combo.setItemData(idx, desc, QtCore.Qt.ToolTipRole)
        if current and self.uri_combo.findText(current) < 0:
            self.uri_combo.addItem(current)
        if current:
            self.uri_combo.setCurrentText(current)
        self.uri_combo.blockSignals(False)
        self.status_label.setText(f"Found {len(devices)} device(s)." if devices else "No devices found.")

    def _reset_session_ui_state(self):
        """Reset PTT/E-STOP visuals to a fresh, safe, unkeyed, re-armed
        state. Used both on a full disconnect and on any flowgraph rebuild
        (device reconnect, file swap) -- the underlying tb is always brand
        new and starts unkeyed/re-armed (PlutoSafety.prepare_for_start()
        runs in its constructor), so the GUI must never show stale
        checked/locked visuals left over from the flowgraph it replaced."""
        self._reset_ptt_button_visual()
        self.estop_button.blockSignals(True)
        self.estop_button.setChecked(False)
        self.estop_button.blockSignals(False)
        self._style_estop_button(locked=False)
        self._armed = True
        self._set_indicator_idle()

    def _disconnect(self):
        self.tb.shutdown_safe()
        self.tb = None
        self._embed_waterfall(None)
        self._reset_session_ui_state()
        self._set_connected_controls_enabled(False)
        self.connect_button.setText("Connect")
        self.uri_combo.setEnabled(True)
        self.compressor_gr_label.setText("GR: 0.0 dB")
        self.status_label.setText("Disconnected.")

    def _connect(self, uri_text):
        uri = config.normalize_uri(uri_text)
        if not uri:
            self.status_label.setText("Please enter a device hostname, IP, or URI.")
            return
        self._rebuild(uri, self._wav_path)

    def _rebuild(self, uri, wav_path):
        """Tear down the current flowgraph (if any) and build a fresh one at
        `uri` loading `wav_path`, carrying over every other current GUI
        setting. Shared by device reconnects (Connect button) and WAV file
        swaps (File button) -- both need the exact same "safely stop the old
        TX chain, then build and start a new one" sequence; a stray
        reference to the old flowgraph here would leak its AD9361 buffer
        claim and break the next connect with 'Unable to create buffer'
        (see run_gui()'s 'del tb' fix for the same underlying issue)."""
        if self.tb is not None:
            self.tb.shutdown_safe()
            self.tb = None
            self._embed_waterfall(None)
            self._reset_session_ui_state()
        self.status_label.setText(f"Connecting to {uri}...")
        QtWidgets.QApplication.processEvents()
        probe_error = probe_uri_with_timeout(uri)
        if probe_error is not None:
            self.status_label.setText(f"Could not connect to {uri}: {probe_error}")
            self._set_connected_controls_enabled(False)
            self.connect_button.setText("Connect")
            self.uri_combo.setEnabled(True)
            return
        try:
            new_tb = PlutoTxFlowgraph(
                uri=uri,
                frequency=self.freq_spin.value() * 1e6,
                atten_ceiling_db=self._atten_ceiling_db,
                wav_path=wav_path,
                mode=self.mode_combo.currentData(),
                source=self.source_combo.currentData(),
                enable_waterfall=True,
                m17_src_callsign=self.m17_src_edit.text(),
                m17_dst_callsign=self.m17_dst_edit.text(),
            )
        except Exception as e:
            self.status_label.setText(f"Could not connect to {uri}: {e}")
            self._set_connected_controls_enabled(False)
            self.connect_button.setText("Connect")
            self.uri_combo.setEnabled(True)
            return
        new_tb.set_fine_offset(float(self.fine_slider.value()))
        new_tb.set_nf_gain(self.nf_gain_slider.value() / 100.0)
        new_tb.set_target_power(float(self.power_slider.value()))
        # Reapply dynamics-processing settings -- a fresh flowgraph starts at
        # config.py's defaults, which would otherwise silently diverge from
        # what these controls still visually show after any rebuild (device
        # reconnect or WAV file swap), the same state-sync gap nf_gain above
        # already avoids.
        new_tb.set_gate_threshold(float(self.gate_threshold_slider.value()))
        new_tb.set_gate_enabled(self.gate_enable.isChecked())
        new_tb.set_compressor_threshold(float(self.compressor_threshold_slider.value()))
        new_tb.set_compressor_ratio(float(self.compressor_ratio_slider.value()))
        new_tb.set_compressor_enabled(self.compressor_enable.isChecked())
        new_tb.set_limiter_enabled(self.limiter_enable.isChecked())
        self._wav_path = new_tb.wav_path
        self.tb = new_tb
        self._embed_waterfall(new_tb)
        self.file_button.setText(self._file_button_text(new_tb.wav_path))
        new_tb.start()
        self._set_connected_controls_enabled(True)
        self.connect_button.setText("Disconnect")
        self.uri_combo.setEnabled(False)
        self.status_label.setText(f"Connected to {uri}.")

    def _on_estop_toggled(self, checked):
        """checked=True: E-STOP triggered. checked=False: re-armed. One
        toggle button covers both directions instead of two separate ones."""
        if checked:
            self.tb.unkey_ptt()
            self.tb.safety.force_safe_state()
            self._armed = False
            self._reset_ptt_button_visual()
            self.ptt_button.setEnabled(False)
            self._set_indicator_idle()
            self._style_estop_button(locked=True)
            self.status_label.setText("E-STOP triggered: attenuation at minimum, LO powered down.")
        else:
            self.tb.safety.prepare_for_start()
            self.tb.pluto_sink.set_attenuation(0, _gr_atten(config.MIN_ATTEN))
            self._armed = True
            self.ptt_button.setEnabled(True)
            self._set_indicator_idle()
            self._style_estop_button(locked=False)
            self.status_label.setText("Re-armed.")

    def _tick(self):
        if self.tb is None:
            self.hw_status_label.setText("")
            return
        try:
            state = self.tb.safety.read_state()
            self.hw_status_label.setText(
                f"HW: hardwaregain={state['hardwaregain_db']:.2f} dB, "
                f"LO powerdown={state['lo_powerdown']}"
            )
        except Exception as e:
            self.hw_status_label.setText(f"HW status read failed: {e}")
        self.compressor_gr_label.setText(f"GR: {self.tb.compressor.gain_reduction_db():.1f} dB")

    # --- shutdown lifecycle -------------------------------------------
    def closeEvent(self, event):
        if self.tb is not None:
            self.tb.shutdown_safe()
        event.accept()


def run_gui(build_tb):
    """build_tb: callable that constructs and returns a PlutoTxFlowgraph.
    Must be called AFTER QApplication exists -- the flowgraph's optional
    waterfall sink is a real Qt widget."""
    qapp = QtWidgets.QApplication(sys.argv)
    tb = build_tb()
    window = MainWindow(tb)
    window.show()
    tb.start()
    del tb  # window.tb is now the only reference -- see MainWindow._disconnect's
    # note: a stray reference here would keep the old flowgraph (and its
    # AD9361 buffer claim) alive forever, since Python never garbage-collects
    # it, breaking every reconnect after the first with "Unable to create
    # buffer: -16" (EBUSY). Verified: this reproduced the exact bug.

    def sig_handler(signum, frame):
        print(f"\nSignal {signum} received, shutting down safely...")
        if window.tb is not None:
            window.tb.shutdown_safe()
        qapp.quit()

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    orig_excepthook = sys.excepthook

    def excepthook(exc_type, exc_value, exc_tb):
        print("Uncaught exception, forcing safe state...", file=sys.stderr)
        if window.tb is not None:
            window.tb.shutdown_safe()
        orig_excepthook(exc_type, exc_value, exc_tb)

    sys.excepthook = excepthook

    try:
        return qapp.exec_()
    finally:
        if window.tb is not None:
            window.tb.shutdown_safe()
