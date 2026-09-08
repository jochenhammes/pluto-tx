"""PyQt5 GUI for the PlutoSDR TX app (stage 3+).

Deliberately PyQt5, not PyQt6: libgnuradio-qtgui on this system is linked
against Qt5, and mixing two Qt runtimes in one process is a crash risk.
"""
import os
import signal
import sys

from PyQt5 import QtCore, QtWidgets, sip

from . import config
from . import devices
from .devices import pluto as pluto_device
from .flowgraph import PlutoTxFlowgraph, M17_AVAILABLE, FREEDV_AVAILABLE, RADE_AVAILABLE, _default_wav_path
from .freedv_ctypes import FREEDV_MODE_2020, FREEDV_MODE_2020B


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, uri, frequency_hz=config.DEFAULT_FREQUENCY,
                 atten_ceiling_db=pluto_device.DEFAULT_ATTEN_CEILING, mode=PlutoTxFlowgraph.MODE_FM,
                 source=PlutoTxFlowgraph.SRC_MIC, wav_path=None, m17_src_callsign="",
                 m17_dst_callsign=config.M17_DEFAULT_DST_CALLSIGN,
                 freedv_variant=config.FREEDV_DEFAULT_MODE, freedv_callsign=""):
        """Builds the window in a disconnected/default state using the given
        initial settings (mirrors PlutoTxFlowgraph's own constructor
        defaults), then immediately attempts one real connection via
        _rebuild() -- the exact same bounded-timeout, exception-safe path
        already used for every later reconnect. If the device isn't
        reachable at startup, __init__ finishes with the window in exactly
        the state a manual Disconnect leaves it in (self.tb is None,
        controls disabled, status message explaining why) instead of
        crashing or hanging the whole app before a window ever appears."""
        super().__init__()
        self.tb = None
        self.setWindowTitle("PlutoSDR TX")
        self._armed = True  # False after emergency stop, until re-armed
        self._atten_ceiling_db = atten_ceiling_db  # fixed for the session, carried across reconnects
        self._wav_path = wav_path or _default_wav_path()  # carried across reconnects; updated on a file pick

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        layout = QtWidgets.QVBoxLayout(central)

        # --- Device type + connection ------------------------------------
        # One connection combo shared by every backend (holds a libiio URI
        # for PlutoSDR, a HackRF serial -- or blank for "the only attached
        # one" -- for HackRF): both are just a connection string with an
        # optional Scan-populated dropdown, so a second widget pair per
        # backend would only duplicate this row, not add real capability.
        # Relabelled/retooltipped by _on_device_type_changed() below.
        device_row = QtWidgets.QHBoxLayout()
        device_row.addWidget(QtWidgets.QLabel("Device Type:"))
        self.device_type_combo = QtWidgets.QComboBox()
        for device_type, device_cls in devices.DEVICE_REGISTRY.items():
            self.device_type_combo.addItem(device_cls.display_name, device_type)
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
        layout.addLayout(device_row)
        self._update_device_connection_labels()

        # --- Frequency + fine tune ---------------------------------
        freq_row = QtWidgets.QHBoxLayout()
        freq_row.addWidget(QtWidgets.QLabel("Frequency (MHz):"))
        self.freq_spin = QtWidgets.QDoubleSpinBox()
        self.freq_spin.setDecimals(4)
        self.freq_spin.setRange(47.0, 6000.0)
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

        # Manual, operator-tuned frequency offset -- only shown for backends
        # with supports_frequency_correction=True (currently just HackRF: a
        # real ~25kHz offset was observed on real hardware, and this
        # system's SoapyHackRF driver exposes no automatic correction to
        # compensate for it in software). Not a calibrated value -- the
        # operator dials it in empirically against their own receiver.
        self.freq_correction_label = QtWidgets.QLabel("Freq. Correction (Hz):")
        self.freq_correction_label.setVisible(False)
        freq_row.addWidget(self.freq_correction_label)
        self.freq_correction_spin = QtWidgets.QSpinBox()
        self.freq_correction_spin.setRange(-200_000, 200_000)
        self.freq_correction_spin.setSingleStep(100)
        self.freq_correction_spin.setValue(0)
        self.freq_correction_spin.valueChanged.connect(self._on_freq_correction_changed)
        self.freq_correction_spin.setVisible(False)
        freq_row.addWidget(self.freq_correction_spin)
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
        self.mode_combo.addItem("FreeDV", PlutoTxFlowgraph.MODE_FREEDV)
        if not FREEDV_AVAILABLE:
            # Unlike M17, this normally shouldn't trigger -- libcodec2 with
            # 2020/2020B support is already a transitive dependency of the
            # `gnuradio` apt package (see config.py's FreeDV comment).
            # Still grey out defensively for an older libcodec2 build.
            freedv_item_idx = self.mode_combo.findData(PlutoTxFlowgraph.MODE_FREEDV)
            item = self.mode_combo.model().item(freedv_item_idx)
            item.setEnabled(False)
            self.mode_combo.setItemData(
                freedv_item_idx, "libcodec2 on this system lacks FreeDV 2020/2020B support", QtCore.Qt.ToolTipRole
            )
        self.mode_combo.addItem("RADE", PlutoTxFlowgraph.MODE_RADE)
        if not RADE_AVAILABLE:
            # rade_c is an optional, from-source dependency (see
            # install-rade.sh) -- grey out rather than hide, same as M17.
            rade_item_idx = self.mode_combo.findData(PlutoTxFlowgraph.MODE_RADE)
            item = self.mode_combo.model().item(rade_item_idx)
            item.setEnabled(False)
            self.mode_combo.setItemData(
                rade_item_idx, "librade.so/lpcnet_demo not found -- see install-rade.sh / README",
                QtCore.Qt.ToolTipRole,
            )
        # Sync to the flowgraph's ACTUAL mode before wiring the change
        # signal -- otherwise the combo always shows "FM" regardless of
        # what mode tb was actually constructed with (e.g. --mode ssb, or
        # M17 passed in directly), and connecting the signal first would
        # fire _on_mode_changed() -> tb.set_mode() before tb.start() has
        # run, which raises (blocks.selector's ninputs isn't known yet).
        initial_idx = self.mode_combo.findData(mode)
        if initial_idx >= 0:
            self.mode_combo.setCurrentIndex(initial_idx)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        mode_row.addWidget(self.mode_combo)

        mode_row.addWidget(QtWidgets.QLabel("Source:"))
        self.source_combo = QtWidgets.QComboBox()
        self.source_combo.addItem("Microphone", PlutoTxFlowgraph.SRC_MIC)
        self.source_combo.addItem("Audio File", PlutoTxFlowgraph.SRC_FILE)
        initial_source_idx = self.source_combo.findData(source)
        if initial_source_idx >= 0:
            self.source_combo.setCurrentIndex(initial_source_idx)
        self.source_combo.currentIndexChanged.connect(self._on_source_changed)
        mode_row.addWidget(self.source_combo)

        self.file_button = QtWidgets.QPushButton(self._file_button_text(self._wav_path))
        self.file_button.setEnabled(False)
        self.file_button.clicked.connect(self._on_pick_file)
        mode_row.addWidget(self.file_button)
        layout.addLayout(mode_row)
        layout.addSpacing(12)

        # --- M17 callsigns -- only VISIBLE in M17 mode (not just enabled/
        # disabled like the other controls), per explicit request: these
        # settings are meaningless outside M17 mode, so hide the whole row
        # rather than just greying it out. Wrapped in a QWidget because
        # QLayout itself has no setVisible() -- only widgets do.
        m17_row = QtWidgets.QHBoxLayout()
        m17_row.addWidget(QtWidgets.QLabel("M17 Src Callsign:"))
        self.m17_src_edit = QtWidgets.QLineEdit(m17_src_callsign)
        self.m17_src_edit.setMaxLength(config.M17_CALLSIGN_MAX_LEN)
        self.m17_src_edit.setPlaceholderText("e.g. DA2JH")
        self.m17_src_edit.textChanged.connect(self._on_m17_src_callsign_changed)
        m17_row.addWidget(self.m17_src_edit)
        m17_row.addWidget(QtWidgets.QLabel("Dst Callsign:"))
        self.m17_dst_edit = QtWidgets.QLineEdit(m17_dst_callsign)
        self.m17_dst_edit.setMaxLength(config.M17_CALLSIGN_MAX_LEN)
        self.m17_dst_edit.textChanged.connect(self._on_m17_dst_callsign_changed)
        m17_row.addWidget(self.m17_dst_edit)
        m17_row.addStretch(1)
        self.m17_row_widget = QtWidgets.QWidget()
        self.m17_row_widget.setLayout(m17_row)
        layout.addWidget(self.m17_row_widget)
        self._update_m17_controls_enabled()

        # --- FreeDV variant + callsign row -- only VISIBLE in FreeDV mode,
        # same reasoning as the M17 row above (settings meaningless outside
        # that mode). The callsign field itself is separately PERMANENTLY
        # DISABLED whenever the row IS visible (not wired to
        # _update_freedv_controls_enabled like the variant combo): linking
        # FreeDV's reliable_text station-ID sideband (which this field would
        # feed) crashes the packaged libcodec2 (1.2.0-4) with a confirmed,
        # deterministic SIGFPE inside freedv_comptx_2020() -- see
        # pluto_tx/freedv_ctypes.py's FreeDVSession docstring and the FreeDV
        # section of README.md for the full writeup. Kept visible-but-
        # disabled (not hidden) so the operator can see the feature exists
        # and why it's off, rather than silently doing nothing -- station ID
        # must be handled some other way (e.g. voice ID before/after a
        # FreeDV transmission) until this is resolved upstream.
        freedv_row = QtWidgets.QHBoxLayout()
        freedv_row.addWidget(QtWidgets.QLabel("FreeDV Variant:"))
        self.freedv_variant_combo = QtWidgets.QComboBox()
        self.freedv_variant_combo.addItem("2020", FREEDV_MODE_2020)
        self.freedv_variant_combo.addItem("2020B", FREEDV_MODE_2020B)
        initial_variant_idx = self.freedv_variant_combo.findData(freedv_variant)
        if initial_variant_idx >= 0:
            self.freedv_variant_combo.setCurrentIndex(initial_variant_idx)
        self.freedv_variant_combo.currentIndexChanged.connect(self._on_freedv_variant_changed)
        freedv_row.addWidget(self.freedv_variant_combo)
        freedv_row.addWidget(QtWidgets.QLabel("Callsign:"))
        self.freedv_callsign_edit = QtWidgets.QLineEdit(freedv_callsign)
        self.freedv_callsign_edit.setMaxLength(config.FREEDV_CALLSIGN_MAX_LEN)
        self.freedv_callsign_edit.setPlaceholderText("disabled -- libcodec2 bug, see README")
        self.freedv_callsign_edit.setToolTip(
            "Disabled: linking FreeDV's station-ID text sideband crashes this system's "
            "libcodec2 (confirmed upstream bug). ID by voice before/after instead."
        )
        self.freedv_callsign_edit.setEnabled(False)
        freedv_row.addWidget(self.freedv_callsign_edit)
        freedv_row.addStretch(1)
        self.freedv_row_widget = QtWidgets.QWidget()
        self.freedv_row_widget.setLayout(freedv_row)
        layout.addWidget(self.freedv_row_widget)
        self._update_freedv_controls_enabled()

        # --- RADE EOO (End-of-Over) row -- only VISIBLE in RADE mode, same
        # "hide the whole row" reasoning as M17/FreeDV above. Off by default
        # and needs its own isolated real-hardware verification (Phase I2 of
        # the RADE integration plan, not done yet this session) before being
        # a safe default to turn on -- see flowgraph.py's
        # rade_eoo_enabled docstring.
        rade_row = QtWidgets.QHBoxLayout()
        self.rade_eoo_checkbox = QtWidgets.QCheckBox("Send EOO tail on unkey")
        self.rade_eoo_checkbox.setChecked(False)
        self.rade_eoo_checkbox.setToolTip(
            "Transmits a brief (144ms) End-of-Over IQ tail after PTT release, "
            "so a receiver gets a clean end-of-stream instead of a hard cutoff. "
            "Off by default -- not yet verified on real hardware."
        )
        self.rade_eoo_checkbox.toggled.connect(self._on_rade_eoo_changed)
        rade_row.addWidget(self.rade_eoo_checkbox)
        rade_row.addStretch(1)
        self.rade_row_widget = QtWidgets.QWidget()
        self.rade_row_widget.setLayout(rade_row)
        layout.addWidget(self.rade_row_widget)
        self._update_rade_controls_enabled()

        # --- Power / attenuation ---------------------------------------
        # power_slider is reused across backends (relabelled/reranged by
        # _on_device_type_changed()) rather than one slider per device --
        # every backend's primary power stage is a single continuous value,
        # so a second widget would only duplicate this row. amp_checkbox is
        # the one backend-specific extra so far (HackRF's coarse +14dB amp,
        # a non-primary stage with no Pluto equivalent) -- hidden outside
        # HackRF mode, same "hide the whole control, don't just greatly it"
        # reasoning as the M17/FreeDV rows above.
        power_row = QtWidgets.QHBoxLayout()
        self.power_row_label = QtWidgets.QLabel("TX Power (Attenuation, dB):")
        power_row.addWidget(self.power_row_label)
        self.power_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.unlock_full_power = QtWidgets.QCheckBox("Unlock full power")
        self._refresh_power_slider_range()
        self.power_slider.valueChanged.connect(self._on_power_changed)
        self.unlock_full_power.stateChanged.connect(self._on_unlock_changed)
        power_row.addWidget(self.power_slider)
        self.power_label = QtWidgets.QLabel()
        power_row.addWidget(self.power_label)
        power_row.addWidget(self.unlock_full_power)
        self.amp_checkbox = QtWidgets.QCheckBox("TX Amp (+14dB)")
        self.amp_checkbox.setChecked(False)
        self.amp_checkbox.setVisible(False)
        self.amp_checkbox.toggled.connect(self._on_amp_changed)
        power_row.addWidget(self.amp_checkbox)
        layout.addLayout(power_row)
        layout.addSpacing(12)
        self.power_slider.setValue(int(round(atten_ceiling_db)))

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
        self._embed_waterfall(None)

        # --- Lifecycle: periodic tick doubles as (a) Ctrl-C responsiveness
        # for Qt's event loop and (b) a live hardware-state readback, so the
        # GUI can never silently disagree with what's actually on the AD9361
        # (the exact failure mode we hit with SDRangel earlier).
        self._timer = QtCore.QTimer()
        self._timer.timeout.connect(self._tick)
        self._timer.start(500)

        # Everything above builds the window with widgets in their normal
        # (enabled, "Disconnect"-labelled) construction defaults, which only
        # makes sense once actually connected. Put it into the disconnected
        # default presentation first (redundant with what _rebuild()'s own
        # failure branches already do, but matches pluto_rx/
        # pluto_advanced_rx's identical setup and doesn't rely on that),
        # then attempt the actual initial connection through the exact same
        # bounded-timeout, exception-safe path a later Connect/reconnect
        # uses. If the device isn't reachable, these defaults are what's
        # left on screen, with an explanatory status message, instead of
        # raising -- so the app always ends up with a window on screen,
        # connected or not.
        self._set_connected_controls_enabled(False)
        self.connect_button.setText("Connect")
        self.uri_combo.setEnabled(True)
        self.device_type_combo.setEnabled(True)
        self._sync_device_dependent_widgets()  # sync freq range/power label/amp visibility to "PlutoSDR"
        self._rebuild(uri, self._wav_path)

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
                  self.power_slider, self.unlock_full_power, self.amp_checkbox, self.nf_gain_slider,
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
        self._freedv_connected = enabled
        self._update_freedv_controls_enabled()
        self._rade_connected = enabled
        self._update_rade_controls_enabled()

    def _update_m17_controls_enabled(self):
        # Visibility follows the selected mode (row hidden entirely outside
        # M17); enabled state within a visible row still follows connection
        # state, same as every other control in the app.
        is_m17_mode = self.mode_combo.currentData() == PlutoTxFlowgraph.MODE_M17
        self.m17_row_widget.setVisible(is_m17_mode)
        connected = getattr(self, "_m17_connected", True)
        self.m17_src_edit.setEnabled(connected)
        self.m17_dst_edit.setEnabled(connected)

    def _update_freedv_controls_enabled(self):
        # Visibility follows the selected mode (row hidden entirely outside
        # FreeDV); enabled state within a visible row still follows
        # connection state, same as every other control in the app.
        is_freedv_mode = self.mode_combo.currentData() == PlutoTxFlowgraph.MODE_FREEDV
        self.freedv_row_widget.setVisible(is_freedv_mode)
        connected = getattr(self, "_freedv_connected", True)
        self.freedv_variant_combo.setEnabled(connected)

    def _update_rade_controls_enabled(self):
        # Visibility follows the selected mode (row hidden entirely outside
        # RADE); enabled state within a visible row still follows connection
        # state, same as every other control in the app.
        is_rade_mode = self.mode_combo.currentData() == PlutoTxFlowgraph.MODE_RADE
        self.rade_row_widget.setVisible(is_rade_mode)
        connected = getattr(self, "_rade_connected", True)
        self.rade_eoo_checkbox.setEnabled(connected)
        # freedv_callsign_edit is NOT touched here -- it's permanently
        # disabled at construction (see the comment above its creation):
        # linking FreeDV's reliable_text station-ID sideband crashes this
        # system's libcodec2.

    def _style_estop_button(self, locked: bool):
        self.estop_button.setText("Re-arm" if locked else "E-STOP")
        color = "#7f8c8d" if locked else "#c0392b"
        self.estop_button.setStyleSheet(f"background-color: {color}; color: white; font-weight: bold;")

    def _refresh_power_slider_range(self):
        device_cls = devices.DEVICE_REGISTRY[self.device_type_combo.currentData()]
        stage = devices.primary_power_stage(device_cls.device_type)
        # Deliberately device_cls.default_power_ceiling here, not
        # self._atten_ceiling_db (the value actually passed to the
        # flowgraph's power_ceiling) -- a pre-existing inconsistency carried
        # forward unchanged into this generalized version, see the device-
        # abstraction plan.
        ceiling = stage.max_value if self.unlock_full_power.isChecked() else device_cls.default_power_ceiling
        self.power_slider.setRange(int(round(stage.min_value)), int(round(ceiling)))

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
            self.device_label.setText("HackRF Serial (blank = auto):")
            self.uri_combo.setToolTip(
                "HackRF serial number, or leave blank to use the only attached "
                "HackRF. Use Scan to discover attached HackRF devices (needs "
                "python3-soapysdr, see install.sh)."
            )

    def _on_device_type_changed(self, idx):
        """Only takes effect on the next Connect -- device_type_combo (like
        uri_combo) is only editable while disconnected. Also clears the
        connection field: a Pluto URI left behind after switching to
        HackRF (or vice versa) looks like a valid value but isn't -- the
        operator has to notice and clear it manually otherwise (real bug
        report)."""
        self.uri_combo.blockSignals(True)
        self.uri_combo.clear()
        self.uri_combo.clearEditText()
        self.uri_combo.blockSignals(False)
        self._sync_device_dependent_widgets()

    def _sync_device_dependent_widgets(self):
        """Cosmetic/range updates only, never touches self.tb. Called both
        by _on_device_type_changed() above and once at the end of __init__
        to sync the initial (PlutoSDR) selection -- split out from that
        handler so the initial call doesn't also wipe the uri_combo value
        the constructor was given."""
        self._update_device_connection_labels()
        device_cls = devices.DEVICE_REGISTRY[self.device_type_combo.currentData()]
        lo_hz, hi_hz = device_cls.frequency_range_hz
        self.freq_spin.setRange(lo_hz / 1e6, hi_hz / 1e6)
        self._atten_ceiling_db = device_cls.default_power_ceiling
        stage = devices.primary_power_stage(device_cls.device_type)
        self.power_row_label.setText(f"TX Power ({stage.label}, {stage.unit}):" if stage.unit
                                      else f"TX Power ({stage.label}):")
        self._refresh_power_slider_range()
        secondary_stages = [s for s in device_cls.power_stages if not s.is_primary]
        if secondary_stages:
            self.amp_checkbox.setText(secondary_stages[0].label)
            self.amp_checkbox.setVisible(True)
        else:
            self.amp_checkbox.setVisible(False)
        self.freq_correction_label.setVisible(device_cls.supports_frequency_correction)
        self.freq_correction_spin.setVisible(device_cls.supports_frequency_correction)
        # Reset to this device type's default correction -- safe to do
        # unconditionally here: this method only runs on an actual device-
        # type switch (see _on_device_type_changed()) or once at startup,
        # never on a plain reconnect within the same type, so it never
        # clobbers a value the operator already tuned this session.
        self.freq_correction_spin.setValue(int(getattr(device_cls, "DEFAULT_FREQUENCY_CORRECTION_HZ", 0)))

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
    # self.tb is None while disconnected (including the brief window during
    # __init__ before the initial _rebuild() attempt resolves) -- normally
    # that's harmless because these controls are also disabled then, and a
    # disabled widget can't fire a signal from user interaction, but a
    # *programmatic* setValue()/setChecked() during initial construction
    # still fires it. So every handler that touches self.tb guards it, same
    # as the M17/FreeDV callsign handlers already did before this.
    def _on_freq_changed(self, mhz):
        if self.tb is not None:
            self.tb.set_frequency(mhz * 1e6)

    def _on_fine_changed(self, value):
        if self.tb is not None:
            self.tb.set_fine_offset(float(value))
        self.fine_label.setText(f"{value} Hz")

    def _on_freq_correction_changed(self, value):
        if self.tb is not None:
            self.tb.device.set_frequency_correction(float(value))

    def _on_nf_gain_changed(self, value):
        if self.tb is not None:
            self.tb.set_nf_gain(value / 100.0)
        self.nf_gain_label.setText(f"{value} %")

    def _on_gate_enabled_changed(self, checked):
        if self.tb is not None:
            self.tb.set_gate_enabled(checked)
        self.gate_threshold_slider.setEnabled(checked and self.gate_enable.isEnabled())

    def _on_gate_threshold_changed(self, value):
        if self.tb is not None:
            self.tb.set_gate_threshold(float(value))
        self.gate_threshold_label.setText(f"{value} dB")

    def _on_compressor_enabled_changed(self, checked):
        if self.tb is not None:
            self.tb.set_compressor_enabled(checked)
        enabled = checked and self.compressor_enable.isEnabled()
        self.compressor_threshold_slider.setEnabled(enabled)
        self.compressor_ratio_slider.setEnabled(enabled)

    def _on_compressor_threshold_changed(self, value):
        if self.tb is not None:
            self.tb.set_compressor_threshold(float(value))
        self.compressor_threshold_label.setText(f"{value} dB")

    def _on_compressor_ratio_changed(self, value):
        if self.tb is not None:
            self.tb.set_compressor_ratio(float(value))
        self.compressor_ratio_label.setText(f"{value}:1")

    def _on_limiter_enabled_changed(self, checked):
        if self.tb is not None:
            self.tb.set_limiter_enabled(checked)

    def _on_mode_changed(self, idx):
        if self.tb is not None:
            self.tb.set_mode(self.mode_combo.currentData())
        self._update_m17_controls_enabled()
        self._update_freedv_controls_enabled()
        self._update_rade_controls_enabled()

    def _on_rade_eoo_changed(self, checked):
        if self.tb is not None:
            self.tb.set_rade_eoo_enabled(checked)

    def _on_m17_src_callsign_changed(self, text):
        if self.tb is not None:
            self.tb.set_m17_src_callsign(text)

    def _on_m17_dst_callsign_changed(self, text):
        if self.tb is not None:
            self.tb.set_m17_dst_callsign(text)

    def _on_freedv_variant_changed(self, idx):
        if self.tb is not None:
            self.tb.set_freedv_variant(self.freedv_variant_combo.currentData())

    def _on_freedv_callsign_changed(self, text):
        if self.tb is not None:
            self.tb.set_freedv_callsign(text)

    def _on_source_changed(self, idx):
        source = self.source_combo.currentData()
        if self.tb is not None:
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
        self._rebuild(self.tb.device.connection, path)

    def _on_power_changed(self, value):
        if self.tb is not None:
            self.tb.set_target_power(float(value))
        stage = devices.primary_power_stage(self.device_type_combo.currentData())
        self.power_label.setText(f"{value} {stage.unit}".strip())

    def _on_unlock_changed(self, _state):
        self._refresh_power_slider_range()

    def _on_amp_changed(self, checked):
        if self.tb is not None:
            self.tb.set_secondary_power("AMP", checked)

    def _on_ptt_mode_toggle_clicked(self):
        self._configure_ptt_button(hold_mode=not self._ptt_hold_mode)

    def _configure_ptt_button(self, hold_mode: bool):
        """Rewire the PTT button between click-toggle and press-and-hold
        semantics. Switching modes while keyed would leave the RF on with no
        way to release it under the new mode's signals -- always unkey first."""
        if self.tb is not None and self.tb.keyed:
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
        if checked and (not self._armed or self.tb is None):
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
        if not self._armed or self.tb is None:
            return
        self.tb.key_ptt()
        self._set_indicator_on_air()

    def _on_ptt_released(self):
        if self.tb is None or not self.tb.keyed:
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
        if self.tb is None:
            return
        self.tb.unkey_ptt()
        if self.mode_combo.currentData() == PlutoTxFlowgraph.MODE_M17:
            self._set_indicator_ending()
            QtCore.QTimer.singleShot(int(config.M17_EOT_HOLD_S * 1000), self._finish_m17_unkey)
        elif self.mode_combo.currentData() == PlutoTxFlowgraph.MODE_RADE and self.tb.rade_eoo_enabled:
            self._set_indicator_ending()
            QtCore.QTimer.singleShot(int(self.tb.rade_eoo_hold_s * 1000), self._finish_rade_unkey)
        else:
            self._set_indicator_idle()

    def _finish_m17_unkey(self):
        if self.tb is not None:
            self.tb.finish_unkey_m17()
        self._set_indicator_idle()

    def _finish_rade_unkey(self):
        if self.tb is not None:
            self.tb.finish_unkey_rade()
        self._set_indicator_idle()

    def _on_connect_clicked(self):
        if self.tb is not None:
            self._disconnect()
        else:
            self._connect(self.uri_combo.currentText())

    def _on_scan_clicked(self):
        device_type = self.device_type_combo.currentData()
        device_cls = devices.DEVICE_REGISTRY[device_type]
        self.status_label.setText(f"Scanning for {device_cls.display_name} devices...")
        QtWidgets.QApplication.processEvents()
        found, error = device_cls.scan_devices_with_timeout()
        if error is not None:
            self.status_label.setText(f"Scan failed: {error}")
            return
        found.pop("local:", None)  # libiio's local-context artifact, never a Pluto (harmless no-op for HackRF)
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
        self.device_type_combo.setEnabled(True)
        self.compressor_gr_label.setText("GR: 0.0 dB")
        self.status_label.setText("Disconnected.")

    def _connect(self, uri_text):
        device_cls = devices.DEVICE_REGISTRY[self.device_type_combo.currentData()]
        # Pluto's connection is a libiio URI (bare hostname/IP gets 'ip:'
        # prefixed); HackRF's is a bare serial (or blank for "the only
        # attached one") -- normalize_uri() would wrongly mangle that.
        connection = config.normalize_uri(uri_text) if device_cls.connection_kind == "uri" else uri_text.strip()
        if device_cls.connection_kind == "uri" and not connection:
            self.status_label.setText("Please enter a device hostname, IP, or URI.")
            return
        self._rebuild(connection, self._wav_path)

    def _rebuild(self, connection, wav_path):
        """Tear down the current flowgraph (if any) and build a fresh one at
        `connection` loading `wav_path`, carrying over every other current
        GUI setting (including the currently selected device type). Shared
        by device reconnects (Connect button) and WAV file swaps (File
        button) -- both need the exact same "safely stop the old TX chain,
        then build and start a new one" sequence; a stray reference to the
        old flowgraph here would leak its AD9361 buffer claim and break the
        next connect with 'Unable to create buffer' (the same class of bug
        run_gui() avoids by never holding a second reference to self.tb of
        its own)."""
        if self.tb is not None:
            self.tb.shutdown_safe()
            self.tb = None
            self._embed_waterfall(None)
            self._reset_session_ui_state()
        device_type = self.device_type_combo.currentData()
        device_cls = devices.DEVICE_REGISTRY[device_type]
        label = connection or "auto-detect"
        self.status_label.setText(f"Connecting to {device_cls.display_name} ({label})...")
        QtWidgets.QApplication.processEvents()
        probe_error = device_cls.probe_with_timeout(connection)
        if probe_error is not None:
            self.status_label.setText(f"Could not connect to {device_cls.display_name} ({label}): {probe_error}")
            self._set_connected_controls_enabled(False)
            self.connect_button.setText("Connect")
            self.uri_combo.setEnabled(True)
            self.device_type_combo.setEnabled(True)
            return
        try:
            new_tb = PlutoTxFlowgraph(
                device_type=device_type,
                connection=connection,
                frequency=self.freq_spin.value() * 1e6,
                power_ceiling=self._atten_ceiling_db,
                wav_path=wav_path,
                mode=self.mode_combo.currentData(),
                source=self.source_combo.currentData(),
                enable_waterfall=True,
                m17_src_callsign=self.m17_src_edit.text(),
                m17_dst_callsign=self.m17_dst_edit.text(),
                freedv_variant=self.freedv_variant_combo.currentData(),
                freedv_callsign=self.freedv_callsign_edit.text(),
            )
        except Exception as e:
            self.status_label.setText(f"Could not connect to {device_cls.display_name} ({label}): {e}")
            self._set_connected_controls_enabled(False)
            self.connect_button.setText("Connect")
            self.uri_combo.setEnabled(True)
            self.device_type_combo.setEnabled(True)
            return
        new_tb.set_fine_offset(float(self.fine_slider.value()))
        new_tb.set_nf_gain(self.nf_gain_slider.value() / 100.0)
        new_tb.set_target_power(float(self.power_slider.value()))
        new_tb.set_secondary_power("AMP", self.amp_checkbox.isChecked())
        new_tb.device.set_frequency_correction(float(self.freq_correction_spin.value()))
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
        new_tb.set_rade_eoo_enabled(self.rade_eoo_checkbox.isChecked())
        self._wav_path = new_tb.wav_path
        self.tb = new_tb
        self._embed_waterfall(new_tb)
        self.file_button.setText(self._file_button_text(new_tb.wav_path))
        new_tb.start()
        self._set_connected_controls_enabled(True)
        self.connect_button.setText("Disconnect")
        self.uri_combo.setEnabled(False)
        self.device_type_combo.setEnabled(False)
        self.status_label.setText(f"Connected to {device_cls.display_name} ({label}).")

    def _on_estop_toggled(self, checked):
        """checked=True: E-STOP triggered. checked=False: re-armed. One
        toggle button covers both directions instead of two separate ones."""
        if checked:
            self.tb.unkey_ptt()
            self.tb.device.force_safe_state()
            self._armed = False
            self._reset_ptt_button_visual()
            self.ptt_button.setEnabled(False)
            self._set_indicator_idle()
            self._style_estop_button(locked=True)
            self.status_label.setText("E-STOP triggered: attenuation at minimum, LO powered down.")
        else:
            # Deliberately NOT prepare_for_start() -- that also powers the LO
            # back up (on Pluto), which would violate the PTT<->device-safety
            # hard tie (LO stays off until the operator actually keys PTT
            # again; see flowgraph.py's key_ptt()/unkey_ptt()). Just
            # re-confirm the primary power stage is at its safe/off value.
            stage = self.tb.device.primary_stage
            self.tb.device.set_power(stage.name, stage.off_value)
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
            state = self.tb.device.read_hw_state()
            parts = (f"{k}={v:.2f}" if isinstance(v, float) else f"{k}={v}" for k, v in state.items())
            self.hw_status_label.setText("HW: " + ", ".join(parts))
        except Exception as e:
            self.hw_status_label.setText(f"HW status read failed: {e}")
        self.compressor_gr_label.setText(f"GR: {self.tb.compressor.gain_reduction_db():.1f} dB")

    # --- shutdown lifecycle -------------------------------------------
    def closeEvent(self, event):
        if self.tb is not None:
            self.tb.shutdown_safe()
        event.accept()


def run_gui(uri, frequency_hz=config.DEFAULT_FREQUENCY, atten_ceiling_db=pluto_device.DEFAULT_ATTEN_CEILING,
            mode=PlutoTxFlowgraph.MODE_FM):
    """Builds and shows the main window, which itself attempts the initial
    connection to `uri` (see MainWindow.__init__/its _rebuild() call) --
    unlike the old build-tb-first shape, this never raises just because the
    device isn't reachable at startup: the window still comes up, showing
    the same disconnected state a manual Disconnect leaves it in, with a
    status message and a Connect button to retry. self.tb (only ever set by
    MainWindow itself) stays the single reference to the running flowgraph,
    so there's no stray local reference here to leak an AD9361 buffer claim
    (the "Unable to create buffer: -16" bug this project hit before when a
    second reference existed)."""
    qapp = QtWidgets.QApplication(sys.argv)
    window = MainWindow(uri, frequency_hz=frequency_hz, atten_ceiling_db=atten_ceiling_db, mode=mode)
    window.show()

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
