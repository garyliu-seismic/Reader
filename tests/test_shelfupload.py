"""阶段 4：手动上传（design §4.10）。进程内 fake WebDAV；界面部分用 offscreen Qt 在信号 / 模型层验证。
重点：.part + 完成校验、MOVE 不覆盖（Overwrite: F）、不支持 MOVE 时退化、撞名提示改名、绝不静默覆盖、绝不自动上传。"""
import os
import time
import unicodedata

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from pyreader import shelf as sh  # noqa: E402
from pyreader import shelfsync as ss  # noqa: E402
from pyreader import shelfview as sv  # noqa: E402
from pyreader import sync  # noqa: E402
from pyreader.webdav import WebDavClient  # noqa: E402

OLD = 1000


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


def raw_put(h, path, data, mtime=OLD):
    parts = path.strip("/").split("/")
    for i in range(1, len(parts)):
        h.dirs.add("/" + "/".join(parts[:i]))
    h.files[path] = data
    h.mtimes[path] = mtime
    h.etags[path] = '"e%d-%d"' % (next(h.counter), len(data))


def mk_file(tmp_path, name="沙丘.epub", size=200_000):
    d = tmp_path / "src"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_bytes((b"0123456789abcdef" * (size // 16 + 1))[:size])
    return str(p)


def puts(h):
    return [p for m, p in h.log if m == "PUT"]


def methods(h):
    return [m for m, _p in h.log]


# ================================================================== 名字 / 文件夹校验
def test_normalize_folder_and_upload_rel():
    nf = ss.normalize_folder
    assert nf("") == "" and nf(None) == "" and nf("/") == "" and nf("  ") == ""
    assert nf("小说") == "小说" and nf("/小说/科幻/") == "小说/科幻" and nf("a b/c") == "a b/c"
    for bad in ("..", "a/../b", "./a", ".hidden", "a/.hidden", "a\\b", "C:/x", "a:b", "CON", "~$tmp", "x/" * 9 + "y", "a/ /b",
                "x\x00y", "evil\u202e", 5, ["a"]):
        assert nf(bad) is None, bad
    ur = ss.upload_rel
    assert ur("", "三体.epub") == "三体.epub" and ur("小说", "三体.epub") == "小说/三体.epub"
    for bad in ("x.exe", "noext", ".三体.epub", "~$a.docx", "a/b.epub", "a\\b.epub", "CON.txt", "a:b.txt", " a.txt", "a.txt ",
                "x" * 241 + ".txt", "", None, "evil\u202e.txt"):
        assert ur("", bad) is None, bad
    assert ur("..", "a.txt") is None


def test_invalid_targets_send_no_requests(server, tmp_path):
    h, url = server
    c = WebDavClient(url)
    f = mk_file(tmp_path)
    for folder, name in (("..", "a.epub"), ("a/../b", "a.epub"), ("", ".x.epub"), ("", "a.exe"), ("", "x/y.epub"),
                         (".h", "a.epub"), ("", "CON.epub")):
        r = ss.upload_book_safe(c, f, folder, name)
        assert r.status == "error", (folder, name)
    assert h.log == []


def test_missing_empty_and_oversize_files(server, tmp_path):
    h, url = server
    c = WebDavClient(url)
    assert ss.upload_book_safe(c, str(tmp_path / "nope.epub"), "", "a.epub").status == "error"
    e = tmp_path / "e.epub"
    e.write_bytes(b"")
    assert ss.upload_book_safe(c, str(e), "", "a.epub").status == "error"
    big = mk_file(tmp_path, "big.epub", 5000)
    r = ss.upload_book_safe(c, big, "", "big.epub", max_bytes=1000)
    assert r.status == "error" and "上限" in r.message
    assert h.log == []


# ================================================================== 正常路径：.part + MOVE
def test_upload_via_part_then_move(server, tmp_path):
    h, url = server
    c = WebDavClient(url)
    f = mk_file(tmp_path, size=300_000)
    target = "/dav/library/小说/科幻/沙丘.epub"
    seen = {}
    h.put_hooks.append(lambda p, d: seen.update(part_path=p, target_present=target in h.files, size=len(d)))
    r = ss.upload_book_safe(c, f, "小说/科幻", "沙丘.epub")
    assert (r.status, r.method, r.rel) == ("ok", "move", "小说/科幻/沙丘.epub")
    assert h.files[target] == open(f, "rb").read()                           # 流式上传的内容完整
    assert seen["part_path"] == "/dav/library/小说/科幻/.沙丘.epub.part" and seen["target_present"] is False
    assert "/dav/library/小说/科幻/.沙丘.epub.part" not in h.files            # .part 已改名，不留残渣
    assert puts(h) == ["/dav/library/小说/科幻/.沙丘.epub.part"]               # 目标从未被直接 PUT：不会出现半截文件
    assert methods(h).count("MOVE") == 1
    hdrs = {(m, p): hd for m, p, hd in h.req_headers}
    assert hdrs[("PUT", "/dav/library/小说/科幻/.沙丘.epub.part")]["If-None-Match"] == "*"   # .part 不存在时带条件头
    mv = hdrs[("MOVE", "/dav/library/小说/科幻/.沙丘.epub.part")]
    assert mv["Overwrite"] == "F" and mv["Destination"].endswith("/dav/library/%E5%B0%8F%E8%AF%B4/%E7%A7%91%E5%B9%BB/%E6%B2%99%E4%B8%98.epub")
    # 对其它设备的列表：.part 阶段不可见，完成后可见
    lst = ss.fetch_library(c, now=time.time() + 1000)
    assert [x.rel for x in lst.files] == ["小说/科幻/沙丘.epub"]


def test_part_is_hidden_from_other_devices_during_upload(server, tmp_path):
    h, url = server
    c = WebDavClient(url)
    f = mk_file(tmp_path)
    snap = {}

    def hook(p, d):
        snap["rels"] = [x.rel for x in ss.fetch_library(WebDavClient(url), now=time.time() + 1000).files]
    h.put_hooks.append(hook)
    assert ss.upload_book_safe(c, f, "", "沙丘.epub").status == "ok"
    assert snap["rels"] == []                                                  # 上传进行中，别的设备的列表里没有半截文件


def test_upload_creates_missing_folder_and_root_upload(server, tmp_path):
    h, url = server
    c = WebDavClient(url)
    f = mk_file(tmp_path, "根.epub", 100)
    assert ss.upload_book_safe(c, f, "新/文件夹", "根.epub").status == "ok"
    assert "/dav/library/新/文件夹/根.epub" in h.files
    assert ss.upload_book_safe(c, f, "", "根2.epub").status == "ok"
    assert "/dav/library/根2.epub" in h.files


# ================================================================== 撞名：绝不静默覆盖
def test_name_clash_is_reported_and_nothing_is_written(server, tmp_path):
    h, url = server
    c = WebDavClient(url)
    raw_put(h, "/dav/library/小说/沙丘.epub", b"THEIRS")
    f = mk_file(tmp_path)
    h.log.clear()
    r = ss.upload_book_safe(c, f, "小说", "沙丘.epub")
    assert r.status == "exists" and "小说/沙丘.epub" in r.message
    assert h.files["/dav/library/小说/沙丘.epub"] == b"THEIRS"
    assert puts(h) == [] and "MOVE" not in methods(h)                         # 一个字节都没上传


@pytest.mark.parametrize("existing", ["DUNE.EPUB", unicodedata.normalize("NFD", "café.epub")])
def test_name_clash_ignores_case_and_unicode_normalization(server, tmp_path, existing):
    h, url = server
    c = WebDavClient(url)
    raw_put(h, "/dav/library/" + existing, b"THEIRS")
    name = "dune.epub" if existing == "DUNE.EPUB" else unicodedata.normalize("NFC", "café.epub")
    r = ss.upload_book_safe(c, mk_file(tmp_path, "any.epub"), "", name)
    assert r.status == "exists" and puts(h) == [] and h.files["/dav/library/" + existing] == b"THEIRS"


def test_two_uploaders_same_name_never_overwrite(server, tmp_path):
    """双上传同名：别人的最终文件在我们 MOVE 之前出现了 -> MOVE 带 Overwrite: F 回 412 -> 提示改名，对方内容原样保留。"""
    h, url = server
    c = WebDavClient(url)
    f = mk_file(tmp_path)
    target = "/dav/library/沙丘.epub"
    h.move_hooks.append(lambda s, d: raw_put(h, target, b"THEIRS"))           # 另一人恰好在这一刻完成了同名上传
    r = ss.upload_book_safe(c, f, "", "沙丘.epub")
    assert r.status == "exists"
    assert h.files[target] == b"THEIRS"
    assert len(h.files["/dav/library/.沙丘.epub.part"]) == 0                   # .part 已清空（不暴露 DELETE），下次可直接复用


def test_clash_appearing_while_part_uploads_is_detected(server, tmp_path):
    h, url = server
    c = WebDavClient(url)
    f = mk_file(tmp_path)
    h.put_hooks.append(lambda p, d: raw_put(h, "/dav/library/沙丘.epub", b"THEIRS") if p.endswith(".part") else None)
    r = ss.upload_book_safe(c, f, "", "沙丘.epub")
    assert r.status == "exists" and h.files["/dav/library/沙丘.epub"] == b"THEIRS" and "MOVE" not in methods(h)


def test_busy_when_someone_else_is_uploading_same_name(server, tmp_path):
    h, url = server
    c = WebDavClient(url)
    raw_put(h, "/dav/library/.沙丘.epub.part", b"half", mtime=int(time.time()))     # 刚刚还在写的 .part
    r = ss.upload_book_safe(c, mk_file(tmp_path), "", "沙丘.epub")
    assert r.status == "busy" and puts(h) == [] and h.files["/dav/library/.沙丘.epub.part"] == b"half"


def test_part_created_between_check_and_put_is_busy(server, tmp_path):
    """列表检查时没有 .part，PUT 时别人刚好建了：If-None-Match: * 回 412 -> busy，对方的 .part 不被覆盖。"""
    h, url = server
    c = WebDavClient(url)
    orig = c.put_file

    def racing(path, fileobj, size, headers=None, ok_codes=None):
        raw_put(h, "/dav/" + path, b"OTHER", mtime=int(time.time()))
        return orig(path, fileobj, size, headers, ok_codes)
    c.put_file = racing
    r = ss.upload_book_safe(c, mk_file(tmp_path), "", "沙丘.epub")
    assert r.status == "busy" and h.files["/dav/library/.沙丘.epub.part"] == b"OTHER"


@pytest.mark.parametrize("kind", ["old", "empty"])
def test_stale_part_is_reused(server, tmp_path, kind):
    h, url = server
    c = WebDavClient(url)
    if kind == "old":
        raw_put(h, "/dav/library/.沙丘.epub.part", b"half", mtime=int(time.time()) - 7200)
    else:
        raw_put(h, "/dav/library/.沙丘.epub.part", b"", mtime=int(time.time()))
    f = mk_file(tmp_path)
    r = ss.upload_book_safe(c, f, "", "沙丘.epub")
    assert r.status == "ok" and h.files["/dav/library/沙丘.epub"] == open(f, "rb").read()


# ================================================================== 完成校验 / 退化
def test_truncated_part_is_not_published(server, tmp_path):
    h, url = server
    c = WebDavClient(url)
    h.put_hooks.append(lambda p, d: d[:-1] if p.endswith(".part") else None)     # 服务器只存下了一部分
    r = ss.upload_book_safe(c, mk_file(tmp_path), "", "沙丘.epub")
    assert r.status == "error" and "不完整" in r.message
    assert "/dav/library/沙丘.epub" not in h.files and "MOVE" not in methods(h)


@pytest.mark.parametrize("code", [405, 501])
def test_server_without_move_falls_back_to_direct_put(server, tmp_path, code):
    h, url = server
    h.move_status = code
    c = WebDavClient(url)
    f = mk_file(tmp_path)
    r = ss.upload_book_safe(c, f, "小说", "沙丘.epub")
    assert (r.status, r.method) == ("ok", "put")
    assert h.files["/dav/library/小说/沙丘.epub"] == open(f, "rb").read()
    assert len(h.files["/dav/library/小说/.沙丘.epub.part"]) == 0                  # 退化后 .part 被清空
    hdrs = [hd for m, p, hd in h.req_headers if m == "PUT" and p.endswith("沙丘.epub") and not p.endswith(".part")]
    assert hdrs and hdrs[0]["If-None-Match"] == "*"                              # 直接 PUT 目标时仍带条件头


def test_fallback_put_does_not_overwrite_when_target_appears(server, tmp_path):
    h, url = server
    h.move_status = 501
    h.move_hooks.append(lambda s, d: raw_put(h, "/dav/library/沙丘.epub", b"THEIRS"))
    r = ss.upload_book_safe(WebDavClient(url), mk_file(tmp_path), "", "沙丘.epub")
    assert r.status == "exists" and h.files["/dav/library/沙丘.epub"] == b"THEIRS"


def test_post_upload_size_is_verified(server, tmp_path):
    h, url = server
    h.move_hooks.append(lambda s, d: h.files.__setitem__(s, h.files[s][:-3]))    # MOVE 之前内容被截（离谱的服务器）
    r = ss.upload_book_safe(WebDavClient(url), mk_file(tmp_path), "", "沙丘.epub")
    assert r.status == "error" and "校验失败" in r.message


def test_network_failure_returns_error_never_raises(tmp_path):
    c = WebDavClient("http://127.0.0.1:9/dav", timeout=2)
    r = ss.upload_book_safe(c, mk_file(tmp_path), "", "沙丘.epub")
    assert r.status == "error" and "上传失败" in r.message


def test_unauthorized_returns_error(auth_server, tmp_path):
    h, url = auth_server
    r = ss.upload_book_safe(WebDavClient(url, "bob", "wrong"), mk_file(tmp_path), "", "沙丘.epub")
    assert r.status == "error" and "401" in r.message
    assert ss.upload_book_safe(WebDavClient(url, "bob", "secret"), mk_file(tmp_path), "", "沙丘.epub").status == "ok"


# ================================================================== worker / 旧入口
def test_worker_upload_signals_and_noop_when_disabled(server, tmp_path):
    h, url = server
    w = ss.ShelfSyncWorker()
    got = []
    w.uploadDone.connect(lambda tok, res: got.append((tok, res)))
    assert w.upload({}, "x", "", "a.epub", 1) is None
    cfg = {"webdav_enabled": True, "webdav_url": url}
    t = w.upload(cfg, mk_file(tmp_path), "小说", "沙丘.epub", 7)
    t.join(15)
    assert wait_for(lambda: got)
    assert got[0][0] == 7 and got[0][1].status == "ok" and "/dav/library/小说/沙丘.epub" in h.files
    t = w.upload(cfg, mk_file(tmp_path), "小说", "沙丘.epub", 8)                  # 再传一次：撞名，不覆盖
    t.join(15)
    assert wait_for(lambda: len(got) == 2)
    assert got[1][1].status == "exists"


def test_legacy_sync_worker_upload_no_longer_overwrites(server, tmp_path):
    h, url = server
    raw_put(h, "/dav/library/up.txt", b"THEIRS")
    f = tmp_path / "up.txt"
    f.write_bytes(b"MINE")
    msgs = []
    w = sync.SyncWorker()
    w.status.connect(msgs.append)
    w.upload_book({"webdav_enabled": True, "webdav_url": url}, str(f)).join(15)
    QApplication.processEvents()
    assert h.files["/dav/library/up.txt"] == b"THEIRS" and msgs and "未覆盖" in msgs[-1]


# ================================================================== 界面层
def cfg_for(url):
    return {"webdav_enabled": True, "webdav_url": url, "sync_user_name": "gary", "device_name": "gary-pc"}


def make_view(tmp_path, url, dirs=(), root="home"):
    cfg = cfg_for(url) if url else {}
    ctx = {"library_dirs": list(dirs), "recent": [], "bookmarks": {}, "cfg": cfg}
    v = sv.ShelfView(lambda: ctx, root=str(tmp_path / root))
    v.ctx = ctx
    v.finished = []
    v.uploadFinished.connect(v.finished.append)
    v.synced = []
    v.cloudSynced.connect(v.synced.append)
    v.confirm_push = lambda: False
    return v


@pytest.fixture
def rb(tmp_path, monkeypatch):
    d = tmp_path / "remote_books"
    monkeypatch.setattr(sync, "REMOTE_BOOKS_DIR", str(d))
    return d


def test_view_upload_flow_with_rename_on_clash(server, tmp_path, rb):
    h, url = server
    raw_put(h, "/dav/library/小说/沙丘.epub", b"THEIRS")
    v = make_view(tmp_path, url)
    f = mk_file(tmp_path)
    seen = {}

    def chooser(items, folders):
        seen["items"], seen["folders"] = items, folders
        return "小说", [(items[0][0], items[0][1])]
    v.upload_chooser = chooser
    renames = []

    def renamer(name, folder, message):
        renames.append((name, folder, message))
        return "沙丘 (2).epub"
    v.rename_chooser = renamer
    assert v.start_upload([f]) is True
    assert wait_for(lambda: len(v.finished) == 2), [r.status for r in v.finished]
    assert [r.status for r in v.finished] == ["exists", "ok"]
    assert renames and renames[0][0] == "沙丘.epub" and renames[0][1] == "小说" and "已有同名" in renames[0][2]
    assert h.files["/dav/library/小说/沙丘.epub"] == b"THEIRS"                   # 原文件原样保留
    assert h.files["/dav/library/小说/沙丘 (2).epub"] == open(f, "rb").read()
    assert seen["items"] == [(f, "沙丘.epub")]
    assert wait_for(lambda: "已上传" in v._notice)


def test_view_rename_declined_skips_and_does_not_upload(server, tmp_path, rb):
    h, url = server
    raw_put(h, "/dav/library/沙丘.epub", b"THEIRS")
    v = make_view(tmp_path, url)
    v.upload_chooser = lambda items, folders: ("", [(items[0][0], items[0][1])])
    v.rename_chooser = lambda *a: None
    v.start_upload([mk_file(tmp_path)])
    assert wait_for(lambda: len(v.finished) == 1) and v.finished[0].status == "exists"
    assert wait_for(lambda: "已跳过" in v._notice)
    assert h.files["/dav/library/沙丘.epub"] == b"THEIRS" and puts(h) == []
    # 同名再次「改名」成同一个名字也不会覆盖
    v2 = make_view(tmp_path, url, root="home2")
    v2.upload_chooser = lambda items, folders: ("", [(items[0][0], items[0][1])])
    v2.rename_chooser = lambda name, *a: name
    v2.start_upload([mk_file(tmp_path)])
    assert wait_for(lambda: len(v2.finished) == 1) and puts(h) == [] and h.files["/dav/library/沙丘.epub"] == b"THEIRS"


def test_view_upload_is_never_automatic(server, tmp_path, rb):
    """打开书架、同步、读书、刷新、推送进度……任何自动流程都不会往 library/ 上传任何东西。"""
    h, url = server
    lib = tmp_path / "mine"
    lib.mkdir()
    (lib / "仅我有.txt").write_bytes(b"m" * 40)
    raw_put(h, "/dav/library/云端.txt", b"c" * 10)
    v = make_view(tmp_path, url, [str(lib)])
    v.confirm_push = lambda: True
    asked = []
    v.upload_chooser = lambda *a: asked.append(1)
    v.rescan(force=True)
    assert wait_for(lambda: not v.scanning)
    v.sync_cloud(force=True)
    assert wait_for(lambda: v.synced and not v._cloud_busy and not v._cloud._prog_running and not v.scanning)
    local = next(b for b in v.books.values() if b.where == "local")
    v.note_progress(local.local_paths[0], 30.0, 5000.0, "gary-pc")
    v.maybe_push()
    assert wait_for(lambda: not v.push_pending())
    v.open_book(local)
    assert asked == []
    lib_puts = [p for p in puts(h) if p.startswith("/dav/library")]
    assert lib_puts == [] and not any(m == "MOVE" for m in methods(h))
    assert [p for p in puts(h)] == [p for p in puts(h) if "/users/gary/shelf/" in p]    # 只写了本设备的摘要文件


def test_view_upload_guards(server, tmp_path, rb):
    h, url = server
    v = make_view(tmp_path, url)
    called = []
    v.upload_chooser = lambda items, folders: called.append(items)
    # 云端缓存副本（remote_books/ 下）已经在云端：跳过
    cached = rb / "小说" / "三体.txt"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"x")
    assert v.start_upload([str(cached)]) is False and "已经在云端" in v._notice and called == []
    # 不支持的格式 / 不存在的文件
    exe = tmp_path / "a.exe"
    exe.write_bytes(b"x")
    assert v.start_upload([str(exe), str(tmp_path / "ghost.txt")]) is False and called == []
    # 用户取消对话框：没有任何网络请求
    assert v.start_upload([mk_file(tmp_path)]) is False and len(called) == 1 and h.log == []
    # 未启用云同步
    v0 = make_view(tmp_path, None, root="h0")
    assert v0.start_upload([mk_file(tmp_path)]) is False and "未启用" in v0._notice


def test_view_queue_uploads_sequentially(server, tmp_path, rb):
    h, url = server
    v = make_view(tmp_path, url)
    a, b = mk_file(tmp_path, "甲.txt", 50), mk_file(tmp_path, "乙.txt", 60)
    v.upload_chooser = lambda items, folders: ("合集", [(p, n) for p, n in items])
    assert v.start_upload([a, b]) is True
    assert wait_for(lambda: len(v.finished) == 2) and all(r.status == "ok" for r in v.finished)
    assert sorted(k for k in h.files if k.startswith("/dav/library/合集/")) == ["/dav/library/合集/乙.txt", "/dav/library/合集/甲.txt"]


# ================================================================== 对话框模型
def test_upload_dialog_validation_and_result(tmp_path):
    f = mk_file(tmp_path, "a.epub", 10)
    d = sv.UploadDialog([(f, "a.epub")], ["小说", "小说/科幻"])
    ok = d.buttons.button(sv.QDialogButtonBox.StandardButton.Ok)
    assert ok.isEnabled() and d.result_value() == ("", [(f, "a.epub")])
    d.folder_box.setCurrentIndex(d.folder_box.findData("小说/科幻"))
    assert d.result_value() == ("小说/科幻", [(f, "a.epub")])
    d.name_edit.setText("../evil.epub")
    assert not ok.isEnabled() and d.result_value() is None and d.error.text()
    d.name_edit.setText("b.exe")
    assert not ok.isEnabled()
    d.name_edit.setText("新名字.epub")
    assert ok.isEnabled()
    d.folder_box.setCurrentIndex(d.folder_box.findData(sv.UploadDialog.NEW))
    d.new_edit.setText("..\\x")
    assert not ok.isEnabled() and d.folder() is None
    d.new_edit.setText("新建/子夹")
    assert ok.isEnabled() and d.result_value() == ("新建/子夹", [(f, "新名字.epub")])
    assert d.error.textFormat().name == "PlainText"
    g = mk_file(tmp_path, "g.epub", 10)
    multi = sv.UploadDialog([(f, "a.epub"), (g, "g.epub")], [])
    assert multi.name_edit is None and multi.result_value() == ("", [(f, "a.epub"), (g, "g.epub")])
    dup = sv.UploadDialog([(f, "a.epub"), (g, "A.epub")], [])
    assert dup.result_value() is None


def test_suggest_name():
    assert sv.suggest_name("沙丘.epub") == "沙丘 (2).epub"
    assert sv.suggest_name("noext") == "noext (2)"
    assert ss.upload_rel("", sv.suggest_name("沙丘.epub")) is not None


# ================================================================== 上传进度 / 状态栏反馈
class _Src:
    def __init__(self, data):
        self.data, self.pos = data, 0

    def read(self, n=-1):
        n = len(self.data) - self.pos if n is None or n < 0 else n
        chunk = self.data[self.pos:self.pos + n]
        self.pos += len(chunk)
        return chunk


def test_progress_reader_counts_throttles_and_always_reports_the_end():
    calls, t = [], [0.0]
    r = ss._ProgressReader(_Src(b"x" * 1000), 1000, lambda s, tot: calls.append((s, tot)), clock=lambda: t[0])
    t[0] = 1.0
    r.read(100)                                           # 10%：距上次 > 0.3s -> 回调
    t[0] = 1.1
    r.read(100)                                           # 20% 但只过了 0.1s -> 节流
    t[0] = 1.5
    r.read(100)                                           # 30%，过了 0.4s -> 回调
    r.read(10_000)                                        # 读完：无视节流，一定回调
    assert r.read(10) == b""                              # 结束后再读不再回调
    assert calls == [(100, 1000), (300, 1000), (1000, 1000)]


def test_progress_reader_callback_errors_do_not_break_the_upload():
    def boom(s, t):
        raise RuntimeError("ui gone")
    r = ss._ProgressReader(_Src(b"abc"), 3, boom)
    assert r.read(10) == b"abc"


def test_upload_book_safe_reports_monotonic_progress_ending_at_total(server, tmp_path):
    h, url = server
    f = mk_file(tmp_path, size=3_000_000)
    seen = []
    r = ss.upload_book_safe(WebDavClient(url), f, "小说", "大书.epub", progress=lambda s, t: seen.append((s, t)))
    assert r.status == "ok" and seen
    assert all(t == 3_000_000 for _, t in seen)
    assert [s for s, _ in seen] == sorted(s for s, _ in seen)
    assert seen[-1] == (3_000_000, 3_000_000)
    assert h.files["/dav/library/小说/大书.epub"] == open(f, "rb").read()          # 加了包装，内容仍然逐字节一致


def test_progress_is_also_reported_on_the_move_fallback_path(server, tmp_path):
    h, url = server
    h.move_status = 405
    f = mk_file(tmp_path, size=1_000_000)
    seen = []
    r = ss.upload_book_safe(WebDavClient(url), f, "", "a.epub", progress=lambda s, t: seen.append((s, t)))
    assert (r.status, r.method) == ("ok", "put")
    assert seen[-1] == (1_000_000, 1_000_000)


def test_worker_emits_upload_progress_with_token(server, tmp_path):
    h, url = server
    w = ss.ShelfSyncWorker()
    prog, done = [], []
    w.uploadProgress.connect(lambda tok, s, t: prog.append((tok, s, t)))
    w.uploadDone.connect(lambda tok, res: done.append(res))
    f = mk_file(tmp_path, size=2_000_000)
    w.upload({"webdav_enabled": True, "webdav_url": url}, f, "", "a.epub", 42).join(15)
    assert wait_for(lambda: done and prog and prog[-1][1] == 2_000_000)
    assert {p[0] for p in prog} == {42} and prog[-1] == (42, 2_000_000, 2_000_000)


def test_view_mirrors_upload_status_for_the_main_window(server, tmp_path, rb):
    h, url = server
    v = make_view(tmp_path, url)
    status = []
    v.uploadStatus.connect(lambda text, ms: status.append((text, ms)))
    f = mk_file(tmp_path, size=2_000_000)
    v.upload_chooser = lambda items, folders: ("小说", [(items[0][0], items[0][1])])
    assert v.start_upload([f]) is True
    assert wait_for(lambda: len(v.finished) == 1 and v.finished[0].status == "ok")
    assert wait_for(lambda: status and "已上传" in status[-1][0])
    assert status[0] == ("正在上传：沙丘.epub", 0)                               # 一开始就有反馈，且常驻
    assert any("%" in t and "MB" in t and ms == 0 for t, ms in status)         # 中途带百分比 / MB
    assert status[-1][1] == 15000                                             # 结果提示会自己消失
    assert "已上传" in v._notice


def test_view_ignores_late_progress_from_a_finished_upload(server, tmp_path, rb):
    h, url = server
    v = make_view(tmp_path, url)
    status = []
    v.uploadStatus.connect(lambda text, ms: status.append((text, ms)))
    v._on_upload_progress(99, 50, 100)                                         # 没有进行中的上传
    assert status == [] and "99" not in v._notice


def test_view_upload_status_for_guard_failures(server, tmp_path, rb):
    h, url = server
    v0 = make_view(tmp_path, None)
    status = []
    v0.uploadStatus.connect(lambda text, ms: status.append((text, ms)))
    assert v0.start_upload([mk_file(tmp_path)]) is False
    assert status == [("未启用云同步，无法上传", 8000)]
