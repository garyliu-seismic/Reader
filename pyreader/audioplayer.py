"""有声书 / 音频播放视图：QMediaPlayer + QAudioOutput（FFmpeg 后端）。
不用 QtMultimediaWidgets（打包时已排除），封面 / 标题 / 艺术家取自媒体标签，缺了就退回文件名。

用法（由 MainWindow 集成）：
    view = AudioPlayerView()
    ok = view.open_audio(path, position_ms=saved)     # False：文件不存在 / 没有音频输出设备；解码失败走 failed 信号
    view.positionChanged  (pos_ms, duration_ms)       # 节流到约 1 次 / 秒；暂停 / 跳转 / 播完 / 关闭时必发一次
    view.playbackStateChanged (playing)
    view.backRequested                                 # Esc：回到书架
    view.close_audio()                                 # 停止并释放文件（Windows 上否则文件被占用，删不掉 / 改不了名）
断点续播的取舍用 mediautil.should_save_resume(pos, length) 判断。
"""
import os
import time
from typing import Optional

from PySide6.QtCore import QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtMultimedia import QAudio, QAudioOutput, QMediaDevices, QMediaMetaData, QMediaPlayer
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QSlider, QStyle, QVBoxLayout, QWidget)

from . import mediautil as mu

POS_EMIT_MS = 1000       # positionChanged 信号的最小间隔（界面本身随播放器实时刷新）
COVER_SIZE = 220
VOLUME_STEP = 5          # 上下方向键每次调的音量百分点


def audio_available() -> bool:
    """有没有可用的音频输出设备（没有就别尝试播放；测试也据此跳过）。"""
    try:
        return len(QMediaDevices.audioOutputs()) > 0
    except Exception:
        return False


class _SeekSlider(QSlider):
    """点哪儿跳哪儿：先把值设到点击处，再交给基类，这时点击正好落在滑块上，接着拖动也照常。"""

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.maximum() > self.minimum():
            self.setValue(QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), int(e.position().x()), max(1, self.width())))
        super().mousePressEvent(e)


class AudioPlayerView(QWidget):
    positionChanged = Signal(int, int)       # (位置 ms, 总长 ms)
    playbackStateChanged = Signal(bool)      # True = 正在播放
    backRequested = Signal()
    failed = Signal(str)                     # 后端无法解码 / 打开（错误文字）
    seeked = Signal()                        # 用户主动跳转（按键 / 拖滑块 / 点滑块）；载入后自动续播到断点不发

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._path = ""
        self._pending_pos: Optional[int] = None     # 等媒体载入、知道总长后再跳的续播位置
        self._failed = False
        self._last_emit = 0.0
        self._syncing = False                       # 程序更新滑块时不触发跳转
        self._volume = 100                          # 百分比（滑块刻度，感知响度）

        self._audio = QAudioOutput(self)
        self._player = QMediaPlayer(self)
        self._player.setAudioOutput(self._audio)
        self._player.positionChanged.connect(self._on_position)
        self._player.durationChanged.connect(self._on_duration)
        self._player.playbackStateChanged.connect(self._on_state)
        self._player.mediaStatusChanged.connect(self._on_status)
        self._player.metaDataChanged.connect(self._refresh_meta)
        self._player.errorOccurred.connect(self._on_error)

        self._build_ui()
        self.set_volume(self._volume)
        self._reset_meta()
        self._set_controls_enabled(False)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        self._cover = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        self._cover.setFixedSize(COVER_SIZE, COVER_SIZE)
        self._cover.setStyleSheet("background:#2a2a30;color:#6c6c78;font-size:72px;border-radius:8px;")
        self._title = QLabel(alignment=Qt.AlignmentFlag.AlignCenter, wordWrap=True)
        self._title.setStyleSheet("font-size:20px;font-weight:bold;")
        self._artist = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        self._artist.setStyleSheet("color:#8a8a96;")
        for lb in (self._title, self._artist):
            lb.setTextFormat(Qt.TextFormat.PlainText)       # 标签内容来自文件，一律按纯文本
        self._error = QLabel(alignment=Qt.AlignmentFlag.AlignCenter, wordWrap=True)
        self._error.setTextFormat(Qt.TextFormat.PlainText)
        self._error.setStyleSheet("color:#d9534f;")
        self._error.hide()

        self._cur = QLabel(mu.fmt_time(0))
        self._tot = QLabel(mu.fmt_time(0))
        self._seek = _SeekSlider(Qt.Orientation.Horizontal)
        self._seek.setRange(0, 0)
        self._seek.setSingleStep(5000)                       # 量纲是毫秒：默认的 1 / 10 几乎不动
        self._seek.setPageStep(30000)
        self._seek.setTracking(False)                       # 拖动期间只更新时间标签，松手才真正跳转
        self._seek.sliderMoved.connect(lambda v: self._cur.setText(mu.fmt_time(v)))
        self._seek.valueChanged.connect(self._on_seek_value)
        seek_row = QHBoxLayout()
        seek_row.addWidget(self._cur)
        seek_row.addWidget(self._seek, 1)
        seek_row.addWidget(self._tot)

        self._btn_back = QPushButton("-%ds" % (mu.SKIP_BACK_MS // 1000))
        self._btn_back.setToolTip("后退 %d 秒（←）" % (mu.SKIP_BACK_MS // 1000))
        self._btn_back.clicked.connect(lambda: self.skip(-mu.SKIP_BACK_MS))
        self._btn_play = QPushButton("播放")
        self._btn_play.setMinimumWidth(72)
        self._btn_play.setToolTip("播放 / 暂停（空格）")
        self._btn_play.clicked.connect(self.toggle_play)
        self._btn_fwd = QPushButton("+%ds" % (mu.SKIP_FWD_MS // 1000))
        self._btn_fwd.setToolTip("前进 %d 秒（→）" % (mu.SKIP_FWD_MS // 1000))
        self._btn_fwd.clicked.connect(lambda: self.skip(mu.SKIP_FWD_MS))
        self._speed = QComboBox()
        for s in mu.SPEEDS:
            self._speed.addItem(mu.fmt_speed(s), s)
        self._speed.setCurrentIndex(mu.nearest_speed_index(mu.DEFAULT_SPEED))
        self._speed.setToolTip("倍速（[ / ]）")
        self._speed.currentIndexChanged.connect(lambda i: self._player.setPlaybackRate(self._speed.itemData(i)))
        self._vol = QSlider(Qt.Orientation.Horizontal)
        self._vol.setRange(0, 100)
        self._vol.setFixedWidth(110)
        self._vol.setToolTip("音量（↑ / ↓）")
        self._vol.valueChanged.connect(self.set_volume)
        ctl_row = QHBoxLayout()
        ctl_row.addStretch(1)
        for w in (self._speed, self._btn_back, self._btn_play, self._btn_fwd):
            ctl_row.addWidget(w)
        ctl_row.addSpacing(16)
        ctl_row.addWidget(QLabel("音量"))
        ctl_row.addWidget(self._vol)
        ctl_row.addStretch(1)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)
        lay.setSpacing(10)
        lay.addStretch(1)
        lay.addWidget(self._cover, 0, Qt.AlignmentFlag.AlignHCenter)
        lay.addWidget(self._title)
        lay.addWidget(self._artist)
        lay.addWidget(self._error)
        lay.addSpacing(12)
        lay.addLayout(seek_row)
        lay.addLayout(ctl_row)
        lay.addStretch(1)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        # 键盘全部由本控件处理：子控件不抢焦点，否则空格会触发按钮、方向键会被滑块 / 下拉框吃掉
        for w in (self._seek, self._vol, self._btn_back, self._btn_play, self._btn_fwd, self._speed):
            w.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def _set_controls_enabled(self, on: bool):
        for w in (self._seek, self._btn_back, self._btn_play, self._btn_fwd, self._speed):
            w.setEnabled(on)

    # ------------------------------------------------------------------ 公开 API
    def open_audio(self, path: str, position_ms: int = 0, autoplay: bool = False) -> bool:
        """打开一个音频文件并（可选）从 position_ms 续播。False = 文件不存在或没有音频输出设备；
        文件存在但后端解码不了时返回 True，之后经 failed 信号 / 错误标签报告（解码是异步的）。"""
        self._release(emit_final=True)
        self._failed = False
        self._error.hide()
        self._reset_meta()
        if not os.path.isfile(path):
            self._show_error("找不到文件：%s" % os.path.basename(path))
            return False
        if not audio_available():
            self._show_error("未检测到音频输出设备，无法播放")
            return False
        self._path = path
        self._title.setText(os.path.splitext(os.path.basename(path))[0])
        self._pending_pos = max(0, int(position_ms)) or None
        self._set_controls_enabled(True)
        self._player.setSource(QUrl.fromLocalFile(path))
        self._player.setPlaybackRate(self._speed.currentData())
        if autoplay:
            self._player.play()
        self.setFocus()
        return True

    def close_audio(self):
        """停止并释放媒体源（文件解除占用）。关闭前会补发一次 positionChanged，方便调用方存断点。"""
        self._release(emit_final=True)
        self._reset_meta()
        self._error.hide()
        self._failed = False

    def path(self) -> str:
        return self._path

    def position_ms(self) -> int:
        return self._player.position() if self._path else 0

    def duration_ms(self) -> int:
        return max(0, self._player.duration()) if self._path else 0

    def is_playing(self) -> bool:
        return self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    def play(self):
        if self._path and not self._failed:
            self._player.play()

    def pause(self):
        self._player.pause()

    def toggle_play(self):
        self.pause() if self.is_playing() else self.play()

    def seek(self, ms: int):
        if not self._path or self._failed:
            return
        self._pending_pos = None
        self._player.setPosition(mu.seek_target(0, ms, self.duration_ms()))
        self.seeked.emit()
        self._emit_position(force=True)

    def skip(self, delta_ms: int):
        self.seek(mu.seek_target(self.position_ms(), delta_ms, self.duration_ms()))

    def speed(self) -> float:
        return float(self._speed.currentData())

    def set_speed(self, rate: float):
        self._speed.setCurrentIndex(mu.nearest_speed_index(rate))     # 触发 currentIndexChanged → 设到播放器

    def volume(self) -> int:
        return self._vol.value()

    def set_volume(self, pct: int):
        """0-100，按感知响度刻度（QAudio.convertVolume）映射到线性音量。"""
        pct = max(0, min(100, int(pct)))
        self._audio.setVolume(QAudio.convertVolume(pct / 100.0, QAudio.VolumeScale.LogarithmicVolumeScale,
                                                   QAudio.VolumeScale.LinearVolumeScale))
        if self._vol.value() != pct:
            self._vol.setValue(pct)              # 回到本函数一次，值相同即停

    # ------------------------------------------------------------------ 内部
    def _release(self, emit_final: bool):
        if self._path and emit_final:
            self._emit_position(force=True)
        self._path = ""                          # 先清路径：stop() 触发的状态回调不再发位置信号
        self._pending_pos = None
        self._player.stop()
        self._player.setSource(QUrl())           # 真正放开文件句柄
        self._set_controls_enabled(False)
        self._sync_slider(0, 0)

    def _emit_position(self, force: bool = False):
        if not self._path:
            return
        now = time.monotonic()
        if not force and (now - self._last_emit) * 1000 < POS_EMIT_MS:
            return
        self._last_emit = now
        self.positionChanged.emit(self.position_ms(), self.duration_ms())

    def _sync_slider(self, pos: int, dur: int):
        self._syncing = True
        try:
            if self._seek.maximum() != dur:
                self._seek.setRange(0, dur)
            if not self._seek.isSliderDown():
                self._seek.setValue(min(pos, dur) if dur else 0)
                self._cur.setText(mu.fmt_time(pos))
            self._tot.setText(mu.fmt_time(dur))
        finally:
            self._syncing = False

    def _on_position(self, pos: int):
        self._sync_slider(pos, self.duration_ms())
        self._emit_position()

    def _on_duration(self, dur: int):
        self._sync_slider(self._player.position(), max(0, dur))

    def _on_seek_value(self, v: int):
        if not self._syncing and self._path:
            self.seek(v)

    def _on_state(self, st):
        playing = st == QMediaPlayer.PlaybackState.PlayingState
        self._btn_play.setText("暂停" if playing else "播放")
        if not playing:
            self._emit_position(force=True)
        self.playbackStateChanged.emit(playing)

    def _on_status(self, st):
        S = QMediaPlayer.MediaStatus
        if st in (S.LoadedMedia, S.BufferedMedia):
            self._apply_pending()
            self._refresh_meta()
        elif st == S.EndOfMedia:
            self._emit_position(force=True)
        elif st == S.InvalidMedia and not self._failed:
            self._on_error(QMediaPlayer.Error.FormatError, self._player.errorString())

    def _apply_pending(self):
        """媒体载入、总长已知后才把续播位置夹到 [0, 总长-3s] 并跳过去（只做一次）。"""
        dur = self._player.duration()
        if self._pending_pos is None or dur <= 0:
            return
        pos, self._pending_pos = self._pending_pos, None
        self._player.setPosition(mu.clamp_resume(pos, dur))

    def _on_error(self, err, text=""):
        if err == QMediaPlayer.Error.NoError or self._failed:
            return
        self._failed = True
        msg = text or self._player.errorString() or "无法播放该音频文件"
        self._show_error("无法播放：%s" % msg)
        self._set_controls_enabled(False)
        self.failed.emit(msg)

    def _show_error(self, msg: str):
        self._error.setText(msg)
        self._error.show()

    # 标签：标题 / 艺术家 / 封面，缺哪项就用默认的
    def _reset_meta(self):
        self._title.setText("")
        self._artist.setText("")
        self._cover.setPixmap(QPixmap())
        self._cover.setText("♪")

    def _refresh_meta(self):
        if not self._path:
            return
        md = self._player.metaData()
        K = QMediaMetaData.Key
        title = (md.stringValue(K.Title) or "").strip()
        artist = (md.stringValue(K.AlbumArtist) or md.stringValue(K.ContributingArtist) or "").strip()
        self._title.setText(title or os.path.splitext(os.path.basename(self._path))[0])
        self._artist.setText(artist)
        img = md.value(K.ThumbnailImage)
        if not isinstance(img, QImage) or img.isNull():
            img = md.value(K.CoverArtImage)
        if isinstance(img, QImage) and not img.isNull():
            pm = QPixmap.fromImage(img).scaled(COVER_SIZE, COVER_SIZE, Qt.AspectRatioMode.KeepAspectRatio,
                                               Qt.TransformationMode.SmoothTransformation)
            self._cover.setPixmap(pm)
        else:
            self._cover.setPixmap(QPixmap())
            self._cover.setText("♪")

    def keyPressEvent(self, e):
        k = e.key()
        if k == Qt.Key.Key_Space:
            self.toggle_play()
        elif k == Qt.Key.Key_Left:
            self.skip(-mu.SKIP_BACK_MS)
        elif k == Qt.Key.Key_Right:
            self.skip(mu.SKIP_FWD_MS)
        elif k == Qt.Key.Key_Up:
            self.set_volume(self.volume() + VOLUME_STEP)
        elif k == Qt.Key.Key_Down:
            self.set_volume(self.volume() - VOLUME_STEP)
        elif k == Qt.Key.Key_BracketLeft:
            self.set_speed(mu.step_speed(self.speed(), -1))
        elif k == Qt.Key.Key_BracketRight:
            self.set_speed(mu.step_speed(self.speed(), 1))
        elif k == Qt.Key.Key_Escape:
            self.backRequested.emit()
        else:
            super().keyPressEvent(e)

    def closeEvent(self, e):
        self.close_audio()
        super().closeEvent(e)
