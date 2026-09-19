"""PyQt5 GUI for the PlutoSDR advanced RX app -- FM/SSB/RADE/M17/Baseband
demod, gain, NF volume, RX bandwidth/zoom, device connect/disconnect/scan,
plus the interactive pyqtgraph waterfall (AdvancedWaterfallWidget):
tuned-frequency marker, click-to-tune, demod-bandwidth shading, and a live
frequency axis above the waterfall. Deliberately PyQt5, not PyQt6: see
pluto_tx/gui.py's docstring (libgnuradio-qtgui is linked against Qt5; mixing
Qt runtimes is a crash risk) -- pyqtgraph itself doesn't care, but the rest
of this codebase does.

The waterfall widget here is a plain Python/PyQt-owned QWidget (not a
sip.wrapinstance()-wrapped C++ object owned by a gr-qtgui sink block), so it
is built ONCE and simply persists across every device reconnect and RX
bandwidth rebuild -- deliberately avoiding a whole class of SIGSEGV
(a C++-owned widget racing its owning flowgraph's teardown) that a
gr-qtgui-based widget would need careful `deleteLater()`-vs-`setParent(None)`
discipline to avoid. Nothing here is C++-owned, so that risk doesn't exist
at all.
"""
import signal
import sys
import threading
import time

from PyQt5 import QtCore, QtWidgets

from pluto_tx import audio_devices
from pluto_tx import freq_correction

from . import config
from . import devices
from . import rade_autotune
from .fft_probe import FftProbe
from .filebroadcast_state import FileBroadcastState
from .flowgraph import AdvancedRxFlowgraph, RADE_AVAILABLE, M17_AVAILABLE, LORA_AVAILABLE
from .meshtastic_state import MeshtasticState
from .pocsag_state import PocsagState
if LORA_AVAILABLE:
    from pluto_tx import meshtastic_codec
from .psk31_state import Psk31ChatState
from .rtty_state import RttyChatState
from .waterfall_widget import AdvancedWaterfallWidget
from .devices.rtlsdr import DIRECT_SAMPLING_NYQUIST_HZ, DIRECT_SAMPLING_RANGE_HZ

DIRECT_SAMPLING_MHZ = (DIRECT_SAMPLING_RANGE_HZ[0] / 1e6, DIRECT_SAMPLING_RANGE_HZ[1] / 1e6)
DIRECT_SAMPLING_DEFAULT_MHZ = 7.1  # start frequency when direct sampling is switched on out of range


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

        # File Broadcast RX state -- constructed ONCE here, survives every
        # flowgraph rebuild (bandwidth change, reconnect) by construction,
        # per the plan's RX-side persistent state design (mirrors why
        # _last_audio_mode/_wav_path live on MainWindow in pluto_tx, not on
        # the flowgraph). AdvancedRxFlowgraph's File Broadcast branch is
        # always wired regardless of connection state, so frames can start
        # arriving from the moment _connect()/`_on_bandwidth_changed()
        # construct a new tb -- this must already exist by then.
        self._filebroadcast_state = FileBroadcastState()
        self._filebroadcast_snapshot = []

        # PSK31/RTTY chat RX state -- same "constructed ONCE, survives
        # every flowgraph rebuild" reasoning as _filebroadcast_state
        # above. Unlike File Broadcast, only ONE of these two is ever the
        # active digimode at a time (see AdvancedRxFlowgraph.
        # active_digimode) -- both state objects still always exist so
        # switching back to a previously-used digimode doesn't lose its
        # accumulated transcript.
        self._psk31_state = Psk31ChatState()
        self._psk31_last_afc_poll_time = 0.0
        self._rtty_state = RttyChatState()
        self._rtty_last_afc_poll_time = 0.0
        # Meshtastic traffic log -- same "constructed ONCE, survives every
        # flowgraph rebuild" reasoning as the two above.
        self._meshtastic_state = MeshtasticState()
        self._meshtastic_rendered_version = -1
        self._meshtastic_last_frames = 0
        self._meshtastic_last_activity_time = 0.0
        self._meshtastic_psk_error = None
        # POCSAG paging calls -- same "constructed ONCE, survives rebuilds" reasoning.
        self._pocsag_state = PocsagState()
        self._pocsag_rendered_version = -1
        self._pocsag_last_batches = 0
        self._pocsag_last_activity_time = 0.0
        self._link_state = None  # (id(tb), status) of the last network-link status shown, see _update_link_status()
        # Carrier (Hz) to restore when leaving Meshtastic, whose presets retune the receiver.
        self._freq_before_lora = None
        # Which digimode (None/"psk31"/"rtty") the CURRENT self.tb was
        # built with -- source of truth threaded into every
        # AdvancedRxFlowgraph(...) call, kept in sync by
        # _rebuild_for_digimode() (see its own docstring for why
        # switching needs a full flowgraph rebuild, not a cheap runtime
        # toggle). Starts None: the Digimodes tab isn't the default
        # active tab, so nothing should be decoding yet at startup.
        self._active_digimode = None

        # M17 RX status -- same "constructed ONCE, survives every flowgraph
        # rebuild" reasoning as _psk31_state above. Just the most recent
        # decoded Link Setup Frame's fields, not an accumulating transcript
        # (see _on_m17_fields()) -- a plain lock-guarded pair is enough,
        # no dedicated state class needed.
        self._m17_lock = threading.Lock()
        self._m17_last_fields = None

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        outer_layout = QtWidgets.QVBoxLayout(central)

        # Two-column layout: left column holds every RX control (Device +
        # Mode groups), right column holds the Waterfall/Spectrum group
        # (which already contains its own zoom/averaging/FFT-size/dB
        # sliders alongside the plot itself). Neither group's internal
        # construction changes below -- only which of these two columns
        # each group's own layout.addWidget(...) call targets, further
        # down. Fixes a real small/low-vertical-resolution-screen problem:
        # stacking controls above the waterfall (the old single-column
        # layout) made the window's minimum height the SUM of both; side
        # by side, it's just the max of the two, and the control stack
        # alone is comfortably short (~300-450px).
        columns_layout = QtWidgets.QHBoxLayout()
        left_column = QtWidgets.QVBoxLayout()
        right_column = QtWidgets.QVBoxLayout()
        columns_layout.addLayout(left_column, 0)   # natural width -- controls don't need to grow
        columns_layout.addLayout(right_column, 1)  # takes all extra horizontal space -- the waterfall should stay wide
        outer_layout.addLayout(columns_layout, 1)

        # --- Device group: type + connection, scan/connect -----------------
        # One connection combo shared by every backend (a libiio URI for
        # Pluto, a serial/Soapy-args string for HackRF/RTL-SDR) -- mirrors
        # pluto_tx/gui.py's identical device_type_combo + uri_combo pattern.
        # Relabelled/retooltipped by _on_device_type_changed() below.
        device_group = QtWidgets.QGroupBox("Device")
        device_group_layout = QtWidgets.QVBoxLayout(device_group)
        device_row = QtWidgets.QHBoxLayout()
        device_group_layout.addLayout(device_row)

        # --- Second device row: oscillator correction (all RF backends) and, for the
        # RTL-SDR only, direct sampling. Defaults: 0 ppm, direct sampling off.
        correction_row = QtWidgets.QHBoxLayout()
        device_group_layout.addLayout(correction_row)
        self.ppm_label = QtWidgets.QLabel("Freq. correction (ppm):")
        correction_row.addWidget(self.ppm_label)
        self.ppm_spin = QtWidgets.QDoubleSpinBox()
        self.ppm_spin.setDecimals(2)
        self.ppm_spin.setRange(*freq_correction.PPM_RANGE)
        self.ppm_spin.setSingleStep(0.1)
        self.ppm_spin.setValue(0.0)
        self.ppm_spin.setToolTip(
            "Oscillator error of this device in ppm, scaling with the tuned frequency. "
            "Positive = the device runs too HIGH (signals appear above their true frequency); "
            "the app tunes the hardware lower to compensate. Example: a 100.000 MHz broadcast "
            "carrier appears at 100.003 MHz -> +30 ppm. Default 0."
        )
        self.ppm_spin.valueChanged.connect(self._on_ppm_changed)
        correction_row.addWidget(self.ppm_spin)
        self.direct_checkbox = QtWidgets.QCheckBox("Direct sampling")
        self.direct_checkbox.setToolTip(
            "RTL-SDR only: bypass the tuner and sample the antenna input directly (HF, "
            f"{DIRECT_SAMPLING_MHZ[0]:g}-{DIRECT_SAMPLING_MHZ[1]:g} MHz). The tuner gain has no effect "
            "in this mode. Pick the branch your dongle's HF input is wired to. Above "
            f"{DIRECT_SAMPLING_NYQUIST_HZ / 1e6:g} MHz the reception is aliased: the band you tune is "
            "mixed with the mirror image of (28.8 MHz - frequency), so use a band-pass filter there."
        )
        self.direct_checkbox.toggled.connect(self._on_direct_changed)
        correction_row.addWidget(self.direct_checkbox)
        self.direct_branch_combo = QtWidgets.QComboBox()
        self.direct_branch_combo.addItem("Q branch", 2)  # RTL-SDR Blog V3 and most HF-mod dongles
        self.direct_branch_combo.addItem("I branch", 1)
        self.direct_branch_combo.setEnabled(False)
        self.direct_branch_combo.currentIndexChanged.connect(self._on_direct_changed)
        correction_row.addWidget(self.direct_branch_combo)
        correction_row.addStretch(1)
        self.direct_checkbox.setVisible(False)  # RTL-SDR only, shown by _sync_device_dependent_widgets()
        self.direct_branch_combo.setVisible(False)
        self._direct_active = False
        self._freq_before_direct = None  # frequency (MHz) to restore when direct sampling is switched off

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
        left_column.addWidget(device_group)
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
        digimodes_tab_layout = QtWidgets.QVBoxLayout(digimodes_tab)
        self._digimodes_tab_index = self.mode_tab_widget.addTab(digimodes_tab, "Digimodes")
        # Digimode selector -- mirrors pluto_tx/gui.py's own digimode_combo
        # (there: Waterfall Writer/PSK31/RTTY; here: PSK31/RTTY only,
        # Waterfall Writer being TX-only). Exactly ONE of these is ever the
        # active digimode (see AdvancedRxFlowgraph.active_digimode) --
        # selecting a different entry, or entering/leaving this tab,
        # triggers _rebuild_for_digimode() (see its own docstring for why
        # a full flowgraph rebuild is needed here, unlike demod_combo's
        # cheap runtime switch).
        digimode_row = QtWidgets.QHBoxLayout()
        digimode_row.addWidget(QtWidgets.QLabel("Digimode:"))
        self.digimode_combo = QtWidgets.QComboBox()
        self.digimode_combo.addItem("PSK31 (BPSK31 Chat)", "psk31")
        self.digimode_combo.addItem("RTTY", "rtty")
        self.digimode_combo.addItem("POCSAG (Paging)", "pocsag")
        self.digimode_combo.addItem("Meshtastic (LoRa)", "meshtastic")
        if not LORA_AVAILABLE:
            lora_item = self.digimode_combo.model().item(self.digimode_combo.findData("meshtastic"))
            lora_item.setEnabled(False)
            lora_item.setToolTip("gr-lora_sdr / the meshtastic package is not installed -- see install-lora.sh / README")
        self.digimode_combo.currentIndexChanged.connect(self._on_digimode_changed)
        digimode_row.addWidget(self.digimode_combo)
        digimode_row.addStretch(1)
        digimodes_tab_layout.addLayout(digimode_row)

        # --- PSK31 controls -- own group widget, shown only while PSK31 is
        # the selected digimode_combo entry (mirrors pluto_tx/gui.py's
        # digitext_group_widget/psk31_group_widget visibility pattern).
        psk31_group = QtWidgets.QWidget()
        psk31_group_layout = QtWidgets.QVBoxLayout(psk31_group)
        psk31_group_layout.setContentsMargins(0, 0, 0, 0)
        psk31_group_layout.addWidget(QtWidgets.QLabel(
            f"PHY: {config.PSK31_SYMBOL_RATE_HZ:g} baud BPSK31, Varicode -- keyboard-to-keyboard chat"
        ))
        psk31_tone_row = QtWidgets.QHBoxLayout()
        psk31_tone_row.addWidget(QtWidgets.QLabel("Tone (Hz):"))
        self.psk31_tone_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.psk31_tone_slider.setRange(int(config.PSK31_TONE_RANGE_HZ[0]), int(config.PSK31_TONE_RANGE_HZ[1]))
        self.psk31_tone_slider.setSingleStep(10)
        self.psk31_tone_slider.setPageStep(100)
        self.psk31_tone_slider.setValue(int(config.PSK31_DEFAULT_TONE_HZ))
        self.psk31_tone_slider.setToolTip(
            "Audio tone offset from the carrier -- must match the sending station's own "
            "tone. Real hardware testing found a substantial, continuously DRIFTING "
            "frequency offset can appear between independent TX/RX devices -- this app "
            "runs an automatic background search (AFC) to track it, but a manual retune "
            "here is always the fallback if the signal indicator below stays quiet."
        )
        self.psk31_tone_slider.valueChanged.connect(self._on_psk31_tone_changed)
        psk31_tone_row.addWidget(self.psk31_tone_slider)
        self.psk31_tone_label = QtWidgets.QLabel(f"{int(config.PSK31_DEFAULT_TONE_HZ)} Hz")
        self.psk31_tone_label.setMinimumWidth(60)
        psk31_tone_row.addWidget(self.psk31_tone_label)
        psk31_group_layout.addLayout(psk31_tone_row)
        self.psk31_signal_label = QtWidgets.QLabel()
        psk31_group_layout.addWidget(self.psk31_signal_label)
        self._psk31_last_chars_decoded = 0
        self._psk31_last_activity_time = 0.0
        self._update_psk31_signal_label()
        self.psk31_transcript = QtWidgets.QTextEdit()
        self.psk31_transcript.setReadOnly(True)
        psk31_group_layout.addWidget(self.psk31_transcript)
        psk31_clear_row = QtWidgets.QHBoxLayout()
        psk31_clear_row.addStretch(1)
        self.psk31_clear_button = QtWidgets.QPushButton("Clear Transcript")
        self.psk31_clear_button.clicked.connect(self._on_psk31_clear_clicked)
        psk31_clear_row.addWidget(self.psk31_clear_button)
        psk31_group_layout.addLayout(psk31_clear_row)
        digimodes_tab_layout.addWidget(psk31_group)
        self.psk31_group_widget = psk31_group

        # --- RTTY controls -- own group widget, same pattern as
        # psk31_group above. Unlike PSK31 (fixed baud, single tone), mark/
        # shift/baud/Normal-Reverse are all real, user-adjustable settings
        # (per explicit request) -- mirrors pluto_tx/gui.py's own RTTY
        # controls group.
        rtty_group = QtWidgets.QWidget()
        rtty_group_layout = QtWidgets.QVBoxLayout(rtty_group)
        rtty_group_layout.setContentsMargins(0, 0, 0, 0)
        rtty_group_layout.addWidget(QtWidgets.QLabel("PHY: 2-tone FSK, Baudot/ITA2 -- RTTY"))
        rtty_settings_row = QtWidgets.QHBoxLayout()
        rtty_settings_row.addWidget(QtWidgets.QLabel("Mark (Hz):"))
        self.rtty_mark_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.rtty_mark_slider.setRange(int(config.RTTY_MARK_HZ_RANGE[0]), int(config.RTTY_MARK_HZ_RANGE[1]))
        self.rtty_mark_slider.setSingleStep(5)
        self.rtty_mark_slider.setPageStep(50)
        self.rtty_mark_slider.setValue(int(config.RTTY_MARK_HZ_DEFAULT))
        self.rtty_mark_slider.setToolTip(
            "Mark tone frequency -- must match the sending station's own. Subject to the "
            "same TX/RX drift as PSK31 -- see this mode's own AFC status in the signal "
            "indicator below."
        )
        self.rtty_mark_slider.valueChanged.connect(self._on_rtty_mark_changed)
        rtty_settings_row.addWidget(self.rtty_mark_slider)
        self.rtty_mark_label = QtWidgets.QLabel(f"{int(config.RTTY_MARK_HZ_DEFAULT)} Hz")
        self.rtty_mark_label.setMinimumWidth(60)
        rtty_settings_row.addWidget(self.rtty_mark_label)

        rtty_settings_row.addWidget(QtWidgets.QLabel("Shift:"))
        self.rtty_shift_combo = QtWidgets.QComboBox()
        for shift in config.RTTY_SHIFT_HZ_PRESETS:
            self.rtty_shift_combo.addItem(f"{shift:g} Hz", shift)
        initial_shift_idx = self.rtty_shift_combo.findData(config.RTTY_SHIFT_HZ_DEFAULT)
        self.rtty_shift_combo.setCurrentIndex(initial_shift_idx if initial_shift_idx >= 0 else 0)
        self.rtty_shift_combo.currentIndexChanged.connect(self._on_rtty_shift_changed)
        rtty_settings_row.addWidget(self.rtty_shift_combo)

        rtty_settings_row.addWidget(QtWidgets.QLabel("Baud:"))
        self.rtty_baud_combo = QtWidgets.QComboBox()
        for baud in config.RTTY_BAUD_RATE_PRESETS:
            self.rtty_baud_combo.addItem(f"{baud:g}", baud)
        initial_baud_idx = self.rtty_baud_combo.findData(config.RTTY_BAUD_RATE_DEFAULT)
        self.rtty_baud_combo.setCurrentIndex(initial_baud_idx if initial_baud_idx >= 0 else 0)
        self.rtty_baud_combo.currentIndexChanged.connect(self._on_rtty_baud_changed)
        rtty_settings_row.addWidget(self.rtty_baud_combo)

        self.rtty_reverse_checkbox = QtWidgets.QCheckBox("Reverse")
        self.rtty_reverse_checkbox.setToolTip(
            "Swaps which audio tone is Mark vs. Space -- use this if a sending station's "
            "tone assignment is flipped relative to this one."
        )
        self.rtty_reverse_checkbox.toggled.connect(self._on_rtty_reverse_changed)
        rtty_settings_row.addWidget(self.rtty_reverse_checkbox)
        rtty_settings_row.addStretch(1)
        rtty_group_layout.addLayout(rtty_settings_row)
        self.rtty_signal_label = QtWidgets.QLabel()
        rtty_group_layout.addWidget(self.rtty_signal_label)
        self._rtty_last_chars_decoded = 0
        self._rtty_last_activity_time = 0.0
        self._update_rtty_signal_label()
        self.rtty_transcript = QtWidgets.QTextEdit()
        self.rtty_transcript.setReadOnly(True)
        rtty_group_layout.addWidget(self.rtty_transcript)
        rtty_clear_row = QtWidgets.QHBoxLayout()
        rtty_clear_row.addStretch(1)
        self.rtty_clear_button = QtWidgets.QPushButton("Clear Transcript")
        self.rtty_clear_button.clicked.connect(self._on_rtty_clear_clicked)
        rtty_clear_row.addWidget(self.rtty_clear_button)
        rtty_group_layout.addLayout(rtty_clear_row)
        digimodes_tab_layout.addWidget(rtty_group)
        self.rtty_group_widget = rtty_group

        # --- Meshtastic (LoRa) controls -- own group widget, same pattern as
        # rtty_group above. Receive-only here; sending lives in pluto_tx.
        meshtastic_group = QtWidgets.QWidget()
        meshtastic_group_layout = QtWidgets.QVBoxLayout(meshtastic_group)
        meshtastic_group_layout.setContentsMargins(0, 0, 0, 0)
        meshtastic_preset_row = QtWidgets.QHBoxLayout()
        meshtastic_preset_row.addWidget(QtWidgets.QLabel("Preset:"))
        self.meshtastic_preset_combo = QtWidgets.QComboBox()
        for i, preset in enumerate(config.MESHTASTIC_PRESETS):
            self.meshtastic_preset_combo.addItem(preset.name, i)
        # MeshCore stays a visible but disabled placeholder (no protocol layer yet).
        for preset in config.MESHCORE_PRESETS:
            self.meshtastic_preset_combo.addItem(f"{preset.name} (placeholder)", -1)
            item = self.meshtastic_preset_combo.model().item(self.meshtastic_preset_combo.count() - 1)
            item.setEnabled(False)
            item.setToolTip(config.MESHCORE_PLACEHOLDER_TIP)
        self._meshtastic_default_channel = self._meshtastic_preset().default_channel_name
        self.meshtastic_preset_combo.currentIndexChanged.connect(self._on_meshtastic_preset_changed)
        meshtastic_preset_row.addWidget(self.meshtastic_preset_combo)
        meshtastic_preset_row.addWidget(QtWidgets.QLabel("Channel:"))
        self.meshtastic_channel_edit = QtWidgets.QLineEdit(self._meshtastic_default_channel)
        self.meshtastic_channel_edit.setMaximumWidth(110)
        self.meshtastic_channel_edit.setToolTip("Channel name (feeds the header's channel-hash pre-filter).")
        self.meshtastic_channel_edit.textChanged.connect(self._on_meshtastic_channel_changed)
        meshtastic_preset_row.addWidget(self.meshtastic_channel_edit)
        meshtastic_preset_row.addWidget(QtWidgets.QLabel("PSK:"))
        self.meshtastic_psk_edit = QtWidgets.QLineEdit(config.MESHTASTIC_DEFAULT_PSK_B64)
        self.meshtastic_psk_edit.setMaximumWidth(260)
        self.meshtastic_psk_edit.setToolTip(
            "Base64 channel key as shown in the Meshtastic apps. \"AQ==\" is the default "
            "channel key; empty = unencrypted. Frames on Ham-Mode presets (433 MHz) are "
            "tried unencrypted first.")
        self.meshtastic_psk_edit.textChanged.connect(self._on_meshtastic_channel_changed)
        meshtastic_preset_row.addWidget(self.meshtastic_psk_edit)
        meshtastic_preset_row.addStretch(1)
        meshtastic_group_layout.addLayout(meshtastic_preset_row)
        self.meshtastic_regulatory_label = QtWidgets.QLabel()
        self.meshtastic_regulatory_label.setWordWrap(True)
        meshtastic_group_layout.addWidget(self.meshtastic_regulatory_label)
        self.meshtastic_signal_label = QtWidgets.QLabel()
        meshtastic_group_layout.addWidget(self.meshtastic_signal_label)
        self.meshtastic_table = QtWidgets.QTableWidget(0, 6)
        self.meshtastic_table.setHorizontalHeaderLabels(["Time", "From", "To", "Type", "Hops", "Message"])
        self.meshtastic_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.meshtastic_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.meshtastic_table.verticalHeader().setVisible(False)
        self.meshtastic_table.horizontalHeader().setStretchLastSection(True)
        self.meshtastic_table.setMinimumHeight(160)
        self.meshtastic_table.setToolTip(
            "Every CRC-valid LoRa frame heard on the preset's frequency. Text of frames on "
            "another channel (different name/key) can't be read -- they are listed as \"other channel\".")
        meshtastic_group_layout.addWidget(self.meshtastic_table)
        meshtastic_clear_row = QtWidgets.QHBoxLayout()
        meshtastic_clear_row.addStretch(1)
        self.meshtastic_clear_button = QtWidgets.QPushButton("Clear")
        self.meshtastic_clear_button.clicked.connect(self._on_meshtastic_clear_clicked)
        meshtastic_clear_row.addWidget(self.meshtastic_clear_button)
        meshtastic_group_layout.addLayout(meshtastic_clear_row)
        digimodes_tab_layout.addWidget(meshtastic_group)
        self.meshtastic_group_widget = meshtastic_group
        # --- POCSAG (paging) RX: tune the carrier of a 25 kHz paging channel; 512/1200/2400 Bd are
        # decoded in parallel. Rows keep the raw payload, so charset/interpretation re-render the table.
        pocsag_group = QtWidgets.QWidget()
        pocsag_layout = QtWidgets.QVBoxLayout(pocsag_group)
        pocsag_layout.setContentsMargins(0, 0, 0, 0)
        pocsag_row = QtWidgets.QHBoxLayout()
        pocsag_row.addWidget(QtWidgets.QLabel("Show as:"))
        self.pocsag_interp_combo = QtWidgets.QComboBox()
        self.pocsag_interp_combo.addItem("Auto (function 0 = numeric)", "auto")
        self.pocsag_interp_combo.addItem("Alphanumeric", "alpha")
        self.pocsag_interp_combo.addItem("Numeric", "numeric")
        self.pocsag_interp_combo.currentIndexChanged.connect(
            lambda _i: self._pocsag_state.set_interpretation(self.pocsag_interp_combo.currentData()))
        pocsag_row.addWidget(self.pocsag_interp_combo)
        self.pocsag_charset_checkbox = QtWidgets.QCheckBox("German charset")
        self.pocsag_charset_checkbox.setToolTip(
            "DIN 66003 mapping used by German paging networks: [ \\ ] { | } ~ = \u00c4 \u00d6 \u00dc \u00e4 \u00f6 \u00fc \u00df.")
        self.pocsag_charset_checkbox.toggled.connect(
            lambda on: self._pocsag_state.set_charset("de" if on else "ascii"))
        pocsag_row.addWidget(self.pocsag_charset_checkbox)
        self.pocsag_hide_damaged_checkbox = QtWidgets.QCheckBox("Hide damaged calls")
        self.pocsag_hide_damaged_checkbox.setChecked(True)
        self.pocsag_hide_damaged_checkbox.setToolTip(
            "Hide calls with codewords the error correction could not recover -- on a weak signal these are mostly junk.")
        self.pocsag_hide_damaged_checkbox.toggled.connect(self._pocsag_state.set_hide_damaged)
        pocsag_row.addWidget(self.pocsag_hide_damaged_checkbox)
        pocsag_row.addStretch(1)
        self.pocsag_clear_button = QtWidgets.QPushButton("Clear")
        self.pocsag_clear_button.clicked.connect(self._on_pocsag_clear_clicked)
        pocsag_row.addWidget(self.pocsag_clear_button)
        pocsag_layout.addLayout(pocsag_row)
        self.pocsag_signal_label = QtWidgets.QLabel()
        pocsag_layout.addWidget(self.pocsag_signal_label)
        self.pocsag_table = QtWidgets.QTableWidget(0, 6)
        self.pocsag_table.setHorizontalHeaderLabels(["Time", "Baud", "RIC", "Fn", "Message", "Errors"])
        self.pocsag_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.pocsag_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.pocsag_table.verticalHeader().setVisible(False)
        self.pocsag_table.horizontalHeader().setStretchLastSection(True)
        self.pocsag_table.setMinimumHeight(160)
        self.pocsag_table.setToolTip(
            "Errors = bit errors corrected by the BCH code / codewords that could not be recovered "
            "(the message text is incomplete then).")
        pocsag_layout.addWidget(self.pocsag_table)
        digimodes_tab_layout.addWidget(pocsag_group)
        self.pocsag_group_widget = pocsag_group
        self._update_pocsag_signal_label()

        self._apply_meshtastic_channels()
        self._update_meshtastic_regulatory_label()
        self._update_meshtastic_signal_label()

        self._update_digimode_controls_enabled()
        # File-Transfer: Phase 4's directory table -- this codebase's first
        # QTableWidget (Filename/Size/Progress/Record-toggle per row),
        # replacing Phase 1-3's plain QListWidget. Always live, independent
        # of demod_mode/connection state, since the underlying GNU Radio
        # branch is always wired too. Driven by the same QTimer poll of
        # FileBroadcastState.get_snapshot() as before (see
        # _poll_filebroadcast()) -- the lock-guarded "compute a plain
        # snapshot under the lock, rebuild the widget from it" idiom
        # already proven by FftProbe/waterfall_widget.py, not new
        # cross-thread Qt signal plumbing.
        filetransfer_tab = QtWidgets.QWidget()
        filetransfer_tab_layout = QtWidgets.QVBoxLayout(filetransfer_tab)
        # Static PHY info -- the operator needs to know what to expect/tune
        # for; these are fixed constants (not adjustable), unlike FM/SSB's
        # width sliders, so a plain label is enough.
        filetransfer_tab_layout.addWidget(QtWidgets.QLabel(
            f"PHY: {config.FILEBROADCAST_SYMBOL_RATE_HZ/1000:.0f} kbaud GFSK, "
            f"deviation {config.FILEBROADCAST_DEVIATION_HZ/1000:.0f} kHz, BT={config.FILEBROADCAST_BT}"
        ))
        # Start/Stop reception -- mirrors receive_button's mute-gate
        # pattern below (own toggle, own _style_*, defaults to NOT
        # receiving so a fresh connect never silently starts accumulating
        # files the operator hasn't asked for yet). Gates only WHETHER
        # incoming frames get applied to FileBroadcastState (see
        # _on_filebroadcast_frame()) -- the underlying GNU Radio GFSK
        # branch keeps running regardless (cheap, passive, and its
        # frame_count/crc_fail_count counters are what the signal-status
        # label below reads to show "is anything even being decoded here"
        # independently of whether the operator has pressed Start).
        self.filebroadcast_receive_button = QtWidgets.QPushButton()
        self.filebroadcast_receive_button.setCheckable(True)
        self.filebroadcast_receive_button.setChecked(False)
        self.filebroadcast_receive_button.setMinimumHeight(40)
        self.filebroadcast_receive_button.toggled.connect(self._on_filebroadcast_receive_toggled)
        self._style_filebroadcast_receive_button(receiving=False)
        filetransfer_tab_layout.addWidget(self.filebroadcast_receive_button)
        self._filebroadcast_receiving = False
        # Live signal/tuning indicator: derived from the deframer's own
        # frame_count/crc_fail_count -- EITHER counter moving means the
        # bit-by-bit SYNC_WORD search is actually matching something in
        # the incoming bit stream (a real, always-available proxy for "is
        # there a File Broadcast signal here at all, roughly correctly
        # tuned", since a sync match only happens after real symbol-level
        # lock -- see filebroadcast_deframer.py), regardless of whether
        # those frames go on to pass their CRC-16 check. Updated every
        # _poll_filebroadcast() tick.
        self.filebroadcast_signal_label = QtWidgets.QLabel()
        filetransfer_tab_layout.addWidget(self.filebroadcast_signal_label)
        self._filebroadcast_last_attempt_total = 0
        self._filebroadcast_last_activity_time = 0.0
        self._update_filebroadcast_signal_label()
        self.filebroadcast_table = QtWidgets.QTableWidget(0, 4)
        self.filebroadcast_table.setHorizontalHeaderLabels(["Filename", "Size", "Received", "Record"])
        self.filebroadcast_table.horizontalHeader().setStretchLastSection(False)
        self.filebroadcast_table.horizontalHeader().setSectionResizeMode(
            0, QtWidgets.QHeaderView.Stretch,
        )
        self.filebroadcast_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.filebroadcast_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.filebroadcast_table.currentCellChanged.connect(self._on_filebroadcast_selection_changed)
        self.filebroadcast_table.itemChanged.connect(self._on_filebroadcast_item_changed)
        filetransfer_tab_layout.addWidget(self.filebroadcast_table)
        self.filebroadcast_save_button = QtWidgets.QPushButton("Save Selected File...")
        self.filebroadcast_save_button.setEnabled(False)
        self.filebroadcast_save_button.clicked.connect(self._on_filebroadcast_save_clicked)
        filetransfer_tab_layout.addWidget(self.filebroadcast_save_button)
        self.mode_tab_widget.addTab(filetransfer_tab, "File-Transfer")
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

        # Audio-domain counterpart to freq_spin, for backends with no RF LO
        # to retune but a real DSP frequency shift makes sense anyway (e.g.
        # Audio Input's ~10kHz of incoming sound-card spectrum) -- native Hz,
        # not MHz, since that span is far too small for freq_spin's scale.
        # Exactly one of the two is ever visible, see
        # _sync_device_dependent_widgets(). fine_slider/fine_label just
        # below are SHARED between both -- already Hz-scale, already the
        # right role for a fine nudge on top of either.
        self.audio_tune_label = QtWidgets.QLabel("Audio Tune (Hz):")
        freq_row.addWidget(self.audio_tune_label)
        self.audio_tune_spin = QtWidgets.QDoubleSpinBox()
        self.audio_tune_spin.setDecimals(0)
        self.audio_tune_spin.setSingleStep(10.0)
        self.audio_tune_spin.valueChanged.connect(self._on_audio_tune_changed)
        freq_row.addWidget(self.audio_tune_spin)

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
        self.demod_combo.addItem("SSB (LSB)", AdvancedRxFlowgraph.MODE_LSB)
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
        self.demod_combo.addItem("M17", AdvancedRxFlowgraph.MODE_M17)
        if not M17_AVAILABLE:
            # gr-m17 is an optional, from-source-built OOT module (see
            # install-m17.sh) -- same grey-out-with-tooltip idiom as RADE.
            m17_item_idx = self.demod_combo.findData(AdvancedRxFlowgraph.MODE_M17)
            item = self.demod_combo.model().item(m17_item_idx)
            item.setEnabled(False)
            self.demod_combo.setItemData(
                m17_item_idx, "gnuradio.m17 not found -- see install-m17.sh / README",
                QtCore.Qt.ToolTipRole,
            )
        self.demod_combo.addItem("Baseband", AdvancedRxFlowgraph.MODE_BASEBAND)
        # Always available, same reasoning as pluto_tx's own Baseband entry
        # -- pure GNU Radio blocks, no optional/from-source dependency.
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

        # M17 status row -- same "hide the whole row" pattern as RADE's
        # above, showing the decoded src/dst callsign from the most
        # recently received Link Setup Frame (M17FieldsDeframer's
        # on_m17_fields callback -- see _on_m17_fields()) rather than a
        # continuous sync metric like RADE's SNR/freq-offset (M17 has no
        # equivalent -- it's frame-based, not a continuous-sync modem).
        m17_row = QtWidgets.QHBoxLayout()
        self.m17_status_label = QtWidgets.QLabel("No signal")
        m17_row.addWidget(self.m17_status_label)
        m17_row.addStretch(1)
        self.m17_row_widget = QtWidgets.QWidget()
        self.m17_row_widget.setLayout(m17_row)
        self.m17_row_widget.setVisible(False)
        audio_tab_layout.addWidget(self.m17_row_widget)

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

        # Where RX's own demodulated audio plays out -- real ALSA devices
        # (System Default first, from list_output_devices()) plus a
        # persistent, qpwgraph-visible pw-loopback node
        # (ensure_persistent_output_node()) so another application can
        # treat this app's received audio as a stable input source. Exact
        # same pattern as pluto_tx's Soundcard-mode output combo -- see
        # pluto_tx/audio_devices.py and pluto_tx/devices/soundcard.py.
        audio_device_row = QtWidgets.QHBoxLayout()
        audio_device_row.addWidget(QtWidgets.QLabel("Audio Output:"))
        self.audio_device_combo = QtWidgets.QComboBox()
        for device_str, label in audio_devices.list_output_devices().items():
            self.audio_device_combo.addItem(label, device_str)
        persistent_device_str = audio_devices.ensure_persistent_output_node(name="pluto-advanced-rx-output")
        if persistent_device_str is not None:
            self.audio_device_combo.addItem("pluto-advanced-rx Output (qpwgraph)", persistent_device_str)
        # The INPUT-side persistent node (see devices/audio.py's
        # AudioDevice.scan_devices_with_timeout()) is normally only ensured
        # once the operator switches to the "Audio Input" device type and
        # clicks Scan -- create/verify it here too, unconditionally, so
        # BOTH persistent nodes are already up and qpwgraph-visible right
        # after launch (e.g. after a crash took them down), not only after
        # that specific combo is first opened. Idempotent/cheap if it
        # already exists; the returned string itself isn't needed here.
        audio_devices.ensure_persistent_input_node(name="pluto-advanced-rx-input")
        self._audio_device = self.audio_device_combo.currentData()  # "" (System Default) -- single
        # source of truth threaded into every AdvancedRxFlowgraph(...) call below, kept in sync by
        # _on_audio_device_changed() only after a rebuild actually succeeds.
        self.audio_device_combo.currentIndexChanged.connect(self._on_audio_device_changed)
        audio_device_row.addWidget(self.audio_device_combo)
        audio_tab_layout.addLayout(audio_device_row)

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

        # Pluto-only libiio buffer size (see RxDevice.supports_buffer_size/
        # config.py's PLUTO_RX_BUFFER_SIZE_* for the real-hardware
        # throughput story). Row visibility gated on the current device's
        # supports_buffer_size in _sync_device_dependent_widgets(), same
        # capability-flag idiom already used for agc_gain_widget/
        # manual_gain_widget. Always populated with the same preset list
        # regardless of device (nothing depends on device_cls here, unlike
        # bandwidth_combo) -- harmless when hidden/ignored by a non-Pluto
        # backend.
        buffer_size_row = QtWidgets.QHBoxLayout()
        self.buffer_size_label = QtWidgets.QLabel("RX Buffer Size:")
        buffer_size_row.addWidget(self.buffer_size_label)
        self.buffer_size_combo = QtWidgets.QComboBox()
        for bs in config.PLUTO_RX_BUFFER_SIZE_PRESETS:
            self.buffer_size_combo.addItem(str(bs), bs)
        self.buffer_size_combo.setCurrentIndex(
            config.PLUTO_RX_BUFFER_SIZE_PRESETS.index(config.PLUTO_RX_BUFFER_SIZE_DEFAULT))
        self.buffer_size_combo.currentIndexChanged.connect(self._on_buffer_size_changed)
        buffer_size_row.addWidget(self.buffer_size_combo)
        # Real-hardware-measured hint (see config.py's PLUTO_RX_BUFFER_SIZE_*
        # docstring): 262144 was the actual verified throughput sweet spot,
        # both over direct Ethernet (~9-10 -> ~11-12 Msps) and, more
        # modestly, over the USB-Ethernet-gadget path (~4.7 -> ~5.1 Msps) --
        # larger values were NOT re-verified and 1M/2M measured WORSE, so
        # this points at a specific preset, not just "bigger is better".
        self.buffer_size_hint_label = QtWidgets.QLabel("262144 recommended for highest throughput")
        self.buffer_size_hint_label.setStyleSheet("color: gray; font-style: italic;")
        buffer_size_row.addWidget(self.buffer_size_hint_label)
        buffer_size_row.addStretch(1)
        device_group_layout.addLayout(buffer_size_row)

        left_column.addWidget(mode_group)
        left_column.addStretch(1)  # keeps device_group/mode_group at natural height instead of
        # stretching to fill the (likely taller) right column's height -- see the column-layout comment above

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
        # Each digimode's marker/band should only be visible while it's
        # both the selected digimode AND the operator is actually on the
        # Digimodes tab -- see set_psk31_visible()/set_rtty_visible()'s
        # own docstrings. mode_tab_widget defaults to the "Audio" tab
        # (index 0), so both start hidden; _on_mode_tab_changed()/
        # _sync_waterfall() keep them in sync from here on.
        self.mode_tab_widget.currentChanged.connect(self._on_mode_tab_changed)
        self.waterfall.set_psk31_visible(False)
        self.waterfall.set_rtty_visible(False)

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

        right_column.addWidget(waterfall_group)

        self.status_label = QtWidgets.QLabel()
        outer_layout.addWidget(self.status_label)

        self._sync_waterfall()

        # QTimer polls fft_probe for new rows (waterfall render) and also
        # doubles as Ctrl-C responsiveness for Qt's event loop, same reason
        # as every other GUI in this repo.
        self._timer = QtCore.QTimer()
        self._timer.timeout.connect(self._poll_fft)
        self._timer.timeout.connect(self._poll_filebroadcast)
        self._timer.timeout.connect(self._poll_digimode)
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
        for w in (self.freq_spin, self.audio_tune_spin, self.fine_slider, self.demod_combo, self.width_slider,
                  self.agc_gain_widget, self.manual_gain_widget, self.nf_gain_slider,
                  self.bandwidth_combo, self.fft_size_combo, self.zoom_slider, self.avg_slider,
                  self.receive_button, self.autotune_button, self.filebroadcast_receive_button,
                  self.digimode_combo, self.psk31_tone_slider, self.rtty_mark_slider,
                  self.meshtastic_preset_combo, self.meshtastic_channel_edit, self.meshtastic_psk_edit,
                  self.rtty_shift_combo, self.rtty_baud_combo, self.rtty_reverse_checkbox):
            w.setEnabled(enabled)
        self.gain_slider.setEnabled(enabled and self.gain_mode_combo.currentData() == "manual")
        if not enabled:
            self._autotune_cancel()

    def _style_receive_button(self, receiving: bool):
        self.receive_button.setText("Receiving (click to mute)" if receiving else "Muted (click to receive)")
        color = "#27ae60" if receiving else "#7f8c8d"
        self.receive_button.setStyleSheet(f"background-color: {color}; color: white; font-weight: bold;")

    def _style_filebroadcast_receive_button(self, receiving: bool):
        self.filebroadcast_receive_button.setText(
            "Receiving Files (click to stop)" if receiving else "Stopped (click to start receiving)"
        )
        color = "#27ae60" if receiving else "#7f8c8d"
        self.filebroadcast_receive_button.setStyleSheet(f"background-color: {color}; color: white; font-weight: bold;")

    def _update_digimode_controls_enabled(self):
        # Which of psk31_group_widget/rtty_group_widget is showing follows
        # digimode_combo's own selection -- mirrors pluto_tx/gui.py's
        # _update_psk31_controls_enabled()/_update_digitext_controls_enabled().
        selected = self.digimode_combo.currentData()
        self.psk31_group_widget.setVisible(selected == "psk31")
        self.rtty_group_widget.setVisible(selected == "rtty")
        self.meshtastic_group_widget.setVisible(selected == "meshtastic")
        self.pocsag_group_widget.setVisible(selected == "pocsag")

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
        elif device_cls.device_type == "rtlsdr":
            self.device_label.setText("RTL-SDR (serial, or host[:port] of an rtl_tcp server):")
            self.uri_combo.setToolTip(
                "Blank = the only USB-attached RTL-SDR; a serial number picks a specific one "
                "(Scan lists attached dongles). To use a dongle on another machine, run "
                "'rtl_tcp -a 0.0.0.0 -p 1234' there and enter its address here, e.g. "
                "192.168.178.34:1234 (port defaults to 1234; a prefix rtl_tcp:// also works). "
                "rtl_tcp serves one client at a time and has no authentication -- trusted "
                "network only. At 2.4 MS/s the stream is ~4.8 MB/s; pick a lower RX bandwidth "
                "over WLAN."
            )
            self.uri_combo.lineEdit().setPlaceholderText("serial or 192.168.178.34:1234")
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
        # a new device type starts with the defaults: 0 ppm, direct sampling off
        self.ppm_spin.blockSignals(True)
        self.ppm_spin.setValue(0.0)
        self.ppm_spin.blockSignals(False)
        self.direct_checkbox.blockSignals(True)
        self.direct_checkbox.setChecked(False)
        self.direct_checkbox.blockSignals(False)
        self.direct_branch_combo.setEnabled(False)
        self._direct_active = False
        self._freq_before_direct = None
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

        # RF tuning (freq_spin) vs. audio-domain tuning (audio_tune_spin) --
        # mutually exclusive, hide rather than just grey out (same "hide,
        # don't just grey out" principle already used for the RADE-mode
        # width slider). fine_slider/fine_label are shared, visible for
        # either.
        has_frequency = device_cls.frequency_range_hz != (0.0, 0.0)
        has_audio_tuning = device_cls.audio_tuning_range_hz is not None
        self.freq_spin.setVisible(has_frequency)
        self.ppm_label.setVisible(has_frequency)
        self.ppm_spin.setVisible(has_frequency)
        self.direct_checkbox.setVisible(device_cls.supports_direct_sampling)
        self.direct_branch_combo.setVisible(device_cls.supports_direct_sampling)
        # LoRa needs a real RF front end -- a sound card can't carry it.
        lora_item = self.digimode_combo.model().item(self.digimode_combo.findData("meshtastic"))
        lora_item.setEnabled(LORA_AVAILABLE and has_frequency)
        if LORA_AVAILABLE and not has_frequency:
            lora_item.setToolTip("LoRa needs an RF device, not a soundcard")
            if self.digimode_combo.currentData() == "meshtastic":
                self.digimode_combo.setCurrentIndex(self.digimode_combo.findData("psk31"))
        pocsag_item = self.digimode_combo.model().item(self.digimode_combo.findData("pocsag"))
        pocsag_item.setEnabled(has_frequency)
        if not has_frequency:
            pocsag_item.setToolTip("POCSAG needs an RF device, not a soundcard")
            if self.digimode_combo.currentData() == "pocsag":
                self.digimode_combo.setCurrentIndex(self.digimode_combo.findData("psk31"))
        self.audio_tune_label.setVisible(has_audio_tuning)
        self.audio_tune_spin.setVisible(has_audio_tuning)
        self.fine_slider.setVisible(has_frequency or has_audio_tuning)
        self.fine_label.setVisible(has_frequency or has_audio_tuning)
        if has_frequency:
            lo_hz, hi_hz = device_cls.frequency_range_hz
            self.freq_spin.setRange(lo_hz / 1e6, hi_hz / 1e6)
        if has_audio_tuning:
            lo_hz, hi_hz = device_cls.audio_tuning_range_hz
            self.audio_tune_spin.setRange(lo_hz, hi_hz)

        # See RxDevice.max_waterfall_zoom's docstring -- a backend with a
        # much lower sample rate than RF's needs a tighter zoom cap to keep
        # the waterfall from stuttering. setMaximum() clamps an
        # out-of-range current value automatically, so switching FROM a
        # high RF zoom level needs no separate clamping here.
        self.zoom_slider.setMaximum(device_cls.max_waterfall_zoom)

        self.bandwidth_combo.blockSignals(True)
        self.bandwidth_combo.clear()
        for bw in device_cls.sample_rate_hz_choices:
            self.bandwidth_combo.addItem(self._format_hz(bw), bw)
        default_idx = device_cls.sample_rate_hz_choices.index(device_cls.default_sample_rate_hz)
        self.bandwidth_combo.setCurrentIndex(default_idx)
        self.bandwidth_combo.blockSignals(False)

        self.buffer_size_label.setVisible(device_cls.supports_buffer_size)
        self.buffer_size_combo.setVisible(device_cls.supports_buffer_size)
        self.buffer_size_hint_label.setVisible(device_cls.supports_buffer_size)
        if device_cls.supports_buffer_size:
            self.buffer_size_combo.blockSignals(True)
            self.buffer_size_combo.setCurrentIndex(
                config.PLUTO_RX_BUFFER_SIZE_PRESETS.index(config.PLUTO_RX_BUFFER_SIZE_DEFAULT))
            self.buffer_size_combo.blockSignals(False)

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

    def _direct_mode(self):
        """0 = off, 1 = I branch, 2 = Q branch."""
        return self.direct_branch_combo.currentData() if self.direct_checkbox.isChecked() else 0

    def _on_ppm_changed(self, value):
        if self.tb is not None:
            self.tb.set_frequency_correction_ppm(float(value))

    def _on_direct_changed(self, *_):
        """Direct-sampling checkbox / I-Q dropdown. Switches the tuning range of freq_spin to
        the HF range (and back), remembering the previous frequency, then re-tunes."""
        mode = self._direct_mode()
        device_cls = devices.DEVICE_REGISTRY[self.device_type_combo.currentData()]
        self.direct_branch_combo.setEnabled(self.direct_checkbox.isChecked())
        enabling, disabling = bool(mode) and not self._direct_active, not mode and self._direct_active
        self._direct_active = bool(mode)
        if enabling or disabling:
            self.freq_spin.blockSignals(True)
            if enabling:
                self._freq_before_direct = self.freq_spin.value()
                lo, hi = device_cls.direct_sampling_range_hz
                self.freq_spin.setRange(lo / 1e6, hi / 1e6)
                if not lo / 1e6 <= self._freq_before_direct <= hi / 1e6:
                    self.freq_spin.setValue(DIRECT_SAMPLING_DEFAULT_MHZ)
            else:
                lo, hi = device_cls.frequency_range_hz
                self.freq_spin.setRange(lo / 1e6, hi / 1e6)
                if self._freq_before_direct is not None:
                    self.freq_spin.setValue(self._freq_before_direct)
                self._freq_before_direct = None
            self.freq_spin.blockSignals(False)
        if self.tb is not None:
            self.tb.set_direct_sampling(mode)
            self.tb.set_frequency(self.freq_spin.value() * 1e6)
            self._sync_waterfall()

    def _current_gain_kwargs(self, device_cls):
        """gain_mode/manual_gain_db for an AGC-capable device (Pluto/
        RTL-SDR, read from gain_mode_combo/gain_slider), or gain_values for
        a purely-manual one (HackRF, read from the per-stage manual gain
        panel). AdvancedRxFlowgraph accepts both unconditionally -- the
        unused one is simply inert for that backend (see its own
        docstring) -- so the caller doesn't need to branch on device_cls
        itself, just call this and pass the result through."""
        # also carries the device options of the second device row (ppm correction, direct
        # sampling), which every rebuild has to re-apply to the fresh flowgraph
        options = dict(frequency_correction_ppm=float(self.ppm_spin.value()),
                       direct_sampling=self._direct_mode() if device_cls.supports_direct_sampling else 0)
        if device_cls.supports_agc_mode:
            return dict(gain_mode=self.gain_mode_combo.currentData(),
                        manual_gain_db=float(self.gain_slider.value()), gain_values=None, **options)
        gain_values = {name: (widget.isChecked() if stage.kind == "bool" else float(widget.value()))
                       for name, (widget, _label, stage) in self._manual_gain_controls.items()}
        return dict(gain_mode=config.DEFAULT_GAIN_MODE, manual_gain_db=0.0, gain_values=gain_values, **options)

    def _digimode_kwargs(self, active_digimode):
        """PSK31/RTTY callback+setting kwargs shared by every
        AdvancedRxFlowgraph(...) construction site (fresh connect,
        bandwidth/audio-device rebuild, digimode-switch rebuild) --
        pulled out once here since this session added a second digimode
        (RTTY) alongside PSK31 with several more settings of its own
        (mark/shift/baud/reverse), and every construction site already
        needs to carry PSK31's own tone setting over too."""
        return dict(
            active_digimode=active_digimode,
            on_psk31_char=self._on_psk31_char, psk31_tone_hz=float(self.psk31_tone_slider.value()),
            on_rtty_char=self._on_rtty_char,
            rtty_mark_hz=float(self.rtty_mark_slider.value()),
            rtty_shift_hz=float(self.rtty_shift_combo.currentData()),
            rtty_baud_rate=float(self.rtty_baud_combo.currentData()),
            rtty_reverse=self.rtty_reverse_checkbox.isChecked(),
            on_meshtastic_frame=self._meshtastic_state.on_frame,
            meshtastic_preset_index=self._meshtastic_preset_index(),
            on_pocsag_message=self._pocsag_state.on_message,
        )

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
        # fft_probe taps pluto_source BEFORE the audio_rotator (see
        # flowgraph.py), so its data is always centered on real DC = 0 Hz
        # for Audio Input, regardless of tuning -- unlike RF, where a real
        # LO retune actually moves the hardware's own capture window, so
        # the displayed axis correctly follows `freq` there. Audio Input's
        # axis must stay pinned to what the data actually contains; only
        # the tuned-frequency marker/demod-band shading below (still
        # `freq`) show where the rotator now points against that fixed axis.
        zoomed_span = self.tb.sample_rate / self.tb.fft_probe.zoom
        if self.tb.device.is_audio_only():
            # _poll_fft() crops each row to its upper (DC-to-Nyquist) half
            # for this backend -- the real-to-complex conversion
            # (AudioDevice's _AudioToComplexSource) produces a spectrum
            # mirror-symmetric around 0 Hz, and the negative half is
            # redundant/not meaningful to show. The double-sided
            # zoomed_span always spans [-zoomed_span/2, +zoomed_span/2]
            # around real DC; keeping only the upper half of the bins gives
            # exactly [0, +zoomed_span/2] -- span=zoomed_span/2 centered at
            # zoomed_span/4.
            positive_span = zoomed_span / 2
            self.waterfall.set_frequency_range(positive_span / 2, positive_span)
        else:
            self.waterfall.set_frequency_range(freq, zoomed_span)
        self.waterfall.set_tuned_frequency(freq)
        # Exactly one digimode (or none) is ever active at a time (see
        # AdvancedRxFlowgraph.active_digimode) -- show that one's
        # marker/band and hide the other, rather than the old always-both
        # PSK31-only behavior. *_center_hz reflects the LIVE AFC-tracked
        # position, not just the static nominal, so this shows where the
        # decoder is actually locked, not just where it started.
        self.waterfall.set_psk31_visible(self.tb.active_digimode == "psk31")
        self.waterfall.set_rtty_visible(self.tb.active_digimode == "rtty")
        if self.tb.active_digimode == "psk31":
            psk31_freq = freq + self.tb.psk31_tone_center_hz
            psk31_half_bw = config.PSK31_DISPLAY_BANDWIDTH_HZ / 2
            self.waterfall.set_psk31_marker(psk31_freq, psk31_freq - psk31_half_bw, psk31_freq + psk31_half_bw)
        elif self.tb.active_digimode == "rtty":
            mark_freq = freq + self.tb.rtty_mark_center_hz
            space_freq = mark_freq + self.tb.rtty_shift_center_hz
            margin = config.RTTY_FILTER_GUARD_HZ
            self.waterfall.set_rtty_marker(mark_freq, space_freq, mark_freq - margin, space_freq + margin)
        mode = self.demod_combo.currentData()
        if mode == AdvancedRxFlowgraph.MODE_FM:
            half_bw = self.tb.fm_demod_width_hz / 2
            self.waterfall.set_demod_band(freq - half_bw, freq + half_bw)
        elif mode == AdvancedRxFlowgraph.MODE_SSB:
            f_lo = config.SSB_AUDIO_BAND_HZ[0]
            self.waterfall.set_demod_band(freq + f_lo, freq + f_lo + self.tb.ssb_demod_width_hz)
        elif mode == AdvancedRxFlowgraph.MODE_LSB:
            f_lo = config.SSB_AUDIO_BAND_HZ[0]
            self.waterfall.set_demod_band(freq - f_lo - self.tb.ssb_demod_width_hz, freq - f_lo)
        elif mode == AdvancedRxFlowgraph.MODE_RADE:
            # Not operator-adjustable (RADE's OFDM occupied bandwidth is a
            # fixed protocol constant, unlike FM/SSB's width sliders) -- but
            # a real bug before this fix: this branch didn't exist at all,
            # so the overlay just kept showing whatever FM/SSB band was last
            # drawn, making visual tuning against it meaningless. See
            # config.RADE_OFDM_LOW_HZ/HIGH_HZ's docstring for the derivation.
            self.waterfall.set_demod_band(freq + config.RADE_OFDM_LOW_HZ, freq + config.RADE_OFDM_HIGH_HZ)
        elif mode == AdvancedRxFlowgraph.MODE_M17:
            # Not operator-adjustable, same reasoning as RADE above --
            # M17's 4-level-FSK occupied bandwidth is a fixed protocol
            # constant derived from its deviation + symbol rate (Carson's
            # rule: BW ~= 2*(deviation + symbol_rate)), not a real signal-
            # measurement estimate.
            half_bw = config.M17_DEVIATION_HZ + config.M17_SYMBOL_RATE
            self.waterfall.set_demod_band(freq - half_bw, freq + half_bw)
        elif mode == AdvancedRxFlowgraph.MODE_BASEBAND:
            half_bw = self.tb.baseband_width_hz / 2
            self.waterfall.set_demod_band(freq - half_bw, freq + half_bw)

    def _update_link_status(self):
        """Surface a dropping/returning network link (rtl_tcp) in the status line --
        only on a state CHANGE, so it never overwrites other status messages."""
        status = self.tb.device.connection_status()
        state = (id(self.tb), status)
        if status is None or state == self._link_state:
            self._link_state = state
            return
        previous = self._link_state[1] if self._link_state and self._link_state[0] == id(self.tb) else None
        self._link_state = state
        label = self.tb.device.connection or "auto-detect"
        if status == "lost":
            self.status_label.setText(f"Connection to {label} lost -- reconnecting...")
        elif status == "connecting":
            self.status_label.setText(f"Waiting for the rtl_tcp server at {label}...")
        elif status == "connected":
            self.status_label.setText(
                f"Connection to {label} restored." if previous == "lost" else f"Connected to RTL-SDR ({label}).")

    def _poll_fft(self):
        if self.tb is None:
            return
        self._update_link_status()
        row, self._fft_gen = self.tb.fft_probe.get_latest_row(self._fft_gen)
        if row is not None:
            if self.tb.device.is_audio_only():
                # fftshift'd -> DC sits at len//2; this slice is [0, +Nyquist),
                # matching _sync_waterfall()'s positive-only span for this
                # backend -- see its comment for the full reasoning.
                row = row[len(row) // 2:]
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
        if self.demod_combo.currentData() == AdvancedRxFlowgraph.MODE_M17 and M17_AVAILABLE:
            with self._m17_lock:
                fields = self._m17_last_fields
            if fields is not None:
                src = fields.get("src", "?")
                dst = fields.get("dst", "?")
                self.m17_status_label.setText(f"SRC: {src}   DST: {dst}")
            else:
                self.m17_status_label.setText("No signal")

    def _on_filebroadcast_receive_toggled(self, checked):
        self._filebroadcast_receiving = checked
        self._style_filebroadcast_receive_button(receiving=checked)

    def _update_filebroadcast_signal_label(self):
        """Independent of _filebroadcast_receiving (Start/Stop) -- reads
        FileBroadcastDeframer's own counters directly off the flowgraph,
        which keeps running/counting regardless of the Start/Stop button,
        so the operator can see whether they're even correctly tuned
        BEFORE pressing Start. See the button's own construction comment
        for why either counter moving is a valid proxy for "a real signal
        is being decoded here", not just successfully-parsed frames."""
        if self.tb is None:
            self.filebroadcast_signal_label.setText("Not connected.")
            return
        if self.tb.filebroadcast_deframer is None:
            # Audio-only device (Soundcard) -- the branch isn't built at
            # all for these, see AdvancedRxFlowgraph's own comment (File
            # Broadcast needs real RF bandwidth, a sound card has none).
            self.filebroadcast_signal_label.setText("Not available for this device (no RF).")
            return
        frame_count = self.tb.filebroadcast_deframer.frame_count
        crc_fail_count = self.tb.filebroadcast_deframer.crc_fail_count
        total = frame_count + crc_fail_count
        if total > self._filebroadcast_last_attempt_total:
            self._filebroadcast_last_attempt_total = total
            self._filebroadcast_last_activity_time = time.time()
        recently_active = (
            self._filebroadcast_last_activity_time > 0
            and time.time() - self._filebroadcast_last_activity_time < 2.0
        )
        if recently_active:
            self.filebroadcast_signal_label.setText(
                f"Signal detected -- {frame_count} valid frame(s), {crc_fail_count} CRC failure(s) since connecting."
            )
        else:
            self.filebroadcast_signal_label.setText(
                "No signal detected -- check frequency/tuning (no File Broadcast sync word seen recently)."
            )

    def _on_filebroadcast_frame(self, frame):
        """Passed to AdvancedRxFlowgraph as on_filebroadcast_frame -- called
        from FileBroadcastDeframer.work() on the GNU Radio SCHEDULER thread,
        not the Qt thread. FileBroadcastState's own methods are lock-
        guarded (safe to call directly from here); nothing here touches any
        Qt widget -- _poll_filebroadcast() (Qt-thread, timer-driven) is what
        actually updates the list, via get_snapshot().

        Gated on _filebroadcast_receiving (Start/Stop button): while
        stopped, frames are simply dropped here rather than applied to
        FileBroadcastState -- the GNU Radio GFSK branch itself keeps
        running regardless (see the Start/Stop button's construction
        comment), so the signal-status label stays accurate even while
        stopped."""
        if not self._filebroadcast_receiving:
            return
        if frame["type"] == "directory":
            self._filebroadcast_state.on_directory_frame(
                frame["file_id"], frame["filename"], frame["total_size"], frame["checksum"],
            )
        elif frame["type"] == "data":
            self._filebroadcast_state.on_data_frame(frame["file_id"], frame["offset"], frame["payload"])

    def _on_psk31_tone_changed(self, value):
        self.psk31_tone_label.setText(f"{value} Hz")
        if self.tb is not None:
            self.tb.set_psk31_tone_hz(float(value))

    def _on_psk31_clear_clicked(self):
        self._psk31_state.clear()
        self.psk31_transcript.clear()

    def _update_psk31_signal_label(self):
        """Reads PSK31VaricodeDeframer's own chars_decoded counter
        directly off the flowgraph. Mirrors
        _update_filebroadcast_signal_label()'s own reasoning; only
        meaningful while PSK31 is the active digimode (self.tb.
        psk31_deframer never receives any real bits otherwise, so this
        just reports "no signal" forever, which is correct)."""
        if self.tb is None:
            self.psk31_signal_label.setText("Not connected.")
            return
        chars_decoded = self.tb.psk31_deframer.chars_decoded
        if chars_decoded > self._psk31_last_chars_decoded:
            self._psk31_last_chars_decoded = chars_decoded
            self._psk31_last_activity_time = time.time()
        recently_active = (
            self._psk31_last_activity_time > 0
            and time.time() - self._psk31_last_activity_time < 5.0
        )
        afc_note = f" -- AFC tracking near {self.tb.psk31_tone_center_hz:.0f} Hz"
        if recently_active:
            self.psk31_signal_label.setText(
                f"Signal detected -- {chars_decoded} character(s) decoded since connecting.{afc_note}"
            )
        else:
            self.psk31_signal_label.setText(
                f"No signal detected -- check tone/tuning (no valid Varicode character seen recently).{afc_note}"
            )

    def _on_psk31_char(self, char):
        """Passed to AdvancedRxFlowgraph as on_psk31_char -- called from
        PSK31VaricodeDeframer.work() on the GNU Radio SCHEDULER thread, not
        the Qt thread. Psk31ChatState.on_char() is lock-guarded (safe to
        call directly from here); nothing here touches any Qt widget --
        _poll_digimode() (Qt-thread, timer-driven) is what actually
        updates the transcript, via get_snapshot(). Unconditional now
        (unlike the old Start/Stop-gated version): this callback only
        ever fires at all while PSK31 is the active digimode (see
        AdvancedRxFlowgraph.active_digimode), so there's no separate gate
        needed anymore."""
        self._psk31_state.on_char(char)

    def _on_rtty_mark_changed(self, value):
        self.rtty_mark_label.setText(f"{value} Hz")
        if self.tb is not None:
            self.tb.set_rtty_mark_hz(float(value))

    def _on_rtty_shift_changed(self, idx):
        if self.tb is not None:
            self.tb.set_rtty_shift_hz(float(self.rtty_shift_combo.currentData()))

    def _on_rtty_baud_changed(self, idx):
        if self.tb is not None:
            self.tb.set_rtty_baud_rate(float(self.rtty_baud_combo.currentData()))

    def _on_rtty_reverse_changed(self, checked):
        if self.tb is not None:
            self.tb.set_rtty_reverse(checked)

    def _on_rtty_clear_clicked(self):
        self._rtty_state.clear()
        self.rtty_transcript.clear()

    def _update_rtty_signal_label(self):
        """Structural mirror of _update_psk31_signal_label()."""
        if self.tb is None:
            self.rtty_signal_label.setText("Not connected.")
            return
        chars_decoded = self.tb.rtty_deframer.chars_decoded
        if chars_decoded > self._rtty_last_chars_decoded:
            self._rtty_last_chars_decoded = chars_decoded
            self._rtty_last_activity_time = time.time()
        recently_active = (
            self._rtty_last_activity_time > 0
            and time.time() - self._rtty_last_activity_time < 5.0
        )
        afc_note = (
            f" -- AFC tracking near mark={self.tb.rtty_mark_center_hz:.0f}Hz/"
            f"shift={self.tb.rtty_shift_center_hz:.0f}Hz"
        )
        if recently_active:
            self.rtty_signal_label.setText(
                f"Signal detected -- {chars_decoded} character(s) decoded since connecting.{afc_note}"
            )
        else:
            self.rtty_signal_label.setText(
                f"No signal detected -- check mark/shift/baud (no valid character seen recently).{afc_note}"
            )

    def _on_rtty_char(self, char):
        """Structural mirror of _on_psk31_char() -- see its own docstring."""
        self._rtty_state.on_char(char)

    # --- Meshtastic -------------------------------------------------------

    def _meshtastic_preset_index(self):
        idx = self.meshtastic_preset_combo.currentData()
        return idx if idx is not None and idx >= 0 else 0

    def _meshtastic_preset(self):
        return config.MESHTASTIC_PRESETS[self._meshtastic_preset_index()]

    def _update_meshtastic_regulatory_label(self):
        # Permanent while the mode is showing, not a one-time notice -- the 868 MHz
        # certification risk stays visible in actual use (see the LoRa mesh plan).
        preset = self._meshtastic_preset()
        colour = "#1f6fb2" if preset.ham_mode_required else "#b9770e"
        self.meshtastic_regulatory_label.setText(
            f"<b>{preset.frequency_hz / 1e6:.3f} MHz, SF{preset.spreading_factor}, "
            f"{preset.bandwidth_hz / 1e3:g} kHz, CR {preset.coding_rate}</b> -- {preset.regulatory_label}"
            + ("" if preset.phy_verified else
               "<br><i>PHY parameters from the firmware table, not yet verified against a real node.</i>"))
        self.meshtastic_regulatory_label.setStyleSheet(f"color: {colour};")

    def _apply_meshtastic_channels(self):
        """Push the channel name/PSK fields into the traffic state. An invalid
        PSK keeps the previous key and is flagged in the signal label."""
        if not LORA_AVAILABLE:
            return
        name = self.meshtastic_channel_edit.text().strip() or self._meshtastic_preset().default_channel_name
        try:
            psk = meshtastic_codec.parse_psk(self.meshtastic_psk_edit.text())
        except ValueError as e:
            self._meshtastic_psk_error = str(e)
            return
        self._meshtastic_psk_error = None
        channels = [(name, psk)]
        if self._meshtastic_preset().ham_mode_required and psk:
            channels.insert(0, (name, b""))  # amateur band: unencrypted first
        self._meshtastic_state.set_channels(channels)

    def _on_meshtastic_channel_changed(self, _text=None):
        self._apply_meshtastic_channels()
        self._update_meshtastic_signal_label()

    def _on_meshtastic_preset_changed(self, idx):
        # the channel name follows the preset (display name = default primary channel
        # name) unless the operator typed a custom one
        preset = self._meshtastic_preset()
        if self.meshtastic_channel_edit.text() in ("", self._meshtastic_default_channel):
            self.meshtastic_channel_edit.setText(preset.default_channel_name)
        self._meshtastic_default_channel = preset.default_channel_name
        self._update_meshtastic_regulatory_label()
        self._apply_meshtastic_channels()
        if self.tb is not None and self.tb.active_digimode == "meshtastic":
            self._rebuild_for_digimode("meshtastic", force=True)  # decoder + carrier are per preset

    def _on_meshtastic_clear_clicked(self):
        self._meshtastic_state.clear()
        self._render_meshtastic_table()

    def _on_pocsag_clear_clicked(self):
        self._pocsag_state.clear()
        self._render_pocsag_table()

    def _update_pocsag_signal_label(self):
        if self.tb is None:
            self.pocsag_signal_label.setText("Not connected.")
            return
        if self.tb.active_digimode != "pocsag":
            self.pocsag_signal_label.setText("Not listening.")
            return
        deframers = self.tb.pocsag_deframers
        baud = max(deframers, key=lambda b: (deframers[b].codewords_ok, b))
        best = deframers[baud]
        batches = sum(d.batches for d in deframers.values())
        if batches > self._pocsag_last_batches:
            self._pocsag_last_activity_time = time.time()
        self._pocsag_last_batches = batches
        freq = (self.tb.nominal_freq_hz + self.tb.fine_offset_hz) / 1e6
        recent = self._pocsag_last_activity_time > 0 and time.time() - self._pocsag_last_activity_time < 5.0
        if recent:
            total = best.codewords_ok + best.codewords_bad
            calls, damaged, hidden = self._pocsag_state.counts()
            extra = f", {calls - hidden} call(s) shown" + (f", {hidden} damaged hidden" if hidden else "")
            self.pocsag_signal_label.setText(
                f"POCSAG {baud} Bd on {freq:.4f} MHz -- {best.batches} batches, {best.codewords_ok}/{total} codewords OK{extra}"
                + ("" if calls else " (most batches carry no call: idle keep-alive)"))
        else:
            self.pocsag_signal_label.setText(
                f"No POCSAG signal on {freq:.4f} MHz right now (paging channels are used in bursts; tune exactly to the channel "
                f"centre, e.g. 466.075 / 465.970 / 466.230). "
                f"Calls so far: {self._pocsag_state.counts()[0]}.")

    def _render_pocsag_table(self):
        version, rows = self._pocsag_state.get_snapshot()
        self._pocsag_rendered_version = version
        table = self.pocsag_table
        scrollbar = table.verticalScrollBar()
        at_bottom = scrollbar.value() >= scrollbar.maximum() - 4
        table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            errors = f"{row['corrected']} fixed" + (f", {row['uncorrectable']} lost" if row["uncorrectable"] else "")
            cells = [row["time"], str(row["baud"]), str(row["ric"]), str(row["function"]), row["text"], errors]
            for c, text in enumerate(cells):
                table.setItem(r, c, QtWidgets.QTableWidgetItem(text))
        table.resizeColumnsToContents()
        if at_bottom:
            table.scrollToBottom()

    def _update_meshtastic_signal_label(self):
        if self._meshtastic_psk_error:
            self.meshtastic_signal_label.setText(
                f"Invalid PSK ({self._meshtastic_psk_error}) -- still using the previous key.")
            self.meshtastic_signal_label.setStyleSheet("color: #c0392b;")
            return
        self.meshtastic_signal_label.setStyleSheet("")
        if self.tb is None:
            self.meshtastic_signal_label.setText("Not connected.")
            return
        if self.tb.active_digimode != "meshtastic":
            self.meshtastic_signal_label.setText("Not listening.")
            return
        frames = self.tb.meshtastic_deframer.frames_decoded
        if frames > self._meshtastic_last_frames:
            self._meshtastic_last_frames = frames
            self._meshtastic_last_activity_time = time.time()
        freq = (self.tb.nominal_freq_hz + self.tb.fine_offset_hz) / 1e6
        recent = self._meshtastic_last_activity_time > 0 and time.time() - self._meshtastic_last_activity_time < 10.0
        state = "Frame received" if recent else "Listening"
        self.meshtastic_signal_label.setText(f"{state} on {freq:.3f} MHz -- {frames} frame(s) decoded since connecting.")

    def _render_meshtastic_table(self):
        version, rows = self._meshtastic_state.get_snapshot()
        self._meshtastic_rendered_version = version
        table = self.meshtastic_table
        scrollbar = table.verticalScrollBar()
        at_bottom = scrollbar.value() >= scrollbar.maximum() - 4
        table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            stamp = time.strftime("%H:%M:%S", time.localtime(row["time"]))
            if "from" in row:
                to = "broadcast" if row["to"] == meshtastic_codec.BROADCAST_ADDR else meshtastic_codec.node_name(row["to"])
                cells = [stamp, meshtastic_codec.node_name(row["from"]), to, row["kind"],
                         f"{row['hop_limit']}/{row['hop_start']}", row["text"]]
            else:
                cells = [stamp, "", "", row["kind"], "", f"{row['raw_len']} B"]
            for c, text in enumerate(cells):
                table.setItem(r, c, QtWidgets.QTableWidgetItem(text))
        table.resizeColumnsToContents()
        if at_bottom:
            table.scrollToBottom()

    def _on_digimode_changed(self, idx):
        """digimode_combo's own change handler -- only meaningfully fires
        while the Digimodes tab is already active (the combo is hidden/
        irrelevant otherwise); triggers the same rebuild
        _on_mode_tab_changed() uses to enter this tab, just with the
        newly-selected digimode instead."""
        self._update_digimode_controls_enabled()
        if self.mode_tab_widget.currentIndex() == self._digimodes_tab_index:
            self._rebuild_for_digimode(self.digimode_combo.currentData())

    def _on_mode_tab_changed(self, index):
        """Entering the Digimodes tab activates digimode_combo's current
        selection; leaving it deactivates back to None -- see
        _rebuild_for_digimode()'s own docstring for why this is a full
        flowgraph rebuild, not a cheap runtime toggle, and why the
        resulting brief audio interruption on every tab visit is an
        accepted, explicit trade-off (per user request) for guaranteeing
        NOTHING decodes while this tab isn't even visible."""
        if index == self._digimodes_tab_index:
            self._rebuild_for_digimode(self.digimode_combo.currentData())
        else:
            self._rebuild_for_digimode(None)

    def _on_m17_fields(self, fields):
        """Passed to AdvancedRxFlowgraph as on_m17_fields -- called from
        M17FieldsDeframer's message handler on the GNU Radio message-passing
        thread, not the Qt thread. Same cross-thread pattern as
        _on_psk31_char()/_on_filebroadcast_frame(): store under a lock here,
        read back (snapshot) from _poll_fft() on the Qt thread. Unlike
        PSK31's accumulating transcript, M17 only needs the MOST RECENT
        Link Setup Frame's fields for the status row -- no dedicated state
        class needed for just that."""
        with self._m17_lock:
            self._m17_last_fields = fields

    def _poll_digimode(self):
        """Structural merge of the old _poll_psk31() (only PSK31 existed
        before) -- now updates whichever digimode's transcript is showing
        and, only for that ONE (self.tb.active_digimode), runs its own
        AFC step. The inactive digimode's transcript still refreshes (its
        state persists across the rebuild that deactivated it -- see
        __init__'s own comment), it just never gets NEW characters."""
        self._update_psk31_signal_label()
        self._update_rtty_signal_label()
        self._update_meshtastic_signal_label()
        self._update_pocsag_signal_label()
        if self._meshtastic_state.version != self._meshtastic_rendered_version:
            self._render_meshtastic_table()
        if self._pocsag_state.version != self._pocsag_rendered_version:
            self._render_pocsag_table()
        # Independent of self.tb's connection state (unlike the AFC step
        # below) -- each transcript lives on MainWindow and should keep
        # showing whatever was already received even across a rebuild,
        # same reasoning as _poll_filebroadcast().
        psk31_snapshot = self._psk31_state.get_snapshot()
        if psk31_snapshot != self.psk31_transcript.toPlainText():
            scrollbar = self.psk31_transcript.verticalScrollBar()
            at_bottom = scrollbar.value() >= scrollbar.maximum() - 4
            self.psk31_transcript.setPlainText(psk31_snapshot)
            if at_bottom:
                scrollbar.setValue(scrollbar.maximum())
        rtty_snapshot = self._rtty_state.get_snapshot()
        if rtty_snapshot != self.rtty_transcript.toPlainText():
            scrollbar = self.rtty_transcript.verticalScrollBar()
            at_bottom = scrollbar.value() >= scrollbar.maximum() - 4
            self.rtty_transcript.setPlainText(rtty_snapshot)
            if at_bottom:
                scrollbar.setValue(scrollbar.maximum())
        if self.tb is None:
            return
        now = time.time()
        # AFC step, throttled to each mode's own *_AFC_POLL_INTERVAL_S --
        # see AdvancedRxFlowgraph.psk31_afc_step()/rtty_afc_step()'s own
        # docstrings for why this doesn't need to run on every ~33ms
        # waterfall-poll tick.
        if self.tb.active_digimode == "psk31":
            if now - self._psk31_last_afc_poll_time >= config.PSK31_AFC_POLL_INTERVAL_S:
                self._psk31_last_afc_poll_time = now
                self.tb.psk31_afc_step()
            # Refresh just the PSK31 marker/band (not the full
            # _sync_waterfall(), which also recomputes the axis/FM-SSB-RADE
            # band every call) so it visibly tracks psk31_afc_step()'s live
            # corrections to psk31_tone_center_hz, at this same cadence.
            freq = self.tb.nominal_freq_hz + self.tb.fine_offset_hz
            psk31_freq = freq + self.tb.psk31_tone_center_hz
            psk31_half_bw = config.PSK31_DISPLAY_BANDWIDTH_HZ / 2
            self.waterfall.set_psk31_marker(psk31_freq, psk31_freq - psk31_half_bw, psk31_freq + psk31_half_bw)
        elif self.tb.active_digimode == "rtty":
            if now - self._rtty_last_afc_poll_time >= config.RTTY_AFC_POLL_INTERVAL_S:
                self._rtty_last_afc_poll_time = now
                self.tb.rtty_afc_step()
            freq = self.tb.nominal_freq_hz + self.tb.fine_offset_hz
            mark_freq = freq + self.tb.rtty_mark_center_hz
            space_freq = mark_freq + self.tb.rtty_shift_center_hz
            margin = config.RTTY_FILTER_GUARD_HZ
            self.waterfall.set_rtty_marker(mark_freq, space_freq, mark_freq - margin, space_freq + margin)

    _FB_COL_FILENAME, _FB_COL_SIZE, _FB_COL_RECEIVED, _FB_COL_RECORD = range(4)

    def _poll_filebroadcast(self):
        self._update_filebroadcast_signal_label()
        # Independent of self.tb's connection state (unlike _poll_fft) --
        # the state itself lives on MainWindow and should keep showing
        # whatever was already received even across a brief reconnect.
        snapshot = self._filebroadcast_state.get_snapshot()
        self._filebroadcast_snapshot = snapshot
        table = self.filebroadcast_table
        selected_file_id = self._filebroadcast_selected_file_id()
        # blockSignals covers both currentCellChanged (row count/selection
        # churn while rebuilding) and itemChanged (setCheckState() below
        # would otherwise fire _on_filebroadcast_item_changed() on every
        # single poll tick, not just real operator clicks).
        table.blockSignals(True)
        table.setRowCount(len(snapshot))
        restore_row = -1
        for row, entry in enumerate(snapshot):
            if entry["file_id"] == selected_file_id:
                restore_row = row
            name_item = QtWidgets.QTableWidgetItem(entry["filename"])
            name_item.setData(QtCore.Qt.UserRole, entry["file_id"])
            name_item.setFlags(name_item.flags() & ~QtCore.Qt.ItemIsEditable)
            table.setItem(row, self._FB_COL_FILENAME, name_item)

            size_item = QtWidgets.QTableWidgetItem(f"{entry['total_size']} B")
            size_item.setFlags(size_item.flags() & ~QtCore.Qt.ItemIsEditable)
            table.setItem(row, self._FB_COL_SIZE, size_item)

            pct = 100.0 * entry["bytes_received"] / entry["total_size"] if entry["total_size"] else 0.0
            status = "COMPLETE" if entry["is_complete"] else f"{entry['bytes_received']}/{entry['total_size']} ({pct:.0f}%)"
            recv_item = QtWidgets.QTableWidgetItem(status)
            recv_item.setFlags(recv_item.flags() & ~QtCore.Qt.ItemIsEditable)
            table.setItem(row, self._FB_COL_RECEIVED, recv_item)

            record_item = QtWidgets.QTableWidgetItem()
            record_item.setFlags(
                (record_item.flags() | QtCore.Qt.ItemIsUserCheckable) & ~QtCore.Qt.ItemIsEditable
            )
            record_item.setCheckState(QtCore.Qt.Checked if entry["record_flag"] else QtCore.Qt.Unchecked)
            record_item.setData(QtCore.Qt.UserRole, entry["file_id"])
            table.setItem(row, self._FB_COL_RECORD, record_item)
        if restore_row >= 0:
            table.setCurrentCell(restore_row, 0)
        table.blockSignals(False)
        self._on_filebroadcast_selection_changed()

    def _filebroadcast_selected_file_id(self):
        row = self.filebroadcast_table.currentRow()
        if not (0 <= row < len(self._filebroadcast_snapshot)):
            return None
        return self._filebroadcast_snapshot[row]["file_id"]

    def _on_filebroadcast_selection_changed(self, *_args):
        # *_args swallows currentCellChanged's (row, col, prevRow, prevCol)
        # signature -- the actual selection is re-read via currentRow()
        # either way (also called directly from _poll_filebroadcast() after
        # a rebuild, with no signal args at all).
        file_id = self._filebroadcast_selected_file_id()
        entry = next((e for e in self._filebroadcast_snapshot if e["file_id"] == file_id), None)
        self.filebroadcast_save_button.setEnabled(entry is not None and entry["is_complete"])

    def _on_filebroadcast_item_changed(self, item):
        if item.column() != self._FB_COL_RECORD:
            return
        file_id = item.data(QtCore.Qt.UserRole)
        self._filebroadcast_state.set_record(file_id, item.checkState() == QtCore.Qt.Checked)

    def _on_filebroadcast_save_clicked(self):
        file_id = self._filebroadcast_selected_file_id()
        entry = next((e for e in self._filebroadcast_snapshot if e["file_id"] == file_id), None)
        if entry is None:
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save Received File", entry["filename"])
        if not path:
            return
        self._filebroadcast_state.save_to_disk(entry["file_id"], path)

    # --- slots ------------------------------------------------------
    def _on_freq_changed(self, mhz):
        if self.tb is None:
            return
        self.tb.set_frequency(mhz * 1e6)
        self._sync_waterfall()

    def _on_audio_tune_changed(self, hz):
        """audio_tune_spin's counterpart to _on_freq_changed() above --
        already native Hz, no unit conversion needed."""
        if self.tb is None:
            return
        self.tb.set_frequency(hz)
        self._sync_waterfall()

    def _on_fine_changed(self, value):
        if self.tb is not None:
            self.tb.set_fine_offset(float(value))
            self._sync_waterfall()
        self.fine_label.setText(f"{value} Hz")

    def _on_demod_changed(self, idx):
        mode = self.demod_combo.currentData()
        is_rade_mode = mode == AdvancedRxFlowgraph.MODE_RADE
        is_m17_mode = mode == AdvancedRxFlowgraph.MODE_M17
        self._autotune_cancel()  # leaving/re-entering RADE mode invalidates any in-flight run
        # No operator-adjustable demod width for RADE or M17 -- hide the
        # whole width control rather than leave it interactive-but-
        # meaningless, same "hide, don't just grey out" reasoning as the
        # RADE/M17 status rows.
        self.width_slider.setVisible(not is_rade_mode and not is_m17_mode)
        self.width_label.setVisible(not is_rade_mode and not is_m17_mode)
        self.rade_row_widget.setVisible(is_rade_mode)
        if not is_rade_mode:
            self.rade_status_label.setText("Not synced")
        self.m17_row_widget.setVisible(is_m17_mode)
        if not is_m17_mode:
            self.m17_status_label.setText("No signal")

        if not is_rade_mode and not is_m17_mode:
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
            elif mode == AdvancedRxFlowgraph.MODE_BASEBAND:
                w_lo, w_hi = config.BASEBAND_WIDTH_RANGE_HZ
                width = self.tb.baseband_width_hz if self.tb is not None else config.BASEBAND_WIDTH_DEFAULT_HZ
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
        elif self.demod_combo.currentData() in (AdvancedRxFlowgraph.MODE_SSB, AdvancedRxFlowgraph.MODE_LSB):
            self.tb.set_ssb_demod_width(float(value))
        elif self.demod_combo.currentData() == AdvancedRxFlowgraph.MODE_BASEBAND:
            self.tb.set_baseband_width(float(value))
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
        if self.tb.device.is_audio_only():
            self.audio_tune_spin.setValue(freq_hz)
        else:
            self.freq_spin.setValue(freq_hz / 1e6)

    def _on_bandwidth_changed(self, idx):
        """RX bandwidth ("zoom") change: GNU Radio's FIR/resampler blocks
        can't change their decimation ratio at runtime, so this rebuilds the
        whole flowgraph from scratch, carrying over every other current
        setting. The waterfall widget itself is NOT swapped -- it persists,
        just gets a new frequency range (see this module's own docstring
        for why that's possible here)."""
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
        baseband_width = self.tb.baseband_width_hz

        try:
            new_tb = AdvancedRxFlowgraph(
                uri=self.tb.uri, frequency=freq, sample_rate=new_rate,
                demod_mode=demod_mode, nf_gain=nf_gain, fft_size=fft_size,
                fm_demod_width_hz=fm_width, ssb_demod_width_hz=ssb_width, baseband_width_hz=baseband_width,
                device_type=device_cls.device_type, on_filebroadcast_frame=self._on_filebroadcast_frame,
                audio_device=self._audio_device, on_m17_fields=self._on_m17_fields,
                buffer_size=self.buffer_size_combo.currentData(),
                **self._digimode_kwargs(self.tb.active_digimode),
                **self._current_gain_kwargs(device_cls),
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
        self._filebroadcast_last_attempt_total = 0  # new tb's deframer counters start at 0 too, see _update_filebroadcast_signal_label()
        self._filebroadcast_last_activity_time = 0.0
        self._psk31_last_chars_decoded = 0  # new tb's psk31_deframer.chars_decoded starts at 0 too
        self._psk31_last_activity_time = 0.0
        self._rtty_last_chars_decoded = 0
        self._rtty_last_activity_time = 0.0
        self.tb.shutdown()
        self.tb = new_tb
        self._sync_waterfall()
        self.tb.start()
        self.status_label.setText(f"Switched to {self._format_hz(new_rate)}.")

    def _on_buffer_size_changed(self, idx):
        """Pluto-only libiio buffer size change -- exact same "construction-
        time-only, not runtime-retunable" shape as _on_bandwidth_changed()
        above (the buffer size is fixed for the life of the
        fmcomms2_source_fc32 block, see PlutoDevice.build_source()), so this
        rebuilds the whole flowgraph from scratch too, carrying over every
        other current setting."""
        if self.tb is None:
            return
        self._autotune_cancel()
        device_cls = devices.DEVICE_REGISTRY[self.device_type_combo.currentData()]
        new_buffer_size = self.buffer_size_combo.currentData()
        freq = self.tb.nominal_freq_hz
        fine = self.tb.fine_offset_hz
        demod_mode = self.demod_combo.currentData()
        nf_gain = self.nf_gain_slider.value() / 100.0
        fft_size = self.fft_size_combo.currentData()
        fm_width = self.tb.fm_demod_width_hz
        ssb_width = self.tb.ssb_demod_width_hz
        baseband_width = self.tb.baseband_width_hz

        try:
            new_tb = AdvancedRxFlowgraph(
                uri=self.tb.uri, frequency=freq, sample_rate=self.tb.sample_rate,
                demod_mode=demod_mode, nf_gain=nf_gain, fft_size=fft_size,
                fm_demod_width_hz=fm_width, ssb_demod_width_hz=ssb_width, baseband_width_hz=baseband_width,
                device_type=device_cls.device_type, on_filebroadcast_frame=self._on_filebroadcast_frame,
                audio_device=self._audio_device, on_m17_fields=self._on_m17_fields,
                buffer_size=new_buffer_size,
                **self._digimode_kwargs(self.tb.active_digimode),
                **self._current_gain_kwargs(device_cls),
            )
        except Exception as e:
            self.status_label.setText(f"Could not switch buffer size: {e}")
            self.buffer_size_combo.blockSignals(True)
            self.buffer_size_combo.setCurrentIndex(
                config.PLUTO_RX_BUFFER_SIZE_PRESETS.index(int(self.tb.device.buffer_size
                                                                or config.PLUTO_RX_BUFFER_SIZE_DEFAULT)))
            self.buffer_size_combo.blockSignals(False)
            return

        new_tb.set_fine_offset(fine)
        new_tb.set_rx_muted(self._rx_muted)
        new_tb.set_fft_zoom(self.zoom_slider.value())
        new_tb.set_fft_avg_count(self.avg_slider.value())
        self._fft_gen = -1
        self._filebroadcast_last_attempt_total = 0
        self._filebroadcast_last_activity_time = 0.0
        self._psk31_last_chars_decoded = 0
        self._psk31_last_activity_time = 0.0
        self._rtty_last_chars_decoded = 0
        self._rtty_last_activity_time = 0.0
        self.tb.shutdown()
        self.tb = new_tb
        self._sync_waterfall()
        self.tb.start()
        self.status_label.setText(f"Switched RX buffer size to {new_buffer_size}.")

    def _rebuild_for_digimode(self, new_digimode, force=False):
        """Rebuilds self.tb with a different active_digimode -- see
        AdvancedRxFlowgraph's own Digimodes intro comment for why a live
        runtime toggle isn't possible here (a GNU Radio block with a
        required input can't be left disconnected while the flowgraph is
        running -- confirmed empirically while building this feature,
        unlike demod_combo's cheap set_demod_mode() switch). Same shape
        as _on_bandwidth_changed()/_on_audio_device_changed() -- build
        the new flowgraph FIRST, only shutdown/swap the old one on
        success. Called on every Digimodes-tab enter/leave AND every
        digimode_combo selection change while already on that tab -- per
        explicit user request, so NOTHING decodes while the tab isn't
        visible, at the accepted cost of a brief audio interruption on
        every such switch (confirmed acceptable: digimode switching is
        an occasional, deliberate operator action, not something done
        continuously)."""
        old_digimode = self._active_digimode
        self._active_digimode = new_digimode
        if self.tb is None or (new_digimode == self.tb.active_digimode and not force):
            return
        self._autotune_cancel()
        device_cls = devices.DEVICE_REGISTRY[self.device_type_combo.currentData()]
        freq = self.tb.nominal_freq_hz
        # Meshtastic presets retune the receiver to the preset's carrier; the
        # previous frequency comes back when leaving the mode again.
        if new_digimode == "meshtastic":
            if self.tb.active_digimode != "meshtastic":
                self._freq_before_lora = freq
            freq = self._meshtastic_preset().frequency_hz
        elif self.tb.active_digimode == "meshtastic" and self._freq_before_lora is not None:
            freq = self._freq_before_lora
            self._freq_before_lora = None
        fine = self.tb.fine_offset_hz
        demod_mode = self.demod_combo.currentData()
        nf_gain = self.nf_gain_slider.value() / 100.0
        fft_size = self.fft_size_combo.currentData()
        fm_width = self.tb.fm_demod_width_hz
        ssb_width = self.tb.ssb_demod_width_hz
        baseband_width = self.tb.baseband_width_hz

        try:
            new_tb = AdvancedRxFlowgraph(
                uri=self.tb.uri, frequency=freq, sample_rate=self.tb.sample_rate,
                demod_mode=demod_mode, nf_gain=nf_gain, fft_size=fft_size,
                fm_demod_width_hz=fm_width, ssb_demod_width_hz=ssb_width, baseband_width_hz=baseband_width,
                device_type=device_cls.device_type, on_filebroadcast_frame=self._on_filebroadcast_frame,
                audio_device=self._audio_device, on_m17_fields=self._on_m17_fields,
                buffer_size=self.buffer_size_combo.currentData(),
                **self._digimode_kwargs(new_digimode),
                **self._current_gain_kwargs(device_cls),
            )
        except Exception as e:
            self.status_label.setText(f"Could not switch digimode: {e}")
            self._active_digimode = old_digimode
            return

        new_tb.set_fine_offset(fine)
        new_tb.set_rx_muted(self._rx_muted)  # carry the CURRENT mute state over, same reasoning as the other rebuilds
        new_tb.set_fft_zoom(self.zoom_slider.value())
        new_tb.set_fft_avg_count(self.avg_slider.value())
        self._fft_gen = -1
        self._filebroadcast_last_attempt_total = 0
        self._filebroadcast_last_activity_time = 0.0
        self._psk31_last_chars_decoded = 0  # new tb's psk31_deframer.chars_decoded starts at 0 too
        self._psk31_last_activity_time = 0.0
        self._rtty_last_chars_decoded = 0
        self._rtty_last_activity_time = 0.0
        self._meshtastic_last_frames = 0
        self._meshtastic_last_activity_time = 0.0
        self.tb.shutdown()
        self.tb = new_tb
        if abs(self.freq_spin.value() - freq / 1e6) > 1e-9:
            self.freq_spin.blockSignals(True)  # the new flowgraph was already built at `freq`
            self.freq_spin.setValue(freq / 1e6)
            self.freq_spin.blockSignals(False)
        self._sync_waterfall()
        self.tb.start()

    def _on_audio_device_changed(self, idx):
        """Audio Output combo change: gnuradio's audio.sink() has no
        runtime device-swap API, so this rebuilds the whole flowgraph
        from scratch, carrying over every other current setting -- exact
        same shape as _on_bandwidth_changed() above (build the new
        flowgraph FIRST, only shutdown/swap the old one on success, so a
        bad/busy device never costs the existing, working RX connection)."""
        new_device = self.audio_device_combo.currentData()
        if new_device == self._audio_device:
            return
        if self.tb is None:
            self._audio_device = new_device
            return
        self._autotune_cancel()
        device_cls = devices.DEVICE_REGISTRY[self.device_type_combo.currentData()]
        freq = self.tb.nominal_freq_hz
        fine = self.tb.fine_offset_hz
        demod_mode = self.demod_combo.currentData()
        nf_gain = self.nf_gain_slider.value() / 100.0
        fft_size = self.fft_size_combo.currentData()
        fm_width = self.tb.fm_demod_width_hz
        ssb_width = self.tb.ssb_demod_width_hz
        baseband_width = self.tb.baseband_width_hz

        try:
            new_tb = AdvancedRxFlowgraph(
                uri=self.tb.uri, frequency=freq, sample_rate=self.tb.sample_rate,
                demod_mode=demod_mode, nf_gain=nf_gain, fft_size=fft_size,
                fm_demod_width_hz=fm_width, ssb_demod_width_hz=ssb_width, baseband_width_hz=baseband_width,
                device_type=device_cls.device_type, on_filebroadcast_frame=self._on_filebroadcast_frame,
                audio_device=new_device, on_m17_fields=self._on_m17_fields,
                buffer_size=self.buffer_size_combo.currentData(),
                **self._digimode_kwargs(self.tb.active_digimode),
                **self._current_gain_kwargs(device_cls),
            )
        except Exception as e:
            self.status_label.setText(f"Could not switch audio output: {e}")
            self.audio_device_combo.blockSignals(True)
            self.audio_device_combo.setCurrentIndex(self.audio_device_combo.findData(self._audio_device))
            self.audio_device_combo.blockSignals(False)
            return

        new_tb.set_fine_offset(fine)
        new_tb.set_rx_muted(self._rx_muted)  # carry the CURRENT mute state over, same reasoning
        # as _on_bandwidth_changed() -- this is an in-session rebuild, not a fresh connect.
        new_tb.set_fft_zoom(self.zoom_slider.value())
        new_tb.set_fft_avg_count(self.avg_slider.value())
        self._fft_gen = -1
        self._filebroadcast_last_attempt_total = 0
        self._filebroadcast_last_activity_time = 0.0
        self._psk31_last_chars_decoded = 0
        self._psk31_last_activity_time = 0.0
        self._rtty_last_chars_decoded = 0
        self._rtty_last_activity_time = 0.0
        self.tb.shutdown()
        self.tb = new_tb
        self._sync_waterfall()
        self.tb.start()
        self._audio_device = new_device
        self.status_label.setText("Audio output switched.")

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
        self._filebroadcast_last_attempt_total = 0
        self._filebroadcast_last_activity_time = 0.0
        self._psk31_last_chars_decoded = 0
        self._psk31_last_activity_time = 0.0
        self._rtty_last_chars_decoded = 0
        self._rtty_last_activity_time = 0.0
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
        # Same reset-only-on-disconnect reasoning as _rx_muted above --
        # a fresh connect (or reconnect via _on_bandwidth_changed, which
        # does NOT call _disconnect()) shouldn't silently resume dumping
        # frames into FileBroadcastState without the operator pressing
        # Start again.
        self._filebroadcast_receiving = False
        self.filebroadcast_receive_button.blockSignals(True)
        self.filebroadcast_receive_button.setChecked(False)
        self.filebroadcast_receive_button.blockSignals(False)
        self._style_filebroadcast_receive_button(receiving=False)
        # Digimode decoding itself is already gated by active_digimode/the
        # Digimodes tab (see _rebuild_for_digimode()) -- nothing extra to
        # reset here for PSK31/RTTY, unlike filebroadcast's separate
        # manual Start/Stop button above.

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
        current_mode = self.demod_combo.currentData()
        fm_width = float(self.width_slider.value()) if current_mode == AdvancedRxFlowgraph.MODE_FM \
            else config.FM_DEMOD_WIDTH_DEFAULT_HZ
        ssb_width = float(self.width_slider.value()) \
            if current_mode in (AdvancedRxFlowgraph.MODE_SSB, AdvancedRxFlowgraph.MODE_LSB) \
            else config.SSB_DEMOD_WIDTH_DEFAULT_HZ
        baseband_width = float(self.width_slider.value()) if current_mode == AdvancedRxFlowgraph.MODE_BASEBAND \
            else config.BASEBAND_WIDTH_DEFAULT_HZ
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
                buffer_size=self.buffer_size_combo.currentData(),
                demod_mode=self.demod_combo.currentData(),
                nf_gain=self.nf_gain_slider.value() / 100.0,
                fft_size=self.fft_size_combo.currentData(),
                fm_demod_width_hz=fm_width, ssb_demod_width_hz=ssb_width, baseband_width_hz=baseband_width,
                device_type=device_cls.device_type, on_filebroadcast_frame=self._on_filebroadcast_frame,
                on_m17_fields=self._on_m17_fields,
                audio_device=self._audio_device,
                **self._digimode_kwargs(self._active_digimode),
                **self._current_gain_kwargs(device_cls),
            )
        except Exception as e:
            self.status_label.setText(f"Could not connect to {device_cls.display_name}: {e}")
            return
        new_tb.set_fine_offset(float(self.fine_slider.value()))
        new_tb.set_rx_muted(self._rx_muted)  # True here (see _disconnect()/__init__) -- fresh connect starts muted
        new_tb.set_fft_zoom(self.zoom_slider.value())  # carry the operator's current zoom/averaging
        new_tb.set_fft_avg_count(self.avg_slider.value())  # preference over a fresh connect too
        self._fft_gen = -1
        self._filebroadcast_last_attempt_total = 0
        self._filebroadcast_last_activity_time = 0.0
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
