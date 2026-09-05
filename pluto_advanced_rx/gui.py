"""PyQt5 GUI for the PlutoSDR advanced RX app -- same controls as pluto_rx
(FM/SSB demod, gain, NF volume, RX bandwidth/zoom, device connect/disconnect
/scan) plus the interactive pyqtgraph waterfall (AdvancedWaterfallWidget):
tuned-frequency marker, click-to-tune, demod-bandwidth shading, and a live
frequency axis above the waterfall. Deliberately PyQt5, not PyQt6: see
pluto_tx/gui.py's docstring (libgnuradio-qtgui is linked against Qt5; mixing
Qt runtimes is a crash risk) -- pyqtgraph itself doesn't care, but the rest
of this codebase does.

Unlike pluto_rx/gui.py, there is no _embed_waterfall swap-out dance: the
waterfall widget here is a plain Python/PyQt-owned QWidget (not a
sip.wrapinstance()-wrapped C++ object owned by a gr-qtgui sink block), so it
is built ONCE and simply persists across every device reconnect and RX
bandwidth rebuild -- the whole class of bug pluto_rx/gui.py's
"never call deleteLater(), only setParent(None)" rule works around (a real
SIGSEGV, from the old widget racing its owning flowgraph's C++ teardown)
cannot happen here, because nothing here is C++-owned.
"""
import signal
import sys

from PyQt5 import QtCore, QtWidgets

from pluto_tx.netutil import probe_uri_with_timeout, scan_devices_with_timeout

from . import config
from .flowgraph import AdvancedRxFlowgraph
from .waterfall_widget import AdvancedWaterfallWidget


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, tb: AdvancedRxFlowgraph):
        super().__init__()
        self.tb = tb
        self._fft_gen = -1
        self.setWindowTitle("PlutoSDR Advanced RX")

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        layout = QtWidgets.QVBoxLayout(central)

        # --- Device group: URI entry/scan/connect -------------------------
        device_group = QtWidgets.QGroupBox("Device")
        device_row = QtWidgets.QHBoxLayout(device_group)
        device_row.addWidget(QtWidgets.QLabel("Hostname or IP:"))
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
        layout.addWidget(device_group)

        # --- Receiver group: frequency, demodulator, gain, bandwidth ------
        receiver_group = QtWidgets.QGroupBox("Receiver")
        receiver_layout = QtWidgets.QVBoxLayout(receiver_group)

        freq_row = QtWidgets.QHBoxLayout()
        freq_row.addWidget(QtWidgets.QLabel("Frequency (MHz):"))
        self.freq_spin = QtWidgets.QDoubleSpinBox()
        self.freq_spin.setDecimals(4)
        self.freq_spin.setRange(70.0, 6000.0)
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
        receiver_layout.addLayout(freq_row)

        demod_row = QtWidgets.QHBoxLayout()
        demod_row.addWidget(QtWidgets.QLabel("Mode:"))
        self.demod_combo = QtWidgets.QComboBox()
        self.demod_combo.addItem("FM", AdvancedRxFlowgraph.MODE_FM)
        self.demod_combo.addItem("SSB (USB)", AdvancedRxFlowgraph.MODE_SSB)
        self.demod_combo.currentIndexChanged.connect(self._on_demod_changed)
        demod_row.addWidget(self.demod_combo)

        demod_row.addWidget(QtWidgets.QLabel("Width (Hz):"))
        self.width_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        w_lo, w_hi = config.FM_DEMOD_WIDTH_RANGE_HZ
        self.width_slider.setRange(int(w_lo), int(w_hi))
        self.width_slider.setValue(int(config.FM_DEMOD_WIDTH_DEFAULT_HZ))
        self.width_slider.valueChanged.connect(self._on_width_changed)
        demod_row.addWidget(self.width_slider)
        self.width_label = QtWidgets.QLabel(f"{int(config.FM_DEMOD_WIDTH_DEFAULT_HZ)} Hz")
        self.width_label.setMinimumWidth(60)
        demod_row.addWidget(self.width_label)
        receiver_layout.addLayout(demod_row)

        gain_row = QtWidgets.QHBoxLayout()
        gain_row.addWidget(QtWidgets.QLabel("Gain Mode:"))
        self.gain_mode_combo = QtWidgets.QComboBox()
        for m in config.GAIN_MODES:
            self.gain_mode_combo.addItem(m, m)
        self.gain_mode_combo.setCurrentText(config.DEFAULT_GAIN_MODE)
        self.gain_mode_combo.currentIndexChanged.connect(self._on_gain_mode_changed)
        gain_row.addWidget(self.gain_mode_combo)

        gain_row.addWidget(QtWidgets.QLabel("RF Gain (dB):"))
        self.gain_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        lo, hi = config.MANUAL_GAIN_RANGE_DB
        self.gain_slider.setRange(int(lo), int(hi))
        self.gain_slider.setValue(int(config.DEFAULT_MANUAL_GAIN_DB))
        self.gain_slider.setEnabled(config.DEFAULT_GAIN_MODE == "manual")
        self.gain_slider.valueChanged.connect(self._on_gain_changed)
        gain_row.addWidget(self.gain_slider)
        self.gain_label = QtWidgets.QLabel(f"{int(config.DEFAULT_MANUAL_GAIN_DB)} dB")
        self.gain_label.setMinimumWidth(50)
        gain_row.addWidget(self.gain_label)
        receiver_layout.addLayout(gain_row)

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
        receiver_layout.addLayout(nf_row)

        bandwidth_row = QtWidgets.QHBoxLayout()
        bandwidth_row.addWidget(QtWidgets.QLabel("RX Bandwidth:"))
        self.bandwidth_combo = QtWidgets.QComboBox()
        for bw in config.RX_BANDWIDTH_PRESETS:
            self.bandwidth_combo.addItem(self._format_hz(bw), bw)
        self.bandwidth_combo.setCurrentIndex(config.RX_BANDWIDTH_PRESETS.index(config.DEFAULT_RX_BANDWIDTH))
        self.bandwidth_combo.currentIndexChanged.connect(self._on_bandwidth_changed)
        bandwidth_row.addWidget(self.bandwidth_combo)
        bandwidth_row.addStretch(1)
        receiver_layout.addLayout(bandwidth_row)

        layout.addWidget(receiver_group)

        # --- Waterfall / spectrum group -------------------------------------
        waterfall_group = QtWidgets.QGroupBox("Waterfall / Spectrum")
        waterfall_layout = QtWidgets.QVBoxLayout(waterfall_group)

        wf_controls_row = QtWidgets.QHBoxLayout()
        wf_controls_row.addWidget(QtWidgets.QLabel("FFT Size:"))
        self.fft_size_combo = QtWidgets.QComboBox()
        for n in config.FFT_SIZE_PRESETS:
            self.fft_size_combo.addItem(str(n), n)
        self.fft_size_combo.setCurrentIndex(config.FFT_SIZE_PRESETS.index(config.DEFAULT_FFT_SIZE))
        self.fft_size_combo.currentIndexChanged.connect(self._on_fft_size_changed)
        wf_controls_row.addWidget(self.fft_size_combo)
        wf_controls_row.addStretch(1)
        waterfall_layout.addLayout(wf_controls_row)

        # Built ONCE, persists across every reconnect/rebuild (see module
        # docstring).
        self.waterfall = AdvancedWaterfallWidget(
            fft_size=config.DEFAULT_FFT_SIZE, history_rows=config.WATERFALL_HISTORY_ROWS,
            colormap_name=config.WATERFALL_COLORMAP, db_range=config.WATERFALL_DB_RANGE,
        )
        self.waterfall.frequency_clicked.connect(self._on_waterfall_clicked)

        # Floor/Ceiling: vertical sliders stacked to the right of the
        # spectrum+waterfall, since the noise floor varies a lot with
        # antenna/location/gain and shouldn't be baked in as a fixed value.
        db_lo, db_hi = config.WATERFALL_DB_RANGE
        db_sliders_col = QtWidgets.QVBoxLayout()
        db_sliders_col.addWidget(QtWidgets.QLabel("Ceiling"), alignment=QtCore.Qt.AlignHCenter)
        self.db_ceiling_slider = QtWidgets.QSlider(QtCore.Qt.Vertical)
        self.db_ceiling_slider.setRange(-150, 40)
        self.db_ceiling_slider.setValue(int(db_hi))
        self.db_ceiling_slider.valueChanged.connect(self._on_db_range_changed)
        db_sliders_col.addWidget(self.db_ceiling_slider, 1, alignment=QtCore.Qt.AlignHCenter)
        self.db_ceiling_label = QtWidgets.QLabel(f"{int(db_hi)} dB")
        db_sliders_col.addWidget(self.db_ceiling_label, alignment=QtCore.Qt.AlignHCenter)

        db_sliders_col.addWidget(QtWidgets.QLabel("Floor"), alignment=QtCore.Qt.AlignHCenter)
        self.db_floor_slider = QtWidgets.QSlider(QtCore.Qt.Vertical)
        self.db_floor_slider.setRange(-150, 40)
        self.db_floor_slider.setValue(int(db_lo))
        self.db_floor_slider.valueChanged.connect(self._on_db_range_changed)
        db_sliders_col.addWidget(self.db_floor_slider, 1, alignment=QtCore.Qt.AlignHCenter)
        self.db_floor_label = QtWidgets.QLabel(f"{int(db_lo)} dB")
        db_sliders_col.addWidget(self.db_floor_label, alignment=QtCore.Qt.AlignHCenter)

        content_row = QtWidgets.QHBoxLayout()
        content_row.addWidget(self.waterfall, 1)
        content_row.addLayout(db_sliders_col)
        waterfall_layout.addLayout(content_row, 1)

        layout.addWidget(waterfall_group, 1)

        self.status_label = QtWidgets.QLabel()
        layout.addWidget(self.status_label)

        self._sync_waterfall()

        # QTimer polls fft_probe for new rows (waterfall render) and also
        # doubles as Ctrl-C responsiveness for Qt's event loop, same reason
        # as every other GUI in this repo.
        self._timer = QtCore.QTimer()
        self._timer.timeout.connect(self._poll_fft)
        self._timer.start(config.WATERFALL_POLL_INTERVAL_MS)

    @staticmethod
    def _format_hz(hz):
        return f"{hz/1e6:g} MHz" if hz >= 1_000_000 else f"{hz/1e3:g} kHz"

    def _set_connected_controls_enabled(self, enabled: bool):
        for w in (self.freq_spin, self.fine_slider, self.demod_combo, self.width_slider,
                  self.gain_mode_combo, self.nf_gain_slider, self.bandwidth_combo, self.fft_size_combo):
            w.setEnabled(enabled)
        self.gain_slider.setEnabled(enabled and self.gain_mode_combo.currentData() == "manual")

    def _sync_waterfall(self):
        """Push the current tuned frequency/span/demod-band to the waterfall
        widget. Must be called any time the tuned frequency, fine offset,
        sample rate, demod mode, or demod width changes -- otherwise the
        widget's displayed frequency axis silently goes stale while new FFT
        rows keep arriving under it (the real bug this fixed: manually
        entering a new frequency retuned the hardware and moved the marker,
        but never told the widget its axis had moved, so the axis stayed
        put while the actual RF content shifted -- and click-to-tune, which
        reads frequencies off that same stale axis, tuned to the wrong
        place as a direct result). The demod-band shading reflects the
        ACTUAL configured filter width now (tb.fm_demod_width_hz /
        tb.ssb_demod_width_hz), not an estimate."""
        if self.tb is None:
            return
        freq = self.tb.nominal_freq_hz + self.tb.fine_offset_hz
        self.waterfall.set_frequency_range(freq, self.tb.sample_rate)
        self.waterfall.set_tuned_frequency(freq)
        if self.demod_combo.currentData() == AdvancedRxFlowgraph.MODE_FM:
            half_bw = self.tb.fm_demod_width_hz / 2
            self.waterfall.set_demod_band(freq - half_bw, freq + half_bw)
        else:
            f_lo = config.SSB_AUDIO_BAND_HZ[0]
            self.waterfall.set_demod_band(freq + f_lo, freq + f_lo + self.tb.ssb_demod_width_hz)

    def _poll_fft(self):
        if self.tb is None:
            return
        row, self._fft_gen = self.tb.fft_probe.get_latest_row(self._fft_gen)
        if row is not None:
            self.waterfall.push_fft_row(row)

    # --- slots ------------------------------------------------------
    def _on_freq_changed(self, mhz):
        if self.tb is None:
            return
        self.tb.set_frequency(mhz * 1e6)
        self._sync_waterfall()

    def _on_fine_changed(self, value):
        if self.tb is not None:
            self.tb.set_fine_offset(float(value))
            self._sync_waterfall()
        self.fine_label.setText(f"{value} Hz")

    def _on_demod_changed(self, idx):
        mode = self.demod_combo.currentData()
        # The width slider always shows/edits whichever mode is now
        # selected -- its range and current value come straight from the
        # flowgraph's own per-mode state (tb.fm_demod_width_hz /
        # tb.ssb_demod_width_hz), which is untouched by merely switching
        # which branch demod_selector picks, so any earlier customization
        # for that mode is preserved rather than reset to a default.
        if mode == AdvancedRxFlowgraph.MODE_FM:
            w_lo, w_hi = config.FM_DEMOD_WIDTH_RANGE_HZ
            width = self.tb.fm_demod_width_hz if self.tb is not None else config.FM_DEMOD_WIDTH_DEFAULT_HZ
        else:
            w_lo, w_hi = config.SSB_DEMOD_WIDTH_RANGE_HZ
            width = self.tb.ssb_demod_width_hz if self.tb is not None else config.SSB_DEMOD_WIDTH_DEFAULT_HZ
        self.width_slider.blockSignals(True)
        self.width_slider.setRange(int(w_lo), int(w_hi))
        self.width_slider.setValue(int(width))
        self.width_slider.blockSignals(False)
        self.width_label.setText(f"{int(width)} Hz")

        if self.tb is None:
            return
        self.tb.set_demod_mode(mode)
        self._sync_waterfall()

    def _on_width_changed(self, value):
        self.width_label.setText(f"{value} Hz")
        if self.tb is None:
            return
        if self.demod_combo.currentData() == AdvancedRxFlowgraph.MODE_FM:
            self.tb.set_fm_demod_width(float(value))
        else:
            self.tb.set_ssb_demod_width(float(value))
        self._sync_waterfall()

    def _on_db_range_changed(self, _value=None):
        lo = self.db_floor_slider.value()
        hi = self.db_ceiling_slider.value()
        self.db_floor_label.setText(f"{lo} dB")
        self.db_ceiling_label.setText(f"{hi} dB")
        if lo >= hi:
            return  # transient invalid state while the operator is still dragging the other slider
        self.waterfall.set_db_range(float(lo), float(hi))

    def _on_gain_mode_changed(self, idx):
        mode = self.gain_mode_combo.currentData()
        if self.tb is not None:
            self.tb.set_gain_mode(mode)
        self.gain_slider.setEnabled(mode == "manual")

    def _on_gain_changed(self, value):
        if self.tb is not None:
            self.tb.set_manual_gain(float(value))
        self.gain_label.setText(f"{value} dB")

    def _on_nf_gain_changed(self, value):
        if self.tb is not None:
            self.tb.set_nf_gain(value / 100.0)
        self.nf_gain_label.setText(f"{value} %")

    def _on_fft_size_changed(self, idx):
        if self.tb is None:
            return
        n = self.fft_size_combo.currentData()
        self.tb.set_fft_size(n)
        self.waterfall.set_fft_size(n)

    def _on_waterfall_clicked(self, freq_hz):
        if self.tb is None:
            return
        # Route through the spin box rather than calling tb.set_frequency()
        # directly, so a click reuses the exact same retune/marker-update
        # path as manual entry, including the spin box's own range clamping.
        self.freq_spin.setValue(freq_hz / 1e6)

    def _on_bandwidth_changed(self, idx):
        """RX bandwidth ("zoom") change: GNU Radio's FIR/resampler blocks
        can't change their decimation ratio at runtime, so this rebuilds the
        whole flowgraph from scratch, carrying over every other current
        setting. Unlike pluto_rx, the waterfall widget itself is NOT
        swapped -- it persists, just gets a new frequency range."""
        if self.tb is None:
            return
        new_rate = self.bandwidth_combo.currentData()
        freq = self.tb.nominal_freq_hz
        fine = self.tb.fine_offset_hz
        gain_mode = self.gain_mode_combo.currentData()
        gain_db = float(self.gain_slider.value())
        demod_mode = self.demod_combo.currentData()
        nf_gain = self.nf_gain_slider.value() / 100.0
        fft_size = self.fft_size_combo.currentData()
        fm_width = self.tb.fm_demod_width_hz
        ssb_width = self.tb.ssb_demod_width_hz

        try:
            new_tb = AdvancedRxFlowgraph(
                uri=self.tb.uri, frequency=freq, sample_rate=new_rate, gain_mode=gain_mode,
                manual_gain_db=gain_db, demod_mode=demod_mode, nf_gain=nf_gain, fft_size=fft_size,
                fm_demod_width_hz=fm_width, ssb_demod_width_hz=ssb_width,
            )
        except Exception as e:
            self.status_label.setText(f"Could not switch to {self._format_hz(new_rate)}: {e}")
            self.bandwidth_combo.blockSignals(True)
            self.bandwidth_combo.setCurrentIndex(config.RX_BANDWIDTH_PRESETS.index(int(self.tb.sample_rate)))
            self.bandwidth_combo.blockSignals(False)
            return

        new_tb.set_fine_offset(fine)
        self._fft_gen = -1
        self.tb.shutdown()
        self.tb = new_tb
        self._sync_waterfall()
        self.tb.start()
        self.status_label.setText(f"Switched to {self._format_hz(new_rate)}.")

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

    def _disconnect(self):
        self.tb.shutdown()
        self.tb = None
        self._fft_gen = -1
        self.waterfall.clear()
        self._set_connected_controls_enabled(False)
        self.connect_button.setText("Connect")
        self.uri_combo.setEnabled(True)
        self.status_label.setText("Disconnected.")

    def _connect(self, uri_text):
        uri = config.normalize_uri(uri_text)
        if not uri:
            self.status_label.setText("Please enter a device hostname, IP, or URI.")
            return
        self.status_label.setText(f"Connecting to {uri}...")
        QtWidgets.QApplication.processEvents()
        probe_error = probe_uri_with_timeout(uri)
        if probe_error is not None:
            self.status_label.setText(f"Could not connect to {uri}: {probe_error}")
            return
        # self.tb is always None here (that's the precondition for calling
        # _connect), so there's no old flowgraph to read the OTHER mode's
        # width from -- the width_slider only ever shows the CURRENTLY
        # selected mode, so that one carries over from it, the other one
        # falls back to its config default.
        if self.demod_combo.currentData() == AdvancedRxFlowgraph.MODE_FM:
            fm_width = float(self.width_slider.value())
            ssb_width = config.SSB_DEMOD_WIDTH_DEFAULT_HZ
        else:
            ssb_width = float(self.width_slider.value())
            fm_width = config.FM_DEMOD_WIDTH_DEFAULT_HZ
        try:
            new_tb = AdvancedRxFlowgraph(
                uri=uri,
                frequency=self.freq_spin.value() * 1e6,
                sample_rate=self.bandwidth_combo.currentData(),
                gain_mode=self.gain_mode_combo.currentData(),
                manual_gain_db=float(self.gain_slider.value()),
                demod_mode=self.demod_combo.currentData(),
                nf_gain=self.nf_gain_slider.value() / 100.0,
                fft_size=self.fft_size_combo.currentData(),
                fm_demod_width_hz=fm_width, ssb_demod_width_hz=ssb_width,
            )
        except Exception as e:
            self.status_label.setText(f"Could not connect to {uri}: {e}")
            return
        new_tb.set_fine_offset(float(self.fine_slider.value()))
        self._fft_gen = -1
        self.tb = new_tb
        self._sync_waterfall()
        new_tb.start()
        self._set_connected_controls_enabled(True)
        self.connect_button.setText("Disconnect")
        self.uri_combo.setEnabled(False)
        self.status_label.setText(f"Connected to {uri}.")

    def closeEvent(self, event):
        if self.tb is not None:
            self.tb.shutdown()
        event.accept()


def run_gui(build_tb):
    """build_tb: callable that constructs and returns an AdvancedRxFlowgraph.
    Must be called AFTER QApplication exists."""
    qapp = QtWidgets.QApplication(sys.argv)
    tb = build_tb()
    window = MainWindow(tb)
    window.show()
    tb.start()
    del tb  # window.tb is now the only reference -- a stray one here would
    # keep the old flowgraph (and its AD9361 buffer claim) alive forever,
    # breaking every reconnect after the first with "Unable to create
    # buffer: -16" (EBUSY). Same fix as pluto_tx/pluto_rx's run_gui().

    def sig_handler(signum, frame):
        print(f"\nSignal {signum} received, shutting down...")
        if window.tb is not None:
            window.tb.shutdown()
        qapp.quit()

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    orig_excepthook = sys.excepthook

    def excepthook(exc_type, exc_value, exc_tb):
        print("Uncaught exception, stopping flowgraph...", file=sys.stderr)
        if window.tb is not None:
            window.tb.shutdown()
        orig_excepthook(exc_type, exc_value, exc_tb)

    sys.excepthook = excepthook

    try:
        return qapp.exec_()
    finally:
        if window.tb is not None:
            window.tb.shutdown()
