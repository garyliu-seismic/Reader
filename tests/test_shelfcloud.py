"""阶段 3：云端书库并入书架的界面层（offscreen Qt + 进程内 fake WebDAV）。
在模型 / 信号层验证：归并、角标、已移除、离线缓存、下载后打开的 token 规则、首次打开合并卡片时的进度种入。
GUI 的真实点击 / 外观无法在此验证。"""
import os
import threading
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from pyreader import shelf as sh  # noqa: E402
from pyreader import shelfsync as ss  # noqa: E402
from pyreader import shelfview as sv  # noqa: E402
from pyreader import sync  # noqa: E402
from pyreader.webdav import WebDavClient  # noqa: E402

OLD = 1000.0


@pytest.fixture(scope="module", autouse=True)
def qapp():
    return QApplication.instance() or QApplication([])


def wait_for(cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        QApplication.processEvents()
        if cond():
            return True
        time.sleep(0.005)
    QApplication.processEvents()
    return cond()


def put_book(h, rel, data=b"x", mtime=OLD):
    segs = rel.split("/")
    h.dirs.update(["/dav", "/dav/library"] + ["/dav/library/" + "/".join(segs[:i]) for i in range(1, len(segs))])
    p = "/dav/library/" + rel
    h.files[p] = data
    h.mtimes[p] = mtime
    h.etags[p] = '"e%d"' % (len(h.files) + len(data))
    return p


@pytest.fixture
def rb(tmp_path, monkeypatch):
    d = tmp_path / "remote_books"
    monkeypatch.setattr(sync, "REMOTE_BOOKS_DIR", str(d))
    return d


def make_view(tmp_path, url, dirs=(), cloud=None, root="home", cfg=None):
    cfg = cfg if cfg is not None else ({"webdav_enabled": True, "webdav_url": url} if url else {})
    ctx = {"library_dirs": list(dirs), "recent": [], "bookmarks": {}, "cfg": cfg}
    v = sv.ShelfView(lambda: ctx, root=str(tmp_path / root), cloud=cloud)
    v.ctx = ctx
    v.synced = []
    v.cloudSynced.connect(v.synced.append)
    v.opened = []
    v.openRequested.connect(v.opened.append)
    return v


def settle(v):
    assert wait_for(lambda: not v.scanning and not v._cloud_busy and not v._cloud._running), "云端同步 / 扫描没有在时限内结束"


def sync_wait(v):
    n = len(v.synced)
    assert v.sync_cloud(force=True) is True
    assert wait_for(lambda: len(v.synced) > n), "云端列表没有并入"
    settle(v)


def sync_fail_wait(v):
    assert v.sync_cloud(force=True) is True
    assert wait_for(lambda: v._cloud_error != "")
    settle(v)


def rel_book(v, rel):
    return v.books.get("remote:" + rel)


# ================================================================== 未启用
def test_cloud_disabled_is_a_noop(tmp_path, rb):
    v = make_view(tmp_path, None)
    assert not v.cloud_enabled() and v.sync_cloud(force=True) is False and v.sync_cloud_if_stale() is False
    assert v.model.show_badges is False and "云端" not in v.status.text()
    assert v._cloud.generation == 0 and not v._cloud._running
    v.rescan(); settle(v)
    assert v.books == {} and not os.path.exists(rb)               # 没有云端：不扫 remote_books，不建目录


# ================================================================== 归并 / 角标
def test_merge_by_source_dedupe_and_badges(server, tmp_path, rb):
    h, url = server
    put_book(h, "小说/三体.txt", b"x" * 10)
    put_book(h, "小说/科幻/沙丘.txt", b"y" * 7)
    put_book(h, "杂项.txt", b"z" * 3)
    put_book(h, "别处/三体.txt", b"x" * 10)                       # 另一个文件夹里的同名同大小：各自独立
    lib = tmp_path / "mine"
    lib.mkdir()
    (lib / "三体.txt").write_bytes(b"q" * 10)                     # 本机同名同大小，来源不同：不合并
    (lib / "仅我有.txt").write_bytes(b"m" * 4)
    v = make_view(tmp_path, url, [str(lib)])
    assert v.model.show_badges is True
    sync_wait(v)
    ids = sorted(v.books)
    cloud_ids = [i for i in ids if i.startswith("remote:")]
    assert cloud_ids == sorted(["remote:小说/三体.txt", "remote:小说/科幻/沙丘.txt", "remote:杂项.txt", "remote:别处/三体.txt"])
    assert len([i for i in ids if i.startswith("local:")]) == 2   # 本机两本；同名同大小的云端书没有吞掉本机的
    local_santi = next(b for b in v.books.values() if b.where == "local" and b.title == "三体")
    assert sorted(local_santi.same_as) == ["remote:别处/三体.txt", "remote:小说/三体.txt"]      # 只是「可能是同一本书」提示
    assert rel_book(v, "小说/三体.txt").same_as == [local_santi.id]
    assert rel_book(v, "别处/三体.txt").id != rel_book(v, "小说/三体.txt").id
    # 角标
    assert v.model.badge(rel_book(v, "杂项.txt")) == "仅云端"
    assert v.model.badge(local_santi) == "仅本地"
    assert rel_book(v, "杂项.txt").shelf == sh.CLOUD and rel_book(v, "小说/科幻/沙丘.txt").shelf == "cloud:小说/科幻"
    # 侧栏：云端书库 + 未分类
    items = [v.sidebar.item(i).text() for i in range(v.sidebar.count())]
    assert "云端书库" in items and any(t.startswith("未分类") for t in items)
    assert any(t.strip().startswith("小说") for t in items) and any("沙丘" not in t and "科幻" in t for t in items)
    assert "云端：已同步" in v.status.text()


def test_hostile_names_are_shown_as_plain_text(server, tmp_path, rb):
    h, url = server
    put_book(h, "<img src=x onerror=1>.txt", b"1")
    v = make_view(tmp_path, url)
    sync_wait(v)
    b = rel_book(v, "<img src=x onerror=1>.txt")
    tip = v.model.data(v.model.index(v.model.row_of(b.id) if v.model.row_of(b.id) >= 0 else 0, 0), 3)    # ToolTipRole
    assert "<img" not in tip and "&lt;img" in tip
    assert v.status.textFormat().name == "PlainText" and v.empty_label.textFormat().name == "PlainText"


def test_downloaded_copy_is_one_card_synced(server, tmp_path, rb):
    h, url = server
    put_book(h, "小说/三体.txt", b"x" * 10)
    v = make_view(tmp_path, url)
    sync_wait(v)
    b = rel_book(v, "小说/三体.txt")
    assert b.where == "cloud" and not b.local_paths
    (rb / "小说").mkdir(parents=True)
    (rb / "小说" / "三体.txt").write_bytes(b"x" * 10)           # 缓存副本落地（路径恰好是 remote_books/<rel>）
    v.rescan(force=True); settle(v)
    b = rel_book(v, "小说/三体.txt")
    assert b.where == "synced" and len(b.local_paths) == 1 and v.model.badge(b) == "已下载"
    assert sum(1 for x in v.books.values() if x.title == "三体") == 1     # 一张卡片，不重复


# ================================================================== 云端已移除
def test_removed_needs_two_complete_listings_via_view(server, tmp_path, rb):
    h, url = server
    put_book(h, "a/甲.txt", b"1" * 5)
    put_book(h, "乙.txt", b"2" * 5)
    v = make_view(tmp_path, url)
    sync_wait(v)
    h.files.pop("/dav/library/a/甲.txt")                           # 别人把它删了
    sync_wait(v)                                                   # 第一轮完整列表缺失
    b = rel_book(v, "a/甲.txt")
    assert b is not None and b.where == "cloud" and not b.removed  # 还不能下结论：仍显示为仅云端
    sync_wait(v)                                                   # 第二轮
    b = rel_book(v, "a/甲.txt")
    assert b.where == "removed" and b.removed and v.model.badge(b) == "云端已移除"
    assert [x.id for x in v.visible_books()] == ["remote:乙.txt"]  # 默认隐藏（本机没有副本）
    v.set_shelf("removed")
    assert [x.id for x in v.visible_books()] == ["remote:a/甲.txt"]
    items = [v.sidebar.item(i).text() for i in range(v.sidebar.count())]
    assert any(t.startswith("云端已移除") for t in items)
    # 重新出现：不再是已移除
    put_book(h, "a/甲.txt", b"1" * 5)
    sync_wait(v)
    assert rel_book(v, "a/甲.txt").where == "cloud"


def test_removed_with_local_copy_stays_readable(server, tmp_path, rb):
    h, url = server
    put_book(h, "甲.txt", b"1" * 5)
    v = make_view(tmp_path, url)
    sync_wait(v)
    rb.mkdir(parents=True)
    (rb / "甲.txt").write_bytes(b"1" * 5)
    v.rescan(force=True); settle(v)
    h.files.pop("/dav/library/甲.txt")
    sync_wait(v); sync_wait(v)
    b = rel_book(v, "甲.txt")
    assert b.where == "removed" and b.local_paths and not b.auto_hidden       # 本机有副本：保留可读
    assert b in v.visible_books() or v.set_shelf("all") is None
    assert b in sh.filter_books(v.books, "all")


def test_subtree_failure_is_not_removal_and_is_reported(server, tmp_path, rb):
    h, url = server
    put_book(h, "a/甲.txt", b"1" * 5)
    put_book(h, "b/乙.txt", b"2" * 5)
    v = make_view(tmp_path, url)
    sync_wait(v)
    h.fail_propfind.add("/dav/library/b")                          # b 子树读取失败
    for _ in range(3):
        sync_wait(v)
        assert rel_book(v, "b/乙.txt").where == "cloud"            # 仍然显示，绝不判为已移除
    assert not v._listing.complete and "不完整" in v.status.text() and "暂不判断" in v.status.text()
    assert v._catalog.removed_ids() == set()
    h.fail_propfind.clear()
    sync_wait(v)
    assert "不完整" not in v.status.text()


def test_rclone_style_move_via_view(server, tmp_path, rb):
    h, url = server
    put_book(h, "旧/书.txt", b"1" * 5)
    v = make_view(tmp_path, url)
    sync_wait(v)
    put_book(h, "新/书.txt", b"1" * 5)                              # 先复制
    sync_wait(v)
    assert {rel_book(v, "旧/书.txt").where, rel_book(v, "新/书.txt").where} == {"cloud"}
    h.files.pop("/dav/library/旧/书.txt")                          # 再删旧的
    sync_wait(v)
    assert rel_book(v, "旧/书.txt").where == "cloud"               # 一轮缺失：还没结论
    sync_wait(v)
    assert rel_book(v, "旧/书.txt").where == "removed" and rel_book(v, "新/书.txt").where == "cloud"


def test_missing_counts_persist_across_view_restart(server, tmp_path, rb):
    h, url = server
    put_book(h, "甲.txt", b"1" * 5)
    put_book(h, "乙.txt", b"2" * 5)
    v = make_view(tmp_path, url)
    sync_wait(v)
    h.files.pop("/dav/library/甲.txt")
    sync_wait(v)                                                   # 缺失 1 轮
    v.flush()
    v2 = make_view(tmp_path, url)                                  # 重启
    assert rel_book(v2, "甲.txt") is not None and v2._catalog.missing["remote:甲.txt"]["n"] == 1
    sync_wait(v2)
    assert rel_book(v2, "甲.txt").where == "removed"


# ================================================================== 半截上传
def test_half_uploaded_file_is_hidden_until_stable(server, tmp_path, rb):
    h, url = server
    put_book(h, "完整.txt", b"1" * 5)
    put_book(h, "上传中.txt", b"2" * 5, mtime=time.time() - 5)    # 5 秒前才写入
    v = make_view(tmp_path, url)
    sync_wait(v)
    assert rel_book(v, "完整.txt") is not None and rel_book(v, "上传中.txt") is None
    assert "1 本正在上传" in v.status.text()
    h.mtimes["/dav/library/上传中.txt"] = OLD                      # 过了一阵
    sync_wait(v)
    assert rel_book(v, "上传中.txt").where == "cloud"
    # 它曾经不在显示列表里，但不会因此被当成「消失」
    assert v._catalog.missing == {}


# ================================================================== 离线保留缓存
def test_offline_keeps_cache_and_cold_start_shows_cloud_books(server, tmp_path, rb):
    h, url = server
    put_book(h, "小说/三体.txt", b"x" * 10)
    put_book(h, "杂项.txt", b"z" * 3)
    v = make_view(tmp_path, url)
    sync_wait(v)
    v.flush()
    assert sorted(v.books) == ["remote:小说/三体.txt", "remote:杂项.txt"]
    v.ctx["cfg"]["webdav_url"] = "http://127.0.0.1:9/dav"          # 断网（端口 9 连不上）
    sync_fail_wait(v)
    assert sorted(v.books) == ["remote:小说/三体.txt", "remote:杂项.txt"]    # 缓存原样保留
    assert "离线" in v.status.text() and "显示本地缓存" in v.status.text()
    assert v._catalog.missing == {}                                 # 失败不产生任何「消失」结论
    # 冷启动且离线：index.json 先画出云端书库，扫描后仍在
    v2 = make_view(tmp_path, "http://127.0.0.1:9/dav")
    assert sorted(v2.books) == ["remote:小说/三体.txt", "remote:杂项.txt"]
    assert v2.model.badge(v2.books["remote:杂项.txt"]) == "仅云端"
    v2.rescan(); settle(v2)
    assert sorted(v2.books) == ["remote:小说/三体.txt", "remote:杂项.txt"]
    # 恢复网络后继续工作
    v2.ctx["cfg"]["webdav_url"] = url
    sync_wait(v2)
    assert v2._cloud_error == "" and "已同步" in v2.status.text()


def test_failure_never_raises_into_ui_and_listing_in_flight_is_single(server, tmp_path, rb):
    v = make_view(tmp_path, "http://127.0.0.1:9/dav")
    sync_fail_wait(v)
    assert v._cloud_error and not v._cloud_busy
    assert v.sync_cloud() is False                                  # 60 秒内不重复（非 force）


# ================================================================== 下载后打开（token）
class Gated(WebDavClient):
    gate = None

    def get_to_file(self, *a, **k):
        if Gated.gate is not None:
            Gated.gate.wait(10)
        return super().get_to_file(*a, **k)


def gated_view(tmp_path, url, show=True):
    Gated.gate = threading.Event()
    w = ss.ShelfSyncWorker(client_factory=lambda cfg: Gated(url))
    v = make_view(tmp_path, url, cloud=w)
    if show:
        v.show()
    return v


def test_double_click_cloud_book_downloads_then_opens(server, tmp_path, rb):
    h, url = server
    put_book(h, "小说/三体.txt", b"x" * 10)
    v = make_view(tmp_path, url)
    v.show()
    sync_wait(v)
    b = rel_book(v, "小说/三体.txt")
    assert v.open_book(b) is True and v.model.busy == {b.id} and "正在下载" in v.status.text()
    assert v.model.badge(b) == "下载中…"
    assert wait_for(lambda: v.opened)
    dest = str(rb / "小说" / "三体.txt")
    assert v.opened == [dest] and open(dest, "rb").read() == b"x" * 10
    assert v.model.busy == set() and not [f for f in os.listdir(rb / "小说") if f.endswith(".part")]
    settle(v)
    assert rel_book(v, "小说/三体.txt").where == "synced"           # 下载后重新归并：已下载
    v.opened.clear()
    assert v.open_book(rel_book(v, "小说/三体.txt")) is True and v.opened == [dest]   # 已有副本：直接打开，不再下载
    assert len([r for r in h.log if r[0] == "GET" and r[1].endswith("三体.txt")]) == 1


def test_download_only_does_not_open(server, tmp_path, rb):
    h, url = server
    put_book(h, "a.txt", b"12345")
    v = make_view(tmp_path, url)
    v.show()
    sync_wait(v)
    assert v.request_download(rel_book(v, "a.txt"), open_when_done=False)
    assert wait_for(lambda: (rb / "a.txt").exists() and not v._dl)
    settle(v)
    assert v.opened == [] and rel_book(v, "a.txt").where == "synced"


def test_late_completion_after_user_opened_something_else_is_ignored(server, tmp_path, rb):
    h, url = server
    put_book(h, "a.txt", b"12345")
    v = gated_view(tmp_path, url)
    sync_wait(v)
    assert v.open_book(rel_book(v, "a.txt"))
    v.note_opened(str(tmp_path / "other.txt"))                      # 下载还在进行时，用户打开了别的书
    Gated.gate.set()
    assert wait_for(lambda: (rb / "a.txt").exists() and not v._dl)
    settle(v)
    assert v.opened == []                                           # 迟到的完成不劫持当前窗口
    assert rel_book(v, "a.txt").where == "synced" and v.model.busy == set()    # 但文件已缓存、卡片已更新


def test_superseded_open_request_only_latest_opens(server, tmp_path, rb):
    h, url = server
    put_book(h, "a.txt", b"12345")
    put_book(h, "b.txt", b"67890")
    v = gated_view(tmp_path, url)
    sync_wait(v)
    v.open_book(rel_book(v, "a.txt"))
    v.open_book(rel_book(v, "b.txt"))                               # 后到的打开意图取代先前的
    Gated.gate.set()
    assert wait_for(lambda: v.opened and not v._dl)
    settle(v)
    assert v.opened == [str(rb / "b.txt")] and (rb / "a.txt").exists()


def test_same_book_double_click_opens_once(server, tmp_path, rb):
    h, url = server
    put_book(h, "a.txt", b"12345")
    v = gated_view(tmp_path, url)
    sync_wait(v)
    b = rel_book(v, "a.txt")
    v.open_book(b)
    v.open_book(b)
    Gated.gate.set()
    assert wait_for(lambda: v.opened and not v._dl)
    settle(v)
    assert v.opened == [str(rb / "a.txt")]
    assert len([r for r in h.log if r[0] == "GET" and r[1].endswith("a.txt")]) == 1


def test_completion_while_shelf_hidden_does_not_open(server, tmp_path, rb):
    h, url = server
    put_book(h, "a.txt", b"12345")
    v = gated_view(tmp_path, url, show=False)                       # 用户已经切到阅读页（书架不可见）
    sync_wait(v)
    v.open_book(rel_book(v, "a.txt"))
    Gated.gate.set()
    assert wait_for(lambda: not v._dl)
    settle(v)
    assert v.opened == [] and (rb / "a.txt").exists()


def test_download_size_mismatch_is_discarded_and_reported(server, tmp_path, rb):
    h, url = server
    put_book(h, "a.txt", b"12345")
    v = make_view(tmp_path, url)
    v.show()
    sync_wait(v)
    h.files["/dav/library/a.txt"] = b"1234567890"                   # 列表之后文件被换了：大小与列表不符
    assert v.open_book(rel_book(v, "a.txt"))
    assert wait_for(lambda: not v._dl)
    settle(v)
    assert v.opened == [] and not (rb / "a.txt").exists() and not (rb / "a.txt.part").exists()
    assert "下载失败" in v.status.text() and v.model.busy == set()
    assert rel_book(v, "a.txt").where == "cloud"


def test_download_offline_reports_and_never_raises(server, tmp_path, rb):
    h, url = server
    put_book(h, "a.txt", b"12345")
    v = make_view(tmp_path, url)
    v.show()
    sync_wait(v)
    v.ctx["cfg"]["webdav_url"] = "http://127.0.0.1:9/dav"
    assert v.open_book(rel_book(v, "a.txt"))
    assert wait_for(lambda: not v._dl)
    assert v.opened == [] and "下载失败" in v.status.text()


def test_open_cloud_only_book_when_cloud_disabled_or_removed(server, tmp_path, rb):
    h, url = server
    put_book(h, "a.txt", b"12345")
    v = make_view(tmp_path, url)
    sync_wait(v)
    b = rel_book(v, "a.txt")
    v.ctx["cfg"]["webdav_enabled"] = False
    assert v.open_book(b) is False and v.opened == [] and not v._dl
    ghost = sh.Book("remote:g.txt", "g", "txt", 1, "cloud:", "", "removed", "g.txt")
    v.ctx["cfg"]["webdav_enabled"] = True
    assert v.open_book(ghost) is False and "云端已移除" in v.status.text() and not v._dl


# ================================================================== 首次打开合并卡片：种入本地进度
def _make_pair(tmp_path, rb, text, name="三体.txt", rel="小说/三体.txt"):
    lib = tmp_path / "mine"
    lib.mkdir(exist_ok=True)
    local = lib / name
    local.write_bytes(text)
    copy = rb / rel
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_bytes(text)
    return str(local), str(copy)


def test_progress_seed_pure_function(tmp_path, rb):
    text = ("第一章\n" + "春眠不觉晓。" * 400 + "\n").encode()
    local, copy = _make_pair(tmp_path, rb, text)
    bm = {local.replace("\\", "/"): {"_progress_": 600, "_progress_ts_": 1_700_000_000.0}}      # 正斜杠键也要认
    seed = sh.progress_seed(bm, copy)
    assert seed and seed["_progress_"] == 600 and seed["_progress_ts_"] == 1_700_000_000.0
    assert sh.norm_path(seed["source"]) == sh.norm_path(local)
    # 副本自己已经有进度：不种
    assert sh.progress_seed({**bm, copy: {"_progress_": 50}}, copy) is None
    # 副本进度为 0 视同没有
    assert sh.progress_seed({**bm, copy: {"_progress_": 0}}, copy)["_progress_"] == 600
    # 不是 remote_books 里的副本：不种
    assert sh.progress_seed(bm, local) is None
    # 内容不同（同名同大小但 fp 不同）：不种
    other = tmp_path / "mine" / "x"
    other.mkdir()
    (other / "三体.txt").write_bytes(b"Q" * len(text))
    assert sh.progress_seed({str(other / "三体.txt"): {"_progress_": 9, "_progress_ts_": 5.0}}, copy) is None
    # 文件名不同 / 大小不同 / 来源本身就在 remote_books / 文件已不存在 / 进度无效：不种
    (tmp_path / "mine" / "改名.txt").write_bytes(text)
    assert sh.progress_seed({str(tmp_path / "mine" / "改名.txt"): {"_progress_": 9}}, copy) is None
    (rb / "另一个").mkdir()
    (rb / "另一个" / "三体.txt").write_bytes(text)
    assert sh.progress_seed({str(rb / "另一个" / "三体.txt"): {"_progress_": 9}}, copy) is None
    assert sh.progress_seed({str(tmp_path / "gone" / "三体.txt"): {"_progress_": 9}}, copy) is None
    for bad in (None, "x", -1, float("nan"), float("inf"), 10 ** 400, True, 0):
        assert sh.progress_seed({local: {"_progress_": bad}}, copy) is None
    # 多个来源：取 _progress_ts_ 最新的
    lib2 = tmp_path / "mine2"
    lib2.mkdir()
    (lib2 / "三体.txt").write_bytes(text)
    bm2 = {local: {"_progress_": 100, "_progress_ts_": 10.0}, str(lib2 / "三体.txt"): {"_progress_": 300, "_progress_ts_": 20.0}}
    assert sh.progress_seed(bm2, copy)["_progress_"] == 300
    assert sh.progress_seed(None, copy) is None and sh.progress_seed({}, copy) is None
    assert bm == {local.replace("\\", "/"): {"_progress_": 600, "_progress_ts_": 1_700_000_000.0}}   # 只读，不改入参


@pytest.fixture
def win(tmp_path, monkeypatch):
    """真实窗口。配置 / 书签 / 书架数据全部指到临时目录：这些用例会启用 WebDAV、打开书（写 recent / 书签 / 索引），
    不能漏进共享的测试家目录，否则会影响后面用例（比如「没有最近阅读 → 启动进书架」）。"""
    import pyreader.mainwin as mw
    monkeypatch.setattr(mw, "CONFIG_PATH", str(tmp_path / "iso-config.json"))
    monkeypatch.setattr(mw, "BOOKMARKS_PATH", str(tmp_path / "iso-bookmarks.json"))
    monkeypatch.setattr(sh, "_default_root", lambda: str(tmp_path / "iso-home"))
    w = mw.MainWindow()
    w.resize(1000, 700)
    w.show()
    QApplication.processEvents()
    yield w
    w._progress_timer.stop()
    w._resize_timer.stop()
    w.close()
    w.deleteLater()


def _book_text():
    return "".join(f"第{i}章 标题{i}\n" + ("这是第%d章的正文，春眠不觉晓，处处闻啼鸟。\n" % i) * 120 for i in range(1, 41))


def test_first_open_of_merged_card_keeps_local_60_percent(win, server, tmp_path, rb):
    """用户在本机另一个路径上读到 60%；之后云端同名文件被下载成缓存副本：首次打开仍在 60%，并把进度推到远端。"""
    h, url = server
    text = _book_text()
    local, copy = _make_pair(tmp_path, rb, text.encode("utf-8"))
    off = int(len(text) * 0.6)
    win.cfg.update({"webdav_enabled": True, "webdav_url": url, "sync_user_name": "gary"})
    win.bookmarks[local] = {"_progress_": off, "_progress_ts_": 1_700_000_000.0}
    win.load_book(copy)
    assert wait_for(lambda: win._loaded_path == copy, 20)
    cur = win.view.left_page_offset()
    assert abs(cur - off) < 3000, (cur, off)                          # 打开后仍在 60% 附近（页首偏移）
    pct = cur * 100.0 / len(text)
    assert 55 < pct < 65
    assert win.bookmarks[copy]["_progress_ts_"] == 1_700_000_000.0 or win.bookmarks[copy]["_progress_"] > 0
    # 远端没有记录：种入的进度被推送一次（remote: id，百分比约 60）
    key = "/dav/" + sync.progress_path("gary", "remote:小说/三体.txt")
    assert wait_for(lambda: key in h.files, 10)
    rec = __import__("json").loads(h.files[key].decode())
    assert rec["book_id"] == "remote:小说/三体.txt" and 55 < rec["percent"] < 65 and rec["offset"] > 0


def test_first_open_without_local_progress_starts_at_top(win, server, tmp_path, rb):
    h, url = server
    text = _book_text()
    local, copy = _make_pair(tmp_path, rb, text.encode("utf-8"))
    win.cfg.update({"webdav_enabled": True, "webdav_url": url, "sync_user_name": "gary"})
    win.load_book(copy)
    assert wait_for(lambda: win._loaded_path == copy, 20)
    assert win.view.left_page_offset() < 3000
    time.sleep(0.3)
    QApplication.processEvents()
    assert "/dav/" + sync.progress_path("gary", "remote:小说/三体.txt") not in h.files      # 没种入：不推


def test_first_open_newer_remote_record_wins_over_seed(win, server, tmp_path, rb):
    h, url = server
    text = _book_text()
    local, copy = _make_pair(tmp_path, rb, text.encode("utf-8"))
    win.cfg.update({"webdav_enabled": True, "webdav_url": url, "sync_user_name": "gary"})
    win.bookmarks[local] = {"_progress_": int(len(text) * 0.6), "_progress_ts_": 1_700_000_000.0}
    remote_off = int(len(text) * 0.2)
    sync.push_progress(WebDavClient(url), "gary", sync.make_record(
        "remote:小说/三体.txt", remote_off, 20.0, "", "phone", updated=1_700_000_500.0))      # 手机上更晚读到 20%
    win.load_book(copy)
    assert wait_for(lambda: win._loaded_path == copy, 20)
    assert wait_for(lambda: abs(win.view.left_page_offset() - remote_off) < 3000, 10)         # 较新的远端记录取胜
