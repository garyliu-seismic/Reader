"""音频 / 视频播放器共用的纯逻辑（不依赖 Qt，可直接单测）。
规则与 ImageViewer 的视频断点续播一致：位置 > 3 秒且离结尾 > 3 秒才记住，续播时最多回到「结尾前 3 秒」。"""
from typing import Optional

RESUME_MARGIN_MS = 3000                 # 开头 / 结尾各这么久之内不记位置
SKIP_BACK_MS = 15_000                   # 后退
SKIP_FWD_MS = 30_000                    # 前进
SPEEDS = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0)
DEFAULT_SPEED = 1.0


def fmt_time(ms) -> str:
    """毫秒 -> 'm:ss'；满一小时 -> 'h:mm:ss'。负数 / 非数字按 0。"""
    try:
        s = max(0, int(ms)) // 1000
    except (TypeError, ValueError, OverflowError):
        s = 0
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return "%d:%02d:%02d" % (h, m, sec) if h else "%d:%02d" % (m, sec)


def fmt_speed(rate: float) -> str:
    """0.5 -> '0.5x'，1.0 -> '1x'，1.25 -> '1.25x'。"""
    return ("%g" % rate) + "x"


def should_save_resume(pos_ms, length_ms) -> bool:
    """是否值得记住这次的播放位置。length 未知（<=0）时只看开头；刚开始或快播完都不记（下次从头播）。"""
    try:
        pos, length = int(pos_ms), int(length_ms)
    except (TypeError, ValueError, OverflowError):
        return False
    if pos <= RESUME_MARGIN_MS:
        return False
    return length <= 0 or pos < length - RESUME_MARGIN_MS


def clamp_resume(pos_ms, length_ms) -> int:
    """续播位置：不小于 0，且最多到 length-3 秒（length 未知则不限上界）。"""
    try:
        pos, length = int(pos_ms), int(length_ms)
    except (TypeError, ValueError, OverflowError):
        return 0
    pos = max(0, pos)
    if length > 0:
        pos = min(pos, max(0, length - RESUME_MARGIN_MS))
    return pos


def nearest_speed_index(rate: float) -> int:
    """配置里存的倍速可能不在列表里，取最近的一档。"""
    try:
        r = float(rate)
    except (TypeError, ValueError):
        return SPEEDS.index(DEFAULT_SPEED)
    return min(range(len(SPEEDS)), key=lambda i: abs(SPEEDS[i] - r))


def step_speed(rate: float, delta: int) -> float:
    """在倍速列表里上 / 下移 delta 档（到头不再动）。"""
    i = max(0, min(len(SPEEDS) - 1, nearest_speed_index(rate) + delta))
    return SPEEDS[i]


def seek_target(pos_ms: int, delta_ms: int, length_ms: Optional[int]) -> int:
    """跳转目标：夹在 [0, length]；length 未知则只限下界。"""
    t = max(0, int(pos_ms) + int(delta_ms))
    if length_ms and length_ms > 0:
        t = min(t, int(length_ms))
    return t
