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
from . import rade_autotune
from .fft_probe import FftProbe
from .flowgraph import AdvancedRxFlowgraph, RADE_AVAILABLE
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
        device_group_layout = QtWidgets.QVBoxLayout(device_group)
        device_row = QtWidgets.QHBoxLayout()
        device_group_layout.addLayout(device_row)
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

        # --- Mode group: a QTabWidget ("Audio" holds today's full FM/SSB/
        # RADE demod+gain+volume controls; "Digimodes"/"File-Transfer" are
        # disabled placeholders for future work) -- constructed here, early,
        # so audio_tab_layout already exists by the time mute_row/demod_row/
        # etc. below are built further down; Qt layouts are "live", so a
        # widget added to audio_tab_layout later in this method still ends
        # up in the right place regardless of source-code order (same
        # technique pluto_tx/gui.py's 4-section restructure used last
        # session). device_group_layout above gets freq_row/the gain
        # widgets/bandwidth_row the same way -- device-specific settings,
        # not mode-specific ones, per the explicit request.
        mode_group = QtWidgets.QGroupBox("Mode")
        mode_group_layout = QtWidgets.QVBoxLayout(mode_group)
        self.mode_tab_widget = QtWidgets.QTabWidget()
        audio_tab = QtWidgets.QWidget()
        audio_tab_layout = QtWidgets.QVBoxLayout(audio_tab)
        self.mode_tab_widget.addTab(audio_tab, "Audio")
        digimodes_tab = QtWidgets.QWidget()
        self.mode_tab_widget.addTab(digimodes_tab, "Digimodes")
        self.mode_tab_widget.setTabEnabled(1, False)
        self.mode_tab_widget.setTabToolTip(1, "Not implemented yet")
        filetransfer_tab = QtWidgets.QWidget()
        self.mode_tab_widget.addTab(filetransfer_tab, "File-Transfer")
        self.mode_tab_widget.setTabEnabled(2, False)
        self.mode_tab_widget.setTabToolTip(2, "Not implemented yet")
        mode_group_layout.addWidget(self.mode_tab_widget)

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
        audio_tab_layout.addLayout(mute_row)

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
        device_group_layout.addLayout(freq_row)

        demod_row = QtWidgets.QHBoxLayout()
        demod_row.addWidget(QtWidgets.QLabel("Mode:"))
        self.demod_combo = QtWidgets.QComboBox()
        self.demod_combo.addItem("FM", AdvancedRxFlowgraph.MODE_FM)
        self.demod_combo.addItem("SSB (USB)", AdvancedRxFlowgraph.MODE_SSB)
        self.demod_combo.addItem("RADE", AdvancedRxFlowgraph.MODE_RADE)
        if not RADE_AVAILABLE:
            # rade_c is an optional, from-source dependency (see
            # install-rade.sh) -- grey out rather than hide, same as
            # pluto_tx's M17/FreeDV/RADE mode entries.
            rade_item_idx = self.demod_combo.findData(AdvancedRxFlowgraph.MODE_RADE)
            item = self.demod_combo.model().item(rade_item_idx)
            item.setEnabled(False)
            self.demod_combo.setItemData(
                rade_item_idx, "librade.so/lpcnet_demo not found -- see install-rade.sh / README",
                QtCore.Qt.ToolTipRole,
            )
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
        audio_tab_layout.addLayout(demod_row)

        # RADE status row -- only VISIBLE in RADE mode, same "hide the whole
        # row" reasoning as pluto_tx's M17/FreeDV/RADE rows. Updated by the
        # existing FFT poll timer (_poll_fft) -- cheap enough to piggyback
        # on rather than adding a second timer.
        rade_row = QtWidgets.QHBoxLayout()
        self.rade_status_label = QtWidgets.QLabel("Not synced")
        rade_row.addWidget(self.rade_status_label)
        rade_row.addStretch(1)
        # One-shot auto fine-tune: RF-domain frequency/gain centering, with
        # a bounded RADE-lock-feedback fallback sweep if that alone isn't
        # enough -- see _autotune_* below. Click again while running to
        # cancel.
        self.autotune_button = QtWidgets.QPushButton("Auto Fine-Tune")
        self.autotune_button.clicked.connect(self._on_autotune_clicked)
        rade_row.addWidget(self.autotune_button)
        self._autotune_token = None  # set to a fresh object() while a run is in flight, checked by
        # every scheduled step so a stale QTimer callback (after cancel/disconnect/mode-change)
        # silently no-ops instead of touching a torn-down flowgraph.
        self.rade_row_widget = QtWidgets.QWidget()
        self.rade_row_widget.setLayout(rade_row)
        self.rade_row_widget.setVisible(False)
        audio_tab_layout.addWidget(self.rade_row_widget)

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
        device_group_layout.addWidget(self.agc_gain_widget)

        # Rebuilt per-device by _rebuild_manual_gain_panel() (called from
        # _sync_device_dependent_widgets() on a device-type switch) --
        # empty and hidden at construction, since the app always starts on
        # Pluto (AGC-capable, uses agc_gain_widget above instead).
        self.manual_gain_widget = QtWidgets.QWidget()
        self.manual_gain_layout = QtWidgets.QVBoxLayout(self.manual_gain_widget)
        self.manual_gain_layout.setContentsMargins(0, 0, 0, 0)
        self.manual_gain_widget.setVisible(False)
        self._manual_gain_controls = {}  # stage_name -> (widget, value_label_or_None, GainStage)
        device_group_layout.addWidget(self.manual_gain_widget)

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
        audio_tab_layout.addLayout(nf_row)
        audio_tab_layout.addStretch(1)

        bandwidth_row = QtWidgets.QHBoxLayout()
        bandwidth_row.addWidget(QtWidgets.QLabel("RX Bandwidth:"))
        self.bandwidth_combo = QtWidgets.QComboBox()
        for bw in config.RX_BANDWIDTH_PRESETS:
            self.bandwidth_combo.addItem(self._format_hz(bw), bw)
        self.bandwidth_combo.setCurrentIndex(config.RX_BANDWIDTH_PRESETS.index(sample_rate))
        self.bandwidth_combo.currentIndexChanged.connect(self._on_bandwidth_changed)
        bandwidth_row.addWidget(self.bandwidth_combo)
        bandwidth_row.addStretch(1)
        device_group_layout.addLayout(bandwidth_row)

        layout.addWidget(mode_group)

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

        # Zoom + Averaging, side by side above the spectrum -- both take
        # effect immediately (no flowgraph rebuild, unlike RX bandwidth/FFT
        # size): FftProbe.set_zoom()/set_avg_count() are pure Python-side
        # state changes. Zoom is a "zoom-FFT" crop (see fft_probe.py's
        # module docstring) -- 1x shows the full RX bandwidth, higher
        # values narrow the displayed span for more visual detail on a
        # narrowband signal. Averaging is a rolling mean of the last N
        # spectra in POWER (not dB) -- reduces noise-floor jitter at the
        # cost of update responsiveness.
        zoom_avg_row = QtWidgets.QHBoxLayout()
        zoom_avg_row.addWidget(QtWidgets.QLabel("Zoom:"))
        self.zoom_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.zoom_slider.setRange(1, FftProbe.MAX_ZOOM)
        self.zoom_slider.setValue(1)
        self.zoom_slider.valueChanged.connect(self._on_zoom_changed)
        zoom_avg_row.addWidget(self.zoom_slider)
        self.zoom_label = QtWidgets.QLabel("1x (full span)")
        self.zoom_label.setMinimumWidth(130)
        zoom_avg_row.addWidget(self.zoom_label)

        zoom_avg_row.addWidget(QtWidgets.QLabel("Averaging:"))
        self.avg_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.avg_slider.setRange(1, FftProbe.MAX_AVG)
        self.avg_slider.setValue(1)
        self.avg_slider.valueChanged.connect(self._on_avg_changed)
        zoom_avg_row.addWidget(self.avg_slider)
        self.avg_label = QtWidgets.QLabel("1 (off)")
        self.avg_label.setMinimumWidth(60)
        zoom_avg_row.addWidget(self.avg_label)
        waterfall_layout.addLayout(zoom_avg_row)

        # Built ONCE, persists across every reconnect/rebuild (see module
        # docstring).
        self.waterfall = AdvancedWaterfallWidget(
            fft_size=config.DEFAULT_FFT_SIZE, history_rows=config.WATERFALL_HISTORY_ROWS,
            colormap_name=config.WATERFALL_COLORMAP, db_range=config.WATERFALL_DB_RANGE,
        )
        self.waterfall.frequency_clicked.connect(self._on_waterfall_clicked)
        self.waterfall.zoom_step_requested.connect(self._on_waterfall_zoom_step)

        # Floor/Ceiling: vertical sliders stacked to the right of the
        # spectrum+waterfall, since the noise floor varies a lot with
        # antenna/location/gain and shouldn't be baked in as a fixed value.
        db_lo, db_hi = config.WATERFALL_DB_RANGE
        db_sliders_col = QtWidgets.QVBoxLayout()
        db_sliders_col.addWidget(QtWidgets.QLabel("Ceiling"), alignment=QtCore.Qt.AlignHCenter)
        self.db_ceiling_slider = QtWidgets.QSlider(QtCore.Qt.Vertical)
        self.db_ceiling_slider.setRange(-150, 40)
        # Inverted: Qt's default vertical-slider orientation is min-at-
        # bottom/max-at-top, but Ceiling and Floor are two SEPARATE
        # full-range sliders stacked in one column (not one double-handled
        # range slider) -- with the default orientation the Ceiling slider
        # (top half of the column) and Floor slider (bottom half) each still
        # run bottom-to-top internally, which reads backwards for a
        # "ceiling"/"floor" pair. Inverted appearance+controls (mouse drag,
        # wheel, and arrow keys) puts max at the bottom of each slider's own
        # track instead, so dragging DOWN raises the value on both --
        # requested for UX reasons.
        self.db_ceiling_slider.setInvertedAppearance(True)
        self.db_ceiling_slider.setInvertedControls(True)
        self.db_ceiling_slider.setValue(int(db_hi))
        self.db_ceiling_slider.valueChanged.connect(self._on_db_range_changed)
        db_sliders_col.addWidget(self.db_ceiling_slider, 1, alignment=QtCore.Qt.AlignHCenter)
        self.db_ceiling_label = QtWidgets.QLabel(f"{int(db_hi)} dB")
        db_sliders_col.addWidget(self.db_ceiling_label, alignment=QtCore.Qt.AlignHCenter)

        db_sliders_col.addWidget(QtWidgets.QLabel("Floor"), alignment=QtCore.Qt.AlignHCenter)
        self.db_floor_slider = QtWidgets.QSlider(QtCore.Qt.Vertical)
        self.db_floor_slider.setRange(-150, 40)
        self.db_floor_slider.setInvertedAppearance(True)  # see db_ceiling_slider's comment above
        self.db_floor_slider.setInvertedControls(True)
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
                  self.bandwidth_combo, self.fft_size_combo, self.zoom_slider, self.avg_slider,
                  self.receive_button, self.autotune_button):
            w.setEnabled(enabled)
        self.gain_slider.setEnabled(enabled and self.gain_mode_combo.currentData() == "manual")
        if not enabled:
            self._autotune_cancel()

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
        elif device_cls.connection_kind == "audio_device":
            self.device_label.setText("Audio Device (blank = system default):")
            self.uri_combo.setToolTip(
                "ALSA/PortAudio device name, or leave blank to use the system's "
                "default input. No structured device scan exists for sound cards -- "
                "Scan will report 0 devices found; enter a name manually if the "
                "default isn't the right one."
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

        # No RF tuning concept for a sound card -- hide rather than just
        # grey out (same "hide, don't just grey out" principle already used
        # for the RADE-mode width slider).
        has_frequency = device_cls.frequency_range_hz != (0.0, 0.0)
        self.freq_spin.setVisible(has_frequency)
        self.fine_slider.setVisible(has_frequency)
        self.fine_label.setVisible(has_frequency)
        if has_frequency:
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
        tb.ssb_demod_width_hz), not an estimate. The displayed span is the
        ZOOMED span (sample_rate/zoom), read back from fft_probe itself
        (not the zoom_slider's own value) so this stays correct even if
        called before the slider's own state has settled."""
        if self.tb is None:
            return
        freq = self.tb.nominal_freq_hz + self.tb.fine_offset_hz
        zoomed_span = self.tb.sample_rate / self.tb.fft_probe.zoom
        self.waterfall.set_frequency_range(freq, zoomed_span)
        self.waterfall.set_tuned_frequency(freq)
        mode = self.demod_combo.currentData()
        if mode == AdvancedRxFlowgraph.MODE_FM:
            half_bw = self.tb.fm_demod_width_hz / 2
            self.waterfall.set_demod_band(freq - half_bw, freq + half_bw)
        elif mode == AdvancedRxFlowgraph.MODE_SSB:
            f_lo = config.SSB_AUDIO_BAND_HZ[0]
            self.waterfall.set_demod_band(freq + f_lo, freq + f_lo + self.tb.ssb_demod_width_hz)
        elif mode == AdvancedRxFlowgraph.MODE_RADE:
            # Not operator-adjustable (RADE's OFDM occupied bandwidth is a
            # fixed protocol constant, unlike FM/SSB's width sliders) -- but
            # a real bug before this fix: this branch didn't exist at all,
            # so the overlay just kept showing whatever FM/SSB band was last
            # drawn, making visual tuning against it meaningless. See
            # config.RADE_OFDM_LOW_HZ/HIGH_HZ's docstring for the derivation.
            self.waterfall.set_demod_band(freq + config.RADE_OFDM_LOW_HZ, freq + config.RADE_OFDM_HIGH_HZ)

    def _poll_fft(self):
        if self.tb is None:
            return
        row, self._fft_gen = self.tb.fft_probe.get_latest_row(self._fft_gen)
        if row is not None:
            self.waterfall.push_fft_row(row)
        if (self.demod_combo.currentData() == AdvancedRxFlowgraph.MODE_RADE and RADE_AVAILABLE
                and self._autotune_token is None):  # suppressed while an autotune run owns the status label
            dec = self.tb.rade_decoder
            if dec.synced:
                self.rade_status_label.setText(
                    f"Synced -- freq offset: {dec.freq_offset_hz:.1f} Hz, SNR: {dec.snr_db:.1f} dB"
                )
            else:
                self.rade_status_label.setText("Not synced")

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
        is_rade_mode = mode == AdvancedRxFlowgraph.MODE_RADE
        self._autotune_cancel()  # leaving/re-entering RADE mode invalidates any in-flight run
        # No operator-adjustable demod width for RADE -- hide the whole
        # width control rather than leave it interactive-but-meaningless,
        # same "hide, don't just grey out" reasoning as the RADE status row.
        self.width_slider.setVisible(not is_rade_mode)
        self.width_label.setVisible(not is_rade_mode)
        self.rade_row_widget.setVisible(is_rade_mode)
        if not is_rade_mode:
            self.rade_status_label.setText("Not synced")

        if not is_rade_mode:
            # The width slider always shows/edits whichever mode is now
            # selected -- its range and current value come straight from
            # the flowgraph's own per-mode state (tb.fm_demod_width_hz /
            # tb.ssb_demod_width_hz), which is untouched by merely
            # switching which branch demod_selector picks, so any earlier
            # customization for that mode is preserved rather than reset to
            # a default.
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
        elif self.demod_combo.currentData() == AdvancedRxFlowgraph.MODE_SSB:
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

    def _on_zoom_changed(self, value):
        if self.tb is None:
            return
        self.tb.set_fft_zoom(value)
        span = self.tb.sample_rate / value
        self.zoom_label.setText(f"{value}x (full span)" if value == 1 else f"{value}x ({self._format_hz(span)})")
        self._sync_waterfall()
        self.waterfall.clear()  # old rows are the WRONG span now -- avoid a stretched/misleading transition

    def _on_waterfall_zoom_step(self, direction):
        """Mouse wheel over the spectrum/waterfall (TuneViewBox.wheelEvent)
        -- reuses the exact same _on_zoom_changed() codepath as dragging the
        Zoom slider (QSlider.setValue() clamps to the slider's own range
        automatically), rather than a second, parallel zoom mechanism."""
        self.zoom_slider.setValue(self.zoom_slider.value() + direction)

    def _on_avg_changed(self, value):
        if self.tb is None:
            return
        self.tb.set_fft_avg_count(value)
        self.avg_label.setText("1 (off)" if value == 1 else str(value))

    # --- RADE Auto Fine-Tune -------------------------------------------
    # A one-shot, multi-step process (RF-domain frequency/gain centering,
    # then a bounded RADE-lock-feedback fallback sweep) driven by a chain of
    # QTimer.singleShot() calls -- the same idiom already used for
    # _finish_m17_unkey()/_finish_rade_unkey()-style delayed follow-ups,
    # just longer. self._autotune_token is both the "is a run in progress"
    # flag (None = idle) and a staleness guard: every scheduled step closes
    # over the token it was scheduled under and checks it's still the
    # current one before touching anything, so a callback left over from a
    # cancelled/superseded run (disconnect, bandwidth rebuild, leaving RADE
    # mode) silently no-ops instead of acting on a torn-down flowgraph.
    def _autotune_gain_stage(self, device_cls):
        """The GainStage this feature treats as "the" gain lever -- the
        AGC-controlled one for AGC-capable devices (Pluto/RTL-SDR), or the
        "VGA" stage for HackRF (matching how the existing manual gain panel
        already treats VGA as the primary drive-level control; LNA/AMP are
        deliberately left alone -- a 3-stage grid search would need
        RADE-lock feedback per combination, far too slow). None if neither
        applies (shouldn't happen for any backend implemented so far)."""
        if device_cls.supports_agc_mode:
            return next(s for s in device_cls.gain_stages if s.controls_agc)
        for s in device_cls.gain_stages:
            if s.name == "VGA":
                return s
        return None

    def _autotune_force_manual_gain(self, device_cls):
        """AGC devices reject direct gain writes while AGC is active, and a
        live AGC adjustment mid-reception would fight RADE V1's total lack
        of internal input-level compensation (confirmed in rade_api.h:
        rade_rx_set_agc is V2-only) -- so force manual and leave it there."""
        if not device_cls.supports_agc_mode:
            return
        idx = self.gain_mode_combo.findData("manual")
        if idx >= 0:
            self.gain_mode_combo.setCurrentIndex(idx)  # -> _on_gain_mode_changed -> tb.set_gain_mode + enables gain_slider

    def _autotune_read_gain_db(self, stage):
        if stage.name in self._manual_gain_controls:
            widget, _label, _stage = self._manual_gain_controls[stage.name]
            return float(widget.value())
        return float(self.gain_slider.value())

    def _autotune_apply_gain_db(self, stage, value):
        """Clamps to the stage's own range and drives it through the
        existing slider (so the already-wired _on_gain_changed/
        _on_manual_gain_changed handlers do the real tb.device.set_gain()
        call) -- returns the actually-applied (clamped, rounded) value."""
        value = max(stage.min_value, min(stage.max_value, value))
        if stage.name in self._manual_gain_controls:
            widget, _label, _stage = self._manual_gain_controls[stage.name]
            widget.setValue(int(round(value)))
        else:
            self.gain_slider.setValue(int(round(value)))
        return value

    def _autotune_set_controls_enabled(self, enabled):
        for w in (self.fine_slider, self.agc_gain_widget, self.manual_gain_widget, self.zoom_slider):
            w.setEnabled(enabled)
        if enabled:
            self.gain_slider.setEnabled(self.gain_mode_combo.currentData() == "manual")

    def _autotune_valid(self, token):
        return (self._autotune_token is token and self.tb is not None
                and self.demod_combo.currentData() == AdvancedRxFlowgraph.MODE_RADE)

    def _autotune_finish(self, success):
        self._autotune_token = None
        self.autotune_button.setText("Auto Fine-Tune")
        self._autotune_set_controls_enabled(True)
        if success and self.tb is not None:
            dec = self.tb.rade_decoder
            self.rade_status_label.setText(
                f"Auto fine-tune: locked! freq offset: {dec.freq_offset_hz:.1f} Hz, SNR: {dec.snr_db:.1f} dB"
            )
        elif not success:
            self.rade_status_label.setText("Auto fine-tune: could not lock -- try adjusting manually.")

    def _autotune_cancel(self):
        if self._autotune_token is None:
            return
        self._autotune_token = None
        self.autotune_button.setText("Auto Fine-Tune")
        self._autotune_set_controls_enabled(True)

    def _on_autotune_clicked(self):
        if self._autotune_token is not None:
            self._autotune_cancel()
            self.rade_status_label.setText("Auto fine-tune cancelled.")
            return
        if self.tb is None or self.demod_combo.currentData() != AdvancedRxFlowgraph.MODE_RADE:
            return
        token = object()
        self._autotune_token = token
        self.autotune_button.setText("Cancel Auto Fine-Tune")
        self._autotune_set_controls_enabled(False)
        self.rade_status_label.setText("Auto fine-tune: preparing...")
        QtCore.QTimer.singleShot(0, lambda: self._autotune_step_prepare(token))

    def _autotune_step_prepare(self, token):
        """Stage 1 start: bump zoom for adequate frequency resolution (left
        zoomed in afterward -- a good side effect, the operator can see the
        now-centered signal) -- see FftProbe's zoom-FFT technique, no
        rebuild needed."""
        if not self._autotune_valid(token):
            return
        zoom = max(self.zoom_slider.value(), config.RADE_AUTOTUNE_MIN_ZOOM)
        if zoom != self.zoom_slider.value():
            self.zoom_slider.setValue(zoom)
        QtCore.QTimer.singleShot(int(config.RADE_AUTOTUNE_SETTLE_S * 1000),
                                  lambda: self._autotune_step_center_freq(token))

    def _autotune_step_center_freq(self, token):
        """Stage 1a: power-weighted centroid within +/-FINE_TUNE_RANGE_HZ of
        the current tuning (reusing the fine-tune slider's own existing
        bound, not a new arbitrary search radius)."""
        if not self._autotune_valid(token):
            return
        self.rade_status_label.setText("Auto fine-tune: centering frequency...")
        row, _gen = self.tb.fft_probe.get_latest_row(-1)
        if row is not None:
            center_hz = self.tb.nominal_freq_hz + self.tb.fine_offset_hz
            span_hz = self.tb.sample_rate / self.tb.fft_probe.zoom
            est = rade_autotune.estimate_signal_center(
                row, center_hz, span_hz, center_hz, config.FINE_TUNE_RANGE_HZ,
                exclude_radius_hz=config.RADE_AUTOTUNE_NOISE_EXCLUDE_HZ,
            )
            if est is not None:
                new_fine = self.tb.fine_offset_hz + (est - center_hz)
                new_fine = max(-config.FINE_TUNE_RANGE_HZ, min(config.FINE_TUNE_RANGE_HZ, new_fine))
                self.fine_slider.setValue(int(round(new_fine)))
            else:
                # Previously a silent no-op -- the operator couldn't tell
                # Stage 1a had found nothing to center on vs. simply having
                # nothing to do. Now visible, briefly, before the next
                # step's own status text overwrites it.
                self.rade_status_label.setText(
                    "Auto fine-tune: no clear signal found, skipping centering..."
                )
        QtCore.QTimer.singleShot(int(config.RADE_AUTOTUNE_SETTLE_S * 1000),
                                  lambda: self._autotune_step_gain(token))

    def _autotune_step_gain(self, token):
        """Stage 1b start: force manual gain (AGC devices), then hand off to
        the measure/adjust loop."""
        if not self._autotune_valid(token):
            return
        self.rade_status_label.setText("Auto fine-tune: adjusting gain...")
        device_cls = devices.DEVICE_REGISTRY[self.device_type_combo.currentData()]
        stage = self._autotune_gain_stage(device_cls)
        if stage is None:
            QtCore.QTimer.singleShot(int(config.RADE_AUTOTUNE_DWELL_S * 1000),
                                      lambda: self._autotune_step_check_initial_sync(token))
            return
        self._autotune_force_manual_gain(device_cls)
        self._autotune_gain_iterations = 0
        QtCore.QTimer.singleShot(int(config.RADE_AUTOTUNE_SETTLE_S * 1000),
                                  lambda: self._autotune_step_gain_iterate(token, stage))

    def _autotune_step_gain_iterate(self, token, stage):
        """Up to 2 damped proportional correction steps toward
        RADE_AUTOTUNE_TARGET_SNR_DB -- relative to the window's own local
        noise floor, NOT an absolute FftProbe dB reading (an explicit
        unmeasured placeholder either way -- see config.py), each clamped
        to +/-MAX_GAIN_STEP_DB to avoid a wild single-step jump."""
        if not self._autotune_valid(token):
            return
        row, _gen = self.tb.fft_probe.get_latest_row(-1)
        if row is not None:
            center_hz = self.tb.nominal_freq_hz + self.tb.fine_offset_hz
            span_hz = self.tb.sample_rate / self.tb.fft_probe.zoom
            snr_db = rade_autotune.measure_peak_snr_db(
                row, center_hz, span_hz, center_hz, config.FINE_TUNE_RANGE_HZ,
                exclude_radius_hz=config.RADE_AUTOTUNE_NOISE_EXCLUDE_HZ,
            )
            delta = config.RADE_AUTOTUNE_TARGET_SNR_DB - snr_db
            delta = max(-config.RADE_AUTOTUNE_MAX_GAIN_STEP_DB, min(config.RADE_AUTOTUNE_MAX_GAIN_STEP_DB, delta))
            self._autotune_apply_gain_db(stage, self._autotune_read_gain_db(stage) + delta)
            self._autotune_gain_iterations += 1
            if abs(delta) > config.RADE_AUTOTUNE_SNR_TOLERANCE_DB and self._autotune_gain_iterations < 2:
                QtCore.QTimer.singleShot(int(config.RADE_AUTOTUNE_SETTLE_S * 1000),
                                          lambda: self._autotune_step_gain_iterate(token, stage))
                return
        QtCore.QTimer.singleShot(int(config.RADE_AUTOTUNE_DWELL_S * 1000),
                                  lambda: self._autotune_step_check_initial_sync(token))

    def _autotune_step_check_initial_sync(self, token):
        """End of Stage 1: already locked? Done. Otherwise start Stage 2's
        bounded frequency sweep (RADE gives no usable continuous feedback
        pre-sync -- rade_freq_offset()/snrdB_3k_est() are only valid once
        already synced, confirmed in rade_api.h and in RadeDecoder itself
        -- so this is a plain sequential search, not gradient-following)."""
        if not self._autotune_valid(token):
            return
        if self.tb.rade_decoder.synced:
            self._autotune_finish(success=True)
            return
        self.rade_status_label.setText("Auto fine-tune: not locked yet, trying nearby frequencies...")
        self._autotune_base_fine = self.tb.fine_offset_hz
        device_cls = devices.DEVICE_REGISTRY[self.device_type_combo.currentData()]
        stage = self._autotune_gain_stage(device_cls)
        self._autotune_base_gain = self._autotune_read_gain_db(stage) if stage is not None else None
        QtCore.QTimer.singleShot(0, lambda: self._autotune_step_freq_sweep(token, 0))

    def _autotune_step_freq_sweep(self, token, index):
        if not self._autotune_valid(token):
            return
        steps = config.RADE_AUTOTUNE_FREQ_SWEEP_STEPS_HZ
        if index >= len(steps):
            QtCore.QTimer.singleShot(0, lambda: self._autotune_step_gain_sweep(token, 0))
            return
        candidate = max(-config.FINE_TUNE_RANGE_HZ,
                         min(config.FINE_TUNE_RANGE_HZ, self._autotune_base_fine + steps[index]))
        self.fine_slider.setValue(int(round(candidate)))
        self.rade_status_label.setText(
            f"Auto fine-tune: trying {candidate:+.0f} Hz ({index + 1}/{len(steps)})..."
        )
        QtCore.QTimer.singleShot(int(config.RADE_AUTOTUNE_DWELL_S * 1000),
                                  lambda: self._autotune_step_freq_sweep_check(token, index))

    def _autotune_step_freq_sweep_check(self, token, index):
        if not self._autotune_valid(token):
            return
        if self.tb.rade_decoder.synced:
            self._autotune_finish(success=True)
            return
        QtCore.QTimer.singleShot(0, lambda: self._autotune_step_freq_sweep(token, index + 1))

    def _autotune_step_gain_sweep(self, token, index):
        if not self._autotune_valid(token):
            return
        self.fine_slider.setValue(int(round(self._autotune_base_fine)))  # back to Stage 1's best frequency
        steps = config.RADE_AUTOTUNE_GAIN_SWEEP_STEPS_DB
        if index >= len(steps) or self._autotune_base_gain is None:
            self._autotune_finish(success=False)
            return
        device_cls = devices.DEVICE_REGISTRY[self.device_type_combo.currentData()]
        stage = self._autotune_gain_stage(device_cls)
        candidate = self._autotune_apply_gain_db(stage, self._autotune_base_gain + steps[index])
        self.rade_status_label.setText(
            f"Auto fine-tune: trying gain {candidate:.0f}dB ({index + 1}/{len(steps)})..."
        )
        QtCore.QTimer.singleShot(int(config.RADE_AUTOTUNE_DWELL_S * 1000),
                                  lambda: self._autotune_step_gain_sweep_check(token, index))

    def _autotune_step_gain_sweep_check(self, token, index):
        if not self._autotune_valid(token):
            return
        if self.tb.rade_decoder.synced:
            self._autotune_finish(success=True)
            return
        QtCore.QTimer.singleShot(0, lambda: self._autotune_step_gain_sweep(token, index + 1))

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
        self._autotune_cancel()  # a rebuild replaces self.tb -- any in-flight run's captured state is now stale
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
        new_tb.set_fft_zoom(self.zoom_slider.value())  # carry the current zoom/averaging over too --
        new_tb.set_fft_avg_count(self.avg_slider.value())  # a fresh FftProbe otherwise silently resets to 1x/off
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
        # freq_spin is hidden (not reset) for a frequency-less device like
        # Audio Input -- its value could be a stale RF frequency left over
        # from whatever device was previously selected, so don't trust it
        # for a device with no frequency concept at all.
        frequency_hz = 0.0 if device_cls.frequency_range_hz == (0.0, 0.0) else self.freq_spin.value() * 1e6
        try:
            new_tb = AdvancedRxFlowgraph(
                uri=connection,
                frequency=frequency_hz,
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
        new_tb.set_fft_zoom(self.zoom_slider.value())  # carry the operator's current zoom/averaging
        new_tb.set_fft_avg_count(self.avg_slider.value())  # preference over a fresh connect too
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
