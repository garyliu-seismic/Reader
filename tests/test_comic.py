"""漫画压缩包：纯逻辑（comic.py）在临时 zip 上验证；ComicView 用 offscreen Qt 验证打开、翻页、跨页、缓存与关闭。
真实的绘制外观与鼠标手感无法在此验证。"""
import os
import threading
import time
import zipfile

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pyreader import comic as cm  # noqa: E402


def make_zip(path, entries):
    """entries: {名字: bytes}；名字以 / 结尾的写成目录条目。"""
    with zipfile.ZipFile(path, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return str(path)


def png_bytes(w=10, h=20, color=0xff3366cc):
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtGui import QImage
    img = QImage(w, h, QImage.Format_RGB32)
    img.fill(color)
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


FAKE = b"not-really-an-image"       # 纯逻辑测试不解码，字节内容无所谓


# ------------------------------------------------------------------ 列页 / 排序
def test_natural_order_numbers(tmp_path):
    p = make_zip(tmp_path / "a.cbz", {f"page{n}.jpg": FAKE for n in (10, 2, 1, 11, 3)})
    assert cm.list_pages(p) == ["page1.jpg", "page2.jpg", "page3.jpg", "page10.jpg", "page11.jpg"]


def test_full_path_sort_nested(tmp_path):
    p = make_zip(tmp_path / "a.zip", {
        "ch10/1.png": FAKE, "ch2/10.png": FAKE, "ch2/2.png": FAKE, "ch1/a.jpg": FAKE, "cover.jpg": FAKE})
    # 逐级自然排序：目录按路径段比较，同目录的页挨在一起
    assert cm.list_pages(p) == ["ch1/a.jpg", "ch2/2.png", "ch2/10.png", "ch10/1.png", "cover.jpg"]


def test_ch2_before_ch10_even_when_basenames_collide(tmp_path):
    p = make_zip(tmp_path / "a.zip", {"ch10/001.jpg": FAKE, "ch2/001.jpg": FAKE, "ch2/002.jpg": FAKE})
    assert cm.list_pages(p) == ["ch2/001.jpg", "ch2/002.jpg", "ch10/001.jpg"]


def test_skips_junk_entries(tmp_path):
    p = make_zip(tmp_path / "a.zip", {
        "dir/": b"", "__MACOSX/dir/._1.jpg": FAKE, ".DS_Store": FAKE, "dir/.hidden.png": FAKE,
        ".cache/2.jpg": FAKE, "readme.txt": FAKE, "dir/Thumbs.db": FAKE, "dir/ok.JPG": FAKE,
        "x.webp": FAKE, "y.gif": FAKE, "z.bmp": FAKE, "w.jpeg": FAKE, "noext": FAKE, "a.tiff": FAKE})
    assert cm.list_pages(p) == ["dir/ok.JPG", "w.jpeg", "x.webp", "y.gif", "z.bmp"]


def test_corrupt_zip_raises_comic_error(tmp_path):
    f = tmp_path / "bad.cbz"
    f.write_bytes(b"PK\x03\x04 this is not a zip at all")
    with pytest.raises(cm.ComicError):
        cm.list_pages(str(f))
    with pytest.raises(cm.ComicError):
        cm.list_pages(str(tmp_path / "missing.zip"))


def test_non_image_only_zip_is_empty(tmp_path):
    p = make_zip(tmp_path / "a.zip", {"a.txt": b"x", "b/c.pdf": b"y"})
    assert cm.list_pages(p) == []


def test_read_page_bytes_and_unknown_entry(tmp_path):
    p = make_zip(tmp_path / "a.zip", {"1.png": b"AAA", "2.png": b"BBB"})
    assert cm.read_page_bytes(p, "2.png") == b"BBB"
    with pytest.raises(cm.ComicError):
        cm.read_page_bytes(p, "../../etc/passwd")
    with pytest.raises(cm.ComicError):
        cm.read_page_bytes(p, "nope.png")


def test_entry_size_cap(tmp_path, monkeypatch):
    p = make_zip(tmp_path / "a.zip", {"1.png": b"x" * 100, "2.png": b"y"})
    monkeypatch.setattr(cm, "MAX_ENTRY_SIZE", 50)
    with cm.ComicArchive(p) as a:
        with pytest.raises(cm.ComicError):
            a.read("1.png")
        assert a.read("2.png") == b"y"


def test_too_many_entries(tmp_path, monkeypatch):
    p = make_zip(tmp_path / "a.zip", {f"{i}.jpg": FAKE for i in range(5)})
    monkeypatch.setattr(cm, "MAX_ENTRIES", 3)
    with pytest.raises(cm.ComicError):
        cm.ComicArchive(p)


def test_archive_shared_across_threads_and_closed(tmp_path):
    p = make_zip(tmp_path / "a.zip", {f"{i}.jpg": bytes([i]) * 1000 for i in range(20)})
    a = cm.ComicArchive(p)
    errs = []

    def work():
        try:
            for _ in range(20):
                for name in a.pages:
                    assert a.read(name) == bytes([int(name.split(".")[0])]) * 1000
        except Exception as e:      # noqa: BLE001
            errs.append(e)
    ts = [threading.Thread(target=work) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs
    a.close()
    with pytest.raises(cm.ComicError):
        a.read("0.jpg")
    a.close()                        # 重复关闭无副作用


def test_chapters_grouping(tmp_path):
    p = make_zip(tmp_path / "a.zip", {"a_cover.jpg": FAKE, "ch1/1.jpg": FAKE, "ch1/2.jpg": FAKE, "ch2/1.jpg": FAKE})
    with cm.ComicArchive(p) as a:
        ch = a.chapters()
    assert ch == [cm.Chapter("", 0, 1), cm.Chapter("ch1", 1, 2), cm.Chapter("ch2", 3, 1)]
    assert cm.group_chapters([]) == []


def test_display_name_gbk_repair():
    name = "第一话/001.jpg"
    garbled = name.encode("gbk").decode("cp437")        # zipfile 对未标 UTF-8 的条目就是这样解的
    assert cm.display_name(garbled) == name
    assert cm.display_name(name) == name                # 已是正确中文：不动
    assert cm.display_name("plain/1.jpg") == "plain/1.jpg"


# ------------------------------------------------------------------ 跨页 / 方向 / 缩放（纯函数）
def test_is_portrait():
    assert cm.is_portrait(10, 20) and not cm.is_portrait(20, 10) and not cm.is_portrait(10, 10)
    assert not cm.is_portrait(0, 0)


def test_spread_pages_rules():
    P = lambda i: True                                   # noqa: E731
    assert cm.spread_pages(0, 10, P) == (0,)             # 封面单独
    assert cm.spread_pages(0, 10, P, cover_alone=False) == (0, 1)
    assert cm.spread_pages(1, 10, P) == (1, 2)
    assert cm.spread_pages(9, 10, P) == (9,)             # 最后一页没有搭档
    assert cm.spread_pages(1, 10, P, spread=False) == (1,)
    land = lambda i: i != 2                              # noqa: E731  第 2 页是横页
    assert cm.spread_pages(1, 10, land) == (1,)
    assert cm.spread_pages(2, 10, land) == (2,)
    assert cm.spread_pages(3, 10, land) == (3, 4)


def test_next_prev_walk_is_consistent():
    port = {0: True, 1: True, 2: True, 3: False, 4: True, 5: True, 6: True}
    P = lambda i: port[i]                                # noqa: E731
    n = 7
    seen, i = [], 0
    while i is not None:
        seen.append(i)
        i = cm.next_start(i, n, P)
    assert seen == [0, 1, 3, 4, 6]                       # (0)(1,2)(3)(4,5)(6)
    back, i = [], seen[-1]
    while i is not None:
        back.append(i)
        i = cm.prev_start(i, n, P)
    assert back == [6, 4, 3, 1, 0]                       # 倒着走经过同样的屏
    assert cm.next_start(6, n, P) is None and cm.prev_start(0, n, P) is None


def test_prev_start_never_pairs_cover():
    P = lambda i: True                                   # noqa: E731
    assert cm.prev_start(2, 10, P) == 1                  # 退到 (1) 而不是与封面合并
    assert cm.prev_start(3, 10, P) == 1                  # 退成跨页 (1,2)
    assert cm.prev_start(2, 10, P, spread=False) == 1


def test_display_order_and_nav():
    assert cm.display_order((4, 5), rtl=False) == (4, 5)
    assert cm.display_order((4, 5), rtl=True) == (5, 4)
    assert cm.display_order((4,), rtl=True) == (4,)
    assert cm.nav_delta("right", False) == 1 and cm.nav_delta("left", False) == -1
    assert cm.nav_delta("right", True) == -1 and cm.nav_delta("left", True) == 1
    assert cm.nav_delta("middle", True) == 0


def test_click_side():
    assert cm.click_side(10, 300) == "left"
    assert cm.click_side(150, 300) == "middle"
    assert cm.click_side(290, 300) == "right"
    assert cm.click_side(5, 0) == "middle"


def test_spread_size_and_zoom():
    assert cm.spread_size([(100, 200)]) == (100, 200)
    assert cm.spread_size([(100, 200), (50, 100)]) == (200, 200)       # 矮的半尺寸页放大到同高
    assert cm.spread_size([(100, 200), (100, 100)]) == (300, 200)      # 宽扁的那页放大到 200 高
    assert cm.compute_zoom("width", 400, 300, 200, 100) == 2.0
    assert cm.compute_zoom("height", 400, 300, 200, 100) == 3.0
    assert cm.compute_zoom("window", 400, 300, 200, 100) == 2.0
    assert cm.compute_zoom("window", 0, 0, 200, 100) == cm.MIN_ZOOM
    assert cm.compute_zoom("width", 10 ** 6, 10, 1, 1) == cm.MAX_ZOOM


# ------------------------------------------------------------------ ComicView（offscreen）
@pytest.fixture(scope="module", autouse=True)
def qapp():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def wait_for(cond, timeout=10.0):
    from PySide6.QtWidgets import QApplication
    end = time.time() + timeout
    while time.time() < end:
        QApplication.processEvents()
        if cond():
            return True
        time.sleep(0.005)
    QApplication.processEvents()
    return cond()


def comic_zip(tmp_path, n=8, name="c.cbz", sizes=None):
    """页尺寸默认都是竖页 (10, 20)；sizes 可以覆盖：{序号: (w, h)}。"""
    sizes = sizes or {}
    return make_zip(tmp_path / name, {f"{i + 1:03d}.png": png_bytes(*sizes.get(i, (10, 20)), color=0xff000000 + i * 20)
                                      for i in range(n)})


def test_view_open_and_navigate(tmp_path):
    from pyreader.comicview import ComicView
    v = ComicView()
    v.resize(300, 400)
    got = []
    v.pageChanged.connect(lambda a, b: got.append((a, b)))
    assert v.open_comic(comic_zip(tmp_path), page=2)
    assert (v.current_page(), v.page_count()) == (2, 8) and got == [(2, 8)]
    assert v.next_screen() and v.current_page() == 3
    assert v.prev_screen() and v.prev_screen() and v.current_page() == 1
    v.go_to(99)
    assert v.current_page() == 7 and not v.next_screen()
    v.go_to(0)
    assert not v.prev_screen()
    assert got[-1] == (0, 8)
    v.grab()                                  # 真正走一遍 paintEvent
    v.close_comic()
    assert not v.is_open() and v.page_count() == 0


def test_view_open_failures(tmp_path):
    from pyreader.comicview import ComicView
    v = ComicView()
    bad = tmp_path / "bad.cbz"
    bad.write_bytes(b"garbage")
    assert not v.open_comic(str(bad)) and v.last_error()
    assert not v.open_comic(make_zip(tmp_path / "t.zip", {"a.txt": b"x"})) and "没有图片" in v.last_error()
    assert not v.open_comic(str(tmp_path / "nope.zip"))
    assert not v.is_open()
    # 失败后仍能正常打开
    assert v.open_comic(comic_zip(tmp_path, n=2)) and v.last_error() == ""
    v.close_comic()


def test_view_spread_rtl_and_keys(tmp_path):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from pyreader.comicview import ComicView
    v = ComicView()
    v.resize(400, 300)
    v.open_comic(comic_zip(tmp_path, n=8, sizes={3: (30, 10)}))      # 第 3 页是横页：不参与跨页
    changed = []
    v.optionsChanged.connect(lambda: changed.append(1))
    v.set_spread(True)
    assert v.spread() and changed
    v.go_to(1)
    v.grab()
    v.next_screen()
    assert v.current_page() == 3                          # (1,2) 之后是 3（横页，单独）
    v.next_screen()
    assert v.current_page() == 4                          # (4,5)
    v.prev_screen()
    assert v.current_page() == 3
    # 方向键：LTR 右 = 下一屏；RTL 右 = 上一屏
    v.set_spread(False)
    v.go_to(3)
    v.show()
    QTest.keyClick(v, Qt.Key_Right)
    assert v.current_page() == 4
    v.set_rtl(True)
    QTest.keyClick(v, Qt.Key_Right)
    assert v.current_page() == 3
    QTest.keyClick(v, Qt.Key_Left)
    assert v.current_page() == 4
    QTest.keyClick(v, Qt.Key_Home)
    assert v.current_page() == 0
    QTest.keyClick(v, Qt.Key_End)
    assert v.current_page() == 7
    QTest.keyClick(v, Qt.Key_PageUp)
    assert v.current_page() == 6
    QTest.keyClick(v, Qt.Key_PageDown)
    assert v.current_page() == 7
    QTest.keyClick(v, Qt.Key_W)
    assert v.fit_mode() == "width"
    QTest.keyClick(v, Qt.Key_D)
    assert v.spread()
    QTest.keyClick(v, Qt.Key_R)
    assert not v.rtl()
    back = []
    v.backRequested.connect(lambda: back.append(1))
    QTest.keyClick(v, Qt.Key_Escape)
    assert back == [1]
    v.close_comic()
    v.hide()


def test_view_click_zones(tmp_path):
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    from pyreader.comicview import ComicView
    v = ComicView()
    v.resize(300, 300)
    v.open_comic(comic_zip(tmp_path, n=5), page=2)
    QTest.mouseClick(v, Qt.LeftButton, pos=QPoint(280, 150))
    assert v.current_page() == 3
    QTest.mouseClick(v, Qt.LeftButton, pos=QPoint(150, 150))      # 中间：无动作
    assert v.current_page() == 3
    QTest.mouseClick(v, Qt.LeftButton, pos=QPoint(10, 150))
    assert v.current_page() == 2
    v.set_rtl(True)
    QTest.mouseClick(v, Qt.LeftButton, pos=QPoint(10, 150))       # RTL：左 = 下一页
    assert v.current_page() == 3
    v.close_comic()


def test_view_wheel_scroll_then_flip(tmp_path):
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from pyreader.comicview import ComicView
    v = ComicView()
    v.resize(200, 100)
    v.open_comic(comic_zip(tmp_path, n=3))
    v.set_fit_mode("width")                                       # 10x20 页按宽适配 → 高 400，超出视口 100
    v.grab()
    assert v._pan_y == 0

    def wheel(dy):
        v.wheelEvent(QWheelEvent(QPointF(50, 50), QPointF(50, 50), QPoint(0, 0), QPoint(0, dy), Qt.NoButton,
                                 Qt.NoModifier, Qt.NoScrollPhase, False))
    wheel(-120)
    assert v._pan_y > 0 and v.current_page() == 0                 # 先在页内滚动
    for _ in range(40):
        v._last_wheel_flip = 0
        wheel(-120)
        if v.current_page() == 1:
            break
    assert v.current_page() == 1                                  # 滚到底再滚才翻页
    v._last_wheel_flip = 0
    wheel(120)                                                    # 往回翻：落在上一页底部
    assert v.current_page() == 0
    v.grab()
    assert v._pan_y > 0
    v.close_comic()


def test_view_prefetch_cache_and_safe_close(tmp_path):
    from pyreader import comicview as cv
    v = cv.ComicView()
    v.resize(200, 200)
    assert v.open_comic(comic_zip(tmp_path, n=12), page=3)
    v.grab()
    assert wait_for(lambda: {2, 4, 5} <= set(v._cache))           # 后台预取后两页和前一页
    for p in range(4, 12):
        v.go_to(p)
        v.grab()
        assert len(v._cache) <= cv.CACHE_PAGES
        assert p in v._cache
    v.go_to(0)                                                    # 关闭时可能还有任务在途：不能崩，迟到的结果被丢弃
    v.close_comic()
    wait_for(lambda: False, 0.1)                                  # 让迟到的 queued 信号走完
    assert not v._cache and not v._pending
    assert not v.open_comic(str(tmp_path / "nope.zip"))


def test_view_undecodable_page_does_not_loop(tmp_path):
    from pyreader.comicview import ComicView
    p = make_zip(tmp_path / "x.zip", {"1.png": png_bytes(), "2.png": b"this is not a png", "3.png": png_bytes()})
    v = ComicView()
    v.resize(200, 200)
    assert v.open_comic(p, page=1)
    v.grab()
    assert 1 in v._bad and v.page_count() == 3
    v.next_screen()
    v.grab()
    assert v.current_page() == 2
    v.close_comic()


def test_view_theme_and_chapters(tmp_path):
    from pyreader.comicview import ComicView
    from pyreader.config import THEMES
    v = ComicView()
    v.set_theme(THEMES["纯白"])
    assert v._bg == THEMES["纯白"]["bg"]
    p = make_zip(tmp_path / "c.zip", {"a/1.png": png_bytes(), "b/1.png": png_bytes()})
    v.open_comic(p)
    assert [c.title for c in v.chapters()] == ["a", "b"]
    v.close_comic()


def test_natural_key_survives_non_decimal_digit_characters(tmp_path):
    assert cm.natural_key("①") == ["①"]                                       # str.isdigit() 为真、int() 会抛 ValueError
    p = make_zip(tmp_path / "w.cbz", {"②/001.jpg": FAKE, "①/001.jpg": FAKE, "²/10.jpg": FAKE})
    assert cm.list_pages(p) and len(cm.list_pages(p)) == 3
    assert cm.natural_key("p٣") == ["p", 3, ""]                                 # 十进制数字（含非 ASCII）照常转数字


def test_view_error_text_readable_on_light_theme(tmp_path):
    from pyreader.comicview import ComicView
    from pyreader.config import THEMES
    p = make_zip(tmp_path / "bad.zip", {"1.png": b"this is not a png"})
    v = ComicView()
    v.resize(300, 200)
    v.set_theme(THEMES["纯白"])
    assert v.open_comic(p)
    v.grab()
    img = v.grab().toImage()
    bg = img.pixelColor(2, 2).lightness()
    dark = sum(1 for x in range(0, 300) for y in range(40, 120) if img.pixelColor(x, y).lightness() < bg - 100)
    assert dark > 20                                                            # 浅底上的提示字要够深（原来 #d0d0d0 几乎看不见）
    v.close_comic()


def test_view_context_menu_has_mouse_entry_points(tmp_path):
    from pyreader.comicview import ComicView
    p = make_zip(tmp_path / "m.zip", {"a/1.png": png_bytes(), "b/1.png": png_bytes(), "b/2.png": png_bytes()})
    v = ComicView()
    v.resize(200, 200)
    assert v.open_comic(p)
    acts = {x.text(): x for x in v._build_menu().actions()}
    acts["双页跨页（D）"].trigger()
    acts["适应宽度（W）"].trigger()
    acts["从右向左（R）"].trigger()
    acts["章节"].menu().actions()[1].trigger()
    assert "跳到页码…" in acts and v.spread() and v.fit_mode() == "width" and v.rtl() and v.current_page() == 1
    v.close_comic()
