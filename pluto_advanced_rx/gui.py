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

from . import config
from . import devices
from .flowgraph import AdvancedRxFlowgraph
from .waterfall_widget import AdvancedWaterfallWidget


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, uri, frequency_hz=config.DEFAULT_FREQUENCY, demod_mode=AdvancedRxFlowgraph.MODE_FM,
                 sample_rate=config.DEFAULT_RX_BANDWIDTH, gain_mode=config.DEFAULT_GAIN_MODE,
                 manual_gain_db=config.DEFAULT_MANUAL_GAIN_DB):
        """Builds the window in a disconnected default state (self.tb is
        None), then immediately attempts one real connection via _connect()
        -- the exact same bounded-timeout, exception-safe path already used
        for every later reconnect. Every _on_*_changed slot below already
        guards self.tb is None (needed regardless, for the normal Disconnect
        state), so it's also already safe to fire during this initial
        widget construction if it fires early -- no crash risk either way."""
        super().__init__()
        self.tb = None
        self._fft_gen = -1
        self._rx_muted = True  # reset True only on fresh connect/disconnect, see _disconnect()/_connect()
        self.setWindowTitle("PlutoSDR Advanced RX")

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        layout = QtWidgets.QVBoxLayout(central)

        # --- Device group: type + connection, scan/connect -----------------
        # One connection combo shared by every backend (a libiio URI for
        # Pluto, a serial/Soapy-args string for HackRF/RTL-SDR) -- mirrors
        # pluto_tx/gui.py's identical device_type_combo + uri_combo pattern.
        # Relabelled/retooltipped by _on_device_type_changed() below.
        device_group = QtWidgets.QGroupBox("Device")
        device_row = QtWidgets.QHBoxLayout(device_group)
        device_row.addWidget(QtWidgets.QLabel("Device Type:"))
        self.device_type_combo = QtWidgets.QComboBox()
        for dtype, device_cls in devices.DEVICE_REGISTRY.items():
            self.device_type_combo.addItem(device_cls.display_name, dtype)
        self.device_type_combo.currentIndexChanged.connect(self._on_device_type_changed)
        device_row.addWidget(self.device_type_combo)
        self.device_label = QtWidgets.QLabel("Device (hostname or IP):")
        device_row.addWidget(self.device_label)
        self.uri_combo = QtWidgets.QComboBox()
        self.uri_combo.setEditable(True)
        self.uri_combo.setInsertPolicy(QtWidgets.QComboBox.NoInsert)
        self.uri_combo.addItem(uri)
        self.uri_combo.setEnabled(False)  # editable only while disconnected
        device_row.addWidget(self.uri_combo, 1)
        self.scan_button = QtWidgets.QPushButton("Scan")
        self.scan_button.clicked.connect(self._on_scan_clicked)
        device_row.addWidget(self.scan_button)
        self.connect_button = QtWidgets.QPushButton("Disconnect")
        self.connect_button.clicked.connect(self._on_connect_clicked)
        device_row.addWidget(self.connect_button)
        layout.addWidget(device_group)
        self._update_device_connection_labels()

        # --- Receiver group: frequency, demodulator, gain, bandwidth ------
        receiver_group = QtWidgets.QGroupBox("Receiver")
        receiver_layout = QtWidgets.QVBoxLayout(receiver_group)

        # Persistent mute gate (not a momentary control like pluto_tx's PTT) --
        # starts muted so a fresh connect never immediately blasts audio, see
        # AdvancedRxFlowgraph.set_rx_muted()/rx_mute's construction comment.
        # Independent of the "Audio Gain" slider below (a volume control, not
        # a gate).
        mute_row = QtWidgets.QHBoxLayout()
        self.receive_button = QtWidgets.QPushButton()
        self.receive_button.setCheckable(True)
        self.receive_button.setChecked(False)
        self.receive_button.setMinimumHeight(40)
        self.receive_button.toggled.connect(self._on_receive_toggled)
        self._style_receive_button(receiving=False)
        mute_row.addWidget(self.receive_button)
        receiver_layout.addLayout(mute_row)

        freq_row = QtWidgets.QHBoxLayout()
        freq_row.addWidget(QtWidgets.QLabel("Frequency (MHz):"))
        self.freq_spin = QtWidgets.QDoubleSpinBox()
        self.freq_spin.setDecimals(4)
        self.freq_spin.setRange(70.0, 6000.0)
        self.freq_spin.setSingleStep(0.001)
        self.freq_spin.setValue(frequency_hz / 1e6)
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
        initial_demod_idx = self.demod_combo.findData(demod_mode)
        if initial_demod_idx >= 0:
            self.demod_combo.setCurrentIndex(initial_demod_idx)
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

        # Two mutually-exclusive gain panels, switched by device type (only
        # one is ever visible at a time) -- agc_gain_widget for AGC-capable
        # single-stage backends (Pluto, RTL-SDR: a mode combo + one slider,
        # unchanged from before the device abstraction existed), and
        # manual_gain_widget for backends with no AGC stage at all (HackRF:
        # one row per gain_stage, built dynamically from GainStage data by
        # _rebuild_manual_gain_panel() rather than hardcoded per device).
        self.agc_gain_widget = QtWidgets.QWidget()
        agc_gain_row = QtWidgets.QHBoxLayout(self.agc_gain_widget)
        agc_gain_row.setContentsMargins(0, 0, 0, 0)
        agc_gain_row.addWidget(QtWidgets.QLabel("Gain Mode:"))
        self.gain_mode_combo = QtWidgets.QComboBox()
        for m in config.GAIN_MODES:
            self.gain_mode_combo.addItem(m, m)
        self.gain_mode_combo.setCurrentText(gain_mode)
        self.gain_mode_combo.currentIndexChanged.connect(self._on_gain_mode_changed)
        agc_gain_row.addWidget(self.gain_mode_combo)

        agc_gain_row.addWidget(QtWidgets.QLabel("RF Gain (dB):"))
        self.gain_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        lo, hi = config.MANUAL_GAIN_RANGE_DB
        self.gain_slider.setRange(int(lo), int(hi))
        self.gain_slider.setValue(int(manual_gain_db))
        self.gain_slider.setEnabled(gain_mode == "manual")
        self.gain_slider.valueChanged.connect(self._on_gain_changed)
        agc_gain_row.addWidget(self.gain_slider)
        self.gain_label = QtWidgets.QLabel(f"{int(manual_gain_db)} dB")
        self.gain_label.setMinimumWidth(50)
        agc_gain_row.addWidget(self.gain_label)
        receiver_layout.addWidget(self.agc_gain_widget)

        # Rebuilt per-device by _rebuild_manual_gain_panel() (called from
        # _sync_device_dependent_widgets() on a device-type switch) --
        # empty and hidden at construction, since the app always starts on
        # Pluto (AGC-capable, uses agc_gain_widget above instead).
        self.manual_gain_widget = QtWidgets.QWidget()
        self.manual_gain_layout = QtWidgets.QVBoxLayout(self.manual_gain_widget)
        self.manual_gain_layout.setContentsMargins(0, 0, 0, 0)
        self.manual_gain_widget.setVisible(False)
        self._manual_gain_controls = {}  # stage_name -> (widget, value_label_or_None, GainStage)
        receiver_layout.addWidget(self.manual_gain_widget)

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
        self.bandwidth_combo.setCurrentIndex(config.RX_BANDWIDTH_PRESETS.index(sample_rate))
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

        # Everything above builds the window with widgets in their normal
        # (enabled, "Disconnect"-labelled) construction defaults, which only
        # makes sense once actually connected. Put it into the disconnected
        # default presentation first -- _connect()'s failure branches only
        # update the status label (their precondition is that the window is
        # already disconnected, which normally holds because _connect() is
        # only ever called right after a manual _disconnect()) -- then
        # attempt the actual initial connection through that same
        # bounded-timeout, exception-safe path. If the device isn't
        # reachable, these defaults are what's left on screen, with an
        # explanatory status message, instead of raising.
        self._set_connected_controls_enabled(False)
        self.connect_button.setText("Connect")
        self.uri_combo.setEnabled(True)
        self.device_type_combo.setEnabled(True)
        self._connect(uri)

    @staticmethod
    def _format_hz(hz):
        return f"{hz/1e6:g} MHz" if hz >= 1_000_000 else f"{hz/1e3:g} kHz"

    def _set_connected_controls_enabled(self, enabled: bool):
        for w in (self.freq_spin, self.fine_slider, self.demod_combo, self.width_slider,
                  self.agc_gain_widget, self.manual_gain_widget, self.nf_gain_slider,
                  self.bandwidth_combo, self.fft_size_combo, self.receive_button):
            w.setEnabled(enabled)
        self.gain_slider.setEnabled(enabled and self.gain_mode_combo.currentData() == "manual")

    def _style_receive_button(self, receiving: bool):
        self.receive_button.setText("Receiving (click to mute)" if receiving else "Muted (click to receive)")
        color = "#27ae60" if receiving else "#7f8c8d"
        self.receive_button.setStyleSheet(f"background-color: {color}; color: white; font-weight: bold;")

    def _update_device_connection_labels(self):
        device_cls = devices.DEVICE_REGISTRY[self.device_type_combo.currentData()]
        if device_cls.connection_kind == "uri":
            self.device_label.setText("Device (hostname or IP):")
            self.uri_combo.setToolTip(
                "libiio context URI, e.g. plutoplus.local, 192.168.1.50, or a full "
                "URI like usb:1.5.5 to pick a specific device when more than one "
                "Pluto is reachable. A bare hostname/IP gets 'ip:' prefixed "
                "automatically. Use Scan to discover devices on the network/USB."
            )
        else:
            self.device_label.setText(f"{device_cls.display_name} Serial (blank = auto):")
            self.uri_combo.setToolTip(
                f"{device_cls.display_name} serial number, or leave blank to use the "
                f"only attached one. Use Scan to discover attached devices (needs "
                f"python3-soapysdr, see install.sh). A full Soapy device-args string "
                f"(containing 'driver=') is also accepted unchanged, e.g. for "
                f"SoapyRemote network access -- documented as a capability, not "
                f"tested end-to-end (no second machine available this session)."
            )

    def _on_device_type_changed(self, idx):
        """Only takes effect on the next Connect -- device_type_combo (like
        uri_combo) is only editable while disconnected. Also clears the
        connection field: a Pluto URI left behind after switching to
        HackRF/RTL-SDR (or vice versa) looks like a valid value but isn't --
        the operator has to notice and clear it manually otherwise (the
        exact bug already found and fixed on the pluto_tx side this
        session)."""
        self.uri_combo.blockSignals(True)
        self.uri_combo.clear()
        self.uri_combo.clearEditText()
        self.uri_combo.blockSignals(False)
        self._sync_device_dependent_widgets()

    def _sync_device_dependent_widgets(self):
        """Range/item-list/visibility updates only, never touches self.tb.
        Called ONLY from _on_device_type_changed() above -- deliberately NOT
        also once at the end of __init__ the way pluto_tx/gui.py's version
        is: the widgets built during __init__ are already correctly
        Pluto-shaped (bandwidth_combo/gain_mode_combo/gain_slider match
        PlutoDevice's own choices exactly), so an extra call here would only
        risk clobbering the constructor's caller-supplied sample_rate/
        gain_mode/manual_gain_db with PlutoDevice's class defaults instead.
        On an ACTUAL type switch there's no equivalent concern -- the old
        values don't even apply to the new device's different value domain
        (a HackRF sample rate isn't a valid Pluto one), so resetting to the
        new device's own defaults here is correct, not just convenient."""
        self._update_device_connection_labels()
        device_cls = devices.DEVICE_REGISTRY[self.device_type_combo.currentData()]

        lo_hz, hi_hz = device_cls.frequency_range_hz
        self.freq_spin.setRange(lo_hz / 1e6, hi_hz / 1e6)

        self.bandwidth_combo.blockSignals(True)
        self.bandwidth_combo.clear()
        for bw in device_cls.sample_rate_hz_choices:
            self.bandwidth_combo.addItem(self._format_hz(bw), bw)
        default_idx = device_cls.sample_rate_hz_choices.index(device_cls.default_sample_rate_hz)
        self.bandwidth_combo.setCurrentIndex(default_idx)
        self.bandwidth_combo.blockSignals(False)

        if device_cls.supports_agc_mode:
            self.agc_gain_widget.setVisible(True)
            self.manual_gain_widget.setVisible(False)
            self.gain_mode_combo.blockSignals(True)
            self.gain_mode_combo.clear()
            for m in device_cls.agc_modes:
                self.gain_mode_combo.addItem(m, m)
            default_mode_idx = self.gain_mode_combo.findData(device_cls.default_gain_mode)
            self.gain_mode_combo.setCurrentIndex(default_mode_idx if default_mode_idx >= 0 else 0)
            self.gain_mode_combo.blockSignals(False)
            agc_stage = next(s for s in device_cls.gain_stages if s.controls_agc)
            self.gain_slider.blockSignals(True)
            self.gain_slider.setRange(int(round(agc_stage.min_value)), int(round(agc_stage.max_value)))
            self.gain_slider.setValue(int(round(agc_stage.default_value)))
            self.gain_slider.blockSignals(False)
            self.gain_slider.setEnabled(self.gain_mode_combo.currentData() == "manual")
            self.gain_label.setText(f"{int(round(agc_stage.default_value))} {agc_stage.unit}")
        else:
            self.agc_gain_widget.setVisible(False)
            self.manual_gain_widget.setVisible(True)
            self._rebuild_manual_gain_panel(device_cls)

    def _rebuild_manual_gain_panel(self, device_cls):
        """(Re)builds manual_gain_widget's contents from scratch, one row
        per device_cls.gain_stages entry -- generic over stage count/kind
        rather than hardcoded per device, so a future manual-only backend
        with a different stage count needs no GUI changes here. Only HackRF
        exercises this today (LNA/AMP/VGA)."""
        while self.manual_gain_layout.count():
            item = self.manual_gain_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._manual_gain_controls = {}
        for stage in device_cls.gain_stages:
            row = QtWidgets.QHBoxLayout()
            row.addWidget(QtWidgets.QLabel(f"{stage.label}:"))
            if stage.kind == "bool":
                widget = QtWidgets.QCheckBox()
                widget.setChecked(bool(stage.default_value))
                widget.toggled.connect(lambda _checked, name=stage.name: self._on_manual_gain_changed(name))
                row.addWidget(widget)
                self._manual_gain_controls[stage.name] = (widget, None, stage)
            else:
                widget = QtWidgets.QSlider(QtCore.Qt.Horizontal)
                widget.setRange(int(round(stage.min_value)), int(round(stage.max_value)))
                widget.setValue(int(round(stage.default_value)))
                value_label = QtWidgets.QLabel(f"{int(round(stage.default_value))} {stage.unit}")
                value_label.setMinimumWidth(60)
                widget.valueChanged.connect(lambda _v, name=stage.name: self._on_manual_gain_changed(name))
                row.addWidget(widget)
                row.addWidget(value_label)
                self._manual_gain_controls[stage.name] = (widget, value_label, stage)
            row_widget = QtWidgets.QWidget()
            row_widget.setLayout(row)
            self.manual_gain_layout.addWidget(row_widget)

    def _on_manual_gain_changed(self, stage_name):
        widget, label, stage = self._manual_gain_controls[stage_name]
        if stage.kind == "bool":
            value = widget.isChecked()
        else:
            value = float(widget.value())
            if label is not None:
                label.setText(f"{int(value)} {stage.unit}")
        if self.tb is not None:
            self.tb.device.set_gain(stage_name, value)

    def _current_gain_kwargs(self, device_cls):
        """gain_mode/manual_gain_db for an AGC-capable device (Pluto/
        RTL-SDR, read from gain_mode_combo/gain_slider), or gain_values for
        a purely-manual one (HackRF, read from the per-stage manual gain
        panel). AdvancedRxFlowgraph accepts both unconditionally -- the
        unused one is simply inert for that backend (see its own
        docstring) -- so the caller doesn't need to branch on device_cls
        itself, just call this and pass the result through."""
        if device_cls.supports_agc_mode:
            return dict(gain_mode=self.gain_mode_combo.currentData(),
                        manual_gain_db=float(self.gain_slider.value()), gain_values=None)
        gain_values = {name: (widget.isChecked() if stage.kind == "bool" else float(widget.value()))
                       for name, (widget, _label, stage) in self._manual_gain_controls.items()}
        return dict(gain_mode=config.DEFAULT_GAIN_MODE, manual_gain_db=0.0, gain_values=gain_values)

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

    def _on_receive_toggled(self, checked):
        self._rx_muted = not checked
        self._style_receive_button(receiving=checked)
        if self.tb is not None:
            self.tb.set_rx_muted(self._rx_muted)

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
        device_cls = devices.DEVICE_REGISTRY[self.device_type_combo.currentData()]
        new_rate = self.bandwidth_combo.currentData()
        freq = self.tb.nominal_freq_hz
        fine = self.tb.fine_offset_hz
        demod_mode = self.demod_combo.currentData()
        nf_gain = self.nf_gain_slider.value() / 100.0
        fft_size = self.fft_size_combo.currentData()
        fm_width = self.tb.fm_demod_width_hz
        ssb_width = self.tb.ssb_demod_width_hz

        try:
            new_tb = AdvancedRxFlowgraph(
                uri=self.tb.uri, frequency=freq, sample_rate=new_rate,
                demod_mode=demod_mode, nf_gain=nf_gain, fft_size=fft_size,
                fm_demod_width_hz=fm_width, ssb_demod_width_hz=ssb_width,
                device_type=device_cls.device_type, **self._current_gain_kwargs(device_cls),
            )
        except Exception as e:
            self.status_label.setText(f"Could not switch to {self._format_hz(new_rate)}: {e}")
            self.bandwidth_combo.blockSignals(True)
            self.bandwidth_combo.setCurrentIndex(device_cls.sample_rate_hz_choices.index(int(self.tb.sample_rate)))
            self.bandwidth_combo.blockSignals(False)
            return

        new_tb.set_fine_offset(fine)
        new_tb.set_rx_muted(self._rx_muted)  # carry the CURRENT mute state over -- this is an
        # in-session rebuild, not a fresh connect, so it must not reset to muted (see _disconnect()).
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
        device_cls = devices.DEVICE_REGISTRY[self.device_type_combo.currentData()]
        self.status_label.setText(f"Scanning for {device_cls.display_name} devices...")
        QtWidgets.QApplication.processEvents()
        found, error = device_cls.scan_devices_with_timeout()
        if error is not None:
            self.status_label.setText(f"Scan failed: {error}")
            return
        found.pop("local:", None)  # libiio's local-context artifact, never a Pluto (harmless no-op for HackRF/RTL-SDR)
        current = self.uri_combo.currentText()
        self.uri_combo.blockSignals(True)
        self.uri_combo.clear()
        for connection, desc in found.items():
            idx = self.uri_combo.count()
            self.uri_combo.addItem(connection)
            self.uri_combo.setItemData(idx, desc, QtCore.Qt.ToolTipRole)
        if current and self.uri_combo.findText(current) < 0:
            self.uri_combo.addItem(current)
        if current:
            self.uri_combo.setCurrentText(current)
        self.uri_combo.blockSignals(False)
        self.status_label.setText(f"Found {len(found)} device(s)." if found else "No devices found.")

    def _disconnect(self):
        self.tb.shutdown()
        self.tb = None
        self._fft_gen = -1
        self.waterfall.clear()
        self._set_connected_controls_enabled(False)
        self.connect_button.setText("Connect")
        self.uri_combo.setEnabled(True)
        self.device_type_combo.setEnabled(True)
        self.status_label.setText("Disconnected.")
        # Reset to muted -- only here and at fresh app startup, NOT on
        # in-session rebuilds (_on_bandwidth_changed), which carry the
        # current value over instead. See AdvancedRxFlowgraph.set_rx_muted().
        self._rx_muted = True
        self.receive_button.blockSignals(True)
        self.receive_button.setChecked(False)
        self.receive_button.blockSignals(False)
        self._style_receive_button(receiving=False)

    def _connect(self, uri_text):
        device_cls = devices.DEVICE_REGISTRY[self.device_type_combo.currentData()]
        # Pluto's connection is a libiio URI (bare hostname/IP gets 'ip:'
        # prefixed); HackRF/RTL-SDR's is a bare serial (or blank for "the
        # only attached one"), or a full Soapy device-args string --
        # normalize_uri() would wrongly mangle either of those.
        connection = config.normalize_uri(uri_text) if device_cls.connection_kind == "uri" else uri_text.strip()
        if device_cls.connection_kind == "uri" and not connection:
            self.status_label.setText("Please enter a device hostname, IP, or URI.")
            return
        self.status_label.setText(f"Connecting to {device_cls.display_name}...")
        QtWidgets.QApplication.processEvents()
        probe_error = device_cls.probe_with_timeout(connection)
        if probe_error is not None:
            self.status_label.setText(f"Could not connect to {device_cls.display_name}: {probe_error}")
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
                uri=connection,
                frequency=self.freq_spin.value() * 1e6,
                sample_rate=self.bandwidth_combo.currentData(),
                demod_mode=self.demod_combo.currentData(),
                nf_gain=self.nf_gain_slider.value() / 100.0,
                fft_size=self.fft_size_combo.currentData(),
                fm_demod_width_hz=fm_width, ssb_demod_width_hz=ssb_width,
                device_type=device_cls.device_type, **self._current_gain_kwargs(device_cls),
            )
        except Exception as e:
            self.status_label.setText(f"Could not connect to {device_cls.display_name}: {e}")
            return
        new_tb.set_fine_offset(float(self.fine_slider.value()))
        new_tb.set_rx_muted(self._rx_muted)  # True here (see _disconnect()/__init__) -- fresh connect starts muted
        self._fft_gen = -1
        self.tb = new_tb
        self._sync_waterfall()
        new_tb.start()
        self._set_connected_controls_enabled(True)
        self.connect_button.setText("Disconnect")
        self.uri_combo.setEnabled(False)
        self.device_type_combo.setEnabled(False)
        self.status_label.setText(f"Connected to {device_cls.display_name} ({connection or 'auto-detect'}).")

    def closeEvent(self, event):
        if self.tb is not None:
            self.tb.shutdown()
        event.accept()


def run_gui(uri, frequency_hz=config.DEFAULT_FREQUENCY, demod_mode=AdvancedRxFlowgraph.MODE_FM,
            sample_rate=config.DEFAULT_RX_BANDWIDTH, gain_mode=config.DEFAULT_GAIN_MODE,
            manual_gain_db=config.DEFAULT_MANUAL_GAIN_DB):
    """Builds and shows the main window, which itself attempts the initial
    connection to `uri` (see MainWindow.__init__/its _connect() call) --
    never raises just because the device isn't reachable at startup: the
    window still comes up, in the same disconnected state a manual
    Disconnect leaves it in. self.tb (only ever set by MainWindow itself)
    stays the single reference to the running flowgraph, so there's no
    stray local reference here to leak an AD9361 buffer claim (the "Unable
    to create buffer: -16" bug this project hit before when a second
    reference existed)."""
    qapp = QtWidgets.QApplication(sys.argv)
    window = MainWindow(uri, frequency_hz=frequency_hz, demod_mode=demod_mode, sample_rate=sample_rate,
                         gain_mode=gain_mode, manual_gain_db=manual_gain_db)
    window.show()

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
