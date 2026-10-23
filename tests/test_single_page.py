import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from pyreader.config import (pages_per_view, view_count, view_of_page,
                             first_page_of_view, DEFAULT_CONFIG, SINGLE_MAX_W)


def test_pages_per_view():
    assert pages_per_view(False) == 2
    assert pages_per_view(True) == 1
    assert DEFAULT_CONFIG["single_page"] is False


@pytest.mark.parametrize("n,ppv,expect", [(0, 2, 0), (1, 2, 1), (5, 2, 3), (6, 2, 3), (5, 1, 5), (0, 1, 0)])
def test_view_count(n, ppv, expect):
    assert view_count(n, ppv) == expect


def test_view_page_mapping():
    assert [view_of_page(i, 2) for i in range(5)] == [0, 0, 1, 1, 2]
    assert [view_of_page(i, 1) for i in range(5)] == [0, 1, 2, 3, 4]
    assert first_page_of_view(3, 2) == 6 and first_page_of_view(3, 1) == 3


@pytest.fixture(scope="module", autouse=True)
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _page(i):
    return SimpleNamespace(index=i, start=i * 100, end=(i + 1) * 100)


def _fake_window(npages, single):
    from pyreader.view import PageView
    pages = [_page(i) for i in range(npages)]
    view = PageView()
    view.single_page = single
    view.pages = pages
    pager = SimpleNamespace(chapter_index_at=lambda off: 0, pages_of=lambda ci: pages)
    win = SimpleNamespace(pager=pager, view=view, load_chapter=lambda ci, goto_end=False: None)
    return win


@pytest.mark.parametrize("single,off,spread", [(False, 250, 1), (False, 0, 0), (False, 450, 2),
                                               (True, 250, 2), (True, 450, 4)])
def test_goto_offset(single, off, spread):
    from pyreader.mainwin import MainWindow
    win = _fake_window(5, single)
    MainWindow.goto_offset(win, off)
    assert win.view.spread == spread
    # 恢复位置：屏首页的 start 不超过目标偏移，且同屏
    assert win.view.left_page_offset() <= off < win.view.left_page_offset() + 100 * win.view.ppv


def test_view_geometry_and_toggle():
    from pyreader.view import PageView
    v = PageView()
    v.resize(1600, 900)
    two = v.page_rects()
    assert len(two) == 2 and v.visible_page_indices() == [0, 1]
    v.spread = 1
    assert v.left_page_offset() == 0          # 无页时为 0
    v.set_single_page(True)
    one = v.page_rects()
    assert len(one) == 1 and v.visible_page_indices() == [v.spread]
    assert one[0].width() <= SINGLE_MAX_W
    assert abs(one[0].center().x() - 800) < 1          # 居中
    assert v.pagination_params()["page_size"].width() == one[0].width()
    v.resize(640, 900)                                   # 窄窗口：占满可用宽度
    assert abs(v.page_rects()[0].width() - (640 - 2 * v.outer)) < 1
    v.pages = [_page(i) for i in range(4)]
    v.spread = 3
    assert v.left_page_offset() == 300
    v.set_single_page(False)
    assert v._flip_anim is None and v.ppv == 2
