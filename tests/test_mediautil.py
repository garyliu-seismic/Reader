"""mediautil：音频 / 视频播放器共用的纯逻辑（不需要 Qt）。"""
import pytest

from pyreader import mediautil as mu


@pytest.mark.parametrize("ms,txt", [
    (0, "0:00"), (999, "0:00"), (1000, "0:01"), (59_999, "0:59"), (60_000, "1:00"),
    (3_599_000, "59:59"), (3_600_000, "1:00:00"), (3_661_000, "1:01:01"), (36_000_000, "10:00:00"),
    (-5, "0:00"), (None, "0:00"), ("x", "0:00"), (1500.7, "0:01"),
])
def test_fmt_time(ms, txt):
    assert mu.fmt_time(ms) == txt


def test_fmt_speed():
    assert [mu.fmt_speed(s) for s in (0.5, 1.0, 1.25, 3.0)] == ["0.5x", "1x", "1.25x", "3x"]


def test_speeds_list():
    assert mu.SPEEDS == (0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0)
    assert mu.DEFAULT_SPEED in mu.SPEEDS


def test_should_save_resume_rule():
    # 位置 > 3000 且 < 总长-3000
    assert not mu.should_save_resume(0, 100_000)
    assert not mu.should_save_resume(3000, 100_000)          # 边界：必须严格大于
    assert mu.should_save_resume(3001, 100_000)
    assert mu.should_save_resume(96_999, 100_000)
    assert not mu.should_save_resume(97_000, 100_000)        # 边界：必须严格小于
    assert not mu.should_save_resume(100_000, 100_000)


def test_should_save_resume_unknown_length_and_garbage():
    assert mu.should_save_resume(10_000, 0)                  # 总长未知：只看开头
    assert mu.should_save_resume(10_000, -1)
    assert not mu.should_save_resume(2000, 0)
    assert not mu.should_save_resume(None, 100_000)
    assert not mu.should_save_resume("a", 100_000)


def test_short_media_never_saves():
    assert not any(mu.should_save_resume(p, 6000) for p in range(0, 6001, 100))   # 6 秒的媒体没有可存区间


def test_clamp_resume():
    assert mu.clamp_resume(50_000, 100_000) == 50_000
    assert mu.clamp_resume(99_000, 100_000) == 97_000
    assert mu.clamp_resume(10**9, 100_000) == 97_000
    assert mu.clamp_resume(-5, 100_000) == 0
    assert mu.clamp_resume(2000, 2000) == 0                  # 比 3 秒还短：回到开头
    assert mu.clamp_resume(12_345, 0) == 12_345              # 总长未知不限上界
    assert mu.clamp_resume(None, 100) == 0


def test_nearest_and_step_speed():
    assert mu.nearest_speed_index(1.0) == 2
    assert mu.nearest_speed_index(1.1) == 2
    assert mu.nearest_speed_index(10) == len(mu.SPEEDS) - 1
    assert mu.nearest_speed_index("bad") == 2
    assert mu.step_speed(1.0, 1) == 1.25
    assert mu.step_speed(1.0, -1) == 0.75
    assert mu.step_speed(3.0, 1) == 3.0
    assert mu.step_speed(0.5, -1) == 0.5


def test_seek_target():
    assert mu.seek_target(10_000, -15_000, 100_000) == 0
    assert mu.seek_target(10_000, 30_000, 100_000) == 40_000
    assert mu.seek_target(90_000, 30_000, 100_000) == 100_000
    assert mu.seek_target(90_000, 30_000, None) == 120_000
    assert mu.seek_target(90_000, 30_000, 0) == 120_000
