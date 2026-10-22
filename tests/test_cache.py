import os
import pytest
from pyreader import cache


@pytest.fixture
def cdir(tmp_path, monkeypatch):
    d = tmp_path / "cache"
    d.mkdir()
    monkeypatch.setattr(cache, "CACHE_DIR", str(d))
    return d


def _book(tmp_path):
    p = tmp_path / "b.txt"
    p.write_text("x" * 100, encoding="utf-8")
    return str(p)


def test_round_trip_two_tuples(tmp_path, cdir):
    p = _book(tmp_path)
    chs = [(0, "开篇"), (10, "第一章"), (50, "第二章")]
    cache.save_cache(p, chs, "书名")
    assert os.listdir(cdir), "save_cache 必须真的写出文件"
    got, title, meta = cache.load_cache(p)
    assert got == chs and title == "书名" and meta is None
    assert all(len(c) == 2 for c in got)


def test_round_trip_chapter_objects_and_legacy_triples(tmp_path, cdir):
    p = _book(tmp_path)

    class Ch:
        def __init__(self, s, e, t):
            self.start, self.end, self.title = s, e, t
    cache.save_cache(p, [Ch(0, 5, "a"), Ch(5, 9, "b")], "t")
    assert cache.load_cache(p)[0] == [(0, "a"), (5, "b")]
    cache.save_cache(p, [(0, 5, "a"), (5, 9, "b")], "t")
    assert cache.load_cache(p)[0] == [(0, "a"), (5, "b")]


def test_parser_version_in_key(tmp_path, cdir, monkeypatch):
    p = _book(tmp_path)
    cache.save_cache(p, [(0, "a")], "t")
    assert cache.load_cache(p) is not None
    monkeypatch.setattr(cache, "PARSER_VERSION", cache.PARSER_VERSION + 1)
    assert cache.load_cache(p) is None          # 解析器升级后旧缓存不再命中


def test_cache_miss_and_corrupt(tmp_path, cdir):
    p = _book(tmp_path)
    assert cache.load_cache(p) is None
    cache.save_cache(p, [(0, "a")], "t")
    (cdir / os.listdir(cdir)[0]).write_text("{bad json", encoding="utf-8")
    assert cache.load_cache(p) is None


def test_pager_accepts_cached_chapters(tmp_path, cdir):
    pytest.importorskip("PySide6")
    try:
        from pyreader.pager import LazyPager
        LazyPager("x" * 100, [(0, "开篇")])      # 先确认无 QApplication 也能构造
    except Exception as e:                        # 无显示环境等
        pytest.skip(f"LazyPager 不可在此环境构造: {e}")
    p = _book(tmp_path)
    cache.save_cache(p, [(0, "开篇"), (40, "第一章")], "t")
    chs, _, _ = cache.load_cache(p)
    pager = LazyPager("x" * 100, chs)
    assert [(c.title, c.start, c.end) for c in pager.chapters] == [("开篇", 0, 40), ("第一章", 40, 100)]
