"""PyQt5 GUI for the PlutoSDR RX app -- FM/SSB demod controls, gain, NF
(audio) volume, RX bandwidth ("zoom") and waterfall FFT size, plus the
embedded waterfall display. Deliberately PyQt5, not PyQt6: see
pluto_tx/gui.py's docstring (libgnuradio-qtgui is linked against Qt5; mixing
Qt runtimes is a crash risk).
"""
import signal
import sys

from PyQt5 import QtCore, QtWidgets, sip

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
        while self.waterfall_container.count():
            item = self.waterfall_container.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        if tb.waterfall is not None:
            widget = sip.wrapinstance(tb.waterfall.qwidget(), QtWidgets.QWidget)
            widget.setMinimumHeight(400)
            self.waterfall_container.addWidget(widget)

    def _on_freq_changed(self, mhz):
        self.tb.set_frequency(mhz * 1e6)

    def _on_fine_changed(self, value):
        self.tb.set_fine_offset(float(value))
        self.fine_label.setText(f"{value} Hz")

    def _on_demod_changed(self, idx):
        self.tb.set_demod_mode(self.demod_combo.currentData())

    def _on_gain_mode_changed(self, idx):
        mode = self.gain_mode_combo.currentData()
        self.tb.set_gain_mode(mode)
        self.gain_slider.setEnabled(mode == "manual")

    def _on_gain_changed(self, value):
        self.tb.set_manual_gain(float(value))
        self.gain_label.setText(f"{value} dB")

    def _on_nf_gain_changed(self, value):
        self.tb.set_nf_gain(value / 100.0)
        self.nf_gain_label.setText(f"{value} %")

    def _on_fft_size_changed(self, idx):
        self.tb.set_fft_size(self.fft_size_combo.currentData())

    def _on_bandwidth_changed(self, idx):
        """RX bandwidth ("zoom") change: GNU Radio's FIR/resampler blocks
        can't change their decimation ratio at runtime, so this rebuilds the
        whole flowgraph from scratch, carrying over every other current
        setting, and swaps in the new waterfall widget."""
        new_rate = self.bandwidth_combo.currentData()
        freq = self.tb.nominal_freq_hz
        fine = self.tb.fine_offset_hz
        gain_mode = self.gain_mode_combo.currentData()
        gain_db = float(self.gain_slider.value())
        demod_mode = self.demod_combo.currentData()
        nf_gain = self.nf_gain_slider.value() / 100.0
        fft_size = self.fft_size_combo.currentData()

        self.tb.shutdown()
        new_tb = PlutoRxFlowgraph(
            uri=self.tb.uri, frequency=freq, sample_rate=new_rate, gain_mode=gain_mode,
            manual_gain_db=gain_db, demod_mode=demod_mode, nf_gain=nf_gain, enable_waterfall=True,
        )
        new_tb.set_fine_offset(fine)
        new_tb.set_fft_size(fft_size)

        self.tb = new_tb
        self._embed_waterfall(new_tb)
        self.tb.start()

    def closeEvent(self, event):
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

    def sig_handler(signum, frame):
        print(f"\nSignal {signum} received, shutting down...")
        window.tb.shutdown()
        qapp.quit()

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    orig_excepthook = sys.excepthook

    def excepthook(exc_type, exc_value, exc_tb):
        print("Uncaught exception, stopping flowgraph...", file=sys.stderr)
        window.tb.shutdown()
        orig_excepthook(exc_type, exc_value, exc_tb)

    sys.excepthook = excepthook

    try:
        return qapp.exec_()
    finally:
        window.tb.shutdown()
