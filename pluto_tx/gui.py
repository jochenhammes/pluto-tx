"""PyQt5 GUI for the PlutoSDR TX app (stage 3+).

Deliberately PyQt5, not PyQt6: libgnuradio-qtgui on this system is linked
against Qt5, and mixing two Qt runtimes in one process is a crash risk.
"""
import os
import signal
import sys

from PyQt5 import QtCore, QtWidgets, sip

from . import config
from .flowgraph import PlutoTxFlowgraph, _gr_atten


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, tb: PlutoTxFlowgraph):
        super().__init__()
        self.tb = tb
        self.setWindowTitle("PlutoSDR TX")
        self._armed = True  # False after emergency stop, until re-armed

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        layout = QtWidgets.QVBoxLayout(central)

        # --- Frequency + fine tune ---------------------------------
        freq_row = QtWidgets.QHBoxLayout()
        freq_row.addWidget(QtWidgets.QLabel("Frequency (MHz):"))
        self.freq_spin = QtWidgets.QDoubleSpinBox()
        self.freq_spin.setDecimals(4)
        self.freq_spin.setRange(47.0, 6000.0)
        self.freq_spin.setSingleStep(0.001)
        self.freq_spin.setValue(tb.nominal_freq_hz / 1e6)
        self.freq_spin.valueChanged.connect(self._on_freq_changed)
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

        self.band_label = QtWidgets.QLabel()
        layout.addWidget(self.band_label)
        self._update_band_label()

        # --- Mode + source -------------------------------------------
        mode_row = QtWidgets.QHBoxLayout()
        mode_row.addWidget(QtWidgets.QLabel("Mode:"))
        self.mode_combo = QtWidgets.QComboBox()
        self.mode_combo.addItem("FM", PlutoTxFlowgraph.MODE_FM)
        self.mode_combo.addItem("SSB (USB)", PlutoTxFlowgraph.MODE_SSB)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        mode_row.addWidget(self.mode_combo)

        mode_row.addWidget(QtWidgets.QLabel("Source:"))
        self.source_combo = QtWidgets.QComboBox()
        self.source_combo.addItem("Mikrofon", PlutoTxFlowgraph.SRC_MIC)
        self.source_combo.addItem("Audiodatei", PlutoTxFlowgraph.SRC_FILE)
        self.source_combo.currentIndexChanged.connect(self._on_source_changed)
        mode_row.addWidget(self.source_combo)

        self.file_button = QtWidgets.QPushButton("Datei wählen…")
        self.file_button.setEnabled(False)
        self.file_button.clicked.connect(self._on_pick_file)
        mode_row.addWidget(self.file_button)
        layout.addLayout(mode_row)

        self.file_label = QtWidgets.QLabel(f"Datei: {os.path.basename(tb.wav_path)}")
        layout.addWidget(self.file_label)

        # --- Power / attenuation ---------------------------------------
        power_row = QtWidgets.QHBoxLayout()
        power_row.addWidget(QtWidgets.QLabel("TX Power (Dämpfung, dB):"))
        self.power_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.unlock_full_power = QtWidgets.QCheckBox("volle Leistung freischalten")
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
        nf_row.addWidget(QtWidgets.QLabel("NF-Verstärkung:"))
        self.nf_gain_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.nf_gain_slider.setRange(0, 300)  # percent
        self.nf_gain_slider.setValue(int(config.DEFAULT_NF_GAIN * 100))
        self.nf_gain_slider.valueChanged.connect(self._on_nf_gain_changed)
        nf_row.addWidget(self.nf_gain_slider)
        self.nf_gain_label = QtWidgets.QLabel(f"{int(config.DEFAULT_NF_GAIN * 100)} %")
        self.nf_gain_label.setMinimumWidth(50)
        nf_row.addWidget(self.nf_gain_label)
        layout.addLayout(nf_row)

        # --- PTT + emergency stop + status -------------------------------
        btn_row = QtWidgets.QHBoxLayout()
        self.ptt_button = QtWidgets.QPushButton("PTT (klicken zum Senden)")
        self.ptt_button.setMinimumHeight(60)
        self.ptt_button.setCheckable(True)
        self.ptt_button.toggled.connect(self._on_ptt_toggled)
        btn_row.addWidget(self.ptt_button)

        self.estop_button = QtWidgets.QPushButton("NOTAUS")
        self.estop_button.setMinimumHeight(60)
        self.estop_button.setStyleSheet("background-color: #c0392b; color: white; font-weight: bold;")
        self.estop_button.clicked.connect(self._on_emergency_stop)
        btn_row.addWidget(self.estop_button)

        self.rearm_button = QtWidgets.QPushButton("Wieder scharf schalten")
        self.rearm_button.setEnabled(False)
        self.rearm_button.clicked.connect(self._on_rearm)
        btn_row.addWidget(self.rearm_button)
        layout.addLayout(btn_row)

        self.tx_indicator = QtWidgets.QLabel("BEREIT")
        self.tx_indicator.setAlignment(QtCore.Qt.AlignCenter)
        self.tx_indicator.setMinimumHeight(40)
        self._set_indicator_idle()
        layout.addWidget(self.tx_indicator)

        self.status_label = QtWidgets.QLabel()
        layout.addWidget(self.status_label)

        # --- Live TX waterfall -------------------------------------------
        if tb.waterfall is not None:
            waterfall_widget = sip.wrapinstance(tb.waterfall.qwidget(), QtWidgets.QWidget)
            waterfall_widget.setMinimumHeight(300)
            layout.addWidget(waterfall_widget)

        # --- Lifecycle: periodic tick doubles as (a) Ctrl-C responsiveness
        # for Qt's event loop and (b) a live hardware-state readback, so the
        # GUI can never silently disagree with what's actually on the AD9361
        # (the exact failure mode we hit with SDRangel earlier).
        self._timer = QtCore.QTimer()
        self._timer.timeout.connect(self._tick)
        self._timer.start(500)

    # --- helpers ------------------------------------------------------
    def _update_band_label(self):
        freq_hz = self.freq_spin.value() * 1e6
        band = config.in_amateur_band(freq_hz)
        if band:
            self.band_label.setText(f"Im Amateurfunkband: {band}")
            self.band_label.setStyleSheet("")
        else:
            self.band_label.setText("WARNUNG: außerhalb der bekannten DE-Amateurfunkbänder")
            self.band_label.setStyleSheet("color: #c0392b; font-weight: bold;")

    def _refresh_power_slider_range(self):
        ceiling = 0 if self.unlock_full_power.isChecked() else config.DEFAULT_ATTEN_CEILING
        self.power_slider.setRange(int(round(config.MIN_ATTEN)), int(round(ceiling)))

    def _set_indicator_idle(self):
        self.tx_indicator.setText("BEREIT" if self._armed else "NOTAUS - GESPERRT")
        color = "#27ae60" if self._armed else "#7f8c8d"
        self.tx_indicator.setStyleSheet(f"background-color: {color}; color: white; font-size: 18pt; font-weight: bold;")

    def _set_indicator_on_air(self):
        self.tx_indicator.setText("ON AIR")
        self.tx_indicator.setStyleSheet("background-color: #c0392b; color: white; font-size: 18pt; font-weight: bold;")

    # --- slots ------------------------------------------------------
    def _on_freq_changed(self, mhz):
        self.tb.set_frequency(mhz * 1e6)
        self._update_band_label()

    def _on_fine_changed(self, value):
        self.tb.set_fine_offset(float(value))
        self.fine_label.setText(f"{value} Hz")

    def _on_nf_gain_changed(self, value):
        gain = value / 100.0
        self.tb.set_nf_gain(gain)
        self.nf_gain_label.setText(f"{value} %")

    def _on_mode_changed(self, idx):
        self.tb.set_mode(self.mode_combo.currentData())

    def _on_source_changed(self, idx):
        source = self.source_combo.currentData()
        self.tb.set_source(source)
        self.file_button.setEnabled(source == PlutoTxFlowgraph.SRC_FILE)

    def _on_pick_file(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Audiodatei wählen", "", "WAV files (*.wav)")
        if path:
            # blocks.wavfile_source has no runtime file-swap in this GNU
            # Radio build; a real file change needs tb.lock()/reconnect,
            # left as a documented follow-up. For now just record the path.
            self.status_label.setText(f"Hinweis: Datei-Wechsel zur Laufzeit ist noch nicht implementiert ({path})")

    def _on_power_changed(self, value):
        self.tb.set_target_power(float(value))
        self.power_label.setText(f"{value} dB")

    def _on_unlock_changed(self, _state):
        self._refresh_power_slider_range()

    def _on_ptt_toggled(self, checked):
        if checked and not self._armed:
            self.ptt_button.blockSignals(True)
            self.ptt_button.setChecked(False)
            self.ptt_button.blockSignals(False)
            return
        if checked:
            self.tb.key_ptt()
            self.ptt_button.setText("PTT (klicken zum Stoppen)")
            self._set_indicator_on_air()
        else:
            self.tb.unkey_ptt()
            self.ptt_button.setText("PTT (klicken zum Senden)")
            self._set_indicator_idle()

    def _on_emergency_stop(self):
        self.tb.unkey_ptt()
        self.tb.safety.force_safe_state()
        self._armed = False
        self.ptt_button.blockSignals(True)
        self.ptt_button.setChecked(False)
        self.ptt_button.setText("PTT (klicken zum Senden)")
        self.ptt_button.blockSignals(False)
        self.ptt_button.setEnabled(False)
        self.rearm_button.setEnabled(True)
        self._set_indicator_idle()
        self.status_label.setText("NOTAUS ausgelöst: Dämpfung minimal, LO abgeschaltet.")

    def _on_rearm(self):
        self.tb.safety.prepare_for_start()
        self.tb.pluto_sink.set_attenuation(0, _gr_atten(config.MIN_ATTEN))
        self._armed = True
        self.ptt_button.setEnabled(True)
        self.rearm_button.setEnabled(False)
        self._set_indicator_idle()
        self.status_label.setText("Wieder scharf geschaltet.")

    def _tick(self):
        try:
            state = self.tb.safety.read_state()
            self.status_label.setText(
                f"HW: hardwaregain={state['hardwaregain_db']:.2f} dB, "
                f"LO powerdown={state['lo_powerdown']}"
            )
        except Exception as e:
            self.status_label.setText(f"HW-Statusabfrage fehlgeschlagen: {e}")

    # --- shutdown lifecycle -------------------------------------------
    def closeEvent(self, event):
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

    def sig_handler(signum, frame):
        print(f"\nSignal {signum} received, shutting down safely...")
        tb.shutdown_safe()
        qapp.quit()

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    orig_excepthook = sys.excepthook

    def excepthook(exc_type, exc_value, exc_tb):
        print("Uncaught exception, forcing safe state...", file=sys.stderr)
        tb.shutdown_safe()
        orig_excepthook(exc_type, exc_value, exc_tb)

    sys.excepthook = excepthook

    try:
        return qapp.exec_()
    finally:
        tb.shutdown_safe()
