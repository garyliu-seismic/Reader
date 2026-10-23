"""阶段 2：本地书架界面（offscreen Qt）。在模型 / 信号层验证数据、过滤、缓存、后台扫描；
MainWindow 的页面切换、按键与计时器防护用真实窗口（offscreen）验证。GUI 的真实点击 / 外观无法在此验证。"""
import json
import os
import threading
import time
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent, QMouseEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QStackedWidget  # noqa: E402

from pyreader import shelf as sh  # noqa: E402
from pyreader import shelfview as sv  # noqa: E402
from pyreader import store  # noqa: E402
from pyreader import sync  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def qapp():
    return QApplication.instance() or QApplication([])


def wait_for(cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        QApplication.processEvents()
        if cond():
            return True
        time.sleep(0.005)             # 不能用 QTest.qWait：它占着 GIL，会饿死后台扫描线程
    QApplication.processEvents()
    return cond()


def wait_scan(view):
    assert wait_for(lambda: not view.scanning), "扫描没有在时限内结束"


@pytest.fixture
def lib(tmp_path):
    d = tmp_path / "books"
    (d / "科幻").mkdir(parents=True)
    (d / ".hidden").mkdir()
    files = {"三体.txt": b"a" * 10, "Dune.epub": b"b" * 20, "Zeta.pdf": b"c" * 5, "科幻/沙丘.txt": b"d" * 7,
             "科幻/Foundation.EPUB": b"e" * 9, ".dot.txt": b"x", "~$lock.docx": b"x", "note.log": b"x",
             ".hidden/secret.txt": b"x"}
    for rel, data in files.items():
        (d / rel).write_bytes(data)
    return d


def make_view(tmp_path, dirs, bookmarks=None, recent=None, **kw):
    ctx = {"library_dirs": list(dirs), "recent": list(recent or []), "bookmarks": bookmarks if bookmarks is not None else {}}
    view = sv.ShelfView(lambda: ctx, root=str(tmp_path / "home"), **kw)
    view.ctx = ctx
    return view


def titles(view):
    return [b.title for b in view.visible_books()]


# ================================================================== 扫描 / 缓存
def test_scan_runs_on_worker_thread_and_fills_shelf(tmp_path, lib, monkeypatch):
    seen = []
    real = sh.scan_local

    def spy(*a, **k):
        seen.append(threading.current_thread())
        return real(*a, **k)
    monkeypatch.setattr(sh, "scan_local", spy)
    v = make_view(tmp_path, [str(lib)])
    assert v.visible_books() == []                  # 构造时不扫描（先画缓存，没有缓存就是空）
    assert v.rescan() is True and v.scanning
    assert v.rescan() is False                      # 已有扫描在跑：忽略重复触发
    wait_scan(v)
    assert seen and all(t is not threading.main_thread() for t in seen)
    assert sorted(titles(v)) == sorted(["三体", "Dune", "Zeta", "沙丘", "Foundation"])   # 点文件 / ~$ / .log / 隐藏目录都不在
    assert len(v.visible_books()) == 5 and v.model.rowCount() == 5


def test_scan_does_not_touch_network(tmp_path, lib, monkeypatch):
    import pyreader.config as cfg

    def boom(*a, **k):
        raise AssertionError("阶段 2 不应有任何网络访问")
    monkeypatch.setattr(cfg, "net_urlopen", boom)
    v = make_view(tmp_path, [str(lib)])
    v.rescan()
    wait_scan(v)
    assert len(v.books) == 5


def test_index_cache_shown_before_any_scan(tmp_path, lib):
    v1 = make_view(tmp_path, [str(lib)])
    v1.rescan()
    wait_scan(v1)
    v1.flush()
    assert os.path.isfile(sh.index_path(str(tmp_path / "home")))
    # 新实例：书库目录给空；缓存仍先显示（没有触发任何扫描）
    v2 = make_view(tmp_path, [])
    assert not v2.scanning and len(v2.visible_books()) == 5
    assert sorted(titles(v2)) == sorted(titles(v1))
    v2.rescan()                                      # 真实扫描结果（库已空）随后替换缓存
    wait_scan(v2)
    assert v2.visible_books() == []


def test_corrupt_index_is_quarantined_and_rebuilt(tmp_path, lib):
    home = tmp_path / "home" / "shelf"
    home.mkdir(parents=True)
    (home / "index.json").write_text("{{{ not json")
    v = make_view(tmp_path, [str(lib)])
    assert v.visible_books() == []
    assert any(".bad-" in f for f in os.listdir(home))
    v.rescan()
    wait_scan(v)
    assert len(v.visible_books()) == 5


def test_stale_cache_open_missing_file_rescans(tmp_path, lib):
    v1 = make_view(tmp_path, [str(lib)])
    v1.rescan(); wait_scan(v1); v1.flush()
    v2 = make_view(tmp_path, [str(lib)])
    target = next(b for b in v2.visible_books() if b.title == "三体")
    os.remove(target.local_paths[0])
    got = []
    v2.openRequested.connect(got.append)
    assert v2.open_book(target) is False and got == []
    assert "找不到文件" in v2.status.text()
    wait_scan(v2)
    assert "三体" not in titles(v2)


def test_scan_result_of_superseded_scan_is_ignored(tmp_path, lib):
    v = make_view(tmp_path, [str(lib)])
    v.rescan()
    first = v._scanner
    v.rescan(force=True)
    second = v._scanner
    assert first is not second and first._stop.is_set()
    wait_scan(v)
    assert len(v.visible_books()) == 5


def test_scan_failure_does_not_raise_into_ui(tmp_path, lib, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk exploded")
    monkeypatch.setattr(sh, "scan_local", boom)
    v = make_view(tmp_path, [str(lib)])
    v.rescan()
    wait_scan(v)
    assert "disk exploded" in v.status.text() and not v.scanning


def test_scan_truncated_message(tmp_path, lib, monkeypatch):
    monkeypatch.setattr(sh, "SCAN_LIMIT", 2)
    real = sh.scan_local
    monkeypatch.setattr(sh, "scan_local", lambda roots, limit=2, **k: real(roots, 2, **k))
    v = make_view(tmp_path, [str(lib)])
    v.rescan(); wait_scan(v)
    assert len(v.books) == 2 and "只显示前" in v.status.text()


def test_rescan_if_stale(tmp_path, lib):
    v = make_view(tmp_path, [str(lib)])
    assert v.rescan_if_stale() is True
    wait_scan(v)
    assert v.rescan_if_stale() is False             # 刚扫过
    v._last_scan -= v.STALE_SECONDS + 1
    assert v.rescan_if_stale() is True
    wait_scan(v)


def test_show_event_triggers_background_scan_once(tmp_path, lib):
    v = make_view(tmp_path, [str(lib)])
    v.show()
    assert v.scanning
    wait_scan(v)
    v.hide(); v.show()
    assert not v.scanning                            # 60 秒内再次显示不重复扫描
    v.close()


# ================================================================== 智能书架 / 过滤 / 排序 / 搜索
def test_smart_shelves_progress_recent_and_counts(tmp_path, lib):
    p_dune, p_zeta, p_santi = (str(lib / "Dune.epub"), str(lib / "Zeta.pdf"), str(lib / "三体.txt"))
    # 书签键用正斜杠 / 大小写不同的盘符（打开对话框的写法）；扫描得到的是系统分隔符：必须归一后才对得上
    bm_key = p_dune.replace("\\", "/")
    bm = {bm_key: {"_progress_": 1234, "_progress_ts_": 1_700_000_500.0, "items": []}}
    v = make_view(tmp_path, [str(lib)], bookmarks=bm, recent=[p_santi, p_zeta])
    v.rescan(); wait_scan(v)
    by = {b.title: b for b in v.books.values()}
    assert by["Dune"].has_progress and by["Dune"].status == "reading" and by["Dune"].percent is None
    assert by["三体"].last_read > 0 and by["Zeta"].last_read > 0 and by["沙丘"].last_read == 0   # recent 种子 last_opened
    v.set_shelf("reading")
    assert titles(v) == ["Dune"]
    v.set_shelf("recent")
    assert set(titles(v)) == {"Dune", "三体", "Zeta"}
    v.set_shelf("done")
    assert titles(v) == []
    assert v.content.currentIndex() == 2 and "没有匹配" in v.empty_label.text()
    # 阅读页记录了进度百分比 → 已读完
    v.note_progress(p_zeta, 98.0, 1_700_000_900.0, "pc")
    v.note_progress(p_santi, 40.0, 1_700_000_901.0, "pc")
    assert titles(v) == ["Zeta"]
    v.set_shelf("reading")
    assert set(titles(v)) == {"Dune", "三体"}
    # 侧栏计数与 filter_books 一致
    counts = sv.count_shelves(v.books)
    for key in ("all", "recent", "reading", "done"):
        assert counts[key] == len(sh.filter_books(v.books, key)), key
    sub = sh.LOCAL + str(lib).replace("\\", "/") + "/科幻"
    assert counts[sub] == len(sh.filter_books(v.books, sub)) == 2
    texts = [v.sidebar.item(i).text() for i in range(v.sidebar.count())]
    assert texts[0].startswith("全部") and texts[0].endswith("5") and "本机书库" in texts
    assert any("科幻" in t and t.endswith("2") for t in texts)


def test_note_opened_moves_book_to_recent_and_persists(tmp_path, lib):
    v = make_view(tmp_path, [str(lib)])
    v.rescan(); wait_scan(v)
    v.set_shelf("recent")
    assert titles(v) == []
    p = str(lib / "科幻" / "沙丘.txt")
    v.note_opened(p, 1_700_000_000.0)
    assert titles(v) == ["沙丘"]
    v.flush()
    v2 = make_view(tmp_path, [str(lib)])
    v2.set_shelf("recent")
    assert titles(v2) == ["沙丘"]                    # opened 随 index.json 落盘，重启仍在
    v2.rescan(); wait_scan(v2)
    assert titles(v2) == ["沙丘"]                    # 扫描重建 books 之后记录不丢


def test_notes_during_scan_are_reapplied(tmp_path, lib):
    v = make_view(tmp_path, [str(lib)])
    v.rescan()
    v.note_progress(str(lib / "Zeta.pdf"), 99.0, 1_700_000_000.0)    # 扫描进行中
    wait_scan(v)
    z = next(b for b in v.books.values() if b.title == "Zeta")
    assert z.status == "done"


def test_search_casefold_substring_title_author_filename(tmp_path, lib):
    v = make_view(tmp_path, [str(lib)])
    v.rescan(); wait_scan(v)
    v.set_query("DUNE")
    assert titles(v) == ["Dune"]
    v.set_query("  sha  ")                            # 无匹配
    assert titles(v) == []
    v.set_query("丘")
    assert titles(v) == ["沙丘"]
    v.set_query("foundation.epub")                   # 文件名（含扩展名，大小写无关）
    assert titles(v) == ["Foundation"]
    v.set_query("")
    assert len(titles(v)) == 5
    b = next(b for b in v.books.values() if b.title == "Zeta")
    b.author = "Ünïcode Author"
    v.set_query("ünïcode")                           # 作者字段
    v._apply_filter()
    assert titles(v) == ["Zeta"]
    assert v.handle_escape() is True and v.search.text() == "" and v.handle_escape() is False


def test_sort_keys_and_state_persistence(tmp_path, lib):
    v = make_view(tmp_path, [str(lib)])
    v.rescan(); wait_scan(v)
    v.set_sort("title")
    assert titles(v) == sorted(titles(v), key=lambda t: t.casefold())
    v.set_sort("size")
    sizes = [b.size for b in v.visible_books()]
    assert sizes == sorted(sizes, reverse=True)
    v.set_view_mode("list")
    assert v.content.currentIndex() == 1 and v.table.isVisibleTo(v.content) and v.btn_list.isChecked()
    v.set_shelf("reading")
    st = json.loads(open(sh.state_path(str(tmp_path / "home")), encoding="utf-8").read())
    assert st["ui"]["view"] == "list" and st["ui"]["sort"] == "size" and st["ui"]["last_shelf"] == "reading"
    v2 = make_view(tmp_path, [str(lib)], default_view="grid", default_sort="title")   # 已有 state.json：config 默认值不覆盖上次选择
    assert v2.view_mode == "list" and v2.sort_key == "size" and v2.current_shelf == "reading"
    assert v2.btn_list.isChecked()


def test_last_shelf_missing_falls_back_but_is_remembered(tmp_path, lib):
    v = make_view(tmp_path, [str(lib)])
    v.rescan(); wait_scan(v)
    sub = sh.LOCAL + str(lib).replace("\\", "/") + "/科幻"
    v.set_shelf(sub)
    assert sorted(titles(v)) == ["Foundation", "沙丘"]
    v.flush()
    v2 = make_view(tmp_path, [str(lib)])             # 缓存里就有这个书架
    assert v2.current_shelf == sub
    v3 = make_view(tmp_path, [])                     # 书架暂时不存在（例如移动硬盘没插）
    v3.books.clear(); v3.refresh()
    assert v3.current_shelf == "all"
    st = json.loads(open(sh.state_path(str(tmp_path / "home")), encoding="utf-8").read())
    assert st["ui"]["last_shelf"] == sub             # 用户的选择没被覆盖


def test_fresh_state_uses_config_defaults_then_state_wins(tmp_path, lib):
    v = make_view(tmp_path, [str(lib)], default_view="list", default_sort="progress")
    assert v.view_mode == "list" and v.sort_key == "progress"
    v2 = make_view(tmp_path, [str(lib)], default_view="grid", default_sort="title")
    assert v2.view_mode == "list" and v2.sort_key == "progress"      # 第二次：以 state.json 为准
    v3 = make_view(tmp_path / "other", [str(lib)], default_view="bogus", default_sort="bogus")
    assert v3.view_mode == "grid" and v3.sort_key == "last_read"      # 非法默认值被忽略


def test_apply_defaults_and_reset_state(tmp_path, lib):
    v = make_view(tmp_path, [str(lib)])
    v.rescan(); wait_scan(v)
    p = str(lib / "Zeta.pdf")
    v.note_progress(p, 98.0, 1_700_000_000.0); v.note_opened(p, 1_700_000_000.0); v.flush()
    v.apply_defaults("list", "title", True)
    assert v.view_mode == "list" and v.sort_key == "title"
    st = store.load_json(sh.state_path(str(tmp_path / "home")), None, dict)
    assert st["ui"]["start_in_shelf"] is True and st["ui"]["view"] == "list"
    dev = st["device_id"]
    ov_path = sh.overlay_path(str(tmp_path / "home"))
    store.save_json(ov_path, {"version": 1, "device_id": dev, "entries": {}})      # overlay.json 在阶段 2 不属于重置范围
    v.set_query("zeta"); v.set_shelf("done")
    v.reset_state("grid", "last_read", False)
    assert v.view_mode == "grid" and v.sort_key == "last_read" and v.current_shelf == "all" and v.search.text() == ""
    assert not os.path.exists(sh.index_path(str(tmp_path / "home")))
    assert v._opened == {} and v._progress == {}
    st2 = store.load_json(sh.state_path(str(tmp_path / "home")), None, dict)
    assert st2["device_id"] == dev and st2["ui"]["start_in_shelf"] is False and st2["ui"]["last_shelf"] == "all"
    assert os.path.exists(ov_path)
    wait_scan(v)                                      # 重置后立刻重新扫描；书还在，进度百分比没了（书签里本来就没有）
    assert len(v.visible_books()) == 5
    z = next(b for b in v.books.values() if b.title == "Zeta")
    assert z.status != "done"


# ================================================================== 打开 / 空状态 / 视图
def test_activate_opens_existing_local_copy(tmp_path, lib):
    v = make_view(tmp_path, [str(lib)])
    v.rescan(); wait_scan(v)
    got = []
    v.openRequested.connect(got.append)
    v.set_query("dune")
    idx = v.model.index(0, 0)
    v.grid.activated.emit(idx)                        # 双击 / 回车都是 activated
    v.table.activated.emit(v.model.index(0, 2))       # 列表视图点任意列
    assert [os.path.normpath(p) for p in got] == [os.path.normpath(str(lib / "Dune.epub"))] * 2


def test_real_double_click_on_grid_item(tmp_path, lib):
    v = make_view(tmp_path, [str(lib)])
    v.resize(900, 600)
    v.show()
    wait_scan(v)
    v.set_query("三体")
    QApplication.processEvents()
    got = []
    v.openRequested.connect(got.append)
    r = v.grid.visualRect(v.model.index(0, 0))
    assert not r.isEmpty(), "网格应已布局出第一张卡片"
    vp = v.grid.viewport()
    pos = r.center()

    def ev(t, btns):                  # QTest.mouseDClick 在 offscreen 下只会发两次普通点击：手动发 press / release / dblclick
        QApplication.sendEvent(vp, QMouseEvent(t, QPointF(pos), QPointF(vp.mapToGlobal(pos)), Qt.MouseButton.LeftButton,
                                               btns, Qt.KeyboardModifier.NoModifier))
    ev(QEvent.Type.MouseButtonPress, Qt.MouseButton.LeftButton)
    ev(QEvent.Type.MouseButtonRelease, Qt.MouseButton.NoButton)
    ev(QEvent.Type.MouseButtonDblClick, Qt.MouseButton.LeftButton)
    ev(QEvent.Type.MouseButtonRelease, Qt.MouseButton.NoButton)
    assert got and len(got) == 1
    assert os.path.normpath(got[0]) == os.path.normpath(str(lib / "三体.txt"))
    v.close()


def test_empty_states(tmp_path, lib):
    v = make_view(tmp_path, [])
    assert v.content.currentIndex() == 2 and "尚未添加书库目录" in v.empty_label.text()
    assert v.empty_btn.isVisibleTo(v.content.widget(2))
    asked = []
    v.addLibraryRequested.connect(lambda: asked.append(1))
    v.empty_btn.click()
    assert asked == [1]
    v.ctx["library_dirs"] = [str(tmp_path / "nonexistent")]
    v.refresh()
    assert "没有找到电子书" in v.empty_label.text() and not v.empty_btn.isVisibleTo(v.content.widget(2))


def test_model_columns_and_plain_text(tmp_path, lib):
    v = make_view(tmp_path, [str(lib)])
    v.rescan(); wait_scan(v)
    v.set_query("zeta")
    m = v.model
    assert [m.headerData(i, Qt.Orientation.Horizontal) for i in range(m.columnCount())] == list(sv.COLUMNS)
    assert m.data(m.index(0, 0)) == "Zeta" and m.data(m.index(0, 1)) == "PDF" and m.data(m.index(0, 4)) == "5 B"
    assert isinstance(m.data(m.index(0, 0), sv.BookModel.BookRole), sh.Book)
    assert m.data(m.index(99, 0)) is None and m.rowCount(m.index(0, 0)) == 0
    # 书名来自文件名：HTML 不会被当富文本解释（QLabel 一律 PlainText）
    for lab in (v.status, v.empty_label):
        assert lab.textFormat() == Qt.TextFormat.PlainText


def test_delegate_paints_without_error(tmp_path, lib):
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtWidgets import QStyleOptionViewItem
    v = make_view(tmp_path, [str(lib)])
    v.rescan(); wait_scan(v)
    v.note_progress(str(lib / "Zeta.pdf"), 55.0, 1_700_000_000.0)
    d = v.grid.itemDelegate()
    img = QImage(sv.CARD_W, sv.CARD_H, QImage.Format.Format_ARGB32)
    for sel in (False, True):
        for r in range(v.model.rowCount()):
            p = QPainter(img)
            opt = QStyleOptionViewItem()
            opt.rect = img.rect()
            if sel:
                from PySide6.QtWidgets import QStyle
                opt.state |= QStyle.StateFlag.State_Selected
            d.paint(p, opt, v.model.index(r, 0))
            p.end()
    assert d.sizeHint(None, None) == img.size()


def test_format_helpers():
    assert sv.fmt_size(0) == "0 B" and sv.fmt_size(2048) == "2.0 KB" and sv.fmt_size(10 ** 400) == ""
    assert sv.fmt_size("x") == "" and sv.fmt_size(None) == ""
    now = 1_700_000_000.0
    assert sv.fmt_ago(0) == "" and sv.fmt_ago(3, now) == "最近" and sv.fmt_ago(now - 5, now) == "刚刚"
    assert sv.fmt_ago(now - 120, now) == "2 分钟前" and sv.fmt_ago(now - 7200, now) == "2 小时前"
    assert sv.fmt_ago(now - 86400 * 3, now) == "3 天前" and sv.fmt_ago(now - 86400 * 90, now).startswith("2023-")
    assert sv.fmt_ago(now + 999, now) == "刚刚"
    b = sh.Book("local:" + "a" * 40, "t")
    assert sv.progress_text(b) == ""
    b.has_progress = True
    assert sv.progress_text(b) == "在读"
    b.percent = 42.4
    assert sv.progress_text(b) == "42%"
    b.percent = 97.0
    assert sv.progress_text(b) == "已读完"


# ================================================================== 设置页
def test_settings_dialog_shelf_fields():
    from pyreader.config import DEFAULT_CONFIG
    from pyreader.settings import SettingsDialog
    assert DEFAULT_CONFIG["shelf_default_view"] == "grid" and DEFAULT_CONFIG["shelf_default_sort"] == "last_read"
    assert DEFAULT_CONFIG["shelf_start_in_shelf"] is False
    d = SettingsDialog(dict(DEFAULT_CONFIG, shelf_default_view="list", shelf_default_sort="title", shelf_start_in_shelf=True))
    assert d.shelf_view.currentData() == "list" and d.shelf_sort.currentData() == "title"
    assert d.shelf_start.isChecked() and not d.shelf_reset.isChecked()
    d2 = SettingsDialog(dict(DEFAULT_CONFIG, shelf_default_view="bogus", shelf_default_sort="bogus"))   # 非法值回落到第一项
    assert d2.shelf_view.currentData() == "grid" and d2.shelf_sort.currentData() == "last_read"
    d3 = SettingsDialog({})                           # 老版本 config.json 没有这些键
    assert d3.shelf_view.currentData() == "grid" and not d3.shelf_start.isChecked()
    assert [d.shelf_sort.itemData(i) for i in range(d.shelf_sort.count())] == list(sh.SORT_KEYS)


# ================================================================== MainWindow（真实窗口，offscreen）
@pytest.fixture
def win(monkeypatch):
    from pyreader.mainwin import MainWindow
    w = MainWindow()
    w.resize(1000, 700)
    w.show()
    QApplication.processEvents()
    yield w
    w._progress_timer.stop()
    w._resize_timer.stop()
    w.close()
    w.deleteLater()


def key(win, k, mod=Qt.KeyboardModifier.NoModifier):
    win.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, k, mod))


def test_window_structure_and_default_page(win):
    assert isinstance(win.centralWidget(), QStackedWidget) and win.centralWidget() is win.stack
    assert win.stack.widget(0) is win.shelf and win.stack.widget(1) is win.view     # self.view 属性名不变
    assert win.shelf.__class__.__name__ == "ShelfView"
    # 测试环境没有最近阅读的书 → 启动进入书架
    assert win._shelf_visible() and win.shelf_act.isChecked() and win.shelf_act.isCheckable()
    assert win.shelf_act.shortcut().toString() == "Ctrl+B"
    assert win.windowTitle() == "书架 — PyReader"


def test_start_page_rules(monkeypatch, tmp_path):
    import pyreader.mainwin as mw
    f = tmp_path / "r.txt"
    f.write_text("hi")
    loaded = []
    monkeypatch.setattr(mw.MainWindow, "load_book", lambda self, p: loaded.append(p))
    wins = []

    def build(cfg_extra, book_path=None):
        monkeypatch.setattr(mw, "load_json", lambda path, default, *a: dict(cfg_extra) if path == mw.CONFIG_PATH else default)
        w = mw.MainWindow(book_path)
        wins.append(w)
        return w

    w1 = build({"recent": [str(f)], "shelf_start_in_shelf": False})
    assert not w1._shelf_visible()                                 # 有最近阅读的书且未勾选 -> 阅读页（保持旧行为）
    w2 = build({"recent": [str(f)], "shelf_start_in_shelf": True})
    assert w2._shelf_visible()                                     # 勾选「启动时显示书架」
    w3 = build({"recent": [str(tmp_path / "gone.txt")], "shelf_start_in_shelf": False})
    assert w3._shelf_visible()                                     # 最近的书都不存在 -> 书架
    w4 = build({"recent": [], "shelf_start_in_shelf": True}, book_path=str(f))
    assert loaded == [str(f)] and not w4._shelf_visible()          # 命令行指定了书：交给 load_book（它负责切到阅读页）
    for w in wins:
        w._progress_timer.stop()
        w.close()
        w.deleteLater()


def test_toggle_with_ctrl_b_action_and_esc(win):
    # 没有打开过书：取消勾选无处可去，留在书架
    win.shelf_act.trigger()
    assert win._shelf_visible() and win.shelf_act.isChecked()
    win.book_path = "x.txt"                           # 有书可返回
    win.shelf_act.trigger()
    assert not win._shelf_visible() and not win.shelf_act.isChecked() and win.stack.currentWidget() is win.view
    key(win, Qt.Key.Key_Escape)                       # 阅读页 Esc → 书架
    assert win._shelf_visible() and win.shelf_act.isChecked()
    win.shelf_act.trigger()                           # 再回阅读页
    assert not win._shelf_visible()
    win.shelf_act.trigger()                           # Ctrl+B 同一个 action（勾选切换）
    assert win._shelf_visible()


def test_reader_actions_disabled_on_shelf(win):
    win.book_path = "x.txt"
    acts = [win.tts_act, win.stop_act, win.bilingual_act, win.sleep_act, win.ai_menu_act, win.single_act]
    assert win._reader_acts and all(a in win._reader_acts for a in acts)
    assert all(not a.isEnabled() for a in win._reader_acts) and not win.search_box.isEnabled()   # 书架可见
    win.show_reader()
    assert all(a.isEnabled() for a in win._reader_acts if a is not win.stop_act)
    assert not win.stop_act.isEnabled()               # 没在朗读：停止仍不可用（不能被一律启用）
    assert win.search_box.isEnabled()
    win.tts_on = True
    win._update_tts_actions()
    assert win.stop_act.isEnabled()
    win.show_shelf()
    assert not win.stop_act.isEnabled()
    win.tts_on = False
    win._update_tts_actions()


def test_paging_keys_never_reach_hidden_reader_page(win, monkeypatch):
    flips, pages = [], []
    monkeypatch.setattr(win.view, "flip", lambda d: flips.append(d))
    win.view.pageChanged.connect(pages.append)
    searched = []
    monkeypatch.setattr(win, "_search_next", lambda: searched.append(1))
    monkeypatch.setattr(win, "_search_prev", lambda: searched.append(-1))
    assert win._shelf_visible()
    for k in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_PageUp, Qt.Key.Key_PageDown, Qt.Key.Key_Space):
        key(win, k)
    key(win, Qt.Key.Key_F3)
    key(win, Qt.Key.Key_F3, Qt.KeyboardModifier.ShiftModifier)
    assert flips == [] and pages == [] and searched == []
    # 回到阅读页：同样的键恢复翻页
    win.book_path = "x.txt"
    win.show_reader()
    key(win, Qt.Key.Key_Left); key(win, Qt.Key.Key_PageUp)
    key(win, Qt.Key.Key_Right); key(win, Qt.Key.Key_PageDown); key(win, Qt.Key.Key_Space)
    assert flips == [-1, -1, 1, 1, 1]
    key(win, Qt.Key.Key_F3)
    assert searched == [1]


def test_real_flip_on_shelf_emits_no_pagechanged(win):
    """不打桩：真实的 view.flip 在书架页下不会被键盘调用，因此没有 pageChanged / 进度推送。"""
    pages = []
    win.view.pageChanged.connect(pages.append)
    pushed = []
    win.sync.push = lambda *a, **k: pushed.append(a)
    for k in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Space):
        key(win, k)
    assert pages == [] and pushed == []


def test_ctrl_f_focus_rules(win, monkeypatch):
    focused = []
    monkeypatch.setattr(win.shelf, "focus_search", lambda: focused.append("shelf"))
    monkeypatch.setattr(win.search_box, "setFocus", lambda *a: focused.append("toolbar"))
    key(win, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
    assert focused == ["shelf"]
    win.book_path = "x.txt"
    win.show_reader()
    key(win, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
    assert focused == ["shelf", "toolbar"]


def test_shelf_search_focus_and_escape_clears_before_leaving(win):
    win.shelf.show()
    win.activateWindow()
    win.shelf.set_query("abc")
    key(win, Qt.Key.Key_Escape)                       # 书架上 Esc：先清搜索
    assert win.shelf.search.text() == "" and win._shelf_visible()
    key(win, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
    QApplication.processEvents()
    assert win.shelf.search.hasFocus() or not win.isActiveWindow()    # offscreen 下窗口可能未被激活：此时只验证没抛异常


def test_progress_timer_guard(win, monkeypatch):
    saves = []
    monkeypatch.setattr(win, "_save_progress", lambda force=True: saves.append(force))
    win._last_saved_offset = win.view.left_page_offset()
    assert win._shelf_visible()
    win._progress_timer.timeout.emit()                # 经由真实的 QTimer 信号接到带守卫的槽
    assert saves == []                                # 书架可见且位置未变：不保存
    win._last_saved_offset = -5                       # 位置变了（例如远端进度刚应用）
    win._on_progress_tick()
    assert saves == [False]
    win.book_path = "x.txt"
    win.show_reader()
    win._last_saved_offset = win.view.left_page_offset()
    win._on_progress_tick()
    assert saves == [False, False]                    # 阅读页可见：照旧交给 _save_progress（它自己判断位置是否变化）
    assert win._progress_timer.isActive() and win._progress_timer.interval() == 30000


def test_show_shelf_saves_progress_once_and_switching_is_idempotent(win, monkeypatch):
    win.book_path = "x.txt"
    win.show_reader()
    saves = []
    monkeypatch.setattr(win, "_save_progress", lambda force=True: saves.append(force))
    win.show_shelf()
    win.show_shelf()
    assert saves == [False] and win._shelf_visible()
    win.show_reader(); win.show_reader()
    assert not win._shelf_visible()


def test_load_book_switches_page_before_loader_starts(win, monkeypatch):
    import pyreader.mainwin as mw
    seen = {}

    class FakeLoader:
        def __init__(self, path):
            seen["page"] = win.stack.currentIndex()
            seen["shelf_visible"] = win._shelf_visible()
            seen["h"], seen["stack_h"] = win.view.height(), win.stack.height()
            from PySide6.QtCore import QObject, Signal

            class S(QObject):
                progress = Signal(int, str); loaded = Signal(str, list, str, object); failed = Signal(str)
            s = S()
            self.progress, self.loaded, self.failed = s.progress, s.loaded, s.failed
            self._s = s

        def run(self):
            pass
    monkeypatch.setattr(mw, "BookLoader", FakeLoader)
    assert win._shelf_visible()
    win.load_book(__file__)
    assert seen["page"] == 1 and not seen["shelf_visible"]          # loader 构造时阅读页已是当前页
    assert seen["h"] == seen["stack_h"] > 100                       # 且已经拿到真实几何（不是隐藏页的旧尺寸）
    assert win.shelf_act.isChecked() is False
    bid = sync.book_id(__file__)
    assert bid in win.shelf._opened                                  # 打开记录进了书架索引


def test_open_from_shelf_failure_returns_to_shelf(win, monkeypatch):
    import pyreader.mainwin as mw
    monkeypatch.setattr(mw.QMessageBox, "critical", lambda *a, **k: None)
    monkeypatch.setattr(win, "load_book", lambda p: win.show_reader())     # 只验证页面逻辑，不真的起 loader
    win._open_from_shelf("x.txt")
    assert not win._shelf_visible() and win._shelf_open_pending
    win._on_load_failed("boom")
    assert win._shelf_visible() and not win._shelf_open_pending
    win.show_reader()
    win._on_load_failed("boom")                                      # 不是从书架打开的失败：留在阅读页
    assert not win._shelf_visible()


def test_open_book_from_shelf_end_to_end_with_fake_loader(win, monkeypatch, tmp_path):
    book = tmp_path / "b.txt"
    book.write_text("hello")
    opened = []
    monkeypatch.setattr(win, "load_book", lambda p: (win.show_reader(), opened.append(p)))
    win.shelf.openRequested.emit(str(book))
    assert opened == [str(book)] and not win._shelf_visible()


def test_resize_while_on_shelf_defers_relayout_until_reader(win):
    win.pager = SimpleNamespace()                     # 假装有一本书（resizeEvent / show_reader 只看它是否存在）
    win.book_path = "x.txt"
    try:
        win._resize_timer.stop()
        assert win._shelf_visible()
        win.resize(900, 650)
        QApplication.processEvents()
        assert win._resized_on_shelf and not win._resize_timer.isActive()    # 书架下不重排隐藏页
        win.show_reader()
        assert win._resize_timer.isActive() and not win._resized_on_shelf      # 回阅读页后按新尺寸重排
        win._resize_timer.stop()
        win.resize(1000, 700)
        QApplication.processEvents()
        assert win._resize_timer.isActive()                                   # 阅读页下仍是原来的防抖重排
    finally:
        win._resize_timer.stop()
        win.pager = None


def test_book_loaded_while_on_shelf_marks_relayout(win, monkeypatch):
    win._resized_on_shelf = False
    def stop(*a, **k):
        raise RuntimeError("stop here")
    monkeypatch.setattr(win.view, "set_layout", stop)     # 只验证入口处的标记；其余部分需要真实分页器，让它在标记之后中止
    with pytest.raises(RuntimeError, match="stop here"):
        win._on_book_loaded("text", [], "t", None)
    assert win._resized_on_shelf is True


def test_save_progress_feeds_shelf_with_percent(win, monkeypatch):
    p = __file__
    win.book_path = p
    win._loaded_path = p
    win.full_text = "x" * 200
    win.pager = SimpleNamespace(chapters=[SimpleNamespace(title="c")])
    win.cur_chapter = 0
    monkeypatch.setattr(win.view, "left_page_offset", lambda: 150)
    monkeypatch.setattr(win.sync, "push", lambda cfg, rec: None)
    try:
        win._save_progress(force=True)
    finally:
        win.pager = None
    rec = win.shelf._progress[sync.book_id(p)]
    assert rec["percent"] == 75.0 and rec["updated"] > 0
    assert win.bookmarks[p]["_progress_"] == 150                     # 原有行为不变


def test_library_dir_change_triggers_rescan(win, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(win.shelf, "rescan", lambda force=False: calls.append(force))
    monkeypatch.setattr("pyreader.mainwin.save_json", lambda *a, **k: None)
    monkeypatch.setattr("pyreader.mainwin.QFileDialog.getExistingDirectory", lambda *a, **k: str(tmp_path))
    win._add_library_dir()
    win._remove_library_dir(os.path.normpath(str(tmp_path)))
    assert calls == [True, True]


def test_close_flushes_shelf(win, monkeypatch):
    flushed = []
    monkeypatch.setattr(win.shelf, "flush", lambda: flushed.append(1))
    win.close()
    assert flushed == [1]


def test_real_key_events_through_qt_dispatch(win, monkeypatch):
    """用 QTest 发真实按键（走 Qt 的快捷键 / 事件分发，而不是直接调 keyPressEvent）。"""
    win.activateWindow()
    QApplication.processEvents()
    if not win.isActiveWindow():
        pytest.skip("offscreen 平台没有激活窗口，快捷键无法分发")
    flips = []
    monkeypatch.setattr(win.view, "flip", lambda d: flips.append(d))
    win.book_path = "x.txt"
    for k in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_PageDown, Qt.Key.Key_Space):
        QTest.keyClick(win, k)                       # 书架可见：不翻页
    assert flips == [] and win._shelf_visible()
    QTest.keyClick(win, Qt.Key.Key_B, Qt.KeyboardModifier.ControlModifier)     # Ctrl+B -> 阅读页
    assert not win._shelf_visible()
    QTest.keyClick(win, Qt.Key.Key_Right)
    assert flips == [1]
    QTest.keyClick(win, Qt.Key.Key_Escape)           # Esc -> 书架
    assert win._shelf_visible()
    QTest.keyClick(win, Qt.Key.Key_Escape)           # 书架上的 Esc（搜索框为空）：不离开、不报错
    assert win._shelf_visible()
    QTest.keyClick(win.shelf.grid, Qt.Key.Key_Right)  # 焦点在书架的列表里：方向键属于列表，不会传给阅读页
    QTest.keyClick(win, Qt.Key.Key_B, Qt.KeyboardModifier.ControlModifier)
    assert not win._shelf_visible() and flips == [1]
