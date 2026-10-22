# 朗读专用音频播放器：用 QAudioSink（push 模式，持续同一条音频流）播放逐句合成的 PCM 数据，
# 替代原先每句 QMediaPlayer.setSourceDevice()+play() 的方式。
#
# 背景：QMediaPlayer 面向"打开一个完整媒体文件"设计，每次切换媒体源都要重新打开/配置
# 音频设备管线，这个"重新开流"的动作本身会产生可闻的咔哒声（"du"音），这是 Qt Multimedia
# 高层 API 的已知限制（参见 Qt Forum "Play small Audio files without gaps"），不是参数能调出来的。
# 改用 QAudioSink 的 push 模式持续写入 PCM 字节流，句子之间不再有"重新开流"这个动作，
# 从架构上消除了爆音。
#
# 用法：
#   player = TtsPlayer(self)
#   player.sentenceFinished.connect(on_one_sentence_done)   # 某句 wav 数据已全部写入播放缓冲区
#   player.play_wav(wav_bytes)     # 播放一句（wav 字节，自动解出 PCM 并按需要重建/复用 QAudioSink）
#   player.pause() / player.resume() / player.stop()
import io
import wave
from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtMultimedia import QAudio, QAudioSink, QAudioFormat, QMediaDevices


class TtsPlayer(QObject):
    sentenceFinished = Signal()   # 当前句的全部 PCM 数据已写完并播放完毕

    def __init__(self, parent=None):
        super().__init__(parent)
        self._sink = None
        self._io_dev = None
        self._fmt = None            # 当前 QAudioSink 使用的格式（采样率/声道/位深）
        self._pending = b""         # 还没写入 sink 的剩余 PCM 字节
        self._playing = False
        self._paused = False
        self._timer = QTimer(self)
        self._timer.setInterval(20)         # 20ms 轮询一次，足够及时又不会占满 CPU
        self._timer.timeout.connect(self._feed)

    def is_playing(self) -> bool:
        return self._playing

    def is_paused(self) -> bool:
        return self._paused

    @staticmethod
    def _wav_to_pcm(data: bytes):
        """解出 wav 字节里的裸 PCM s16le 数据和对应的 QAudioFormat。"""
        with wave.open(io.BytesIO(data), "rb") as w:
            sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
            pcm = w.readframes(w.getnframes())
        fmt = QAudioFormat()
        fmt.setSampleRate(sr)
        fmt.setChannelCount(ch)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16 if sw == 2 else QAudioFormat.SampleFormat.UInt8)
        return pcm, fmt

    def _ensure_sink(self, fmt: QAudioFormat):
        """确保有一个匹配该格式的 QAudioSink；格式变化（如切换了不同采样率的音色）才重建，
        同一段朗读内沿用同一个 sink，不重新开流。"""
        if self._sink is not None and self._fmt is not None \
           and self._fmt.sampleRate() == fmt.sampleRate() \
           and self._fmt.channelCount() == fmt.channelCount() \
           and self._fmt.sampleFormat() == fmt.sampleFormat():
            return
        self._teardown_sink()
        device = QMediaDevices.defaultAudioOutput()
        self._sink = QAudioSink(device, fmt)
        self._fmt = fmt
        self._io_dev = self._sink.start()

    def _teardown_sink(self):
        if self._sink is not None:
            try:
                self._sink.stop()
            except Exception:
                pass
        self._sink = None
        self._io_dev = None
        self._fmt = None

    def play_wav(self, data: bytes):
        """播放一句 wav 音频：解出 PCM，追加到待写队列，继续喂给同一条流。"""
        if not data:
            self.sentenceFinished.emit()
            return
        pcm, fmt = self._wav_to_pcm(data)
        self._ensure_sink(fmt)
        self._pending += pcm
        self._playing = True
        self._paused = False
        if not self._timer.isActive():
            self._timer.start()

    def _feed(self):
        if self._sink is None or self._io_dev is None:
            self._timer.stop()
            return
        if self._paused:
            return
        free = self._sink.bytesFree()
        if free > 0 and self._pending:
            chunk = self._pending[:free]
            n = self._io_dev.write(chunk)
            if n > 0:
                self._pending = self._pending[n:]
        if not self._pending:
            # 所有数据已写入 sink 内部缓冲区；再等缓冲区真正播放完（IdleState 或缓冲区排空）才算这句结束
            if self._sink.state() == QAudio.State.IdleState or self._sink.bytesFree() >= self._sink.bufferSize():
                self._timer.stop()
                self._playing = False
                self.sentenceFinished.emit()

    def pause(self):
        if self._sink is not None and self._playing and not self._paused:
            self._sink.suspend()
            self._paused = True

    def resume(self):
        if self._sink is not None and self._paused:
            self._sink.resume()
            self._paused = False
            if not self._timer.isActive():
                self._timer.start()

    def stop(self):
        self._timer.stop()
        self._pending = b""
        self._playing = False
        self._paused = False
        self._teardown_sink()
