"""批量上传：计划（纯函数）+ 视图流程（进程内 fake WebDAV，offscreen Qt）。
重点：保持子文件夹结构、云端已有同名自动跳过且不弹改名、不覆盖、连续失败自动中止、可停止、结束后可补传。"""
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from pyreader import shelf as sh  # noqa: E402
from pyreader import shelfsync as ss  # noqa: E402
from pyreader import shelfview as sv  # noqa: E402
from pyreader import sync  # noqa: E402

MB = 1024 * 1024


@pytest.fixture(scope="module", autouse=True)
def qapp():
    return QApplication.instance() or QApplication([])


def wait_for(cond, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        QApplication.processEvents()
        if cond():
            return True
        time.sleep(0.005)
    QApplication.processEvents()
    return cond()


def lf(name, sub="", size=100, root="/lib"):
    return sh.LocalFile(f"{root}/{sub}/{name}" if sub else f"{root}/{name}", root, sub, name, size, 1.0)


# ================================================================== 计划（纯函数）
def test_plan_keeps_subfolders_and_skips_what_it_should():
    files = [lf("a.epub", "小说"), lf("b.epub", "有声书/科幻", 5 * MB), lf("c.txt"), lf("dup.txt", "x"), lf("DUP.txt", "x"),
             lf("exists.epub", "小说"), lf("empty.txt", size=0), lf("huge.pdf", size=3 * MB), lf("bad:name.txt")]
    plan = ss.plan_batch_upload(files, ["小说/Exists.epub"], 2 * MB)
    assert [(n, f) for _p, f, n in plan.items] == [("a.epub", "小说"), ("c.txt", ""), ("dup.txt", "x")]
    assert plan.total_bytes == 300
    assert plan.skipped["云端已有同名文件"] == ["exists.epub"]
    assert plan.skipped["本批里重名"] == ["DUP.txt"]
    assert plan.skipped["文件是空的"] == ["empty.txt"]
    assert any("超过单个文件上限" in k for k in plan.skipped) and "名字或文件夹不合法" in plan.skipped
    assert "有声书/科幻" not in [f for _p, f, _n in plan.items]                   # b.epub 5MB > 2MB 上限被跳过


def test_plan_prefix_and_cached_copies():
    plan = ss.plan_batch_upload([lf("a.txt", "x"), lf("b.txt", root="/cache")], [], 10 * MB, "我的书库",
                                is_cached=lambda p: p.startswith("/cache"))
    assert [(n, f) for _p, f, n in plan.items] == [("a.txt", "我的书库/x")]
    assert plan.skipped == {"已经是云端缓存副本": ["b.txt"]}
    assert ss.plan_batch_upload([lf("a.txt")], [], 10 * MB, "bad:prefix").skipped == {"名字或文件夹不合法": ["a.txt"]}


# ================================================================== 视图流程
def cfg_for(url, **kw):
    return {"webdav_enabled": True, "webdav_url": url, "sync_user_name": "gary", "device_name": "gary-pc", **kw}


def make_view(tmp_path, url, dirs, **cfg_kw):
    ctx = {"library_dirs": list(dirs), "recent": [], "bookmarks": {}, "cfg": cfg_for(url, **cfg_kw)}
    v = sv.ShelfView(lambda: ctx, root=str(tmp_path / "home"))
    v.finished = []
    v.uploadFinished.connect(v.finished.append)
    v.confirm_push = lambda: False
    v.reports = []
    v.batch_reporter = lambda summary, failed: v.reports.append((summary, list(failed)))
    v.batch_chooser = lambda plan: ""
    return v


def scanned(v):
    v.rescan(force=True)
    assert wait_for(lambda: not v.scanning)


@pytest.fixture
def rb(tmp_path, monkeypatch):
    d = tmp_path / "remote_books"
    monkeypatch.setattr(sync, "REMOTE_BOOKS_DIR", str(d))
    return d


def mk_lib(tmp_path):
    lib = tmp_path / "mine"
    (lib / "小说").mkdir(parents=True)
    (lib / "漫画").mkdir()
    (lib / "小说" / "沙丘.txt").write_bytes(b"s" * 30)
    (lib / "小说" / "三体.txt").write_bytes(b"t" * 40)
    (lib / "漫画" / "海贼王.pdf").write_bytes(b"z" * 50)
    (lib / "根.txt").write_bytes(b"r" * 20)
    return lib


def test_batch_uploads_all_keeping_structure_skips_existing_without_prompt(server, tmp_path, rb):
    h, url = server
    lib = mk_lib(tmp_path)
    h.files["/dav/library/小说/三体.txt"] = b"THEIRS"
    h.dirs.update({"/dav/library", "/dav/library/小说"})
    v = make_view(tmp_path, url, [str(lib)])
    v.rename_chooser = lambda *a: pytest.fail("批量上传不应弹改名")
    scanned(v)
    assert v.start_batch_upload() is True
    assert wait_for(lambda: v._batch is None and v.reports)
    assert h.files["/dav/library/小说/沙丘.txt"] == b"s" * 30
    assert h.files["/dav/library/漫画/海贼王.pdf"] == b"z" * 50 and h.files["/dav/library/根.txt"] == b"r" * 20
    assert h.files["/dav/library/小说/三体.txt"] == b"THEIRS"                       # 云端已有：原样保留
    assert "成功 3 本" in v.reports[0][0] and v.reports[0][1] == []
    assert not v.btn_batch_stop.isVisible() and v.btn_batch_upload.isEnabled()
    # 再点一次（云端文件不足 60 秒还算「上传中」不显示，所以仍会被列进计划）：全部命中「云端已有」被跳过，一个字节都不会覆盖
    assert wait_for(lambda: not v._cloud_busy)
    scanned(v)
    v.reports.clear()
    puts_before = [p for m, p in h.log if m == "PUT"]
    assert v.start_batch_upload() is True
    assert wait_for(lambda: v._batch is None and v.reports)
    assert "成功 0 本" in v.reports[0][0] and [p for m, p in h.log if m == "PUT"] == puts_before
    assert h.files["/dav/library/小说/三体.txt"] == b"THEIRS"


def test_batch_prefix_and_cancel_by_chooser(server, tmp_path, rb):
    h, url = server
    lib = mk_lib(tmp_path)
    v = make_view(tmp_path, url, [str(lib)])
    scanned(v)
    v.batch_chooser = lambda plan: None
    assert v.start_batch_upload() is False and v._batch is None and h.log == []     # 取消对话框：没有任何网络请求
    v.batch_chooser = lambda plan: "我的书库"
    assert v.start_batch_upload() is True
    assert wait_for(lambda: v._batch is None and v.reports)
    assert "/dav/library/我的书库/小说/沙丘.txt" in h.files and "/dav/library/我的书库/根.txt" in h.files


def test_batch_only_selected_paths_and_guards(server, tmp_path, rb):
    h, url = server
    lib = mk_lib(tmp_path)
    v = make_view(tmp_path, url, [str(lib)])
    scanned(v)
    assert v.start_batch_upload([str(lib / "根.txt")]) is True
    assert v.start_upload([str(lib / "漫画" / "海贼王.pdf")]) is False and "批量上传进行中" in v._notice
    assert v.start_batch_upload() is False                                         # 已有上传在进行
    assert wait_for(lambda: v._batch is None and v.reports)
    assert [k for k in h.files if k.startswith("/dav/library/")] == ["/dav/library/根.txt"]
    v0 = make_view(tmp_path, None, [str(lib)])
    v0._context().get("cfg").clear()
    assert v0.start_batch_upload() is False and "未启用" in v0._notice


def test_batch_aborts_after_consecutive_errors_and_can_resume(server, tmp_path, rb, monkeypatch):
    h, url = server
    lib = tmp_path / "many"
    lib.mkdir()
    for i in range(9):
        (lib / f"b{i}.txt").write_bytes(b"x" * 10)
    v = make_view(tmp_path, url, [str(lib)])
    scanned(v)
    real = ss.upload_book_safe
    monkeypatch.setattr(ss, "upload_book_safe",
                        lambda *a, **k: ss.UploadResult("error", a[3], "上传失败：boom"))
    assert v.start_batch_upload() is True
    assert wait_for(lambda: v._batch is None and v.reports)
    summary, failed = v.reports[0]
    assert "中止" in summary and len(failed) == v.BATCH_MAX_CONSECUTIVE_ERRORS and "未处理 4 本" in summary
    assert not [k for k in h.files if k.startswith("/dav/library/")]
    monkeypatch.setattr(ss, "upload_book_safe", real)                              # 服务器恢复后再点：补传
    v.reports.clear()
    assert v.start_batch_upload() is True
    assert wait_for(lambda: v._batch is None and v.reports)
    assert len([k for k in h.files if k.startswith("/dav/library/") and k.endswith(".txt")]) == 9


def test_batch_cancel_stops_after_current(server, tmp_path, rb):
    h, url = server
    lib = tmp_path / "many"
    lib.mkdir()
    for i in range(6):
        (lib / f"b{i}.txt").write_bytes(b"x" * 10)
    v = make_view(tmp_path, url, [str(lib)])
    scanned(v)
    assert v.start_batch_upload() is True
    v.cancel_batch()
    assert wait_for(lambda: v._batch is None and v.reports)
    assert "已停止" in v.reports[0][0]
    assert len([k for k in h.files if k.endswith(".txt")]) < 6


def test_batch_dialog_prefix_validation(tmp_path):
    plan = ss.BatchPlan(items=[("/a", "", "a.txt")], total_bytes=10, skipped={"云端已有同名文件": ["x.txt"]})
    d = sv.BatchUploadDialog(plan)
    ok = d.buttons.button(sv.QDialogButtonBox.StandardButton.Ok)
    assert ok.isEnabled() and d.result_value() == ""
    d.prefix.setText("a:b")
    assert not ok.isEnabled() and d.result_value() is None
    d.prefix.setText("小说/科幻")
    assert ok.isEnabled() and d.result_value() == "小说/科幻"
