"""阶段 1：shelf.py 纯逻辑（无 Qt 界面、无网络）。"""
import copy
import json
import os
import random
import unicodedata

import pytest

from pyreader import shelf, store, sync
from pyreader.shelf import (Book, LocalFile, build_books, derive_status, filter_books, fp, fp_weak,
                            merge, set_field, sort_books, to_wire, validate_overlay)

NOW = 1_700_000_000_000          # 固定「现在」（毫秒），让合并 / 校验可复现
HOUR = 3600 * 1000
BID_R1 = "remote:小说/三体.epub"
BID_R2 = "remote:小说/科幻/沙丘.epub"
BID_L1 = "local:" + "a" * 40
BID_L2 = "local:" + "b" * 40


def ov_of(dev, entries=None, progress=None, **extra):
    d = {"version": 1, "device_id": dev, "entries": entries or {}, "progress": progress or {}}
    d.update(extra)
    return d


def dev_overlay(dev):
    return shelf.new_overlay(dev, "host-" + dev, "gary")


# ================================================================== 指纹
def test_fp_weak_nfc_nfd_equal():
    nfc = unicodedata.normalize("NFC", "Café.epub")
    nfd = unicodedata.normalize("NFD", "Café.epub")
    assert nfc != nfd
    assert fp_weak(nfc, 10) == fp_weak(nfd, 10)
    assert fp_weak(nfc, 10) != fp_weak(nfc, 11)
    assert fp_weak("a/b/三体.epub", 5) == fp_weak("三体.epub", 5)     # 只取文件名


def test_fp_strong_nfc_nfd_same_and_content_sensitive(tmp_path):
    data = b"x" * 200000
    a = tmp_path / unicodedata.normalize("NFC", "Café.txt")
    b = tmp_path / "d" / unicodedata.normalize("NFD", "Café.txt")
    b.parent.mkdir()
    a.write_bytes(data)
    b.write_bytes(data)
    assert fp(str(a)) == fp(str(b))
    assert len(fp(str(a))) == 20 and store.safe_component(fp(str(a)))
    c = tmp_path / "e" / "Café.txt"
    c.parent.mkdir()
    c.write_bytes(data[:-1] + b"y")                      # 尾部不同
    assert fp(str(c)) != fp(str(a))
    mid = bytearray(data)
    mid[100000] = 1                                      # 中间不同：首尾指纹看不出（已知取舍），但大小同则 fp 同
    d = tmp_path / "f" / "Café.txt"
    d.parent.mkdir()
    d.write_bytes(bytes(mid))
    assert fp(str(d)) == fp(str(a))
    assert fp(str(tmp_path / "nope.txt")) is None


def test_fp_small_file(tmp_path):
    p = tmp_path / "s.txt"
    p.write_text("hi")
    assert fp(str(p)) == fp(str(p))


# ================================================================== derive_status
@pytest.mark.parametrize("pct,want", [(0, "unread"), (0.0, "unread"), (None, "unread"), (-3, "unread"),
                                       (0.01, "reading"), (50, "reading"), (96.99, "reading"),
                                       (97, "done"), (100, "done"), (float("nan"), "unread"), ("x", "unread")])
def test_derive_status_thresholds(pct, want):
    assert derive_status(pct) == want


def test_derive_status_unknown_percent_with_local_progress():
    assert derive_status(None, has_progress=True) == "reading"
    assert derive_status(0, has_progress=True) == "unread"        # 已知百分比为 0 时以百分比为准


# ================================================================== scan_local
def _mk(p, data=b"x"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def test_scan_local_chinese_nested_dotfiles(tmp_path):
    root = tmp_path / "书库"
    _mk(root / "三体.epub")
    _mk(root / "科幻" / "沙丘.txt", b"abc")
    _mk(root / "科幻" / "深层" / "红楼梦.PDF")
    _mk(root / ".hidden.txt")
    _mk(root / ".git" / "x.txt")
    _mk(root / "~$临时.docx")
    _mk(root / "科幻" / "readme.json")                    # 非电子书扩展名
    files = shelf.scan_local([str(root), str(tmp_path / "不存在")])
    got = sorted((f.sub, f.name) for f in files)
    assert got == [("", "三体.epub"), ("科幻", "沙丘.txt"), ("科幻/深层", "红楼梦.PDF")]
    f = next(f for f in files if f.name == "沙丘.txt")
    assert f.size == 3 and os.path.isfile(f.path) and "\\" not in f.root
    assert shelf.shelf_of_local(f) == "local:" + f.root + "/科幻"
    assert [x.path for x in shelf.scan_local([str(root)])] == [x.path for x in shelf.scan_local([str(root)])]


def test_scan_local_limit_and_stop(tmp_path):
    for i in range(7):
        _mk(tmp_path / f"b{i}.txt")
    r = shelf.scan_local([str(tmp_path)], limit=5)
    assert len(r) == 5 and r.truncated
    assert not shelf.scan_local([str(tmp_path)], limit=7).truncated
    assert shelf.scan_local([str(tmp_path)], should_stop=lambda: True) == []


# ================================================================== 书签键归一
def test_bookmark_key_normalization_mixed_slashes(tmp_path):
    p = tmp_path / "x" / "a.txt"
    key = str(p).replace("\\", "/")                      # 打开对话框返回正斜杠
    bms = {key: {"_progress_": 10, "_progress_ts_": 5.0}, 7: "junk", "": {}}
    assert shelf.bookmark_for(bms, str(p))["_progress_"] == 10          # 扫描得到的是系统分隔符
    assert shelf.bookmark_for(bms, str(p.parent / ".." / "x" / "a.txt")) is not None
    assert shelf.bookmark_for(bms, str(tmp_path / "other.txt")) is None
    assert shelf.bookmark_for(None, str(p)) is None


@pytest.mark.skipif(os.name != "nt", reason="盘符大小写只在 Windows 上有意义")
def test_bookmark_key_drive_letter_case():
    bms = {"c:/Users/X/Books/a.txt": {"_progress_": 1}}
    assert shelf.bookmark_for(bms, "C:\\users\\x\\books\\A.TXT") is not None


def test_bookmarks_not_mutated():
    bms = {"C:/a/b.txt": {"_progress_": 3, "items": [1]}}
    before = copy.deepcopy(bms)
    shelf.norm_bookmarks(bms)
    shelf.bookmark_for(bms, "C:/a/b.txt")
    assert bms == before


# ================================================================== build_books
@pytest.fixture
def rb(tmp_path, monkeypatch):
    d = tmp_path / "remote_books"
    d.mkdir()
    monkeypatch.setattr(sync, "REMOTE_BOOKS_DIR", str(d))
    return d


def _lf(path, root=None):
    path = str(path)
    st = os.stat(path)
    root = root or os.path.dirname(path)
    return LocalFile(path, root.replace("\\", "/"), "", os.path.basename(path), st.st_size, st.st_mtime)


def test_build_books_provenance_and_states(tmp_path, rb):
    # 云端：三体（已缓存）、沙丘（仅云端）
    _mk(rb / "小说" / "三体.epub", b"12345")
    remote = [("小说/三体.epub", 5, 100.0), ("小说/科幻/沙丘.epub", 9, 200.0)]
    # 本地：同名同大小但来源不同的「三体.epub」（不在 remote_books 下）和一本独有的
    lib = tmp_path / "lib"
    _mk(lib / "三体.epub", b"12345")
    _mk(lib / "孤本.txt", b"zz")
    local = [_lf(rb / "小说" / "三体.epub", str(rb)), _lf(lib / "三体.epub"), _lf(lib / "孤本.txt")]
    books = build_books(local, remote)
    assert books[BID_R1].where == "synced" and books[BID_R1].shelf == "cloud:小说"
    assert len(books[BID_R1].local_paths) == 1                        # 缓存副本只算一次（扫描 + 列表去重）
    assert books[BID_R2].where == "cloud" and books[BID_R2].shelf == "cloud:小说/科幻"
    lid = sync.book_id(str(lib / "三体.epub"))
    assert lid.startswith("local:") and books[lid].where == "local"   # 来源不同：独立卡片，不合并
    assert books[lid].same_as == [BID_R1] and books[BID_R1].same_as == [lid]
    assert books[BID_R2].same_as == []
    assert books[sync.book_id(str(lib / "孤本.txt"))].title == "孤本"
    assert len(books) == 4


def test_build_books_same_local_id_merges_copies(tmp_path, rb):
    _mk(tmp_path / "a" / "x.txt", b"hello")
    _mk(tmp_path / "b" / "x.txt", b"hello")
    books = build_books([_lf(tmp_path / "a" / "x.txt"), _lf(tmp_path / "b" / "x.txt")], [])
    assert len(books) == 1 and len(next(iter(books.values())).local_paths) == 2


def test_build_books_same_name_in_two_cloud_folders_not_merged(rb):
    books = build_books([], [("a/x.epub", 3, 0), ("b/x.epub", 3, 0)])
    assert set(books) == {"remote:a/x.epub", "remote:b/x.epub"}
    assert books["remote:a/x.epub"].same_as == []                     # 同来源的同名书各自独立，也不互相提示


def test_build_books_filters_bad_remote_entries(rb):
    bad = [("../evil.txt", 1, 0), ("a\\b.txt", 1, 0), (".hidden.txt", 1, 0), ("dir/.x.epub", 1, 0),
           ("~$t.docx", 1, 0), ("note.json", 1, 0), ("CON.txt", 1, 0), ("ok/书.txt", 1, 0), None, 5]
    assert set(build_books([], bad)) == {"remote:ok/书.txt"}


def test_build_books_removed_and_unknown_list(tmp_path, rb):
    _mk(rb / "旧" / "书.txt", b"abc")
    cached = _lf(rb / "旧" / "书.txt", str(rb))
    bid = "remote:旧/书.txt"
    # 列表已知且不含它、且调用方判定为已移除（连续两轮）
    b = build_books([cached], [], removed={bid})[bid]
    assert b.where == "removed" and b.removed and not b.auto_hidden    # 本机有副本：保留可读
    assert bid in {x.id for x in filter_books(build_books([cached], [], removed={bid}), "removed")}
    # 第一轮缺失（未判定）：仍视为已同步
    assert build_books([cached], [])[bid].where == "synced"
    # 列表未知（离线 / 不完整）：即使 removed 里有也不据此下结论
    assert build_books([cached], None, removed={bid})[bid].where == "synced"
    # 列表里又出现了：以列表为准
    assert build_books([cached], [("旧/书.txt", 3, 0)], removed={bid})[bid].where == "synced"


def test_build_books_removed_without_copy_is_auto_hidden(rb):
    bid = "remote:旧/书.txt"
    ov = merge(ov_of("d1", {bid: {"fav": [True, 5], "meta": [{"title": "旧书"}, 5]}}), now_ms=NOW)
    bs = build_books([], [], ov, removed={bid})
    assert bs[bid].removed and bs[bid].auto_hidden and bs[bid].title == "旧书"
    assert filter_books(bs, "all") == [] and filter_books(bs, "fav") == []
    assert [b.id for b in filter_books(bs, "removed")] == [bid]
    # 在读则保留
    ov2 = merge(ov, ov_of("d1", progress={bid: {"percent": 30, "updated": 5}}), now_ms=NOW)
    assert not build_books([], [], ov2, removed={bid})[bid].auto_hidden


def test_build_books_missing_placeholder_from_other_device(rb):
    ov = merge(ov_of("phone", {BID_L1: {"meta": [{"title": "手机上的书"}, 5], "fav": [True, 6]}}), now_ms=NOW)
    bs = build_books([], [], ov)
    b = bs[BID_L1]
    assert b.placeholder and b.title == "手机上的书" and b.auto_hidden and b.fav
    assert filter_books(bs, "all") == [] and [x.id for x in filter_books(bs, "missing")] == [BID_L1]


def test_build_books_overlay_fields_title_and_progress(tmp_path, rb):
    remote = [("小说/三体.epub", 5, 100.0)]
    ov = merge(
        ov_of("pc", {BID_R1: {"fav": [True, 10], "hidden": [False, 10], "meta": [{"title": "三体（全）"}, 10]}},
              {BID_R1: {"percent": 40.0, "updated": 1000.0}}),
        ov_of("phone", progress={BID_R1: {"percent": 55.5, "updated": 2000.0}}), now_ms=NOW)
    b = build_books([], remote, ov)[BID_R1]
    assert b.fav and not b.hidden and b.title == "三体（全）"
    assert b.percent == 55.5 and b.progress_device == "phone" and b.status == "reading"
    assert b.last_read == 2000.0


def test_build_books_progress_alias_and_bookmarks(tmp_path, rb):
    # 先在本地读、后来又进了云端书库：进度还挂在 local: 别名下，取较新者
    alias = shelf.local_id_for("三体.epub", 5)
    assert alias == "local:" + __import__("hashlib").sha1("三体.epub5".encode()).hexdigest()
    ov = merge(ov_of("pc", progress={alias: {"percent": 98.0, "updated": 500.0},
                                     BID_R1: {"percent": 10.0, "updated": 100.0}}), now_ms=NOW)
    b = build_books([], [("小说/三体.epub", 5, 0)], ov)[BID_R1]
    assert b.percent == 98.0 and b.status == "done"
    # 书签：只有偏移，没有百分比 → 在读；键混合斜杠也能找到
    p = _mk(tmp_path / "lib" / "a.txt", b"hello")
    bms = {str(p).replace("\\", "/"): {"_progress_": 3, "_progress_ts_": 777.0}}
    bk = build_books([_lf(p)], [], bookmarks=bms)
    only = next(iter(bk.values()))
    assert only.percent is None and only.has_progress and only.status == "reading" and only.last_read == 777.0


def test_build_books_recent_seeds_last_opened(tmp_path, rb):
    a = _mk(tmp_path / "a.txt", b"a")
    b = _mk(tmp_path / "b.txt", b"bb")
    c = _mk(tmp_path / "c.txt", b"ccc")
    bs = build_books([_lf(a), _lf(b), _lf(c)], [], recent=[str(b), str(a)])
    order = [x.title for x in sort_books(bs, "last_read")]
    assert order == ["b", "a", "c"]                                   # recent 里靠前的更新；没打开过的最后
    opened = {sync.book_id(str(c)): 99999.0}
    assert sort_books(build_books([_lf(a), _lf(b), _lf(c)], [], recent=[str(b)], opened=opened), "last_read")[0].title == "c"


# ================================================================== filter / sort
def _book(id_, title, **kw):
    return Book(id=id_, title=title, **kw)


def test_filter_and_sort():
    bs = {
        "1": _book("1", "三体", shelf="cloud:小说", where="synced", percent=40, last_read=300, mtime=1, size=10, fav=True),
        "2": _book("2", "Dune", shelf="cloud:小说/科幻", where="cloud", percent=100, last_read=100, mtime=3, size=30),
        "3": _book("3", "Café Notes", shelf="cloud:", where="cloud", percent=0, last_read=0, mtime=2, size=20),
        "4": _book("4", "隐藏的", shelf="cloud:", where="local", hidden=True, percent=10, last_read=900),
        "5": _book("5", "gone", shelf="cloud:", where="removed"),
    }
    ids = lambda xs: [b.id for b in xs]
    assert sorted(ids(filter_books(bs, "all"))) == ["1", "2", "3"]
    assert ids(filter_books(bs, "reading")) == ["1"]
    assert ids(filter_books(bs, "done")) == ["2"]
    assert ids(filter_books(bs, "fav")) == ["1"]
    assert ids(filter_books(bs, "recent")) == ["1", "2"]
    assert ids(filter_books(bs, "removed")) == ["5"]
    assert ids(filter_books(bs, "uncategorized")) == ["3"]
    assert ids(filter_books(bs, "hidden")) == ["4"]
    assert ids(filter_books(bs, "cloud:小说")) == ["1"]               # 文件夹书架按名字精确匹配
    assert ids(filter_books(bs, "cloud:小说/科幻")) == ["2"]
    assert ids(filter_books(bs, "all", "DUNE")) == ["2"]               # casefold 子串
    assert ids(filter_books(bs, "all", unicodedata.normalize("NFD", "café"))) == ["3"]   # NFC/NFD 都能搜到
    assert ids(filter_books(bs, "all", "体")) == ["1"]
    assert filter_books(bs, "all", "不存在") == []
    only = {k: v for k, v in bs.items() if k in "123"}
    assert ids(sort_books(only, "last_read")) == ["1", "2", "3"]
    assert ids(sort_books(only, "title")) == ["3", "2", "1"]           # casefold：café < dune < 三体
    assert ids(sort_books(only, "added")) == ["2", "3", "1"]
    assert ids(sort_books(only, "progress")) == ["2", "1", "3"]
    assert ids(sort_books(only, "size")) == ["2", "3", "1"]
    # 完全相同的键由 id 兜底，顺序确定
    ties = [_book("b", "x"), _book("a", "x")]
    assert ids(sort_books(ties, "title")) == ["a", "b"]


def test_filter_matches_filename():
    b = _book("1", "标题", where="cloud", remote_rel="x/original_name.epub", shelf="cloud:x")
    assert filter_books([b], "all", "ORIGINAL_name") == [b]


# ================================================================== validate_overlay
def test_validate_overlay_basic_and_issues():
    raw = ov_of("pc-1", {
        BID_R1: {"fav": [True, 1000], "hidden": ["yes", 1000], "meta": [{"fp": "9f2c0a7e51d3b8a46c10", "title": "三体"}, 5],
                 "junk": 1, "_local": "C:/secret"},
        "../etc/passwd": {"fav": [True, 1]},
        BID_L1: "not a dict",
        "local:XYZ": {"fav": [True, 1]},
    }, {BID_R1: {"percent": 250, "updated": 100.0}, "bad": {"percent": 1, "updated": 1}})
    clean, issues = validate_overlay(raw, NOW)
    assert clean["device_id"] == "pc-1"
    assert set(clean["entries"]) == {BID_R1}
    e = clean["entries"][BID_R1]
    assert e["fav"] == [True, 1000, "pc-1"] and "hidden" not in e         # 非布尔被丢
    assert e["meta"][0] == {"fp": "9f2c0a7e51d3b8a46c10", "title": "三体"}
    assert e["junk"] == 1 and "_local" not in e                            # 未知字段保留；_ 开头簿记键不保留
    assert clean["progress"][BID_R1]["percent"] == 100.0                   # 截断到 0..100
    assert set(clean["progress"]) == {BID_R1}
    assert issues


def test_validate_overlay_device_id_from_filename_wins():
    raw = ov_of("evil/../x", {BID_R1: {"fav": [True, 5, "zzz"]}})
    assert validate_overlay(raw, NOW)[0] is None                            # JSON 内 device_id 不合法
    clean, _ = validate_overlay(raw, NOW, device_id="pc-1")
    assert clean["device_id"] == "pc-1" and clean["entries"][BID_R1]["fav"] == [True, 5, "pc-1"]   # 忽略第三项
    for bad in ("..", "a/b", "CON", ".x", "", "a b", "..\\..\\x"):
        assert validate_overlay(raw, NOW, device_id=bad)[0] is None
    assert validate_overlay({"entries": {}}, NOW)[0] is None                # 没有 device_id
    assert validate_overlay([], NOW)[0] is None and validate_overlay("x", NOW)[0] is None


def test_validate_overlay_versions():
    assert validate_overlay({"version": 2, "device_id": "a"}, NOW)[0] is None
    assert validate_overlay({"version": "1", "device_id": "a"}, NOW)[0] is None
    assert validate_overlay({"device_id": "a"}, NOW)[0] is not None         # 缺 version 视为 1


def test_validate_ts_future_and_garbage():
    far = 4_000_000_000_000                       # 2096 年
    raw = ov_of("pc", {
        BID_R1: {"fav": [True, far], "hidden": [True, 9 * 10 ** 18]},
        BID_R2: {"fav": [True, "abc"], "hidden": [True, None]},
        BID_L1: {"fav": [True, -5], "hidden": [True, 0]},
        BID_L2: {"fav": [True, 1.5], "hidden": [True, float("inf")], "meta": [{"title": "x"}, [1]]},
        "remote:x/y.txt": {"fav": [True, True], "hidden": [True, 1e40]},
    })
    raw["entries"][BID_L1]["hidden"] = [True, 10]
    clean, issues = validate_overlay(raw, NOW)
    e = clean["entries"]
    assert e[BID_R1]["fav"][1] == 1 and e[BID_R1]["hidden"][1] == 1          # 截断为最旧值
    assert "pc" in clean["_clock"]
    assert BID_R2 not in e and BID_L2 not in e                               # 非数字 / 非整数 ts 被跳过
    assert e[BID_L1] == {"hidden": [True, 10, "pc"]}
    assert "remote:x/y.txt" in e and e["remote:x/y.txt"]["hidden"][1] == 1  # 1e40 浮点也是整数值 → 视为未来 → 截断
    assert "fav" not in e["remote:x/y.txt"]                                  # True 不是合法 ts
    # 进度时间戳也一样
    c2, _ = validate_overlay(ov_of("pc", progress={BID_R1: {"percent": 1, "updated": 9e18},
                                                   BID_R2: {"percent": "a", "updated": 1}}), NOW)
    assert c2["progress"][BID_R1]["updated"] == 0.001 and BID_R2 not in c2["progress"]


def test_validate_future_within_24h_kept_and_over_clamped():
    c, _ = validate_overlay(ov_of("pc", {BID_R1: {"fav": [True, NOW + 23 * HOUR]}}), NOW)
    assert c["entries"][BID_R1]["fav"][1] == NOW + 23 * HOUR and c["_clock"] == []
    c, _ = validate_overlay(ov_of("pc", {BID_R1: {"fav": [True, NOW + 25 * HOUR]}}), NOW)
    assert c["entries"][BID_R1]["fav"][1] == 1 and c["_clock"] == ["pc"]


def test_validate_strings_sanitised_and_bounded():
    title = "好\u202e书\x00\n" + "x" * 1000
    c, _ = validate_overlay(ov_of("pc", {BID_R1: {"meta": [{"fp": "../../x", "title": title}, 5]},
                                         BID_R2: {"x_big": "y" * 5000, "ok": {"a": 1}}}), NOW)
    m = c["entries"][BID_R1]["meta"][0]
    assert "fp" not in m and "\u202e" not in m["title"] and "\x00" not in m["title"] and len(m["title"]) <= 256
    assert m["title"].startswith("好书")
    assert c["entries"][BID_R2] == {"ok": {"a": 1}}                           # 超长未知字段丢弃


LONE_SURROGATE_JSON = r'''{
  "version": 1, "device_id": "dev-b", "hostname": "h\ud800", "user": "u\ud800",
  "entries": {
    "remote:a.txt": {"fav": [true, 5], "meta": [{"title": "x\ud800y"}, 5], "zz": "q\ud800",
                     "nest": {"k\ud800": 1}, "bad\ud800key": 1, "ok": {"a": 1},
                     "rekeyed_from": ["remote:r\ud800.txt", "remote:r2.txt"]},
    "remote:b\ud800.txt": {"fav": [true, 5]}
  },
  "progress": {"remote:a.txt": {"percent": 1, "updated": 5, "device": "d\ud800"},
               "remote:c\ud800.txt": {"percent": 1, "updated": 5}}
}'''


def test_validate_lone_surrogates_never_break_serialisation(tmp_path):
    """json.loads 接受孤立代理项（U+D800）；它们不得进入合并视图，否则 save_json / wire_bytes 抛 UnicodeEncodeError。"""
    raw = json.loads(LONE_SURROGATE_JSON)
    assert "\ud800" in raw["hostname"] and "\ud800" in raw["entries"]["remote:a.txt"]["meta"][0]["title"]   # 前提
    c, _ = validate_overlay(raw, NOW, device_id="dev-b")
    ent = c["entries"]["remote:a.txt"]
    assert ent["meta"][0]["title"] == "xy" and ent["ok"] == {"a": 1}      # 标题清洗后保留，其余坏字段丢弃
    assert c.get("hostname") == "h" and "user" not in c
    assert not any(k in ent for k in ("zz", "nest", "bad\ud800key"))
    assert ent["rekeyed_from"] == ["remote:r2.txt"]
    assert list(c["entries"]) == ["remote:a.txt"] and c["progress"]["remote:a.txt"]["device"] == "dev-b"
    assert list(c["progress"]) == ["remote:a.txt"]
    m = merge(raw, now_ms=NOW)
    store.save_json(tmp_path / "index.json", m)                              # 不抛
    shelf.wire_bytes(m, "dev-b")                                             # 不抛
    json.dumps(m, ensure_ascii=False).encode("utf-8")
    assert shelf._clean_str("a\ud800b") == "ab"
    assert shelf.new_overlay("dev-a", "h\ud800x").get("hostname") == "hx"


def test_validate_entry_count_cap():
    ents = {f"remote:b/{i:06d}.txt": {"fav": [True, 5]} for i in range(shelf.MAX_ENTRIES + 10)}
    c, issues = validate_overlay(ov_of("pc", ents), NOW)
    assert len(c["entries"]) == shelf.MAX_ENTRIES and any("too many" in i for i in issues)


def test_validate_never_raises_on_garbage():
    rnd = random.Random(7)
    atoms = [None, True, 1, -1, 2.5, "s", [], {}, [1], ["a", 2], {"a": 1}, float("nan"), 10 ** 30, 10 ** 400, -10 ** 400]

    def junk(depth=0):
        r = rnd.random()
        if depth > 2 or r < 0.5:
            return rnd.choice(atoms)
        if r < 0.75:
            return [junk(depth + 1) for _ in range(rnd.randint(0, 3))]
        return {rnd.choice(["fav", "hidden", "meta", "rekeyed_from", "x", BID_R1, "percent", "updated"]): junk(depth + 1)
                for _ in range(rnd.randint(0, 4))}
    for _ in range(300):
        raw = {"version": 1, "device_id": "pc", "entries": {BID_R1: junk(), BID_L1: junk()}, "progress": {BID_R1: junk()}}
        clean, _issues = validate_overlay(raw, NOW)
        assert clean is not None
        merge(raw, now_ms=NOW)                         # merge 也不能抛


def test_validate_huge_integers_never_raise():
    """json.loads 接受任意大的整数；float() / math.isfinite() 对 >1e308 的 int 抛 OverflowError，不得逃出校验。"""
    big = "1" + "0" * 400
    raw_json = ('{"version":1,"device_id":"dev-b","entries":{"remote:a/b.epub":'
                '{"fav":[true,' + big + '],"hidden":[true,5],"meta":[{"title":"t"},' + big + ']}},'
                '"progress":{"remote:a/b.epub":{"percent":' + big + ',"updated":5},'
                '"remote:c/d.epub":{"percent":5,"updated":' + big + '},'
                '"remote:e/f.epub":{"percent":-' + big + ',"updated":5},'
                '"remote:g/h.epub":{"percent":50,"updated":6}}}')
    raw = json.loads(raw_json)
    clean, _ = validate_overlay(raw, NOW, device_id="dev-b")
    assert clean is not None
    assert list(clean["progress"]) == ["remote:g/h.epub"]            # 巨大 percent / updated 的记录被跳过，其余保留
    assert clean["entries"]["remote:a/b.epub"]["hidden"][:2] == [True, 5]
    m = merge(raw, now_ms=NOW)                                       # merge 也不抛
    assert "remote:g/h.epub" in m["progress"]
    assert validate_overlay(json.loads(raw_json.replace('"version":1', '"version":' + big)), NOW, device_id="dev-b")[0] is None
    assert derive_status(10 ** 400) == "unread"
    assert shelf._num(10 ** 400) is None and shelf._num(-10 ** 400) is None
    ov = shelf.new_overlay("dev-a", "h")
    with pytest.raises(ValueError):
        shelf.set_progress(ov, BID_R1, 10 ** 400)
    with pytest.raises(ValueError):
        shelf.set_progress(ov, BID_R1, 5, updated=10 ** 400)
    ov["progress"][BID_R1] = {"percent": 10 ** 400, "updated": 5, "device": "dev-a"}
    assert shelf.to_wire(ov)["progress"] == {}                       # 上传白名单同样不抛


# ================================================================== merge：具体规则
def test_merge_lww_and_device_tiebreak():
    a = ov_of("dev-a", {BID_R1: {"fav": [True, 100]}})
    b = ov_of("dev-b", {BID_R1: {"fav": [False, 200]}})
    assert merge(a, b, now_ms=NOW)["entries"][BID_R1]["fav"][:2] == [False, 200]
    assert merge(b, a, now_ms=NOW)["entries"][BID_R1]["fav"][:2] == [False, 200]
    a = ov_of("dev-a", {BID_R1: {"fav": [True, 100]}})
    b = ov_of("dev-b", {BID_R1: {"fav": [False, 100]}})                   # 平局：device_id 大者胜
    for args in ((a, b), (b, a)):
        reg = merge(*args, now_ms=NOW)["entries"][BID_R1]["fav"]
        assert reg == [False, 100, "dev-b"]


def test_merge_tiebreak_survives_remerge():
    """合并结果再参与合并时，平局仍按原写者的 device_id 判，而不是按合并后的文件名。"""
    a = ov_of("dev-a", {BID_R1: {"fav": [True, 100]}})
    b = ov_of("dev-b", {BID_R1: {"fav": [False, 100]}})
    c = ov_of("dev-c", {BID_R1: {"fav": [True, 100]}})
    left = merge(merge(a, b, now_ms=NOW), c, now_ms=NOW)
    right = merge(a, merge(b, c, now_ms=NOW), now_ms=NOW)
    assert left == right and left["entries"][BID_R1]["fav"] == [True, 100, "dev-c"]


def test_merge_same_device_same_ts_different_value_is_deterministic():
    """克隆的 device_id 在同一毫秒写出不同值：仍要收敛到同一个结果。"""
    a = ov_of("clone", {BID_R1: {"fav": [True, 5], "meta": [{"title": "a"}, 5]}})
    b = ov_of("clone", {BID_R1: {"fav": [False, 5], "meta": [{"title": "b"}, 5]}})
    assert merge(a, b, now_ms=NOW) == merge(b, a, now_ms=NOW)


def test_merge_meta_register_converges_as_a_whole():
    a = ov_of("dev-a", {BID_R1: {"meta": [{"fp": "aaaa", "title": "旧名"}, 100]}})
    b = ov_of("dev-b", {BID_R1: {"meta": [{"title": "新名"}, 200]}})
    m1, m2 = merge(a, b, now_ms=NOW), merge(b, a, now_ms=NOW)
    assert m1 == m2
    assert m1["entries"][BID_R1]["meta"] == [{"title": "新名"}, 200, "dev-b"]       # 整体取胜者，不混字段


def test_merge_rekeyed_from_union():
    a = ov_of("dev-a", {BID_R1: {"rekeyed_from": [BID_R2]}})
    b = ov_of("dev-b", {BID_R1: {"rekeyed_from": [BID_L1, BID_R2, "bad id"]}})
    assert merge(a, b, now_ms=NOW)["entries"][BID_R1]["rekeyed_from"] == sorted({BID_R2, BID_L1})
    assert merge(b, a, now_ms=NOW) == merge(a, b, now_ms=NOW)


def test_merge_progress_digest():
    a = ov_of("dev-a", progress={BID_R1: {"percent": 10, "updated": 100.0}})
    b = ov_of("dev-b", progress={BID_R1: {"percent": 20, "updated": 100.0}})
    c = ov_of("dev-c", progress={BID_R1: {"percent": 5, "updated": 99.0}})
    m = merge(a, b, c, now_ms=NOW)["progress"][BID_R1]
    assert m == {"percent": 20.0, "updated": 100.0, "device": "dev-b"}
    assert merge(c, b, a, now_ms=NOW) == merge(a, b, c, now_ms=NOW)


def test_merge_unknown_fields_kept_not_merged():
    a = ov_of("dev-a", {BID_R1: {"fav": [True, 1], "future": {"v": 1}}})
    b = ov_of("dev-b", {BID_R1: {"other": 7}})
    e = merge(a, b, now_ms=NOW)["entries"][BID_R1]
    assert e["future"] == {"v": 1} and e["other"] == 7 and e["fav"][0] is True


def test_merge_skips_invalid_inputs_and_reports_clock():
    good = ov_of("dev-a", {BID_R1: {"fav": [True, 5]}})
    skewed = ov_of("dev-x", {BID_R1: {"fav": [False, 4_000_000_000_000]}})
    m = merge(good, "junk", None, {"device_id": "../x"}, skewed, now_ms=NOW)
    assert m["_clock"] == ["dev-x"]
    assert m["entries"][BID_R1]["fav"][:2] == [True, 5]                      # 坏时钟的值被截断为最旧，输给任何正常写入
    only_bad = merge(skewed, now_ms=NOW)
    assert only_bad["entries"][BID_R1]["fav"][:2] == [False, 1]              # 没有别的写入时才生效
    assert merge(now_ms=NOW) == {"version": 1, "_view": True, "entries": {}, "progress": {}, "_clock": []}


# ================================================================== merge：随机化性质（种子固定）
DEVS = ["dev-a", "dev-b", "dev-c", "dev-d"]
BIDS = [BID_R1, BID_R2, BID_L1, BID_L2]


def rand_overlay(rnd):
    ents, prog = {}, {}
    for bid in rnd.sample(BIDS, rnd.randint(0, len(BIDS))):
        e = {}
        for f in ("fav", "hidden"):
            if rnd.random() < 0.6:
                e[f] = [rnd.random() < 0.5, rnd.choice([1, 2, 3, 3, 4, NOW + 30 * HOUR, 10 ** 18])]
        if rnd.random() < 0.5:
            e["meta"] = [rnd.choice([{"title": "t1"}, {"title": "t2", "fp": "ab12"}, {"fp": "cd34"}]), rnd.randint(1, 4)]
        if rnd.random() < 0.3:
            e["rekeyed_from"] = rnd.sample(BIDS, rnd.randint(0, 3))
        if rnd.random() < 0.2:
            e["extra"] = rnd.choice([1, "x", {"k": [1]}])
        if rnd.random() < 0.1:
            e["fav"] = ["bad", 1]
        ents[bid] = e
    for bid in rnd.sample(BIDS, rnd.randint(0, 3)):
        prog[bid] = {"percent": rnd.choice([0, 12.5, 50, 97, 100, 300]), "updated": rnd.choice([1.0, 2.0, 2.0, 3.0])}
    return ov_of(rnd.choice(DEVS), ents, prog)


def test_merge_properties_seeded():
    rnd = random.Random(20261023)
    for _ in range(400):
        a, b, c = rand_overlay(rnd), rand_overlay(rnd), rand_overlay(rnd)
        m = lambda *xs: merge(*xs, now_ms=NOW)
        assert m(a, b) == m(b, a)                                           # 交换律
        assert m(m(a, b), c) == m(a, m(b, c)) == m(a, b, c)                 # 结合律
        assert m(m(a, b), m(a, c)) == m(a, b, c)                            # 分配：重复参与不改变结果
        assert m(a, a) == m(a)                                              # 幂等
        x = m(a, b)
        assert m(x, x) == x and m(x, a) == x and m(a, x) == x               # 对视图幂等 / 吸收
        # 输入顺序无关
        xs = [a, b, c]
        rnd.shuffle(xs)
        assert m(*xs) == m(a, b, c)


def test_merge_does_not_mutate_inputs():
    rnd = random.Random(5)
    a, b = rand_overlay(rnd), rand_overlay(rnd)
    ca, cb = copy.deepcopy(a), copy.deepcopy(b)
    merge(a, b, now_ms=NOW)
    assert a == ca and b == cb


def test_merge_result_is_valid_and_revalidates_to_itself():
    rnd = random.Random(11)
    for _ in range(100):
        v = merge(rand_overlay(rnd), rand_overlay(rnd), now_ms=NOW)
        again, issues = validate_overlay(v, NOW, view=True)
        assert again == v and issues == []


# ================================================================== set_field / 混合时钟
def test_set_field_basic_and_rev():
    ov = dev_overlay("dev-a")
    ts = set_field(ov, BID_R1, "fav", True, now_ms=NOW)
    assert ts == NOW and ov["entries"][BID_R1]["fav"] == [True, NOW, "dev-a"] and ov["_rev"] == 1
    ts2 = set_field(ov, BID_R1, "fav", False, now_ms=NOW)                  # 同一毫秒再点：严格递增
    assert ts2 == NOW + 1 and ov["_rev"] == 2
    ts3 = set_field(ov, BID_R1, "fav", True, now_ms=NOW - 5000)            # 本机时钟倒退：仍然递增
    assert ts3 == NOW + 2


def test_set_field_skew_applied():
    ov = dev_overlay("dev-a")
    assert set_field(ov, BID_R1, "hidden", True, now_ms=NOW, skew_ms=-1500) == NOW - 1500


def test_clock_peer_one_hour_ahead_local_click_still_wins():
    local = dev_overlay("dev-a")
    peer = ov_of("dev-z", {BID_R1: {"fav": [True, NOW + HOUR]}})            # 对端时钟快 1 小时（且设备名更大）
    merged = merge(local, peer, now_ms=NOW)
    ts = set_field(local, BID_R1, "fav", False, merged=merged, now_ms=NOW)
    assert ts == NOW + HOUR + 1
    final = merge(local, peer, now_ms=NOW)
    assert final["entries"][BID_R1]["fav"] == [False, NOW + HOUR + 1, "dev-a"]
    # 没有「观察合并值」就会输（证明这条规则确有作用）
    local2 = dev_overlay("dev-a")
    set_field(local2, BID_R1, "fav", False, now_ms=NOW)
    assert merge(local2, peer, now_ms=NOW)["entries"][BID_R1]["fav"][0] is True


def test_clock_peer_year_2099_does_not_freeze_field():
    local = dev_overlay("dev-a")
    peer = ov_of("dev-z", {BID_R1: {"fav": [True, 4_070_908_800_000], "hidden": [True, 9 * 10 ** 18]}})
    merged = merge(local, peer, now_ms=NOW)
    assert merged["_clock"] == ["dev-z"]
    ts = set_field(local, BID_R1, "fav", False, merged=merged, now_ms=NOW)
    assert ts == NOW                                                        # 对端异常值按最旧算，而不是 2099 年
    for later in (NOW + 10, NOW + 10 * HOUR, NOW + 30 * 24 * HOUR):          # 之后任何时候同步，点击都不会被压回去
        final = merge(local, peer, now_ms=later)
        assert final["entries"][BID_R1]["fav"][0] is False
        assert final["entries"][BID_R1]["hidden"][:2] == [True, 1]           # 没人改过的字段仍保留对端的值
    # 就算调用方直接把未清洗的合并视图传进来，也不会被冻结
    frozen = {"entries": {BID_R1: {"fav": [True, 4_070_908_800_000, "dev-z"]}}}
    assert set_field(local, BID_R1, "fav", True, merged=frozen, now_ms=NOW) == NOW + 1


def test_set_field_meta_written_once_and_overwritten_when_changed():
    ov = dev_overlay("dev-a")
    set_field(ov, BID_R1, "fav", True, now_ms=NOW, meta={"fp": "ab12", "title": "三体"})
    assert ov["entries"][BID_R1]["meta"][0] == {"fp": "ab12", "title": "三体"}
    m_ts = ov["entries"][BID_R1]["meta"][1]
    set_field(ov, BID_R1, "fav", False, now_ms=NOW + 5, meta={"fp": "ab12", "title": "三体"})
    assert ov["entries"][BID_R1]["meta"][1] == m_ts                          # 等值不重写
    set_field(ov, BID_R1, "fav", True, now_ms=NOW + 6, meta={"fp": "ab12", "title": "三体 修订"})
    assert ov["entries"][BID_R1]["meta"][0]["title"] == "三体 修订"


def test_set_field_rejects_bad_arguments():
    ov = dev_overlay("dev-a")
    with pytest.raises(ValueError):
        set_field(ov, BID_R1, "tags", True)
    with pytest.raises(ValueError):
        set_field(ov, BID_R1, "fav", "yes")
    with pytest.raises(ValueError):
        set_field(ov, "../x", "fav", True)
    with pytest.raises(ValueError):
        set_field({"entries": {}}, BID_R1, "fav", True)
    assert ov["entries"] == {} and ov["_rev"] == 0


def test_two_devices_offline_edits_converge():
    a, b = dev_overlay("dev-a"), dev_overlay("dev-b")
    set_field(a, BID_R1, "fav", True, now_ms=NOW)
    set_field(b, BID_R1, "fav", False, now_ms=NOW + 100)
    set_field(a, BID_R2, "hidden", True, now_ms=NOW + 5)
    ma, mb = merge(a, b, now_ms=NOW + 1000), merge(b, a, now_ms=NOW + 1000)
    assert ma == mb
    assert ma["entries"][BID_R1]["fav"][:2] == [False, NOW + 100] and ma["entries"][BID_R2]["hidden"][0] is True
    # 同一毫秒冲突：device_id 大者胜
    c, d = dev_overlay("dev-c"), dev_overlay("dev-d")
    set_field(c, BID_R1, "fav", True, now_ms=NOW)
    set_field(d, BID_R1, "fav", False, now_ms=NOW)
    assert merge(c, d, now_ms=NOW)["entries"][BID_R1]["fav"] == [False, NOW, "dev-d"]


def test_set_progress():
    ov = dev_overlay("dev-a")
    shelf.set_progress(ov, BID_R1, 120, updated=50.0)
    assert ov["progress"][BID_R1] == {"percent": 100.0, "updated": 50.0, "device": "dev-a"} and ov["_rev"] == 1
    with pytest.raises(ValueError):
        shelf.set_progress(ov, BID_R1, "x")


# ================================================================== to_wire 白名单
SECRETS = ("api_key", "webdav_password", "sk-SECRET", "hunter2", "C:\\Users", "C:/Users", "/home/gary")


def test_to_wire_whitelist_only():
    ov = dev_overlay("dev-a")
    set_field(ov, BID_R1, "fav", True, now_ms=NOW, meta={"fp": "ab12", "title": "三体"})
    set_field(ov, BID_L1, "hidden", True, now_ms=NOW)
    shelf.set_progress(ov, BID_R1, 37.0, updated=123.0)
    # 污染：本地簿记键、本机路径、配置里的东西、未知字段、别的设备写的值
    ov["_rev"] = 99
    ov["_path"] = "C:\\Users\\gary\\Books\\a.txt"
    ov["api_key"] = "sk-SECRET"
    ov["cfg"] = {"webdav_password": "hunter2"}
    ov["entries"][BID_R1]["_local_path"] = "C:/Users/gary/Books/a.txt"
    ov["entries"][BID_R1]["future"] = {"webdav_password": "hunter2"}
    ov["entries"][BID_R1]["rekeyed_from"] = [BID_R2]
    ov["entries"][BID_R2] = {"fav": [True, 5, "dev-other"], "_x": 1}      # 不是本设备写的
    ov["progress"][BID_R2] = {"percent": 1, "updated": 1.0, "device": "dev-other"}
    ov["progress"]["not an id"] = {"percent": 1, "updated": 1.0}
    w = to_wire(ov)
    assert set(w) == {"version", "device_id", "hostname", "user", "entries", "progress"}
    assert w["device_id"] == "dev-a" and w["user"] == "gary"
    assert w["entries"] == {BID_L1: {"hidden": [True, NOW]},
                            BID_R1: {"fav": [True, NOW], "meta": [{"fp": "ab12", "title": "三体"}, NOW]}}
    assert w["progress"] == {BID_R1: {"percent": 37.0, "updated": 123.0}}

    def keys(o):
        if isinstance(o, dict):
            for k, v in o.items():
                yield k
                yield from keys(v)
        elif isinstance(o, list):
            for v in o:
                yield from keys(v)
    assert not [k for k in keys(w) if isinstance(k, str) and k.startswith("_")]
    blob = shelf.wire_bytes(ov).decode("utf-8")
    for s in SECRETS:
        assert s not in blob, s
    assert json.loads(blob) == json.loads(json.dumps(w))
    assert shelf.wire_hash(ov) == shelf.wire_hash(ov) and len(shelf.wire_hash(ov)) == 40


def test_to_wire_hash_changes_with_content_only():
    ov = dev_overlay("dev-a")
    set_field(ov, BID_R1, "fav", True, now_ms=NOW)
    h = shelf.wire_hash(ov)
    ov["_rev"] += 5
    ov["_anything"] = 1
    assert shelf.wire_hash(ov) == h
    set_field(ov, BID_R1, "fav", False, now_ms=NOW)
    assert shelf.wire_hash(ov) != h


def test_to_wire_requires_valid_device_id():
    with pytest.raises(ValueError):
        to_wire({"entries": {}})
    with pytest.raises(ValueError):
        to_wire({"device_id": "../x", "entries": {}})


def test_to_wire_from_merged_view_with_device_only_exports_own_values():
    a, b = dev_overlay("dev-a"), dev_overlay("dev-b")
    set_field(a, BID_R1, "fav", True, now_ms=NOW)
    set_field(b, BID_R2, "fav", True, now_ms=NOW)
    view = merge(a, b, now_ms=NOW)
    w = to_wire(view, device_id="dev-a")
    assert list(w["entries"]) == [BID_R1]
    # 往返：wire → validate(文件名取 device_id) → 与原值一致
    clean, issues = validate_overlay(json.loads(shelf.wire_bytes(a)), NOW, device_id="dev-a")
    assert issues == [] and merge(clean, now_ms=NOW) == merge(a, now_ms=NOW)


def test_wire_roundtrip_property():
    rnd = random.Random(99)
    for _ in range(100):
        raw = rand_overlay(rnd)
        clean, _ = validate_overlay(raw, NOW)
        wire = to_wire(clean)
        again, issues = validate_overlay(json.loads(json.dumps(wire)), NOW, device_id=clean["device_id"])
        assert issues == []
        for bid, e in again["entries"].items():                      # 白名单字段无损往返
            for f in ("fav", "hidden", "meta"):
                if f in e:
                    assert e[f] == clean["entries"][bid][f]
        assert again["progress"] == clean["progress"]


# ================================================================== 本机存储
def test_paths_follow_explicit_root(tmp_path):
    r = str(tmp_path / "home")
    assert shelf.state_path(r) == os.path.join(r, "shelf", "state.json")
    assert shelf.overlay_path(r) == os.path.join(r, "shelf", "overlay.json")
    assert shelf.index_path(r) == os.path.join(r, "shelf", "index.json")
    assert shelf.peers_dir(r) == os.path.join(r, "shelf", "peers")


def test_default_root_uses_pyreader_home(tmp_path, monkeypatch):
    monkeypatch.setenv("PYREADER_HOME", str(tmp_path / "ph"))
    assert shelf.shelf_dir() == os.path.join(os.path.abspath(str(tmp_path / "ph")), "shelf")
    from pyreader import config
    assert not shelf.shelf_dir().startswith(os.path.join(os.path.expanduser("~"), ".pyreader"))
    assert os.environ["PYREADER_HOME"] and config.APP_DIR       # conftest 已隔离，测试不会碰真实 home


def test_load_state_creates_stable_device_id_and_defaults(tmp_path):
    r = str(tmp_path)
    st = shelf.load_state(r)
    assert store.safe_component(st["device_id"]) and st["ui"]["view"] == "grid"
    assert st["confirmed_upload"] is False and st["pushed_rev"] == 0 and st["remote_stamp"] is None
    assert os.path.isfile(shelf.state_path(r))
    assert shelf.load_state(r)["device_id"] == st["device_id"]                 # 固定
    assert st["cursors"] == {"peers": {}, "rounds_since_full": 0}


def test_state_roundtrip_and_repairs_bad_fields(tmp_path):
    r = str(tmp_path)
    st = shelf.load_state(r)
    st.update(confirmed_upload=True, pushed_rev=12, pushed_hash="abc", clock_skew_ms=-300,
              remote_stamp={"mtime": 1761200000, "size": 812, "etag": "e1"})
    st["ui"].update(view="list", sort="title", collapsed=["cloud:漫画"], last_shelf="cloud:小说", start_in_shelf=True)
    st["cursors"]["peers"]["gary-phone-1c2d"] = {"mtime": 5, "size": 6, "etag": "x", "fetched_at": 7.5}
    st["_memory_only"] = 1
    assert shelf.save_state(st, r)
    on_disk = json.load(open(shelf.state_path(r), encoding="utf-8"))
    assert "_memory_only" not in on_disk
    st2 = shelf.load_state(r)
    for k in ("confirmed_upload", "pushed_rev", "pushed_hash", "clock_skew_ms", "remote_stamp", "ui", "cursors"):
        assert st2[k] == st[k]
    # 字段类型被篡改 → 回退默认，不抛
    on_disk.update(ui={"view": "weird", "sort": 5, "collapsed": "x"}, pushed_rev="no", clock_skew_ms=10 ** 15,
                   cursors={"peers": {"../x": {"mtime": 1, "size": 1}, "ok-dev": "bad"}}, confirmed_upload="yes",
                   device_id="../evil", remote_stamp=[1], future_field=[1, 2])
    store.save_json(shelf.state_path(r), on_disk)
    st3 = shelf.load_state(r)
    assert st3["ui"] == shelf.DEFAULT_UI and st3["pushed_rev"] == 0 and st3["clock_skew_ms"] == 0
    assert st3["cursors"]["peers"] == {} and st3["confirmed_upload"] is False and st3["remote_stamp"] is None
    assert store.safe_component(st3["device_id"]) and st3["future_field"] == [1, 2]   # 未知字段保留
    assert shelf.load_state(r)["device_id"] == st3["device_id"]                      # 新生成的 id 已落盘固定


def test_state_corrupt_file_quarantined_not_overwritten(tmp_path):
    r = str(tmp_path)
    os.makedirs(os.path.dirname(shelf.state_path(r)))
    open(shelf.state_path(r), "w").write("{not json")
    st = shelf.load_state(r)
    assert st["version"] == 1
    bad = [f for f in os.listdir(os.path.dirname(shelf.state_path(r))) if ".bad-" in f]
    assert len(bad) == 1 and open(os.path.join(os.path.dirname(shelf.state_path(r)), bad[0])).read() == "{not json"
    assert json.load(open(shelf.state_path(r)))["device_id"] == st["device_id"]


def test_state_higher_version_is_read_only(tmp_path):
    r = str(tmp_path)
    os.makedirs(os.path.dirname(shelf.state_path(r)))
    store.save_json(shelf.state_path(r), {"version": 2, "device_id": "dev-new", "pushed_rev": 3})
    before = open(shelf.state_path(r), "rb").read()
    st = shelf.load_state(r)
    assert st["_readonly"] and st["device_id"] == "dev-new"
    assert shelf.save_state(st, r) is False
    assert open(shelf.state_path(r), "rb").read() == before


def test_overlay_save_load_roundtrip_and_atomic(tmp_path):
    r = str(tmp_path)
    ov = shelf.load_overlay(r)                                                   # 不存在 → 空
    assert ov["entries"] == {} and ov["progress"] == {} and ov["version"] == 1 and not os.path.exists(shelf.overlay_path(r))
    set_field(ov, BID_R1, "fav", True, now_ms=NOW, meta={"title": "三体"})
    shelf.set_progress(ov, BID_R1, 37.0, updated=123.0)
    assert shelf.save_overlay(ov, r)
    on_disk = json.load(open(shelf.overlay_path(r), encoding="utf-8"))
    assert on_disk == to_wire(ov) and "_rev" not in on_disk
    assert on_disk["entries"][BID_R1]["fav"] == [True, NOW]                      # 磁盘上是线上格式
    ov2 = shelf.load_overlay(r)
    assert ov2["device_id"] == ov["device_id"] and ov2["_rev"] == 0
    assert merge(ov2, now_ms=NOW) == merge(ov, now_ms=NOW)
    assert not [f for f in os.listdir(os.path.dirname(shelf.overlay_path(r))) if f.endswith(".tmp")]


def test_overlay_load_ignores_device_id_inside_file(tmp_path):
    r = str(tmp_path)
    mine = shelf.load_state(r)["device_id"]
    os.makedirs(os.path.dirname(shelf.overlay_path(r)), exist_ok=True)
    store.save_json(shelf.overlay_path(r), {"version": 1, "device_id": "someone-else",
                                            "entries": {BID_R1: {"fav": [True, 5]}}})
    ov = shelf.load_overlay(r)
    assert ov["device_id"] == mine and ov["entries"][BID_R1]["fav"] == [True, 5, mine]


def test_overlay_corrupt_and_higher_version(tmp_path):
    r = str(tmp_path)
    os.makedirs(os.path.dirname(shelf.overlay_path(r)))
    open(shelf.overlay_path(r), "w").write("[[[")
    ov = shelf.load_overlay(r)
    assert ov["entries"] == {} and not ov.get("_readonly")
    assert any(".bad-" in f for f in os.listdir(os.path.dirname(shelf.overlay_path(r))))
    store.save_json(shelf.overlay_path(r), {"version": 9, "device_id": "x", "entries": {}})
    before = open(shelf.overlay_path(r), "rb").read()
    ov = shelf.load_overlay(r)
    assert ov["_readonly"] and shelf.save_overlay(ov, r) is False
    assert open(shelf.overlay_path(r), "rb").read() == before


def test_shelf_module_does_not_import_qt_or_touch_disk_on_import():
    import subprocess
    import sys
    code = ("import sys, os; os.environ['PYREADER_HOME'] = sys.argv[1]; import pyreader.shelf as s; "
            "mods = [m for m in sys.modules if m.startswith('PySide6')]; "
            "print(len(mods)); print(os.path.exists(os.path.join(sys.argv[1], 'shelf')))")
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        out = subprocess.run([sys.executable, "-I", "-c", "import sys; sys.path.insert(0, %r); %s" % (root, code), d],
                             capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    n_qt, shelf_dir_exists = out.stdout.split()
    assert n_qt == "0" and shelf_dir_exists == "False"


def test_valid_book_id():
    assert shelf.valid_book_id(BID_R1) and shelf.valid_book_id(BID_L1)
    for bad in ("", "remote:", "remote:../x", "remote:a\\b", "remote:.hid/x.txt", "local:abc", "local:" + "A" * 40,
                "other:x", None, 5, "remote:" + "a/" * 600 + "x"):
        assert not shelf.valid_book_id(bad), bad
