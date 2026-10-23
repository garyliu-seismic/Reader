"""阶段 2：shelf.py 新增的纯逻辑——色块封面、书签快照、本机进度 / 打开记录、index.json 缓存（无 Qt、无网络）。"""
import json
import os

from pyreader import shelf, store
from pyreader.shelf import Book, LocalFile, build_books

BID_L1 = "local:" + "a" * 40
BID_R1 = "remote:小说/三体.epub"


def _lf(tmp_path, name, data=b"x", sub=""):
    d = tmp_path / sub if sub else tmp_path
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_bytes(data)
    st = p.stat()
    return LocalFile(str(p), str(tmp_path).replace("\\", "/"), sub, name, st.st_size, st.st_mtime)


# ================================================================== 色块封面
def test_cover_hsl_deterministic_and_in_range():
    seen = set()
    for i in range(200):
        h, s, l = shelf.cover_hsl("%020x" % (i * 7919), "t")
        assert 0 <= h < 360 and 90 <= s < 150 and 70 <= l < 110     # 明度压低：白色大字始终可读
        assert shelf.cover_hsl("%020x" % (i * 7919), "t") == (h, s, l)
        seen.add(h // 30)
    assert len(seen) >= 8                                           # 色相分布足够散
    assert shelf.cover_hsl("aa", "t") != shelf.cover_hsl("bb", "t")


def test_cover_hsl_falls_back_to_title_without_fp():
    assert shelf.cover_hsl("", "三体") == shelf.cover_hsl("", "三体")
    assert shelf.cover_hsl("", "三体") != shelf.cover_hsl("", "红楼梦")
    assert shelf.cover_hsl(None, "x")                                # 脏输入不抛异常


def test_cover_glyph():
    assert shelf.cover_glyph("三体") == "三体"
    assert shelf.cover_glyph("红楼梦（全本）") == "红楼"
    assert shelf.cover_glyph("The Great Gatsby") == "TGG"
    assert shelf.cover_glyph("dune") == "DUN"
    assert shelf.cover_glyph("Dune Messiah") == "DM"
    assert shelf.cover_glyph("") == "?" and shelf.cover_glyph("  \x00 ") == "?"
    assert shelf.cover_glyph("\u202e三体") == "三体"                  # bidi 控制字符不进封面
    assert shelf.cover_glyph("!!!") == "!!"


# ================================================================== 书签快照 / 本机记录
def test_bookmark_snapshot_copies_only_progress_keys():
    bm = {"C:/a.txt": {"_progress_": 10, "_progress_ts_": 5.0, "items": [{"off": 1, "text": "secret"}]},
          "bad": 5, 7: {"_progress_": 1}, "D:/b.txt": {}}
    snap = shelf.bookmark_snapshot(bm)
    assert snap == {"C:/a.txt": {"_progress_": 10, "_progress_ts_": 5.0},
                    "D:/b.txt": {"_progress_": None, "_progress_ts_": None}}
    bm["C:/a.txt"]["_progress_"] = 99                                # 快照与原数据脱钩
    assert snap["C:/a.txt"]["_progress_"] == 10
    assert shelf.bookmark_snapshot(None) == {} and shelf.bookmark_snapshot([1]) == {}


def test_progress_overlay_feeds_build_books(tmp_path):
    f = _lf(tmp_path, "a.txt", b"hello")
    bid = shelf._sync().book_id(f.path)
    ov = shelf.progress_overlay({bid: {"percent": 98.0, "updated": 1_700_000_000.0, "device": "pc"}})
    b = build_books([f], None, ov)[bid]
    assert b.percent == 98.0 and b.status == "done" and b.progress_device == "pc"
    assert b.last_read == 1_700_000_000.0


def test_note_opened_and_progress():
    b = Book(BID_L1, "t")
    shelf.note_opened(b, 100.0)
    shelf.note_opened(b, 50.0)                                       # 较旧的打开不能把时间倒退
    assert b.last_opened == 100.0 and b.last_read == 100.0
    assert shelf.note_progress(b, 42.5, 200.0, "pc") is True
    assert (b.percent, b.progress_updated, b.has_progress, b.last_read, b.status) == (42.5, 200.0, True, 200.0, "reading")
    assert shelf.note_progress(b, 10.0, 150.0, "old") is False       # 比已有记录旧：忽略
    assert b.percent == 42.5 and b.progress_device == "pc"
    assert shelf.note_progress(b, 250.0, 300.0) is True and b.percent == 100.0 and b.status == "done"   # 夹到 100
    for bad in (float("nan"), float("inf"), -1, None, "x", True):
        assert shelf.note_progress(b, bad, 400.0) is False
    assert shelf.note_progress(b, 5.0, float("nan")) is False
    assert shelf.note_progress(b, 0.0, 500.0) is True and b.status == "unread"


# ================================================================== index.json
def _books(tmp_path):
    fs = [_lf(tmp_path, "三体.txt", b"abc"), _lf(tmp_path, "沙丘.epub", b"defg", sub="科幻")]
    return build_books(fs, None, None, recent=[fs[0].path]), fs


def test_index_roundtrip_through_json(tmp_path):
    books, fs = _books(tmp_path)
    bid0 = shelf._sync().book_id(fs[0].path)
    opened = {bid0: 1_700_000_100.0}
    progress = {bid0: {"percent": 33.0, "updated": 1_700_000_200.0, "device": "pc"}}
    wire = json.loads(json.dumps(shelf.index_to_wire(books, opened, progress, built=5.0)))
    assert wire["version"] == 1 and wire["built"] == 5.0 and set(wire) == {"version", "built", "books", "opened", "progress"}
    got = shelf.index_from_wire(wire)
    assert got is not None
    books2, opened2, progress2 = got
    assert set(books2) == set(books) and opened2 == opened and progress2 == progress
    for bid, b in books.items():
        b2 = books2[bid]
        assert (b2.title, b2.fmt, b2.size, b2.shelf, b2.fp, b2.where, b2.local_paths) == \
               (b.title, b.fmt, b.size, b.shelf, b.fp, b.where, b.local_paths)
    b = books2[bid0]
    assert b.percent == 33.0 and b.progress_updated == 1_700_000_200.0 and b.last_opened == 1_700_000_100.0
    assert b.last_read >= 1_700_000_200.0 and b.status == "reading"


def test_index_skips_books_without_local_file_and_has_no_secrets(tmp_path):
    books, _ = _books(tmp_path)
    books["remote:云端.epub"] = Book("remote:云端.epub", "云端", "epub", 5, "cloud:", "", "cloud", "云端.epub")
    wire = shelf.index_to_wire(books)
    assert "remote:云端.epub" not in wire["books"] and len(wire["books"]) == 2
    text = json.dumps(wire)
    for k in ("api_key", "webdav_password", "azure"):
        assert k not in text


def test_index_save_load_and_reset(tmp_path):
    root = str(tmp_path / "home")
    books, _ = _books(tmp_path)
    assert shelf.load_index(root) is None
    assert shelf.save_index(books, root=root) is True
    got = shelf.load_index(root)
    assert got and set(got[0]) == set(books)
    shelf.reset_index(root)
    assert shelf.load_index(root) is None
    shelf.reset_index(root)                                          # 再删不抛异常
    assert os.path.isdir(os.path.dirname(shelf.index_path(root)))     # 只删 index.json，目录与其它文件不动


def test_index_corrupt_is_quarantined_not_fatal(tmp_path):
    root = str(tmp_path)
    os.makedirs(os.path.dirname(shelf.index_path(root)))
    open(shelf.index_path(root), "w").write("{{{")
    assert shelf.load_index(root) is None
    assert any(".bad-" in f for f in os.listdir(os.path.dirname(shelf.index_path(root))))
    store.save_json(shelf.index_path(root), [1, 2])                  # 类型不对（list）同样隔离
    assert shelf.load_index(root) is None


def test_index_higher_major_version_is_readonly(tmp_path):
    root = str(tmp_path)
    store.save_json(shelf.index_path(root), {"version": 9, "books": {}})
    before = open(shelf.index_path(root), "rb").read()
    assert shelf.load_index(root) is None and shelf.index_readonly(root) is True
    books, _ = _books(tmp_path)
    assert shelf.save_index(books, root=root) is False
    assert open(shelf.index_path(root), "rb").read() == before
    shelf.reset_index(root)                                          # reset_index 是显式删除，由调用者先判断 readonly


def test_index_from_wire_rejects_malicious_entries():
    good = {"fp": "ab", "title": "ok", "format": "txt", "size": 3, "shelf": "local:C:/b", "where": "local",
            "mtime": 1.0, "local": ["C:/b/ok.txt"], "has_progress": False, "last_read": 0.0}
    raw = {"version": 1, "books": {
        BID_L1: good,
        "local:zz": good,                                            # 非法 book_id
        "remote:../x.epub": good,
        "local:" + "b" * 40: dict(good, local=[5, None, "", "x" * 5000, "a\x00b"]),     # 没有可用路径
        "local:" + "c" * 40: dict(good, title="\x00\u202e"),                            # 清洗后书名为空
        "local:" + "d" * 40: dict(good, size=10 ** 400, mtime=float("nan"), last_read=10 ** 400,
                                  where="evil", format="x" * 100, title="t" * 1000),
        "local:" + "e" * 40: "notadict",
    }, "opened": {BID_L1: 10 ** 400, "local:" + "b" * 40: float("inf"), "bad": 5, BID_R1: -3},
        "progress": {BID_L1: {"percent": float("nan"), "updated": 1},
                     "local:" + "d" * 40: {"percent": 250, "updated": 5, "device": "x" * 500},
                     "local:" + "e" * 40: {"percent": "x", "updated": 1}}}
    got = shelf.index_from_wire(raw)
    assert got is not None
    books, opened, progress = got
    assert set(books) == {BID_L1, "local:" + "d" * 40}
    d = books["local:" + "d" * 40]
    assert d.size == 0 and d.mtime == 0.0 and d.where == "local" and len(d.title) == shelf.MAX_STR and len(d.fmt) == 16
    assert d.percent == 100.0 and len(d.progress_device) == 64      # 夹到 100、设备名截断
    assert opened == {} and set(progress) == {"local:" + "d" * 40}
    assert books[BID_L1].percent is None


def test_index_from_wire_bad_shapes():
    for bad in (None, 5, [], {"version": "1"}, {"version": True}, {"version": 0}, {"version": 2}, {"books": {}}):
        assert shelf.index_from_wire(bad) is None
    assert shelf.index_from_wire({"version": 1}) == ({}, {}, {})
    assert shelf.index_from_wire({"version": 1, "books": [1], "opened": 3, "progress": "x"}) == ({}, {}, {})


def test_index_notes_are_capped(monkeypatch):
    monkeypatch.setattr(shelf, "MAX_NOTES", 3)
    prog = {"local:" + c * 40: {"percent": 1.0, "updated": float(i), "device": ""} for i, c in enumerate("abcde")}
    wire = shelf.index_to_wire({}, {k: float(i) for i, k in enumerate(prog)}, prog)
    assert len(wire["progress"]) == 3 and len(wire["opened"]) == 3
    assert set(wire["progress"]) == {"local:" + c * 40 for c in "cde"}   # 保留最新的
