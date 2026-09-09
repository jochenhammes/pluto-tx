"""Pure-function text-to-audio encoder for the "Digitext" digimode: renders an
ASCII string to a pixel bitmap (Pillow), then encodes it via direct additive
sine-tone synthesis (one real, continuous tone per active pixel column, held
for the row's dwell time) so that ANY receiver's ordinary FFT/waterfall shows
the text drawn on it, right-side up and readable -- the same core idea as
"SonicPhoto" (a real existing "reverse spectrogram synth" tool, researched
this session) and the general technique amateur radio operators call
"spectrogram art".

Deliberately Qt/GNU-Radio-free -- same separation as fft_probe.py/
rade_autotune.py -- so it's testable with synthetic text alone, no hardware
or running flowgraph needed.

Core mapping (confirmed with the user): bitmap COLUMN -> frequency, bitmap
ROW -> time. A receiver's own FFT of our signal reconstructs the bitmap with
column->frequency (matching the FFT's own X axis) and row->time (matching
the waterfall's own time scroll) -- i.e. the image appears in its own
natural, upright, readable orientation. Real hardware finding this session:
rows must be sent BOTTOM-first, not top-first, to appear upright -- most
waterfalls (SDR++, GQRX, this app's own advanced-rx) scroll with the newest
data at the top, aging rows sliding down, so playing bitmap rows in their
natural top-to-bottom image order draws the image upside down on screen
(operator-confirmed on real hardware). See encode_bitmap_to_audio()'s own
comment for the actual flip.

Two layouts, both built from the exact same row->time/column->frequency
core (encode_bitmap_to_audio) -- they differ only in what one "bitmap" is:
- "horizontal": the WHOLE string rendered as one wide bitmap. Bandwidth
  (column count) grows with text length; duration (row count) stays fixed
  (one font-height's worth of rows).
- "vertical": each CHARACTER rendered as its own narrow bitmap, encoded
  separately, and the resulting audio segments concatenated in time.
  Bandwidth (one character's column count) stays constant regardless of
  text length; duration grows with the number of characters.

Real hardware finding (this session, after the first implementation using an
inverse-STFT/overlap-add approach): that approach synthesizes audio that
only reconstructs cleanly if a receiver's OWN analysis FFT size/hop happens
to match this module's internal frame_len/hop_len -- which no real,
independent receiver (SDR++, GQRX, an RTL-SDR waterfall) has any reason to
do. Verified offline: the same encoded signal looked reasonably legible when
re-analyzed with matching parameters, but degraded into unrecognizable noise
under an independent, mismatched analysis FFT size. Direct additive
synthesis (real, physically continuous tones, exactly like the proven
reference tools `spectrographic`/`spectrology` and genuine "waterfall art"
transmissions) has no such dependency: a real sinusoid at a given frequency,
present for a given duration, shows up correctly on ANY spectrum display
with reasonable resolution, by physical necessity -- there is nothing
receiver-specific to mismatch. A second real finding: many simultaneous
close-together tones (as a solid letter stroke needs) beat/interfere with
each other, which can make individual short observation windows look patchy
-- addressed by spacing columns further apart in frequency and holding each
row for longer, so a real, continuously-scrolling waterfall gets many
independent looks per row rather than depending on any single snapshot.
"""
import numpy as np
from PIL import Image, ImageDraw, ImageFont

LAYOUT_HORIZONTAL = "horizontal"
LAYOUT_VERTICAL = "vertical"

# Searched in order; DejaVu Sans Mono is what this session's own environment
# has (confirmed via `fc-list`) and is also what install.sh now installs
# explicitly (fonts-dejavu-mono) -- but this list is deliberately not just
# that one hardcoded path, since a bare path is fragile across distros (the
# exact lesson from this session's installer audit: don't assume a specific
# file is present just because it happened to be here). Falls back to
# Pillow's own built-in bitmap font (always available, no file needed) if
# none of these are found -- never a hard failure just because a font
# package is missing on some other system.
_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansMono-Regular.ttf",
)


def _load_font(font_size_px):
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, font_size_px)
        except OSError:
            continue
    return ImageFont.load_default()


def _char_bitmap(ch, font):
    """Render a single character to a tight (height, width) float32 array,
    values 0..1. Uses the font's own bounding box, not a fixed cell size --
    monospace fonts still vary in the exact ink extent per glyph (e.g. a
    space has zero ink), so this measures rather than assumes."""
    # A large scratch canvas, cropped to the actual measured bbox below --
    # simpler and more robust than trying to predict the exact bbox in
    # advance for an arbitrary/fallback font.
    scratch = Image.new("L", (1, 1), color=0)
    draw = ImageDraw.Draw(scratch)
    bbox = draw.textbbox((0, 0), ch, font=font)
    left, top, right, bottom = bbox
    width = max(1, right - left)
    height = max(1, bottom - top)
    img = Image.new("L", (width, height), color=0)
    draw = ImageDraw.Draw(img)
    draw.text((-left, -top), ch, font=font, fill=255)
    return np.asarray(img, dtype=np.float32) / 255.0


def _apply_zoom(bitmap, zoom):
    """Nearest-neighbor upscale by an integer factor, both axes equally --
    lets the operator send letters 2x/3x/... as large (in both bandwidth AND
    duration, keeping the glyph's proportions) instead of only this module's
    one fixed font size. Applied BEFORE column downsampling (currently
    always a no-op, see _downsample_columns' own docstring) so zoom always
    means "more logical columns/rows", not "more raw pixels that just get
    grouped straight back down again". zoom<=1 is a no-op."""
    if zoom <= 1:
        return bitmap
    return np.repeat(np.repeat(bitmap, zoom, axis=0), zoom, axis=1)


def _downsample_columns(bitmap, factor):
    """Groups every `factor` adjacent raw pixel columns into one logical
    column (max, not mean -- preserves thin strokes instead of fading them
    out). Real hardware finding this session: transmitting one tone per RAW
    pixel column packs many simultaneous close-together tones into a single
    row (a solid letter stroke is several adjacent columns wide), which beat
    against each other and look patchy on a real receiver. Fewer, more
    widely-spaced logical columns (see DIGITEXT_HZ_PER_COL) directly reduces
    that density. factor=1 is a no-op."""
    if factor <= 1:
        return bitmap
    height, width = bitmap.shape
    pad = (-width) % factor
    if pad:
        bitmap = np.concatenate([bitmap, np.zeros((height, pad), dtype=bitmap.dtype)], axis=1)
    grouped = bitmap.reshape(height, -1, factor)
    return grouped.max(axis=2)


def render_text_bitmap(text, font_size_px, col_downsample=1, zoom=1):
    """The whole string as ONE bitmap (used by LAYOUT_HORIZONTAL): all
    characters' individual glyph bitmaps (see _char_bitmap) placed
    side-by-side, top-aligned to a shared font-height row count so every
    character's column->frequency mapping lands in the same row range."""
    font = _load_font(font_size_px)
    chars = [_char_bitmap(ch, font) for ch in text] or [np.zeros((1, 1), dtype=np.float32)]
    height = max(c.shape[0] for c in chars)
    padded = []
    for c in chars:
        if c.shape[0] < height:
            pad = np.zeros((height - c.shape[0], c.shape[1]), dtype=np.float32)
            c = np.concatenate([c, pad], axis=0)
        padded.append(c)
    bitmap = np.concatenate(padded, axis=1)
    bitmap = _apply_zoom(bitmap, zoom)
    return _downsample_columns(bitmap, col_downsample)


def render_char_bitmaps(text, font_size_px, col_downsample=1, zoom=1):
    """Per-character bitmaps (used by LAYOUT_VERTICAL) -- NOT padded to a
    shared height/width the way render_text_bitmap() pads for concatenation
    along the column axis; each character keeps its own natural size since
    they're encoded and concatenated independently, one after another in
    TIME rather than merged into one wide image."""
    font = _load_font(font_size_px)
    return [_downsample_columns(_apply_zoom(_char_bitmap(ch, font), zoom), col_downsample) for ch in text]


def encode_bitmap_to_audio(bitmap, sample_rate, hz_per_col, row_dwell_s, min_freq_hz=300.0,
                            amplitude=0.7, ramp_s=0.006, rng=None):
    """Direct additive synthesis: each bitmap row is one block of
    row_dwell_s seconds, containing a sum of real, continuous sine tones --
    one per active column (bitmap value > 0.05), at
    min_freq_hz + column_index*hz_per_col, amplitude-weighted by that
    pixel's own value. A short raised-cosine ramp (ramp_s) at each row
    block's start/end avoids a click at row transitions; phase is fixed per
    column for the whole signal (not re-randomized per row) so a column that
    stays active across consecutive rows doesn't get an audible/visible
    phase jump at the boundary.

    No frame_len/hop_len/FFT-grid dependency at all -- unlike this module's
    first (inverse-STFT) implementation, there is nothing here a receiver's
    own analysis parameters need to match; a continuous tone at a given
    frequency for a given duration is unambiguous on any spectrum display.
    """
    if rng is None:
        rng = np.random.default_rng()
    # Flip rows top<->bottom: real hardware finding this session (see module
    # docstring) -- sending the bitmap's natural top-to-bottom row order
    # drew the text upside down on a real waterfall (newest-at-top scroll
    # convention). Sending the bottom row first and the top row last
    # compensates, so the image lands right-side up.
    bitmap = bitmap[::-1]
    height, width = bitmap.shape
    n_per_row = max(1, int(round(row_dwell_s * sample_rate)))
    t = np.arange(n_per_row, dtype=np.float64) / sample_rate
    ramp_n = min(n_per_row // 4, max(1, int(round(ramp_s * sample_rate))))
    envelope = np.ones(n_per_row, dtype=np.float32)
    if ramp_n > 1:
        ramp = np.linspace(0.0, 1.0, ramp_n, dtype=np.float32)
        envelope[:ramp_n] = ramp
        envelope[-ramp_n:] = ramp[::-1]

    freqs = min_freq_hz + np.arange(width) * hz_per_col
    phases = rng.uniform(0, 2 * np.pi, size=width)

    out = np.zeros(height * n_per_row, dtype=np.float32)
    for row in range(height):
        active = np.nonzero(bitmap[row] > 0.05)[0]
        if len(active) == 0:
            continue
        seg = np.zeros(n_per_row, dtype=np.float64)
        for c in active:
            seg += bitmap[row, c] * np.sin(2 * np.pi * freqs[c] * t + phases[c])
        out[row * n_per_row:(row + 1) * n_per_row] = (seg.astype(np.float32)) * envelope

    peak = np.max(np.abs(out))
    if peak > 0:
        out *= amplitude / peak
    return out


def encode_text(text, layout, font_size_px, sample_rate, hz_per_col, row_dwell_s,
                 min_freq_hz=300.0, tail_s=0.15, col_downsample=1, zoom=1):
    """Returns (audio: np.ndarray[float32], duration_s: float). See the
    module docstring for the horizontal/vertical distinction.

    zoom: integer upscale factor (see _apply_zoom) -- >1 sends each letter
    that many times larger in both bandwidth and duration, by explicit
    request (operators wanting a bigger, easier-to-read image accept the
    bandwidth/time cost themselves; the GUI's bandwidth warning was removed
    for the same reason -- estimate stays visible, nothing blocks sending).

    tail_s: true silence appended at the end (real zeros, not just "no more
    input") -- belt-and-suspenders alongside the GUI's own auto-unkey timer
    (a real "RF kept transmitting past the end" issue found on real
    hardware this session): guarantees the LAST thing the modulator/device
    ever sees for this transmission is genuine silence (SSB of true zero
    input is zero, unlike FM's "modulated silence is a full carrier" issue
    elsewhere in this app), regardless of exactly when key_ptt()'s
    fresh-per-transmission vector_source_c actually finishes relative to
    whatever un-keys the RF afterward."""
    if not text:
        text = " "
    if layout == LAYOUT_VERTICAL:
        segments = []
        for bmp in render_char_bitmaps(text, font_size_px, col_downsample, zoom):
            segments.append(encode_bitmap_to_audio(
                bmp, sample_rate, hz_per_col, row_dwell_s, min_freq_hz=min_freq_hz,
            ))
        audio = np.concatenate(segments) if segments else np.zeros(1, dtype=np.float32)
    else:
        bitmap = render_text_bitmap(text, font_size_px, col_downsample, zoom)
        audio = encode_bitmap_to_audio(
            bitmap, sample_rate, hz_per_col, row_dwell_s, min_freq_hz=min_freq_hz,
        )
    if tail_s > 0:
        audio = np.concatenate([audio, np.zeros(int(tail_s * sample_rate), dtype=np.float32)])
    return audio, len(audio) / sample_rate


def estimate_bandwidth_and_duration(text, layout, font_size_px, sample_rate, hz_per_col,
                                     row_dwell_s, tail_s=0.15, col_downsample=1, zoom=1):
    """Cheap preview estimate (character bounding-box metrics only, no full
    bitmap render/synthesis) for a live GUI readout while typing -- the
    real, more expensive encode_text() call happens lazily, only once
    actually needed (see flowgraph.py's _ensure_digitext_audio()). Returns
    (bandwidth_hz, duration_s).

    Row count (-> duration) is measured from the ACTUAL text's own
    characters, not a fixed proxy string -- render_text_bitmap()'s real row
    count is the max height over whichever characters are actually present
    (e.g. a string with no descenders like "DA2JH" renders shorter than one
    with "y"/"g" in it), so a fixed-string proxy would systematically
    mis-estimate depending on the message content.

    zoom: must match what encode_text() will actually be called with (see
    _apply_zoom) -- scales both the estimated bandwidth (more columns) and
    duration (more rows) by the same integer factor."""
    if not text:
        return 0.0, 0.0
    font = _load_font(font_size_px)
    scratch = Image.new("L", (1, 1), color=0)
    draw = ImageDraw.Draw(scratch)
    bboxes = [draw.textbbox((0, 0), ch, font=font) for ch in text]
    if layout == LAYOUT_VERTICAL:
        # Every character reuses the same band -- width of the single
        # widest character in the string (usually all equal for a
        # monospace font, but measured rather than assumed).
        width_px = max((b[2] for b in bboxes), default=0)
        row_count = max((b[3] - b[1] for b in bboxes), default=1)
        n_units = len(text)
    else:
        bbox = draw.textbbox((0, 0), text, font=font)
        width_px = bbox[2] - bbox[0]
        row_count = max((b[3] - b[1] for b in bboxes), default=1)
        n_units = 1
    zoom = max(1, int(zoom))
    width_cols = max(1, -(-(width_px * zoom) // max(1, col_downsample)))  # ceil division
    bandwidth_hz = width_cols * hz_per_col
    duration_s = max(1, row_count) * zoom * row_dwell_s * n_units + tail_s
    return bandwidth_hz, duration_s
