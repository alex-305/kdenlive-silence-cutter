"""Minimal KDE-style Qt interface for silence analysis and project preview."""

from __future__ import annotations

import sys
import math
from pathlib import Path

from PySide6.QtCore import (
    QObject,
    QRunnable,
    QSettings,
    QThreadPool,
    QTimer,
    QUrl,
    Signal,
    Slot,
    Qt,
)
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QScrollBar,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .project import KdenliveProject, Track
from .silence import detect_silence, probe_duration
from .waveform import WaveformData, analyze_with_waveform


class WorkerSignals(QObject):
    succeeded = Signal(object)
    failed = Signal(str)


class Worker(QRunnable):
    def __init__(self, function):
        super().__init__()
        self.function = function
        self.signals = WorkerSignals()

    @Slot()
    def run(self):
        try:
            self.signals.succeeded.emit(self.function())
        except Exception as exc:  # deliver background failures to the UI
            self.signals.failed.emit(str(exc))


def format_time(seconds: float) -> str:
    value = max(0, round(seconds))
    hours, remainder = divmod(value, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def format_precise_time(seconds: float) -> str:
    millis = max(0, round(seconds * 1000))
    hours, remainder = divmod(millis, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{milliseconds:03d}"


class WaveformPreview(QWidget):
    intervalSelected = Signal(int)
    seekRequested = Signal(float)
    viewChanged = Signal(float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(220)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.waveform: WaveformData | None = None
        self.duration = 0.0
        self.intervals: list[tuple[float, float]] = []
        self.selected = -1
        self.playhead = 0.0
        self.zoom = 1.0
        self.offset = 0.0
        self._press_x: float | None = None
        self._press_offset = 0.0
        self._dragged = False

    @property
    def view_duration(self) -> float:
        return self.duration / self.zoom if self.duration else 0.0

    @property
    def max_offset(self) -> float:
        return max(0.0, self.duration - self.view_duration)

    def set_data(self, waveform: WaveformData | None,
                 intervals: list[tuple[float, float]]):
        self.waveform = waveform
        self.duration = waveform.duration if waveform is not None else 0.0
        self.intervals = intervals
        self.selected = 0 if intervals else -1
        self.playhead = 0.0
        self.zoom, self.offset = 1.0, 0.0
        self.update()
        self._emit_view()

    def set_intervals(self, intervals: list[tuple[float, float]]):
        """Update proposed cuts without resetting the current waveform view."""
        self.intervals = intervals
        self.selected = 0 if intervals else -1
        self.update()

    def select(self, index: int):
        self.selected = index
        self.update()

    def set_playhead(self, seconds: float):
        self.playhead = max(0.0, min(self.duration, seconds))
        self.update()

    def _time_to_x(self, seconds: float, plot) -> int:
        return round(plot.left() + (seconds - self.offset) / max(1e-9, self.view_duration) * plot.width())

    def _emit_view(self):
        self.viewChanged.emit(self.offset, self.max_offset)

    def set_scroll_value(self, value: int):
        self.offset = self.max_offset * max(0, min(10000, value)) / 10000
        self.update()

    def fit(self):
        self.zoom, self.offset = 1.0, 0.0
        self.update()
        self._emit_view()

    def zoom_by(self, factor: float, anchor_x: float | None = None):
        if self.duration <= 0:
            return
        plot = self._plot_rect()
        x = plot.left() + plot.width() / 2 if anchor_x is None else anchor_x
        fraction = max(0.0, min(1.0, (x - plot.left()) / max(1, plot.width())))
        anchor_time = self.offset + fraction * self.view_duration
        self.zoom = max(1.0, min(512.0, self.zoom * factor))
        self.offset = max(0.0, min(self.max_offset,
                                   anchor_time - fraction * self.view_duration))
        self.update()
        self._emit_view()

    def _plot_rect(self):
        return self.rect().adjusted(42, 24, -12, -28)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        palette = self.palette()
        plot = self._plot_rect()
        painter.setPen(QPen(palette.mid().color(), 1))
        painter.setBrush(palette.base())
        painter.drawRoundedRect(plot, 5, 5)
        if self.waveform is None or self.duration <= 0:
            painter.setPen(palette.mid().color())
            painter.drawText(plot, Qt.AlignmentFlag.AlignCenter,
                             "Analyze to build the selected audio waveform")
            painter.end()
            return

        visible_duration = self.view_duration
        lane_height = plot.height() / 2
        painter.setPen(palette.mid().color())
        for tick in range(5):
            fraction = tick / 4
            x = plot.left() + round(plot.width() * fraction)
            painter.drawLine(x, plot.bottom() - 3, x, plot.bottom() + 4)
            label_x = x if tick == 0 else x - 80 if tick == 4 else x - 40
            alignment = (Qt.AlignmentFlag.AlignLeft if tick == 0 else
                         Qt.AlignmentFlag.AlignRight if tick == 4 else
                         Qt.AlignmentFlag.AlignHCenter)
            label = (format_precise_time(self.offset + visible_duration * fraction)
                     if visible_duration < 120 else
                     format_time(self.offset + visible_duration * fraction))
            painter.drawText(label_x, plot.bottom() + 20, 80, 16,
                             alignment,
                             label)

        for index, (start, end) in enumerate(self.intervals):
            if end <= self.offset or start >= self.offset + visible_duration:
                continue
            x1 = plot.left() + round(plot.width() *
                                     (max(start, self.offset) - self.offset) / visible_duration)
            x2 = plot.left() + round(plot.width() *
                                     (min(end, self.offset + visible_duration) - self.offset) / visible_duration)
            color = QColor("#d84949")
            color.setAlpha(115 if index == self.selected else 72)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawRect(x1, plot.top() + 2, max(2, x2 - x1), plot.height() - 4)

        if self.offset <= self.playhead <= self.offset + visible_duration:
            x = self._time_to_x(self.playhead, plot)
            painter.setPen(QPen(QColor("#f4c542"), 2))
            painter.drawLine(x, plot.top(), x, plot.bottom())

        levels = self.waveform.levels
        buckets_per_pixel = (visible_duration * self.waveform.sample_rate
                             / self.waveform.frames_per_bucket / max(1, plot.width()))
        level_index = max(0, min(len(levels) - 1,
                                 int(math.log2(max(1.0, buckets_per_pixel)))))
        values = levels[level_index]
        factor = 1 << level_index
        level_buckets = len(values) // 4
        seconds_per_bucket = self.waveform.frames_per_bucket / self.waveform.sample_rate
        waveform_color = palette.text().color()
        waveform_color.setAlpha(220)
        painter.setPen(QPen(waveform_color, 1))
        for x in range(plot.left(), plot.right() + 1):
            fraction = (x - plot.left()) / max(1, plot.width())
            source_start = (self.offset + fraction * visible_duration) / seconds_per_bucket
            source_end = (self.offset + min(1.0, fraction + 1 / max(1, plot.width()))
                          * visible_duration) / seconds_per_bucket
            first = max(0, int(source_start / factor))
            last = min(level_buckets, max(first + 1, math.ceil(source_end / factor)))
            if first >= level_buckets:
                continue
            low_l, high_l, low_r, high_r = 32767, -32768, 32767, -32768
            for bucket in range(first, last):
                base = bucket * 4
                low_l = min(low_l, values[base])
                high_l = max(high_l, values[base + 1])
                low_r = min(low_r, values[base + 2])
                high_r = max(high_r, values[base + 3])
            for channel, (minimum, maximum) in enumerate(((low_l, high_l), (low_r, high_r))):
                center = round(plot.top() + lane_height * (channel + 0.5))
                amplitude = max(1, lane_height * 0.43)
                y1 = round(center - maximum / 32768 * amplitude)
                y2 = round(center - minimum / 32768 * amplitude)
                painter.drawLine(x, y1, x, y2)

        painter.setPen(QPen(palette.mid().color(), 1))
        middle = round(plot.top() + lane_height)
        painter.drawLine(plot.left(), middle, plot.right(), middle)
        painter.setPen(palette.text().color())
        painter.drawText(10, round(plot.top() + lane_height * 0.5 + 5), "L")
        painter.drawText(10, round(plot.top() + lane_height * 1.5 + 5), "R")
        painter.end()

    def wheelEvent(self, event):
        if event.angleDelta().y():
            self.zoom_by(1.25 if event.angleDelta().y() > 0 else 0.8,
                         event.position().x())
            event.accept()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_x = event.position().x()
            self._press_offset = self.offset
            self._dragged = False

    def mouseMoveEvent(self, event):
        if self._press_x is None or not event.buttons() & Qt.MouseButton.LeftButton:
            return
        delta = event.position().x() - self._press_x
        if abs(delta) > 4:
            self._dragged = True
        if self._dragged:
            plot = self._plot_rect()
            self.offset = max(0.0, min(self.max_offset,
                             self._press_offset - delta / max(1, plot.width()) * self.view_duration))
            self.update()
            self._emit_view()

    def mouseReleaseEvent(self, event):
        if self._press_x is None:
            return
        if not self._dragged and self.duration > 0:
            plot = self._plot_rect()
            fraction = (event.position().x() - plot.left()) / max(1, plot.width())
            second = self.offset + max(0.0, min(1.0, fraction)) * self.view_duration
            self.seekRequested.emit(second)
        self._press_x = None


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Kdenlive Silence Cutter")
        self.resize(940, 700)
        self.settings = QSettings()
        self.thread_pool = QThreadPool.globalInstance()
        self.project: KdenliveProject | None = None
        self.project_path: Path | None = None
        self.audio_track: Track | None = None
        self.analysis: dict | None = None
        self.waveform_data: WaveformData | None = None
        self.waveform_source: str | None = None
        self.media_duration: float | None = None
        self.silence_cache: dict[tuple, list] = {}
        self.preview_generation = 0
        self.preview_pending = False
        self.preview_worker_running = False
        self.busy = False
        self.preview_timer = QTimer(self)
        self.preview_timer.setSingleShot(True)
        self.preview_timer.setInterval(300)
        self.preview_timer.timeout.connect(self._launch_preview)
        self.settings_save_timer = QTimer(self)
        self.settings_save_timer.setSingleShot(True)
        self.settings_save_timer.setInterval(400)
        self.settings_save_timer.timeout.connect(self._save_preferences)

        self.player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.audio_output.setVolume(0.8)
        self.player.setAudioOutput(self.audio_output)
        self.playback_end_ms = -1
        self.pending_playback: tuple[int, int] | None = None
        self.playback_timer = QTimer(self)
        self.playback_timer.setInterval(100)
        self.playback_timer.timeout.connect(self._check_playback_end)
        self.player.positionChanged.connect(self._check_playback_end)
        self.player.mediaStatusChanged.connect(self._media_status_changed)
        self.player.errorOccurred.connect(self._player_error)

        self._build_ui()
        self._restore_control_preferences()
        self._connect_events()
        self._set_ready(False)
        self._restore_last_project()

    def _build_ui(self):
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(13)

        heading = QLabel("Remove silence from a Kdenlive timeline")
        heading.setStyleSheet("font-size: 19px; font-weight: 600;")
        subtitle = QLabel("Choose an audio source and adjust the settings; proposed cuts preview automatically.")
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet("color: palette(mid); margin-bottom: 4px;")
        root.addWidget(heading)
        root.addWidget(subtitle)

        project_row = QHBoxLayout()
        self.project_label = QLabel("No project selected")
        self.project_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.project_label.setStyleSheet("padding: 8px; border: 1px solid palette(mid); border-radius: 5px;")
        self.open_project_button = QPushButton("Open Project…")
        project_row.addWidget(self.project_label, 1)
        project_row.addWidget(self.open_project_button)
        root.addLayout(project_row)

        settings = QFormLayout()
        self.track_combo = QComboBox()
        self.source_combo = QComboBox()
        self.threshold_spin = QDoubleSpinBox()
        self.threshold_spin.setRange(-96, 0)
        self.threshold_spin.setDecimals(1)
        self.threshold_spin.setSingleStep(1.0)
        self.threshold_spin.setSuffix(" dB")
        self.threshold_spin.setValue(-40)
        self.minimum_spin = QDoubleSpinBox()
        self.minimum_spin.setRange(0.1, 60)
        self.minimum_spin.setDecimals(2)
        self.minimum_spin.setSingleStep(0.05)
        self.minimum_spin.setSuffix(" s")
        self.minimum_spin.setValue(0.5)
        self.padding_spin = QDoubleSpinBox()
        self.padding_spin.setRange(0, 5)
        self.padding_spin.setDecimals(2)
        self.padding_spin.setSingleStep(0.05)
        self.padding_spin.setSuffix(" s")
        self.padding_spin.setValue(0.1)
        settings.addRow("Audio track", self.track_combo)
        settings.addRow("Media on track", self.source_combo)
        settings.addRow("Silence threshold", self.threshold_spin)
        settings.addRow("Minimum silence", self.minimum_spin)
        settings.addRow("Speech-edge padding", self.padding_spin)
        root.addLayout(settings)

        actions = QHBoxLayout()
        self.analyze_button = QPushButton("Refresh Preview")
        self.play_button = QPushButton("Play")
        self.apply_button = QPushButton("Write New Timeline")
        self.apply_button.setProperty("accent", True)
        actions.addWidget(self.analyze_button)
        actions.addWidget(self.play_button)
        actions.addStretch(1)
        actions.addWidget(self.apply_button)
        root.addLayout(actions)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.hide()
        root.addWidget(self.progress)

        waveform_header = QHBoxLayout()
        timeline_title = QLabel("Selected audio waveform  ·  red = proposed cut")
        timeline_title.setStyleSheet("font-weight: 600; margin-top: 3px;")
        waveform_header.addWidget(timeline_title)
        waveform_header.addStretch(1)
        self.zoom_out_button = QPushButton("−")
        self.zoom_out_button.setToolTip("Zoom out (mouse wheel down also works)")
        self.zoom_label = QLabel("Fit")
        self.zoom_label.setMinimumWidth(52)
        self.zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.zoom_in_button = QPushButton("+")
        self.zoom_in_button.setToolTip("Zoom in (mouse wheel up also works)")
        self.fit_button = QPushButton("Fit")
        for button in (self.zoom_out_button, self.zoom_in_button):
            button.setFixedWidth(32)
        waveform_header.addWidget(self.zoom_out_button)
        waveform_header.addWidget(self.zoom_label)
        waveform_header.addWidget(self.zoom_in_button)
        waveform_header.addWidget(self.fit_button)
        root.addLayout(waveform_header)
        self.waveform = WaveformPreview()
        root.addWidget(self.waveform, 1)
        self.waveform_scroll = QScrollBar(Qt.Orientation.Horizontal)
        self.waveform_scroll.setRange(0, 10000)
        self.waveform_scroll.hide()
        root.addWidget(self.waveform_scroll)

        self.cut_table = QTableWidget(0, 3)
        self.cut_table.setHorizontalHeaderLabels(["#", "Audio start", "Audio end"])
        self.cut_table.verticalHeader().hide()
        self.cut_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.cut_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.cut_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.cut_table.setMaximumHeight(150)
        self.cut_table.horizontalHeader().setStretchLastSection(True)
        self.cut_table.setColumnWidth(0, 55)
        self.cut_table.setColumnWidth(1, 150)
        root.addWidget(self.cut_table)

        summary = QHBoxLayout()
        self.summary_label = QLabel("No analysis yet")
        self.summary_label.setStyleSheet("font-weight: 600;")
        self.status_label = QLabel("The original timeline will be preserved")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.status_label.setWordWrap(True)
        summary.addWidget(self.summary_label)
        summary.addStretch(1)
        summary.addWidget(self.status_label, 1)
        root.addLayout(summary)
        self.setCentralWidget(central)

    def _connect_events(self):
        self.open_project_button.clicked.connect(self._choose_project)
        self.track_combo.currentIndexChanged.connect(self._track_changed)
        self.source_combo.currentIndexChanged.connect(self._source_changed)
        self.analyze_button.clicked.connect(self._analyze)
        self.play_button.clicked.connect(self._play_context)
        self.apply_button.clicked.connect(self._write_project)
        self.waveform.intervalSelected.connect(self._select_interval)
        self.waveform.seekRequested.connect(self._seek_playhead)
        self.waveform.viewChanged.connect(self._waveform_view_changed)
        self.waveform_scroll.valueChanged.connect(self.waveform.set_scroll_value)
        self.zoom_in_button.clicked.connect(lambda: self.waveform.zoom_by(1.5))
        self.zoom_out_button.clicked.connect(lambda: self.waveform.zoom_by(1 / 1.5))
        self.fit_button.clicked.connect(self.waveform.fit)
        self.cut_table.currentCellChanged.connect(self._table_selection_changed)
        self.cut_table.cellClicked.connect(self._table_row_clicked)
        for control in (self.threshold_spin, self.minimum_spin, self.padding_spin):
            control.valueChanged.connect(self._settings_changed)

    def _choose_project(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Kdenlive Project", str(Path.home()), "Kdenlive projects (*.kdenlive)")
        if not path:
            return
        try:
            project = KdenliveProject(path)
            audio_tracks = [track for track in project.tracks() if track.is_audio]
            if not audio_tracks:
                raise ValueError("No audio timeline tracks were found in this project.")
            self._install_project(project, path, audio_tracks)
            self.status_label.setText(
                f"Loaded {len(audio_tracks)} audio track(s); preview scheduled")
        except Exception as exc:
            QMessageBox.critical(self, "Could not open project", str(exc))

    def _install_project(self, project: KdenliveProject, path: str | Path,
                         audio_tracks: list[Track], *, selected_track: Track | None = None,
                         selected_media: str | None = None):
        self.project = project
        self.project_path = Path(path).expanduser().resolve()
        self.project_label.setText(str(self.project_path))
        self.track_combo.blockSignals(True)
        self.track_combo.clear()
        for track in audio_tracks:
            self.track_combo.addItem(track.name, track)
        selected_index = next((index for index, track in enumerate(audio_tracks)
                               if track == selected_track), 0)
        if audio_tracks:
            self.track_combo.setCurrentIndex(selected_index)
        self.track_combo.blockSignals(False)
        self._select_track(preferred_media=selected_media)
        self._queue_preferences_save()

    @staticmethod
    def _restored_track(audio_tracks: list[Track], name: str, index: int) -> Track | None:
        exact = [track for track in audio_tracks
                 if track.index == index and track.name == name]
        if exact:
            return exact[0]
        named = [track for track in audio_tracks if track.name == name]
        return named[0] if len(named) == 1 else None

    def _restore_control_preferences(self):
        for key, control in (("threshold", self.threshold_spin),
                             ("minimum", self.minimum_spin),
                             ("padding", self.padding_spin)):
            try:
                value = float(self.settings.value(key, control.value()))
            except (TypeError, ValueError):
                continue
            if math.isfinite(value):
                control.setValue(value)

    def _restore_last_project(self):
        stored_path = str(self.settings.value("lastProject", "") or "")
        if not stored_path:
            return
        try:
            path = Path(stored_path).expanduser()
            if not path.is_file():
                self._clear_project_preferences()
                return
            project = KdenliveProject(path)
            audio_tracks = [track for track in project.tracks() if track.is_audio]
            stored_name = str(self.settings.value("lastTrackName", "") or "")
            try:
                stored_index = int(self.settings.value("lastTrackIndex", -1))
            except (TypeError, ValueError):
                stored_index = -1
            track = self._restored_track(audio_tracks, stored_name, stored_index)
            if track is None:
                self._clear_project_preferences()
                return
            media = str(self.settings.value("lastMedia", "") or "")
            self._install_project(project, path, audio_tracks,
                                  selected_track=track, selected_media=media)
        except Exception:
            # A stale or unsupported last project should not prevent opening the app.
            self._clear_project_state()
            self._clear_project_preferences()

    def _clear_project_preferences(self):
        for key in ("lastProject", "lastTrackName", "lastTrackIndex", "lastMedia"):
            self.settings.remove(key)
        self.settings.sync()

    def _clear_project_state(self):
        self.preview_timer.stop()
        self.preview_generation += 1
        self.preview_pending = False
        self.project = None
        self.project_path = None
        self.audio_track = None
        self.analysis = None
        self.waveform_data = None
        self.waveform_source = None
        self.media_duration = None
        self.track_combo.blockSignals(True)
        self.track_combo.clear()
        self.track_combo.blockSignals(False)
        self.source_combo.blockSignals(True)
        self.source_combo.clear()
        self.source_combo.blockSignals(False)
        self.project_label.setText("No project selected")
        self.waveform.set_data(None, [])
        self.cut_table.setRowCount(0)
        self.summary_label.setText("No analysis yet")
        self.progress.hide()
        self.status_label.setText("The original timeline will be preserved")
        self._set_ready(False)

    def _queue_preferences_save(self):
        self.settings_save_timer.start()

    def _save_preferences(self):
        self.settings.setValue("threshold", self.threshold_spin.value())
        self.settings.setValue("minimum", self.minimum_spin.value())
        self.settings.setValue("padding", self.padding_spin.value())
        if self.project_path is not None and self.audio_track is not None:
            self.settings.setValue("lastProject", str(self.project_path))
            self.settings.setValue("lastTrackName", self.audio_track.name)
            self.settings.setValue("lastTrackIndex", self.audio_track.index)
            self.settings.setValue("lastMedia", self.source_combo.currentData() or "")
        else:
            for key in ("lastProject", "lastTrackName", "lastTrackIndex", "lastMedia"):
                self.settings.remove(key)
        self.settings.sync()

    def _track_changed(self, *_):
        self._select_track()
        self._queue_preferences_save()

    @Slot()
    def _select_track(self, *_, preferred_media=None):
        if self.project is None:
            return
        previous_source = self.source_combo.currentData()
        track = self.track_combo.currentData()
        if track is None:
            return
        self.audio_track = track
        self.source_combo.blockSignals(True)
        self.source_combo.clear()
        for source in self.project.track_media_sources(track):
            label = source.name
            if not source.exists():
                label += " (missing)"
            self.source_combo.addItem(label, str(source))
        if preferred_media is not None:
            media_exists = bool(preferred_media and Path(preferred_media).is_file())
            media_index = (self.source_combo.findData(preferred_media)
                           if media_exists else -1)
            self.source_combo.setCurrentIndex(media_index)
        self.source_combo.blockSignals(False)
        current_source = self.source_combo.currentData()
        self._schedule_preview(clear_waveform=previous_source != current_source)
        if self.source_combo.count() == 0:
            self.status_label.setText("No media entries found on this audio track")

    @Slot()
    def _source_changed(self, *_):
        source = self.source_combo.currentData()
        self._queue_preferences_save()
        self._schedule_preview(clear_waveform=source != self.waveform_source)

    @Slot()
    def _settings_changed(self, *_):
        self._queue_preferences_save()
        self._schedule_preview()

    def _schedule_preview(self, *, clear_waveform=False, delay_ms=300):
        self.preview_timer.stop()
        self.preview_generation += 1
        self.analysis = None
        if clear_waveform:
            self.waveform_data = None
            self.waveform_source = None
            self.media_duration = None
            self.waveform.set_data(None, [])
        elif self.waveform_data is not None:
            self.waveform.set_intervals([])
        else:
            self.waveform.set_data(None, [])
        self.cut_table.setRowCount(0)
        self.summary_label.setText("Updating preview…")
        source = self.source_combo.currentData()
        self.preview_pending = bool(self.project and self.audio_track and source)
        if self.preview_pending and not self.busy:
            self.progress.show()
            self.status_label.setText("Preview will update when you pause changing settings")
            self.preview_timer.start(delay_ms)
        else:
            self.preview_pending = False
            if not self.busy:
                self.progress.hide()
                self.status_label.setText("Select an audio source to preview proposed cuts")
        self._set_ready(False)

    @Slot(float, float)
    def _waveform_view_changed(self, offset, maximum):
        self.zoom_label.setText(f"{self.waveform.zoom:.1f}×" if self.waveform.zoom > 1 else "Fit")
        self.waveform_scroll.blockSignals(True)
        self.waveform_scroll.setValue(round(10000 * offset / maximum) if maximum > 0 else 0)
        self.waveform_scroll.setVisible(maximum > 0)
        self.waveform_scroll.blockSignals(False)

    def _set_busy(self, busy: bool, message: str):
        self.busy = busy
        self.progress.setVisible(busy)
        self.open_project_button.setEnabled(not busy)
        self.track_combo.setEnabled(not busy)
        self.source_combo.setEnabled(not busy)
        self.analyze_button.setEnabled(not busy and self.source_combo.count() > 0)
        self.threshold_spin.setEnabled(not busy)
        self.minimum_spin.setEnabled(not busy)
        self.padding_spin.setEnabled(not busy)
        self.status_label.setText(message)

    def _set_ready(self, ready: bool):
        available = ready and not self.busy and not self.preview_pending
        self.apply_button.setEnabled(available)
        self.play_button.setEnabled(available)

    def _run_worker(self, function, success, failure):
        worker = Worker(function)
        worker.signals.succeeded.connect(success)
        worker.signals.failed.connect(failure)
        self.thread_pool.start(worker)

    def _analyze(self, *_):
        self._schedule_preview(delay_ms=0)

    def _launch_preview(self):
        if self.preview_worker_running:
            return
        if self.project is None or self.audio_track is None:
            self.preview_pending = False
            self.progress.hide()
            return
        source = self.source_combo.currentData()
        if not source:
            self.preview_pending = False
            self.progress.hide()
            return
        generation = self.preview_generation
        project = self.project
        track = self.audio_track
        threshold = self.threshold_spin.value()
        minimum = self.minimum_spin.value()
        padding = self.padding_spin.value()
        try:
            media_stat = Path(source).expanduser().stat()
            silence_cache_key = (str(Path(source).expanduser().resolve()),
                                 media_stat.st_size, media_stat.st_mtime_ns,
                                 threshold, minimum)
        except OSError:
            silence_cache_key = None
        cached_silences = (self.silence_cache.get(silence_cache_key)
                            if silence_cache_key is not None else None)
        cached_waveform = (self.waveform_data
                           if self.waveform_source == source else None)
        cached_duration = (self.media_duration
                           if self.waveform_source == source else None)
        self.preview_pending = True
        self._set_ready(False)
        self.progress.show()
        self.status_label.setText("Updating proposed cuts…")

        def task():
            try:
                if cached_silences is not None:
                    if cached_waveform is None:
                        waveform, _ = analyze_with_waveform(
                            source, noise_db=threshold, min_duration=minimum)
                    else:
                        waveform = cached_waveform
                    raw = cached_silences
                elif cached_waveform is None:
                    waveform, raw = analyze_with_waveform(
                        source, noise_db=threshold, min_duration=minimum)
                else:
                    waveform = cached_waveform
                    raw = detect_silence(
                        source, noise_db=threshold, min_duration=minimum,
                        duration=cached_duration or waveform.duration)
                media_duration = cached_duration or probe_duration(source)
                padded = [(item.start + padding, item.end - padding)
                          for item in raw if item.duration > 2 * padding]
                frames = project.plan_timeline_cuts(source, padded)
                source_cuts = project.source_cut_intervals(track, source, frames)
                duration_prop = project._properties(project.sequence).get("kdenlive:duration")
                if duration_prop:
                    duration = project._seconds(duration_prop)
                elif project.sequence.get("out"):
                    duration = project._seconds(project.sequence.get("out")) + 1 / project.frame_rate()
                else:
                    duration = media_duration
                result = {
                    "source": source,
                    "raw_silences": raw,
                    "padded_silences": padded,
                    "frames": frames,
                    "source_cuts": source_cuts,
                    "waveform": waveform,
                    "fps": project.frame_rate(),
                    "duration": duration,
                    "media_duration": media_duration,
                    "track_index": track.index,
                    "silence_cache_key": silence_cache_key,
                }
                return generation, result, None
            except Exception as exc:
                return generation, None, str(exc)

        self.preview_worker_running = True
        self._run_worker(task, self._preview_worker_complete, self._preview_worker_failed)

    @Slot(object)
    def _preview_worker_complete(self, payload):
        generation, result, error = payload
        self.preview_worker_running = False
        if generation != self.preview_generation:
            if self.preview_pending and not self.preview_timer.isActive() and not self.busy:
                self._launch_preview()
            return
        self.preview_pending = False
        self.progress.hide()
        if error:
            self.analysis = None
            self.summary_label.setText("Preview failed")
            self.status_label.setText(error)
            self._set_ready(False)
            return
        self._analysis_complete(result)

    @Slot(str)
    def _preview_worker_failed(self, message):
        self.preview_worker_running = False
        self.preview_pending = False
        self.progress.hide()
        self.summary_label.setText("Preview failed")
        self.status_label.setText(message)
        self._set_ready(False)

    @Slot(object)
    def _analysis_complete(self, result):
        same_waveform = self.waveform.waveform is result["waveform"]
        self.analysis = result
        self.waveform_data = result["waveform"]
        self.waveform_source = result["source"]
        self.media_duration = result["media_duration"]
        cache_key = result["silence_cache_key"]
        if cache_key is not None:
            self.silence_cache.pop(cache_key, None)
            self.silence_cache[cache_key] = result["raw_silences"]
            while len(self.silence_cache) > 16:
                self.silence_cache.pop(next(iter(self.silence_cache)))
        source_cuts = result["source_cuts"]
        if same_waveform:
            self.waveform.set_intervals(source_cuts)
        else:
            self.waveform.set_data(self.waveform_data, source_cuts)
        self.cut_table.setRowCount(len(source_cuts))
        for row, (start, end) in enumerate(source_cuts):
            values = (str(row + 1), format_precise_time(start), format_precise_time(end))
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.cut_table.setItem(row, column, item)
        if source_cuts:
            self.cut_table.blockSignals(True)
            self.cut_table.selectRow(0)
            self.cut_table.blockSignals(False)
        removed = sum(end - start for start, end in result["frames"]) / result["fps"]
        cuts_count = len(result["frames"])
        self.summary_label.setText(
            f"{cuts_count} timeline cuts  ·  {format_time(removed)} removed  ·  "
            f"{format_time(max(0, result['duration'] - removed))} remaining"
        )
        message = ("Red regions mark proposed source-audio cuts. Scroll or zoom to inspect."
                   if cuts_count else "No silence intervals overlap this media on the project timeline.")
        self.progress.hide()
        self.status_label.setText(message)
        self._set_ready(bool(result["frames"]))

    @Slot(str)
    def _operation_failed(self, message):
        self._set_busy(False, message)
        self._set_ready(False)
        QMessageBox.critical(self, "Operation failed", message)

    @Slot(int)
    def _select_interval(self, index):
        if not self.analysis or index < 0 or index >= self.cut_table.rowCount():
            return
        self.waveform.select(index)
        self.cut_table.selectRow(index)

    @Slot(int, int, int, int)
    def _table_selection_changed(self, row, *_):
        if row >= 0 and self.analysis and row < len(self.analysis["source_cuts"]):
            self.waveform.select(row)
            self._seek_playhead(self.analysis["source_cuts"][row][1])

    def _table_row_clicked(self, row, _column):
        self._table_selection_changed(row)

    def _seek_playhead(self, seconds):
        if self.analysis:
            seconds = self._skip_cut_at(seconds)
        self.waveform.set_playhead(seconds)
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.setPosition(round(seconds * 1000))

    def _skip_cut_at(self, seconds):
        if not self.analysis:
            return seconds
        for start, end in self.analysis["source_cuts"]:
            if start <= seconds < end:
                return end
        return seconds

    def _play_context(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.stop()
            self.play_button.setText("Play")
            return
        if not self.analysis or self.project is None or self.audio_track is None:
            return
        playback_start = self._skip_cut_at(self.waveform.playhead)
        if playback_start >= self.analysis["media_duration"]:
            playback_start = 0.0
        self.waveform.set_playhead(playback_start)
        self.playback_end_ms = round(self.analysis["media_duration"] * 1000)
        self.player.setSource(QUrl.fromLocalFile(self.analysis["source"]))
        self.pending_playback = (round(playback_start * 1000), self.playback_end_ms)
        if self.player.mediaStatus() in (QMediaPlayer.MediaStatus.LoadedMedia,
                                         QMediaPlayer.MediaStatus.BufferedMedia):
            self._media_status_changed(self.player.mediaStatus())

    @Slot(QMediaPlayer.MediaStatus)
    def _media_status_changed(self, status):
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self.playback_timer.stop()
            self.playback_end_ms = -1
            self.play_button.setText("Play")
        if self.pending_playback is None or status not in (
                QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia):
            return
        start_ms, self.playback_end_ms = self.pending_playback
        self.pending_playback = None
        self.player.setPosition(start_ms)
        self.player.play()
        self.play_button.setText("Stop")
        self.playback_timer.start()

    def _check_playback_end(self, *_):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState and self.analysis:
            position = self.player.position() / 1000
            for start, end in self.analysis["source_cuts"]:
                if start <= position < end:
                    self.player.setPosition(round(end * 1000))
                    self.waveform.set_playhead(end)
                    return
            self.waveform.set_playhead(position)
        if (self.playback_end_ms >= 0
                and self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
                and self.player.position() >= self.playback_end_ms):
            self.player.stop()
            self.playback_end_ms = -1
            self.play_button.setText("Play")
            self.playback_timer.stop()

    def _player_error(self, error, message):
        if error != QMediaPlayer.Error.NoError:
            self.status_label.setText(f"Audio preview error: {message}")

    def _write_project(self):
        if not self.analysis or not self.project_path:
            return
        answer = QMessageBox.warning(
            self,
            "Modify Kdenlive Project",
            f"A new silence-cut timeline will be added to this project file:\n\n"
            f"{self.project_path}\n\n"
            "The original timeline will remain available. Close this project in Kdenlive "
            "before continuing, then reopen it after the write completes.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.preview_timer.stop()
        self.preview_generation += 1
        self.preview_pending = False
        settings = dict(self.analysis)
        source = settings["source"]
        cuts = settings["padded_silences"]
        project_path = self.project_path
        self._set_ready(False)
        self._set_busy(True, "Adding a synchronized silence-cut timeline to the selected project…")

        def task():
            project = KdenliveProject(project_path)
            removed_frames = project.apply_timeline_cuts(source, cuts)
            return project_path, removed_frames, settings["fps"], settings["track_index"], source

        self._run_worker(task, self._write_complete, self._operation_failed)

    @Slot(object)
    def _write_complete(self, result):
        output, frames, fps, selected_track_index, selected_source = result
        project = KdenliveProject(output)
        audio_tracks = [track for track in project.tracks() if track.is_audio]
        self.project = project
        self.track_combo.blockSignals(True)
        self.track_combo.clear()
        for track in audio_tracks:
            self.track_combo.addItem(track.name, track)
        selected_index = next((index for index, track in enumerate(audio_tracks)
                               if track.index == selected_track_index), 0)
        if audio_tracks:
            self.track_combo.setCurrentIndex(selected_index)
        self.track_combo.blockSignals(False)
        self._set_busy(False, f"Added a new timeline to the project: {output}")
        self._select_track()
        source_index = self.source_combo.findData(selected_source)
        if source_index >= 0:
            self.source_combo.setCurrentIndex(source_index)
        self._set_ready(False)
        QMessageBox.information(
            self, "Project written",
            f"Updated:\n{output}\n\nAdded a silence-cut timeline and removed "
            f"{format_time(frames / fps)} from it. The original timeline remains available.\n\n"
            "Reopen the project in Kdenlive to view the new active timeline.")

    def closeEvent(self, event):
        self.settings_save_timer.stop()
        self.preview_timer.stop()
        self._save_preferences()
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Kdenlive Silence Cutter")
    app.setOrganizationName("Silence Cutter")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
