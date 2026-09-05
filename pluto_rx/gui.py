"""PyQt5 GUI for the PlutoSDR RX app -- FM/SSB demod controls, gain, NF
(audio) volume, RX bandwidth ("zoom") and waterfall FFT size, plus the
embedded waterfall display. Deliberately PyQt5, not PyQt6: see
pluto_tx/gui.py's docstring (libgnuradio-qtgui is linked against Qt5; mixing
Qt runtimes is a crash risk).
"""
import signal
import sys

from PyQt5 import QtCore, QtWidgets, sip

from pluto_tx.netutil import probe_uri_with_timeout, scan_devices_with_timeout

from . import config
from .flowgraph import PlutoRxFlowgraph


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, tb: PlutoRxFlowgraph):
        super().__init__()
        self.tb = tb
        self.setWindowTitle("PlutoSDR RX")

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
        layout.addLayout(freq_row)

        # --- Demod mode + gain ------------------------------------------
        demod_row = QtWidgets.QHBoxLayout()
        demod_row.addWidget(QtWidgets.QLabel("Mode:"))
        self.demod_combo = QtWidgets.QComboBox()
        self.demod_combo.addItem("FM", PlutoRxFlowgraph.MODE_FM)
        self.demod_combo.addItem("SSB (USB)", PlutoRxFlowgraph.MODE_SSB)
        self.demod_combo.currentIndexChanged.connect(self._on_demod_changed)
        demod_row.addWidget(self.demod_combo)

        demod_row.addWidget(QtWidgets.QLabel("Gain Mode:"))
        self.gain_mode_combo = QtWidgets.QComboBox()
        for m in config.GAIN_MODES:
            self.gain_mode_combo.addItem(m, m)
        self.gain_mode_combo.setCurrentText(config.DEFAULT_GAIN_MODE)
        self.gain_mode_combo.currentIndexChanged.connect(self._on_gain_mode_changed)
        demod_row.addWidget(self.gain_mode_combo)
        layout.addLayout(demod_row)

        # --- RF gain ------------------------------------------------------
        gain_row = QtWidgets.QHBoxLayout()
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
        layout.addLayout(gain_row)

        # --- NF (audio) gain -----------------------------------------------
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

        # --- RX bandwidth ("zoom") + waterfall FFT size ---------------------
        zoom_row = QtWidgets.QHBoxLayout()
        zoom_row.addWidget(QtWidgets.QLabel("RX Bandwidth:"))
        self.bandwidth_combo = QtWidgets.QComboBox()
        for bw in config.RX_BANDWIDTH_PRESETS:
            self.bandwidth_combo.addItem(self._format_hz(bw), bw)
        self.bandwidth_combo.setCurrentIndex(config.RX_BANDWIDTH_PRESETS.index(config.DEFAULT_RX_BANDWIDTH))
        self.bandwidth_combo.currentIndexChanged.connect(self._on_bandwidth_changed)
        zoom_row.addWidget(self.bandwidth_combo)

        zoom_row.addWidget(QtWidgets.QLabel("Waterfall FFT Size:"))
        self.fft_size_combo = QtWidgets.QComboBox()
        for n in config.FFT_SIZE_PRESETS:
            self.fft_size_combo.addItem(str(n), n)
        self.fft_size_combo.setCurrentIndex(config.FFT_SIZE_PRESETS.index(config.DEFAULT_FFT_SIZE))
        self.fft_size_combo.currentIndexChanged.connect(self._on_fft_size_changed)
        zoom_row.addWidget(self.fft_size_combo)
        layout.addLayout(zoom_row)

        self.status_label = QtWidgets.QLabel()
        layout.addWidget(self.status_label)

        # --- Waterfall (in its own sub-layout so it can be swapped out on
        # an RX-bandwidth change, which rebuilds the flowgraph) -------------
        self.waterfall_container = QtWidgets.QVBoxLayout()
        layout.addLayout(self.waterfall_container)
        self._embed_waterfall(tb)

        # Idle QTimer tick: required for Ctrl-C to reach Python's signal
        # handler while Qt's event loop is running (same reason as the TX GUI).
        self._timer = QtCore.QTimer()
        self._timer.timeout.connect(lambda: None)
        self._timer.start(500)

    @staticmethod
    def _format_hz(hz):
        return f"{hz/1e6:g} MHz" if hz >= 1_000_000 else f"{hz/1e3:g} kHz"

    def _embed_waterfall(self, tb):
        # Only detach the old widget from our layout, never delete it: the
        # underlying Qt widget is owned by its gr-qtgui sink block (part of
        # the OLD flowgraph object), not by this sip.wrapinstance() wrapper.
        # Calling deleteLater() here raced the old flowgraph's own C++
        # teardown and crashed the process (verified: SIGSEGV during a real
        # bandwidth-switch test). Just unparent it; it's freed for real when
        # the old PlutoRxFlowgraph itself gets garbage collected.
        while self.waterfall_container.count():
            item = self.waterfall_container.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
        if tb is not None and tb.waterfall is not None:
            widget = sip.wrapinstance(tb.waterfall.qwidget(), QtWidgets.QWidget)
            widget.setMinimumHeight(400)
            self.waterfall_container.addWidget(widget)

    def _set_connected_controls_enabled(self, enabled: bool):
        for w in (self.freq_spin, self.fine_slider, self.demod_combo, self.gain_mode_combo,
                  self.nf_gain_slider, self.bandwidth_combo, self.fft_size_combo):
            w.setEnabled(enabled)
        self.gain_slider.setEnabled(enabled and self.gain_mode_combo.currentData() == "manual")

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
        self._embed_waterfall(None)
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
        try:
            new_tb = PlutoRxFlowgraph(
                uri=uri,
                frequency=self.freq_spin.value() * 1e6,
                sample_rate=self.bandwidth_combo.currentData(),
                gain_mode=self.gain_mode_combo.currentData(),
                manual_gain_db=float(self.gain_slider.value()),
                demod_mode=self.demod_combo.currentData(),
                nf_gain=self.nf_gain_slider.value() / 100.0,
                enable_waterfall=True,
            )
        except Exception as e:
            self.status_label.setText(f"Could not connect to {uri}: {e}")
            return
        new_tb.set_fine_offset(float(self.fine_slider.value()))
        new_tb.set_fft_size(self.fft_size_combo.currentData())
        self.tb = new_tb
        self._embed_waterfall(new_tb)
        new_tb.start()
        self._set_connected_controls_enabled(True)
        self.connect_button.setText("Disconnect")
        self.uri_combo.setEnabled(False)
        self.status_label.setText(f"Connected to {uri}.")

    def _on_freq_changed(self, mhz):
        if self.tb is None:
            return
        self.tb.set_frequency(mhz * 1e6)

    def _on_fine_changed(self, value):
        if self.tb is not None:
            self.tb.set_fine_offset(float(value))
        self.fine_label.setText(f"{value} Hz")

    def _on_demod_changed(self, idx):
        if self.tb is None:
            return
        self.tb.set_demod_mode(self.demod_combo.currentData())

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
        self.tb.set_fft_size(self.fft_size_combo.currentData())

    def _on_bandwidth_changed(self, idx):
        """RX bandwidth ("zoom") change: GNU Radio's FIR/resampler blocks
        can't change their decimation ratio at runtime, so this rebuilds the
        whole flowgraph from scratch, carrying over every other current
        setting, and swaps in the new waterfall widget."""
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

        # Construct the REPLACEMENT flowgraph before touching the current
        # one: gr-iio only creates/holds its RX buffer once tb.start() runs,
        # not at construction, so building new_tb here is safe while the old
        # one is still running -- and if construction fails (e.g. a bad
        # sample rate the hardware rejects), the old one is untouched and
        # RX keeps working instead of being left dead.
        try:
            new_tb = PlutoRxFlowgraph(
                uri=self.tb.uri, frequency=freq, sample_rate=new_rate, gain_mode=gain_mode,
                manual_gain_db=gain_db, demod_mode=demod_mode, nf_gain=nf_gain, enable_waterfall=True,
            )
        except Exception as e:
            self.status_label.setText(f"Could not switch to {self._format_hz(new_rate)}: {e}")
            self.bandwidth_combo.blockSignals(True)
            self.bandwidth_combo.setCurrentIndex(config.RX_BANDWIDTH_PRESETS.index(int(self.tb.sample_rate)))
            self.bandwidth_combo.blockSignals(False)
            return

        new_tb.set_fine_offset(fine)
        new_tb.set_fft_size(fft_size)

        self.tb.shutdown()
        self.tb = new_tb
        self._embed_waterfall(new_tb)
        self.tb.start()
        self.status_label.setText(f"Switched to {self._format_hz(new_rate)}.")

    def closeEvent(self, event):
        if self.tb is not None:
            self.tb.shutdown()
        event.accept()


def run_gui(build_tb):
    """build_tb: callable that constructs and returns a PlutoRxFlowgraph.
    Must be called AFTER QApplication exists -- the flowgraph's waterfall
    sink is a real Qt widget."""
    qapp = QtWidgets.QApplication(sys.argv)
    tb = build_tb()
    window = MainWindow(tb)
    window.show()
    tb.start()
    del tb  # window.tb is now the only reference -- a stray one here would
    # keep the old flowgraph (and its AD9361 buffer claim) alive forever,
    # breaking every reconnect after the first with "Unable to create
    # buffer: -16" (EBUSY). Same bug, verified on pluto_tx's identical
    # run_gui() shape; fixed the same way here for consistency.

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
