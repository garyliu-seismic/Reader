"""AudioPlayerView：QMediaPlayer + QAudioOutput 的有声书播放视图（offscreen）。
没有 QtMultimedia / 没有音频输出设备时整体跳过。测试音频是用 wave 模块生成的 8 秒小 WAV。"""
import math
import os
import struct
import time
import wave

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
pytest.importorskip("PySide6.QtMultimedia")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from pyreader import audioplayer as ap  # noqa: E402
from pyreader import mediautil as mu  # noqa: E402

SECONDS = 8
LEN_MS = SECONDS * 1000


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    if not ap.audio_available():
        pytest.skip("no audio output device / backend")
    return app


def wait_for(cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        QApplication.processEvents()
        if cond():
            return True
        time.sleep(0.005)
    QApplication.processEvents()
    return cond()


def write_wav(path, seconds=SECONDS):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"".join(struct.pack("<h", int(3000 * math.sin(i * 0.2))) for i in range(8000 * seconds)))
    return str(path)


def add_info_tags(path, title, artist):
    """给 WAV 末尾追加 LIST/INFO 块（INAM=标题、IART=艺术家），并修正 RIFF 总长；FFmpeg 会把它读成媒体标签。"""
    def sub(tag, text):
        b = text.encode("utf-8") + b"\0"
        return tag + struct.pack("<I", len(b)) + b + (b"\0" if len(b) % 2 else b"")
    body = b"INFO" + sub(b"INAM", title) + sub(b"IART", artist)
    with open(path, "ab") as f:
        f.write(b"LIST" + struct.pack("<I", len(body)) + body)
    size = os.path.getsize(path) - 8
    with open(path, "r+b") as f:
        f.seek(4)
        f.write(struct.pack("<I", size))


@pytest.fixture
def wav(tmp_path):
    return write_wav(tmp_path / "章节一.wav")


@pytest.fixture
def view():
    v = ap.AudioPlayerView()
    v.resize(600, 500)
    yield v
    v.close_audio()
    v.deleteLater()


def loaded(v):
    return v.duration_ms() > 0


def test_open_loads_duration_and_filename_title(view, wav):
    assert view.open_audio(wav) is True
    assert wait_for(lambda: loaded(view))
    assert abs(view.duration_ms() - LEN_MS) < 300
    assert view._title.text() == "章节一"                  # 没有标签 → 退回文件名（不含扩展名）
    assert view._artist.text() == ""
    assert view._tot.text() == mu.fmt_time(view.duration_ms())
    assert view._seek.maximum() == view.duration_ms()
    assert view.position_ms() == 0
    assert not view.is_playing()


def test_metadata_title_artist_from_tags(view, tmp_path):
    p = write_wav(tmp_path / "x.wav", 3)
    add_info_tags(p, "My Book", "Alice")
    assert view.open_audio(p)
    assert wait_for(lambda: view._artist.text() == "Alice")
    assert view._title.text() == "My Book"
    assert view._cover.pixmap().isNull() and view._cover.text()      # 没有封面 → 占位符，不崩


def test_open_without_audio_device(view, wav, monkeypatch):
    monkeypatch.setattr(ap, "audio_available", lambda: False)
    assert view.open_audio(wav) is False
    assert view.path() == "" and not view._error.isHidden()
    assert not view._btn_play.isEnabled()


def test_open_missing_file_and_no_state_leak(view, wav, tmp_path):
    assert view.open_audio(wav)
    assert wait_for(lambda: loaded(view))
    assert view.open_audio(str(tmp_path / "nope.mp3")) is False
    assert view.path() == ""
    assert view.duration_ms() == 0 and view.position_ms() == 0
    assert not view._error.isHidden()
    assert not view._btn_play.isEnabled()


def test_resume_position_applied_after_load(view, wav):
    assert view.open_audio(wav, position_ms=4000)
    assert wait_for(lambda: loaded(view) and abs(view.position_ms() - 4000) < 400)


def test_resume_position_clamped_to_end_minus_3s(view, wav):
    assert view.open_audio(wav, position_ms=10**7)
    assert wait_for(lambda: loaded(view) and view.position_ms() > 0)
    assert abs(view.position_ms() - (view.duration_ms() - 3000)) < 400


def test_skip_and_seek(view, wav):
    assert view.open_audio(wav)
    assert wait_for(lambda: loaded(view))
    view.seek(5000)
    assert wait_for(lambda: abs(view.position_ms() - 5000) < 300)
    view.skip(-mu.SKIP_BACK_MS)                          # 5s - 15s 夹到 0
    assert wait_for(lambda: view.position_ms() < 300)
    view.skip(mu.SKIP_FWD_MS)                            # 0 + 30s 夹到末尾
    assert wait_for(lambda: view.position_ms() > view.duration_ms() - 500)


def test_play_pause_signals_and_button(view, wav):
    states = []
    view.playbackStateChanged.connect(states.append)
    assert view.open_audio(wav)
    assert wait_for(lambda: loaded(view))
    view.toggle_play()
    assert wait_for(lambda: view.is_playing())
    assert view._btn_play.text() == "暂停"
    view.toggle_play()
    assert wait_for(lambda: not view.is_playing())
    assert view._btn_play.text() == "播放"
    assert states[-2:] == [True, False]


def test_final_position_emitted_on_close(view, wav):
    got = []
    view.positionChanged.connect(lambda p, d: got.append((p, d)))
    assert view.open_audio(wav, position_ms=4000)
    assert wait_for(lambda: loaded(view) and abs(view.position_ms() - 4000) < 400)
    got.clear()
    view.close_audio()
    assert len(got) == 1
    pos, dur = got[0]
    assert abs(pos - 4000) < 400 and abs(dur - LEN_MS) < 300
    assert mu.should_save_resume(pos, dur)
    assert view.position_ms() == 0 and view.path() == ""


def test_position_signal_is_throttled(view, wav):
    got = []
    assert view.open_audio(wav)
    assert wait_for(lambda: loaded(view))
    view.positionChanged.connect(lambda p, d: got.append(p))
    for _ in range(50):
        view._emit_position()
    assert len(got) <= 1                                 # 1 秒窗口内最多一次（之前刚发过则 0 次）
    n = len(got)
    view._emit_position(force=True)
    assert len(got) == n + 1


def test_speed_list_and_set(view, wav):
    assert [view._speed.itemData(i) for i in range(view._speed.count())] == list(mu.SPEEDS)
    assert view.speed() == 1.0
    assert view.open_audio(wav)
    view.set_speed(2.0)
    assert view.speed() == 2.0 and view._player.playbackRate() == 2.0
    view.set_speed(1.1)                                  # 不在列表里 → 最近一档
    assert view.speed() == 1.0
    view.set_speed(3.0)
    assert view.open_audio(wav)                          # 换文件沿用当前倍速
    assert view._player.playbackRate() == 3.0


def test_volume(view):
    view.set_volume(50)
    assert view.volume() == 50 and view._vol.value() == 50
    assert 0.0 < view._audio.volume() < 0.5              # 感知刻度：50% 刻度 < 50% 线性
    view.set_volume(500)
    assert view.volume() == 100 and view._audio.volume() == pytest.approx(1.0)
    view.set_volume(-3)
    assert view.volume() == 0 and view._audio.volume() == 0.0


def test_keyboard(view, wav):
    view.show()
    backs = []
    view.backRequested.connect(lambda: backs.append(1))
    assert view.open_audio(wav, position_ms=4000)
    assert wait_for(lambda: loaded(view) and abs(view.position_ms() - 4000) < 400)
    QTest.keyClick(view, Qt.Key.Key_Left)
    assert wait_for(lambda: view.position_ms() < 400)    # 4s - 15s 夹到 0
    QTest.keyClick(view, Qt.Key.Key_Right)
    assert wait_for(lambda: view.position_ms() > view.duration_ms() - 500)
    v0 = view.volume()
    QTest.keyClick(view, Qt.Key.Key_Down)
    assert view.volume() == v0 - ap.VOLUME_STEP
    QTest.keyClick(view, Qt.Key.Key_BracketRight)
    assert view.speed() == 1.25
    QTest.keyClick(view, Qt.Key.Key_Space)
    assert wait_for(lambda: view.is_playing() or view.position_ms() >= view.duration_ms() - 50)
    QTest.keyClick(view, Qt.Key.Key_Space)
    QTest.keyClick(view, Qt.Key.Key_Escape)
    assert backs == [1]


def test_undecodable_file_reports_error(view, tmp_path):
    bad = tmp_path / "bad.mp3"
    bad.write_bytes(b"this is not audio" * 100)
    errs = []
    view.failed.connect(errs.append)
    assert view.open_audio(str(bad)) is True             # 解码是异步的：先接受，随后报错
    assert wait_for(lambda: errs, timeout=15)
    assert not view._error.isHidden()
    assert view._error.text().startswith("无法播放")
    assert not view._btn_play.isEnabled()
    view.play()                                          # 失败态下 play 无效、不崩
    assert not view.is_playing()
    # 再打开好文件应复原
    assert view.open_audio(write_wav(tmp_path / "ok.wav", 4))
    assert view._error.isHidden() and view._btn_play.isEnabled()


def test_close_audio_releases_file(view, wav):
    assert view.open_audio(wav, position_ms=0, autoplay=True)
    assert wait_for(lambda: view.is_playing() or loaded(view))
    view.close_audio()
    wait_for(lambda: False, timeout=0.3)                 # 让后端线程收尾
    os.rename(wav, wav + ".moved")                       # Windows 上文件仍被占用会抛 PermissionError
    os.remove(wav + ".moved")
    assert not view._btn_play.isEnabled()


def test_seek_slider_click_jumps_and_user_seek_signals(view, wav):
    from PySide6.QtCore import QPoint
    seeks = []
    view.seeked.connect(lambda: seeks.append(1))
    view.resize(600, 400)
    view.show()
    assert view.open_audio(wav, 0)
    assert wait_for(lambda: view.duration_ms() > 0)
    assert seeks == []                                                  # 载入 / 续播到断点不算用户跳转
    s = view._seek
    assert s.pageStep() >= 10000
    QTest.mouseClick(s, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(int(s.width() * 0.75), s.height() // 2))
    assert wait_for(lambda: abs(view.position_ms() - 0.75 * view.duration_ms()) < 1500)
    assert seeks
