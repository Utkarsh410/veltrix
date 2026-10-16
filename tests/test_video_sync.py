"""Video sync: the sign convention, the fit from anchors, what "validated" means, and persistence."""

import numpy as np
import pytest

from veltrix_app.video_sync import VideoLink, VideoStore


def test_sign_convention_telemetry_time_is_video_time_plus_offset():
    late = VideoLink("onboard.mp4", offset=12.5)          # the camera started 12.5 s into the log
    assert late.to_telemetry(0.0) == 12.5 and late.to_telemetry(30.0) == 42.5
    assert late.to_video(42.5) == 30.0 and late.to_video(0.0) == -12.5, "before the video began"
    early = VideoLink("onboard.mp4", offset=-4.0)         # the camera was already running
    assert early.to_telemetry(4.0) == 0.0 and early.to_video(10.0) == 14.0
    assert np.allclose(late.to_video(late.to_telemetry([0.0, 1.5, 99.0])), [0.0, 1.5, 99.0]), "the two directions agree"


def test_manual_offset_is_never_called_validated():
    link = VideoLink("a.mp4", metadata={"fps": 30.0})
    link.set_manual_offset(3.2)
    s = link.status()
    assert not s["validated"] and s["uncertainty_s"] is None and s["text"].startswith("Not validated: manual offset of +3.200 s")
    assert "One frame is 33.3 ms" in s["text"] and "not how good the sync is" in s["text"]
    one = link.add_anchor(10.0, 13.5, "brake onset T1")
    assert link.offset == 3.5 and link.method == "anchors" and not one["validated"] and "single anchor" in one["text"]


def test_anchors_that_agree_give_an_offset_and_an_uncertainty():
    link = VideoLink("a.mp4")
    for v, err, label in ((20.0, 0.01, "brake T1"), (95.0, -0.02, "gear change"), (240.0, 0.01, "line crossing")):
        status = link.add_anchor(v, v + 7.30 + err, label)
    assert link.rate == 1.0 and link.offset == pytest.approx(7.30, abs=0.01), "three anchors, no drift: offset only"
    assert status["validated"] and status["model"] == "offset" and status["uncertainty_s"] == pytest.approx(0.0141, abs=0.002)
    assert np.allclose(link.residuals(), [-0.01, 0.02, -0.01], atol=1e-9)
    assert "Offset from 3 anchors: offset +7.300 s. Anchors agree to ±0.014 s" in status["text"]


def test_drift_is_fitted_only_when_the_anchors_need_it():
    rate, offset = 1.0005, 5.0                              # the camera's clock runs 500 ppm fast: 0.3 s over ten minutes
    link = VideoLink("a.mp4")
    for v in (10.0, 200.0, 400.0, 590.0):
        status = link.add_anchor(v, rate * v + offset)
    assert link.rate == pytest.approx(rate, rel=1e-6) and link.offset == pytest.approx(offset, abs=1e-6)
    assert status["model"] == "offset and clock rate" and "+500 ppm" in status["text"] and status["validated"]
    assert link.to_telemetry(590.0) == pytest.approx(rate * 590 + offset)

    close = VideoLink("a.mp4")                              # the same drift over 20 s is invisible: do not fit it
    for v in (10.0, 20.0, 30.0):
        close.add_anchor(v, rate * v + offset)
    assert close.rate == 1.0, "anchors must span enough time before a rate is believed"


def test_an_anchor_that_disagrees_is_named_and_the_sync_is_not_validated():
    link = VideoLink("a.mp4", tolerance_s=0.1)
    link.add_anchor(10.0, 17.0, "brake T1")
    link.add_anchor(50.0, 57.0, "gear change")
    status = link.add_anchor(90.0, 98.5, "wrong lap")       # 1.5 s out
    assert not status["validated"] and status["disagree"] and "wrong lap" in status["text"]
    assert "more than 0.1 s from the fit" in status["text"]
    fixed = link.remove_anchor(2)
    assert fixed["validated"] and link.offset == pytest.approx(7.0)
    assert link.remove_anchor(0)["anchors"] == 1 and link.remove_anchor(0)["model"] == "manual offset"


def test_links_are_saved_per_source_file_and_can_be_relinked(tmp_path):
    store = VideoStore(tmp_path / "videos")
    link = VideoLink(str(tmp_path / "gone.mp4"), camera="onboard", metadata={"fps": 50.0, "duration_s": 600.0})
    link.add_anchor(10.0, 17.0, "brake T1")
    link.add_anchor(300.0, 307.02, "line")
    store.save("abc123", [link, VideoLink("rear.mp4", camera="rear", offset=-2.0)])
    back = store.load("abc123")
    assert [v.camera for v in back] == ["onboard", "rear"] and back[0].to_dict() == link.to_dict()
    assert back[0].anchors[1].label == "line" and back[0].status()["validated"] and back[1].offset == -2.0
    assert not back[0].exists()
    moved = tmp_path / "found.mp4"
    moved.write_bytes(b"x")
    back[0].relink(moved)
    assert back[0].exists() and back[0].offset == link.offset, "relinking keeps the sync"
    assert store.load("neverseen") == [] and store.load("../x") == []
    with pytest.raises(ValueError):
        store.save("../x", back)


# ── the player, offscreen ─────────────────────────────────────────────────────

def make_video(path, seconds=6, fps=25):
    """A small real video, encoded by Qt itself (no ffmpeg on the machine needed).
    Synthetic: a solid colour that changes every frame with the time written on it."""
    from PySide6 import QtCore, QtGui, QtMultimedia, QtWidgets
    app = QtWidgets.QApplication.instance()
    session = QtMultimedia.QMediaCaptureSession()
    fmt = QtMultimedia.QVideoFrameFormat(QtCore.QSize(320, 240), QtMultimedia.QVideoFrameFormat.PixelFormat.Format_BGRA8888)
    source, recorder = QtMultimedia.QVideoFrameInput(fmt), QtMultimedia.QMediaRecorder()
    session.setVideoFrameInput(source)
    session.setRecorder(recorder)
    recorder.setOutputLocation(QtCore.QUrl.fromLocalFile(str(path)))
    recorder.setVideoFrameRate(fps)
    sent, total, loop = [0], seconds * fps, QtCore.QEventLoop()

    def push():
        while sent[0] < total:
            image = QtGui.QImage(320, 240, QtGui.QImage.Format_ARGB32)
            image.fill(QtGui.QColor.fromHsv((sent[0] * 3) % 360, 200, 200))
            frame = QtMultimedia.QVideoFrame(image)
            frame.setStartTime(int(sent[0] * 1e6 / fps))
            frame.setEndTime(int((sent[0] + 1) * 1e6 / fps))
            if not source.sendVideoFrame(frame):
                return
            sent[0] += 1
        source.sendVideoFrame(QtMultimedia.QVideoFrame())
        recorder.stop()
    source.readyToSendVideoFrame.connect(push)
    recorder.recorderStateChanged.connect(
        lambda state: state == QtMultimedia.QMediaRecorder.StoppedState and sent[0] >= total and loop.quit())
    recorder.record()
    QtCore.QTimer.singleShot(200, push)
    QtCore.QTimer.singleShot(30000, loop.quit)
    loop.exec()
    return path if sent[0] >= total and path.exists() and path.stat().st_size > 1000 else None


@pytest.fixture(scope="module")
def qt():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture(scope="module")
def video(qt, tmp_path_factory):
    path = make_video(tmp_path_factory.mktemp("video") / "onboard.mp4")
    if path is None:
        pytest.skip("this machine could not encode a test video")
    return path


def wait(done, seconds=20):
    from PySide6 import QtCore
    loop, timer = QtCore.QEventLoop(), QtCore.QElapsedTimer()
    timer.start()
    while not done() and timer.elapsed() < seconds * 1000:
        loop.processEvents(QtCore.QEventLoop.AllEvents, 30)
    return done()


def test_video_and_cursor_drive_each_other(qt, video, tmp_path):
    from conftest import GOLDEN
    from PySide6 import QtMultimedia
    from veltrix_app.window import MainWindow
    from veltrix_engine import load_session
    w = MainWindow(tmp_path / "maths.json")
    w.show()
    s = load_session(GOLDEN / "logger_layout_3lap_20hz.csv")
    w.show_session(s)
    dock = w.video
    assert dock.link is None and "works without one" in dock.status.text(), "no video is a normal state"
    w.set_cursor(50.0)
    assert w.ws.cursor == 50.0

    link = dock.add_video(video, "onboard")
    assert wait(lambda: dock.player.duration() > 0), "the video loaded"
    assert link.metadata["duration_s"] == pytest.approx(6.0, abs=0.3)
    dock.offset.setValue(100.0)                              # video second 0 is telemetry second 100
    assert link.offset == 100.0 and dock.status.text().startswith("Not validated: manual offset of +100.000 s")

    w.set_cursor(103.0)                                      # the cursor moves: the video is sought
    assert wait(lambda: abs(dock.player.position() - 3000) <= 80), dock.player.position()
    assert w.ws.cursor == pytest.approx(103.0), "seeking the video does not push the cursor back"
    w.set_cursor(500.0)
    assert wait(lambda: "outside this video" in dock.clock.text())

    w.set_cursor(101.0)
    wait(lambda: abs(dock.player.position() - 1000) <= 80)
    dock.toggle()                                            # play: now the video moves the cursor
    assert wait(lambda: w.ws.cursor > 102.0), w.ws.cursor
    dock.toggle()
    assert dock.player.playbackState() != QtMultimedia.QMediaPlayer.PlayingState
    assert w.ws.cursor == pytest.approx(100.0 + dock.player.position() / 1000, abs=0.1)
    assert all(c.value() == pytest.approx(w.ws.cursor) for c in w._cursors), "charts follow the playback"

    dock.add_anchor("brake onset", video_time=1.0, telemetry_time=101.5)
    dock.add_anchor("line crossing", video_time=5.0, telemetry_time=105.52)
    assert link.method == "anchors" and link.offset == pytest.approx(100.51) and dock.offset.value() == pytest.approx(100.51)
    assert dock.anchors.rowCount() == 2 and "Offset from 2 anchors" in dock.status.text()
    w.close()

    again = MainWindow(tmp_path / "maths.json")              # another day: the link and its sync are back
    again.show_session(s)
    assert [v.camera for v in again.video.links] == ["onboard"] and again.video.link.offset == pytest.approx(100.51)
    assert len(again.video.link.anchors) == 2
    again.close()


def test_a_missing_video_is_reported_and_can_be_relinked(qt, video, tmp_path):
    import shutil
    from conftest import GOLDEN
    from veltrix_app.video_sync import VideoLink
    from veltrix_app.window import MainWindow
    from veltrix_engine import load_session
    s = load_session(GOLDEN / "logger_layout_3lap_20hz.csv")
    w = MainWindow(tmp_path / "maths.json")
    gone = VideoLink(str(tmp_path / "moved_away.mp4"), camera="onboard", offset=12.0)
    gone.add_anchor(1.0, 13.0, "start")
    w.videos.save(s.raw.sha256, [gone])
    w.show_session(s)
    assert "not where it was" in w.video.status.text() and "Relink" in w.video.status.text()
    w.set_cursor(40.0)
    assert w.ws.cursor == 40.0 and len(w._plots) == 5, "everything else works without the file"
    found = tmp_path / "found.mp4"
    shutil.copyfile(video, found)
    w.video.relink(found)
    assert wait(lambda: w.video.player.duration() > 0) and w.video.link.offset == 12.0 and len(w.video.link.anchors) == 1
    assert w.videos.load(s.raw.sha256)[0].path == str(found)
    w.video.remove_video()
    assert w.video.link is None and w.videos.load(s.raw.sha256) == []
    w.close()


def test_piecewise_sync_follows_a_recording_that_does_not_drift_evenly():
    """A camera that dropped two seconds of frames at video second 300: one
    offset before it, another after. No single straight line fits both."""
    truth = lambda v: v + 10.0 if v < 300 else v + 12.0
    link = VideoLink("a.mp4", tolerance_s=0.1)
    for v in (20.0, 150.0, 280.0, 320.0, 450.0, 580.0):
        status = link.add_anchor(v, truth(v), f"event at {v:.0f}")
    assert not status["validated"] and status["disagree"], "one line through all six misses several of them"
    assert max(abs(r) for r in link.residuals()) > 0.3

    status = link.set_piecewise(True)
    assert link.method == "piecewise" and status["model"] == "piecewise"
    assert all(abs(float(link.to_telemetry(v)) - truth(v)) < 1e-9 for v in (20.0, 100.0, 280.0, 320.0, 500.0, 580.0))
    assert float(link.to_telemetry(700.0)) == pytest.approx(712.0) and float(link.to_telemetry(5.0)) == pytest.approx(15.0), \
        "beyond the anchors it carries on one for one"
    assert float(link.to_video(link.to_telemetry(444.4))) == pytest.approx(444.4), "both directions agree"
    assert "Leaving each anchor out in turn" in status["text"] and status["uncertainty_s"] > 0.3, \
        "the two anchors beside the jump cannot be predicted from the others, and the uncertainty says so"
    assert status["disagree"] and not status["validated"]

    back = VideoLink.from_dict(link.to_dict())
    assert back.piecewise and back.method == "piecewise" and float(back.to_telemetry(500.0)) == pytest.approx(512.0)
    assert link.set_piecewise(False)["model"] in ("offset", "offset and clock rate") and link.method == "anchors"
    few = VideoLink("b.mp4", piecewise=True)
    few.add_anchor(1.0, 5.0)
    assert few.add_anchor(9.0, 13.0)["model"] == "offset", "two anchors are not enough to go piecewise"


def test_two_cameras_are_shown_together_each_on_its_own_sync(qt, video, tmp_path):
    import shutil
    from conftest import GOLDEN
    from veltrix_app.window import MainWindow
    from veltrix_engine import load_session
    rear = tmp_path / "rear.mp4"
    shutil.copyfile(video, rear)
    w = MainWindow(tmp_path / "maths.json")
    w.resize(1400, 900)
    w.show()
    s = load_session(GOLDEN / "logger_layout_3lap_20hz.csv")
    w.show_session(s)
    dock = w.video
    dock.add_video(video, "onboard")
    dock.add_video(rear, "rear")
    assert wait(lambda: all(p.player.duration() > 0 for p in dock.panes)), "both loaded"
    assert len(dock.panes) == 2 and dock.link.camera == "rear" and dock.grid.count() == 2, "both pictures are in the panel"
    dock.offset.setValue(102.0)                               # the rear camera started two seconds after the onboard
    dock.camera.setCurrentIndex(0)
    dock.offset.setValue(100.0)
    assert [link.offset for link in dock.links] == [100.0, 102.0] and dock.link.camera == "onboard"

    w.set_cursor(104.0)                                       # one moment in the data …
    onboard, back = dock.panes
    assert wait(lambda: abs(onboard.player.position() - 4000) <= 80 and abs(back.player.position() - 2000) <= 80), \
        (onboard.player.position(), back.player.position())   # … is a different second of each recording
    assert w.ws.cursor == pytest.approx(104.0)

    w.set_cursor(100.5)
    wait(lambda: abs(onboard.player.position() - 500) <= 80)
    dock.toggle()                                             # play: both run, the selected camera moves the cursor
    assert wait(lambda: w.ws.cursor > 102.5), w.ws.cursor
    dock.toggle()
    from PySide6 import QtMultimedia
    assert not dock.playing() and back.player.playbackState() != QtMultimedia.QMediaPlayer.PlayingState
    t = w.ws.cursor
    assert onboard.player.position() / 1000 == pytest.approx(t - 100.0, abs=0.15)
    assert back.player.position() / 1000 == pytest.approx(max(t - 102.0, 0.0), abs=0.6), "the other camera kept to its own sync"

    dock.camera.setCurrentIndex(1)                            # the controls now belong to the rear camera
    assert dock.offset.value() == 102.0 and "manual offset of +102.000 s" in dock.status.text()
    for v in (1.0, 3.0, 5.0):
        dock.add_anchor(f"event {v:.0f}", video_time=v, telemetry_time=v + (102.0 if v < 4 else 102.5))
    dock.piecewise.setChecked(True)
    assert dock.link.method == "piecewise" and "Piecewise through 3 anchors" in dock.status.text()
    assert float(dock.link.to_telemetry(5.0)) == pytest.approx(107.5) and dock.links[0].method == "manual"
    w.close()

    again = MainWindow(tmp_path / "maths.json")
    again.show_session(s)
    assert [(v.camera, v.method) for v in again.video.links] == [("onboard", "manual"), ("rear", "piecewise")]
    assert again.video.piecewise.isChecked() is False and again.video.link.camera == "onboard"
    again.video.camera.setCurrentIndex(1)
    assert again.video.piecewise.isChecked() and again.video.anchors.rowCount() == 3
    again.close()
