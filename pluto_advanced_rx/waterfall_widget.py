"""A pyqtgraph-based interactive waterfall widget: SDR++-style spectrum line
above a scrolling waterfall image, click-to-tune, a tuned-frequency marker,
and a shaded region showing the active demodulator's occupied bandwidth.

Deliberately pure PyQt5 + pyqtgraph -- no gnuradio imports at all, so this
can be built and tested standalone with synthetic data (see the offline
widget test), independent of GNU Radio/libiio/real hardware.

Pan/zoom is pyqtgraph's own default mouse behavior (drag to pan, scroll to
zoom), restricted to the X (frequency) axis -- the user explicitly chose
this standard interaction over bespoke draggable min/max handles. This is
why TuneViewBox below does NOT set RectMode the way GNU Radio's own
gr-filter GUI does in its near-identical CustomViewBox (see
/usr/lib/python3/dist-packages/gnuradio/filter/CustomViewBox.py, which this
click-to-tune idiom is modeled on): RectMode turns a left-drag into a
rubber-band zoom box, which would conflict with plain left-click-to-tune and
with drag-to-pan. Left-click (no drag) still reaches mouseClickEvent();
pyqtgraph itself routes an actual drag to mouseDragEvent instead, so
click-to-tune and drag-to-pan coexist without any extra bookkeeping here.
"""
import numpy as np
import pyqtgraph as pg
from PyQt5 import QtCore, QtWidgets


class TuneViewBox(pg.ViewBox):
    frequency_clicked = QtCore.pyqtSignal(float)

    def __init__(self, *args, **kwargs):
        kwargs["enableMenu"] = False
        super().__init__(*args, **kwargs)
        self.setMouseEnabled(x=True, y=False)  # amplitude/time axis stays fixed; only frequency pans/zooms

    def mouseClickEvent(self, ev):
        if ev.button() == QtCore.Qt.LeftButton:
            pt = self.mapSceneToView(ev.scenePos())
            self.frequency_clicked.emit(pt.x())
            ev.accept()
        elif ev.button() == QtCore.Qt.RightButton:
            self.autoRange()
            ev.accept()
        else:
            super().mouseClickEvent(ev)


class AdvancedWaterfallWidget(QtWidgets.QWidget):
    frequency_clicked = QtCore.pyqtSignal(float)

    def __init__(self, fft_size, history_rows, colormap_name, db_range, parent=None):
        super().__init__(parent)
        self._fft_size = fft_size
        self._history_rows = history_rows
        self._db_range = db_range
        self._center_hz = 0.0
        self._span_hz = 1.0
        self._write_row = 0

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Both plots' left axes are given the SAME fixed width (one shown,
        # one blanked-but-reserved) -- pyqtgraph's linked views compensate a
        # linked plot's data range to keep pixel columns aligned when the
        # two plots' axis-label widths differ (a deliberate, useful feature
        # for the general case), which we don't want here: we want the
        # waterfall's NUMERIC X range to match set_frequency_range exactly,
        # not just be pixel-compensated (empirically found: a plain
        # left-axis .hide() left the linked range ~8% off). Reserving equal
        # width on both sides makes the compensation a no-op.
        AXIS_WIDTH = 50

        self.spectrum_plot = pg.PlotWidget(viewBox=TuneViewBox())
        self.spectrum_plot.setLabel("left", "dB")
        self.spectrum_plot.getPlotItem().getAxis("left").setWidth(AXIS_WIDTH)
        # units="Hz" turns on pyqtgraph's automatic SI-prefix formatting
        # (e.g. "431.500 M" instead of the raw "4.315e+08"), which is what
        # makes this axis an actually readable "live frequency display".
        self.spectrum_plot.setLabel("bottom", "Frequency", units="Hz")
        self.spectrum_plot.setYRange(*db_range, padding=0)
        self.spectrum_plot.showGrid(x=True, y=True, alpha=0.3)
        self.spectrum_curve = self.spectrum_plot.plot(pen=pg.mkPen(color="#39d353", width=1))
        layout.addWidget(self.spectrum_plot, 1)

        self.waterfall_plot = pg.PlotWidget(viewBox=TuneViewBox())
        self.waterfall_plot.setXLink(self.spectrum_plot)
        # X must come ONLY from the link above, never from the waterfall
        # plot's own auto-range -- otherwise image_item.setRect() (called on
        # every set_frequency_range/push_fft_row) fights the linked-X update
        # for the same axis. Y auto-range stays on so the image's row-count
        # extent is framed nicely.
        self.waterfall_plot.getPlotItem().getViewBox().enableAutoRange(x=False, y=True)
        self.waterfall_plot.getPlotItem().getAxis("bottom").hide()
        waterfall_left_axis = self.waterfall_plot.getPlotItem().getAxis("left")
        waterfall_left_axis.setWidth(AXIS_WIDTH)
        waterfall_left_axis.setStyle(showValues=False, tickLength=0)
        waterfall_left_axis.setPen(None)
        self.waterfall_plot.setMouseEnabled(x=True, y=False)
        layout.addWidget(self.waterfall_plot, 3)

        self.image_item = pg.ImageItem()
        self.image_item.setColorMap(pg.colormap.get(colormap_name))
        self.waterfall_plot.addItem(self.image_item)

        for vb in (self.spectrum_plot.getPlotItem().getViewBox(), self.waterfall_plot.getPlotItem().getViewBox()):
            vb.frequency_clicked.connect(self.frequency_clicked.emit)

        self._marker_lines = []
        self._band_regions = []
        for plot in (self.spectrum_plot, self.waterfall_plot):
            marker = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen(color="#ffffff", width=1))
            plot.addItem(marker)
            self._marker_lines.append(marker)

            region = pg.LinearRegionItem(movable=False, brush=pg.mkBrush(255, 255, 255, 40),
                                          pen=pg.mkPen(color="#ffffff", width=0, style=QtCore.Qt.NoPen))
            region.setZValue(-10)
            plot.addItem(region)
            self._band_regions.append(region)

        self._init_image_buffer(fft_size)

    # --- internal ------------------------------------------------------
    def _init_image_buffer(self, fft_size):
        self._fft_size = fft_size
        self._image_buf = np.full((self._history_rows, fft_size), self._db_range[0], dtype=np.float32)
        self._write_row = 0
        self._update_image_rect()
        self.image_item.setImage(self._image_buf.T, autoLevels=False, levels=self._db_range)

    def _update_image_rect(self):
        f_start = self._center_hz - self._span_hz / 2
        self.image_item.setRect(QtCore.QRectF(f_start, 0, self._span_hz, self._history_rows))

    # --- public API ------------------------------------------------------
    def push_fft_row(self, row_db):
        if len(row_db) != self._fft_size:
            self._init_image_buffer(len(row_db))
        # Scroll history up by one row, write the newest row at the bottom
        # (row index history_rows-1) -- so the image reads top=oldest,
        # bottom=newest, matching a typical waterfall's downward time flow
        # combined with setRect's y-in-[0, history_rows] mapping.
        self._image_buf[:-1] = self._image_buf[1:]
        self._image_buf[-1] = row_db
        self.image_item.setImage(self._image_buf.T, autoLevels=False, levels=self._db_range)

        freq_axis = self._center_hz + (np.arange(self._fft_size) - self._fft_size // 2) * (self._span_hz / self._fft_size)
        self.spectrum_curve.setData(freq_axis, row_db)

    def set_frequency_range(self, center_hz, span_hz):
        self._center_hz = center_hz
        self._span_hz = span_hz
        self._update_image_rect()
        self.spectrum_plot.setXRange(center_hz - span_hz / 2, center_hz + span_hz / 2, padding=0)

    def set_tuned_frequency(self, freq_hz):
        for marker in self._marker_lines:
            marker.setPos(freq_hz)

    def set_demod_band(self, lo_hz, hi_hz):
        if lo_hz is None or hi_hz is None:
            for region in self._band_regions:
                region.setRegion((0, 0))
            return
        for region in self._band_regions:
            region.setRegion((lo_hz, hi_hz))

    def set_fft_size(self, fft_size):
        self._init_image_buffer(fft_size)

    def set_db_range(self, lo_db, hi_db):
        """Adjust the floor/ceiling of both the spectrum plot's Y-axis and
        the waterfall image's color mapping -- the noise floor varies a lot
        with antenna/location/gain, so this needs to be operator-adjustable
        rather than fixed at config.WATERFALL_DB_RANGE forever."""
        self._db_range = (lo_db, hi_db)
        self.spectrum_plot.setYRange(lo_db, hi_db, padding=0)
        self.image_item.setImage(self._image_buf.T, autoLevels=False, levels=self._db_range)

    def clear(self):
        self._init_image_buffer(self._fft_size)
        self.spectrum_curve.setData([], [])
