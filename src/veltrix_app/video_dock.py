"""
The video dock: one or more videos beside the telemetry, moving together.

Every camera linked to the session is shown, side by side, each on its own
sync. Playing moves the cursor; moving the cursor seeks every camera. One
camera is the selected one: it drives the cursor during playback, and the
offset, anchors and sync status shown are its own. The others are held to the
cursor through their own sync and pulled back if they wander. How
video time relates to telemetry time, and how well that is known, is
`veltrix_app.video_sync.VideoLink`; this is the player and the controls.
The app works the same with no video, and a video whose file has moved is
reported and can be relinked.
"""

from __future__ import annotations

from pathlib import Path

from PySide6 import QtCore, QtMultimedia, QtMultimediaWidgets, QtWidgets

from .video_sync import VideoLink

SEEK_INTERVAL_MS = 60          # the cursor may move many times a second; the video is sought at most this often
RESYNC_S = 0.25                # a second camera this far from where its sync puts it during playback is sought back
MAX_SHOWN = 4                  # cameras shown side by side; more stay in the list and are shown when selected


class _Pane:
    """One camera: its link, its player and its picture."""

    def __init__(self, link: VideoLink, parent):
        self.link = link
        self.player = QtMultimedia.QMediaPlayer(parent)
        self.audio = QtMultimedia.QAudioOutput(parent)
        self.audio.setMuted(True)
        self.player.setAudioOutput(self.audio)
        self.view = QtMultimediaWidgets.QVideoWidget()
        self.view.setMinimumSize(160, 100)
        self.player.setVideoOutput(self.view)
        self.caption = QtWidgets.QLabel(link.camera or Path(link.path).name)
        self.box = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(self.box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(self.view, 1)
        layout.addWidget(self.caption)

    def load(self) -> bool:
        if not self.link.exists():
            self.caption.setText(f"{self.link.camera or Path(self.link.path).name} — file not found")
            return False
        self.player.setSource(QtCore.QUrl.fromLocalFile(str(Path(self.link.path).resolve())))
        return True

    def close(self):
        self.player.stop()
        self.player.setSource(QtCore.QUrl())
        self.box.setParent(None)
        self.box.deleteLater()
ANCHOR_COLUMNS = ("Video (s)", "Telemetry (s)", "Off by (s)", "What")


class VideoDock(QtWidgets.QDockWidget):
    cursor_from_video = QtCore.Signal(float)      # playback or a drag of the video slider: telemetry time
    changed = QtCore.Signal()                     # links were edited: save them

    def __init__(self, parent=None):
        super().__init__("Video", parent)
        self.cursor = 0.0                          # the telemetry cursor, as last told
        self._from_cursor = False
        self._pending = None

        self.panes: list = []
        self._idle = QtMultimedia.QMediaPlayer(self)       # stands in when no camera is linked
        self.grid_box = QtWidgets.QWidget()
        self.grid_box.setMinimumHeight(160)
        self.grid = QtWidgets.QGridLayout(self.grid_box)
        self.grid.setContentsMargins(0, 0, 0, 0)

        self.camera = QtWidgets.QComboBox()
        self.camera.currentIndexChanged.connect(self._select)
        add, relink, remove = (QtWidgets.QPushButton(t) for t in ("Add video…", "Relink…", "Remove"))
        add.clicked.connect(self._choose)
        relink.clicked.connect(self._choose_relink)
        remove.clicked.connect(self.remove_video)
        self.play = QtWidgets.QPushButton("Play")
        self.play.clicked.connect(self.toggle)
        self.slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.slider.sliderMoved.connect(lambda ms: self.player.setPosition(ms))
        self.piecewise = QtWidgets.QCheckBox("Piecewise")
        self.piecewise.setToolTip("Follow the anchors segment by segment instead of one straight fit — for a recording "
                                  "that dropped frames or was joined from clips. Needs three or more anchors.")
        self.piecewise.toggled.connect(self._piecewise_toggled)
        self.clock = QtWidgets.QLabel("—")
        self.offset = QtWidgets.QDoubleSpinBox(decimals=3, minimum=-86400, maximum=86400, singleStep=0.04, suffix=" s")
        self.offset.setToolTip("telemetry time = video time + offset. Positive: the video started after the log did.")
        self.offset.valueChanged.connect(self._offset_edited)
        self.what = QtWidgets.QLineEdit(placeholderText="What happens here, e.g. brake onset T1")
        anchor = QtWidgets.QPushButton("Anchor: this frame is the cursor")
        anchor.setToolTip("Pause on an event in the video, put the cursor on the same event in the data, then press.")
        anchor.clicked.connect(lambda: self.add_anchor(self.what.text()))
        self.anchors = QtWidgets.QTableWidget(0, len(ANCHOR_COLUMNS))
        self.anchors.setHorizontalHeaderLabels(ANCHOR_COLUMNS)
        self.anchors.verticalHeader().hide()
        self.anchors.horizontalHeader().setStretchLastSection(True)
        self.anchors.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.anchors.setMaximumHeight(120)
        drop = QtWidgets.QPushButton("Remove anchor")
        drop.clicked.connect(lambda: self.anchors.currentRow() >= 0 and self.remove_anchor(self.anchors.currentRow()))
        self.status = QtWidgets.QLabel("No video. The session works without one.")
        self.status.setWordWrap(True)

        rows = [QtWidgets.QHBoxLayout() for _ in range(4)]
        for w in (self.camera, add, relink, remove):
            rows[0].addWidget(w, 1 if w is self.camera else 0)
        for w in (self.play, self.slider, self.clock):
            rows[1].addWidget(w, 1 if w is self.slider else 0)
        for w in (QtWidgets.QLabel("Offset"), self.offset, self.piecewise, self.what, anchor):
            rows[2].addWidget(w, 1 if w is self.what else 0)
        rows[3].addWidget(drop)
        rows[3].addStretch(1)
        box = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(box)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addLayout(rows[0])
        layout.addWidget(self.grid_box, 1)
        layout.addLayout(rows[1])
        layout.addLayout(rows[2])
        layout.addWidget(self.status)
        layout.addWidget(self.anchors)
        layout.addLayout(rows[3])
        self.setWidget(box)
        self._seek_timer = QtCore.QTimer(self, singleShot=True, interval=SEEK_INTERVAL_MS, timeout=self._seek_now)

    # ── which video ───────────────────────────────────────────────────────────

    @property
    def pane(self) -> _Pane | None:
        i = self.camera.currentIndex()
        return self.panes[i] if 0 <= i < len(self.panes) else None

    @property
    def link(self) -> VideoLink | None:
        return self.pane.link if self.pane else None

    @property
    def links(self) -> list:
        return [p.link for p in self.panes]

    @property
    def player(self):
        """The selected camera's player."""
        return self.pane.player if self.pane else self._idle

    def set_links(self, links: list, cursor: float = 0.0):
        """Show a session's videos (possibly none)."""
        self.cursor = cursor
        for pane in self.panes:
            pane.close()
        self.panes = []
        self.camera.blockSignals(True)
        self.camera.clear()
        self.camera.blockSignals(False)
        for link in links:
            self._add_pane(link)
        self._arrange()
        self._select()

    def _add_pane(self, link: VideoLink) -> _Pane:
        pane = _Pane(link, self)
        pane.player.positionChanged.connect(lambda ms, pane=pane: self._position_changed(pane, ms))
        pane.player.durationChanged.connect(lambda ms, pane=pane: self._duration_changed(pane, ms))
        pane.player.errorOccurred.connect(lambda _e, text, pane=pane: self._say(
            f"{pane.link.camera or 'The video'} could not be played: {text}"))
        self.panes.append(pane)
        self.camera.blockSignals(True)
        self.camera.addItem(link.camera or Path(link.path).name)
        self.camera.blockSignals(False)
        pane.load()
        return pane

    def _arrange(self):
        """Lay the cameras out side by side; with more than four, the selected one and its neighbours."""
        selected = max(self.camera.currentIndex(), 0)
        first = min(max(selected - MAX_SHOWN + 1, 0), max(len(self.panes) - MAX_SHOWN, 0))
        shown = self.panes[first:first + MAX_SHOWN]
        columns = 1 if len(shown) <= 1 else 2
        for i, pane in enumerate(self.panes):
            self.grid.removeWidget(pane.box)
            pane.box.setVisible(pane in shown)
            pane.caption.setStyleSheet("font-weight: 600;" if i == selected and len(self.panes) > 1 else "")
        for k, pane in enumerate(shown):
            self.grid.addWidget(pane.box, k // columns, k % columns)

    def add_video(self, path: str | Path, camera: str = "") -> VideoLink:
        link = VideoLink(str(path), camera=camera or Path(path).stem)
        self._add_pane(link)
        self.camera.setCurrentIndex(len(self.panes) - 1)
        self._arrange()
        self._select()
        self.changed.emit()
        return link

    def remove_video(self):
        i = self.camera.currentIndex()
        if 0 <= i < len(self.panes):
            self.panes.pop(i).close()
            self.camera.removeItem(i)
            self._arrange()
            self._select()
            self.changed.emit()

    def relink(self, path: str | Path):
        if self.pane:
            self.pane.link.relink(path)
            self.pane.caption.setText(self.pane.link.camera or Path(path).name)
            self.pane.load()
            self._select()
            self.changed.emit()

    def _select(self, *_):
        """Another camera was chosen: the controls and the sync shown are now its."""
        self._arrange()
        link = self.link
        if link is None:
            self._say("No video. The session works without one.")
            self._show_anchors()
            return
        for widget, value in ((self.offset, link.offset), (self.piecewise, link.piecewise)):
            widget.blockSignals(True)
            widget.setValue(value) if widget is self.offset else widget.setChecked(value)
            widget.blockSignals(False)
        self.slider.setMaximum(max(self.player.duration(), 0))
        if not link.exists():
            self._say(f"The video file is not where it was: {link.path}. Use Relink… to point at it; the sync is kept.")
            self._show_anchors(status=False)
            return
        self._show_anchors()
        self.follow_cursor(self.cursor)

    def _choose(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Add a video", "", "Video (*.mp4 *.mov *.m4v *.mkv *.avi);;All files (*)")
        if path:
            self.add_video(path)

    def _choose_relink(self):
        if self.link:
            path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Where is this video now?", "", "Video (*.mp4 *.mov *.m4v *.mkv *.avi);;All files (*)")
            if path:
                self.relink(path)

    # ── playback → cursor ─────────────────────────────────────────────────────

    def playing(self) -> bool:
        return self.player.playbackState() == QtMultimedia.QMediaPlayer.PlayingState

    def toggle(self):
        """Play or pause every camera together."""
        if self.playing():
            for pane in self.panes:
                pane.player.pause()
            self.play.setText("Play")
        elif self.link and self.link.exists():
            for pane in self.panes:
                if pane.link.exists():
                    pane.player.play()
            self.play.setText("Pause")

    def _duration_changed(self, pane: _Pane, ms: int):
        if ms > 0:
            pane.link.metadata["duration_s"] = ms / 1000.0
            rate = pane.player.metaData().value(QtMultimedia.QMediaMetaData.Key.VideoFrameRate)
            if rate:
                pane.link.metadata["fps"] = float(rate)
        if pane is self.pane:
            self.slider.setMaximum(max(ms, 0))
            self._show_anchors()

    def _position_changed(self, pane: _Pane, ms: int):
        if pane is not self.pane:
            return                                 # only the selected camera speaks for the cursor
        if not self.slider.isSliderDown():
            self.slider.setValue(ms)
        t = float(pane.link.to_telemetry(ms / 1000.0))
        self.clock.setText(f"video {ms / 1000:.2f} s  =  data {t:.2f} s")
        if self._from_cursor:                      # the video moved because the cursor did: do not send it back
            return
        self.cursor = t
        self.cursor_from_video.emit(t)
        if self.playing():
            self._hold_others(t)

    def _hold_others(self, t: float):
        """During playback, pull any other camera back to where its own sync puts this moment."""
        for other in self.panes:
            if other is self.pane or not other.link.exists():
                continue
            want = float(other.link.to_video(t))
            length = other.player.duration() / 1000.0
            if 0 <= want <= (length or want) and abs(other.player.position() / 1000.0 - want) > RESYNC_S:
                other.player.setPosition(int(round(want * 1000)))

    # ── cursor → playback ─────────────────────────────────────────────────────

    def follow_cursor(self, t: float):
        """The cursor moved in the data: bring every camera to the same moment.
        Seeks are spaced out, so dragging the cursor does not flood the decoders."""
        self.cursor = float(t)
        if not self.panes or self.playing():
            return
        self._pending = self.cursor
        if not self._seek_timer.isActive():
            self._seek_timer.start()

    def _seek_now(self):
        if self._pending is None:
            return
        t, self._pending = self._pending, None
        self._from_cursor = True
        for pane in self.panes:
            if not pane.link.exists():
                continue
            v = float(pane.link.to_video(t))
            length = pane.player.duration() / 1000.0
            if v < 0 or (length > 0 and v > length):
                if pane is self.pane:
                    self.clock.setText("the cursor is outside this video")
                continue
            pane.player.setPosition(int(round(v * 1000)))
        QtCore.QTimer.singleShot(SEEK_INTERVAL_MS, lambda: setattr(self, "_from_cursor", False))

    # ── sync ──────────────────────────────────────────────────────────────────

    def _offset_edited(self, value: float):
        if self.link:
            self.link.set_manual_offset(value)
            self._show_anchors()
            self.follow_cursor(self.cursor)
            self.changed.emit()

    def _piecewise_toggled(self, on: bool):
        if self.link:
            self.link.set_piecewise(on)
            self._after_fit()
            self.follow_cursor(self.cursor)

    def add_anchor(self, label: str = "", video_time: float | None = None, telemetry_time: float | None = None):
        """Pin the frame on screen to the cursor's moment (or the two times given)."""
        if not self.link:
            return
        v = self.player.position() / 1000.0 if video_time is None else video_time
        self.link.add_anchor(v, self.cursor if telemetry_time is None else telemetry_time, label)
        self.what.clear()
        self._after_fit()

    def remove_anchor(self, index: int):
        if self.link and 0 <= index < len(self.link.anchors):
            self.link.remove_anchor(index)
            self._after_fit()

    def _after_fit(self):
        self.offset.blockSignals(True)
        self.offset.setValue(self.link.offset)
        self.offset.blockSignals(False)
        self._show_anchors()
        self.changed.emit()

    def _show_anchors(self, status: bool = True):
        link = self.link
        rows = link.anchors if link else []
        res = link.residuals() if link else []
        self.anchors.setRowCount(len(rows))
        for r, a in enumerate(rows):
            for k, text in enumerate((f"{a.video_time:.3f}", f"{a.telemetry_time:.3f}", f"{res[r]:+.3f}", a.label)):
                self.anchors.setItem(r, k, QtWidgets.QTableWidgetItem(text))
        if link and status:
            self._say(link.status()["text"])

    def _say(self, text: str):
        self.status.setText(text)
