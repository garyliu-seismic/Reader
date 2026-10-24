"""阶段 0：store.py（原子写 / 损坏隔离 / 名字校验）与 PYREADER_HOME 隔离。"""
import glob
import json
import os
import subprocess
import sys
import threading

import pytest

from pyreader import config, store
from pyreader.store import (load_json, normalize_rel, quarantine, safe_component, safe_segment,
                            save_json)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------- 原子写 ----------------
def test_save_load_roundtrip_and_no_tmp_left(tmp_path):
    p = tmp_path / "sub" / "a.json"                     # 父目录不存在也能写
    save_json(p, {"书": "三体", "n": [1, 2]})
    assert load_json(p, None) == {"书": "三体", "n": [1, 2]}
    assert "三体" in p.read_text(encoding="utf-8")       # ensure_ascii=False
    assert os.listdir(p.parent) == ["a.json"]            # 没有残留的临时文件


def test_missing_file_returns_default_without_side_effects(tmp_path):
    p = tmp_path / "nope.json"
    d = {"x": 1}
    assert load_json(p, d) is d
    assert os.listdir(tmp_path) == []


class _Boom(Exception):
    pass


def test_unserializable_object_keeps_old_file(tmp_path):
    p = tmp_path / "a.json"
    save_json(p, {"v": 1})
    with pytest.raises(TypeError):
        save_json(p, {"v": object()})
    assert load_json(p, None) == {"v": 1}
    assert os.listdir(tmp_path) == ["a.json"]


@pytest.mark.parametrize("target", ["fsync", "replace"])
def test_failure_midway_keeps_old_file_and_cleans_tmp(tmp_path, monkeypatch, target):
    p = tmp_path / "a.json"
    save_json(p, {"v": "old"})

    def boom(*a, **k):
        raise _Boom()
    monkeypatch.setattr(store.os, target, boom)
    with pytest.raises(_Boom):
        save_json(p, {"v": "new", "pad": "x" * 100000})
    monkeypatch.undo()
    assert load_json(p, None) == {"v": "old"}            # 旧文件完好
    assert os.listdir(tmp_path) == ["a.json"]            # 临时文件已清理


def test_partial_write_crash_never_leaves_truncated_target(tmp_path, monkeypatch):
    """写临时文件写到一半崩：目标文件要么是旧内容，要么是新内容，绝不是截断的。"""
    p = tmp_path / "a.json"
    save_json(p, {"v": "old"})
    real_open = open

    class _Half:
        def __init__(self, f):
            self.f = f

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.f.close()

        def write(self, b):
            self.f.write(b[: len(b) // 2])
            raise _Boom()

        def __getattr__(self, n):
            return getattr(self.f, n)

    def fake_open(path, mode="r", *a, **k):
        f = real_open(path, mode, *a, **k)
        return _Half(f) if str(path).endswith(".tmp") else f
    monkeypatch.setattr("builtins.open", fake_open)
    with pytest.raises(_Boom):
        save_json(p, {"v": "new", "pad": "y" * 5000})
    monkeypatch.undo()
    assert load_json(p, None) == {"v": "old"}
    assert os.listdir(tmp_path) == ["a.json"]


def test_concurrent_saves_always_leave_valid_json(tmp_path):
    p = tmp_path / "a.json"
    save_json(p, {"i": -1})
    errs = []

    def writer(k):
        try:
            for i in range(25):
                save_json(p, {"w": k, "i": i, "pad": "z" * 2000})
        except Exception as e:      # pragma: no cover
            errs.append(e)
    ts = [threading.Thread(target=writer, args=(k,)) for k in range(4)]
    [t.start() for t in ts]
    [t.join(60) for t in ts]
    assert not errs
    assert load_json(p, None)["i"] == 24
    assert glob.glob(str(tmp_path / "*.bad-*")) == []    # 并发写没有制造损坏文件
    assert os.listdir(tmp_path) == ["a.json"]


# ---------------- 损坏隔离 ----------------
@pytest.mark.parametrize("raw", [b"{not json", b"", b"\xff\xfe\x00bad", b'{"a": 1'])
def test_corrupt_json_is_quarantined_not_overwritten(tmp_path, raw):
    p = tmp_path / "state.json"
    p.write_bytes(raw)
    assert load_json(p, {"default": True}) == {"default": True}
    assert not p.exists()                                # 原路径空出来了：后续 save 写的是新文件
    bad = glob.glob(str(p) + ".bad-*")
    assert len(bad) == 1 and open(bad[0], "rb").read() == raw    # 坏文件原样保留
    save_json(p, {"fresh": 1})
    assert load_json(p, None) == {"fresh": 1}
    assert open(bad[0], "rb").read() == raw              # 保存新数据不会动坏文件


def test_repeated_quarantine_never_clobbers_earlier_bad_files(tmp_path):
    p = tmp_path / "state.json"
    for i in range(3):
        p.write_bytes(b"corrupt-%d" % i)
        load_json(p, {})
    contents = sorted(open(b, "rb").read() for b in glob.glob(str(p) + ".bad-*"))
    assert contents == [b"corrupt-0", b"corrupt-1", b"corrupt-2"]


def test_expect_type_mismatch_is_quarantined(tmp_path):
    p = tmp_path / "bm.json"
    p.write_text("[1, 2]")
    assert load_json(p, {}, expect=dict) == {}
    assert glob.glob(str(p) + ".bad-*") and not p.exists()
    p.write_text('{"ok": 1}')
    assert load_json(p, {}, expect=dict) == {"ok": 1}


def test_utf8_bom_is_accepted(tmp_path):
    p = tmp_path / "a.json"
    p.write_bytes(b"\xef\xbb\xbf" + json.dumps({"a": 1}).encode())
    assert load_json(p, None) == {"a": 1}


def test_unreadable_path_returns_default_without_rename(tmp_path):
    d = tmp_path / "isdir.json"
    d.mkdir()                                            # 读目录 -> OSError，不能当「损坏」去改名
    assert load_json(d, "dflt") == "dflt"
    assert d.is_dir() and not glob.glob(str(d) + ".bad-*")


def test_quarantine_missing_file_returns_none(tmp_path):
    assert quarantine(tmp_path / "ghost.json") is None


def test_config_keeps_old_entry_points():
    assert config.load_json is store.load_json and config.save_json is store.save_json


# ---------------- 名字校验 ----------------
@pytest.mark.parametrize("name", ["gary-pc-3f9a1c", "a", "A_b.c-d", "9f2c0a7e51d3b8a46c10", "x" * 64,
                                  "default", "console", "com10", "a.epub"])
def test_safe_component_accepts(name):
    assert safe_component(name) == name


@pytest.mark.parametrize("name", [
    "", ".", "..", ".hidden", "a.", "x" * 65, "..\\..\\x", "../x", "a/b", "a\\b", "a b", "a:b",
    "a.epub:stream", "CON", "con", "Con.txt", "NUL", "aux.tar.gz", "COM1", "lpt9.log", "COM1.",
    "小明", "a\x00b", "a\nb", "a\u202eb", None, 5, b"abc",
])
def test_safe_component_rejects(name):
    assert safe_component(name) is None


@pytest.mark.parametrize("seg", ["三体.epub", "a b.txt", "Dune (2021).epub", "标题：副标题.txt", "x" * 255])
def test_safe_segment_accepts(seg):
    assert safe_segment(seg) == seg


@pytest.mark.parametrize("seg", [
    "", ".", "..", "a/b", "a\\b", "C:", "a:b.txt", "a.epub:stream", "x\x00", "tab\there", "\x7fdel",
    "name.", "name ", "CON", "nul.txt", "COM3.epub", "evil\u202etxt.exe", "x" * 256, None,
])
def test_safe_segment_rejects(seg):
    assert safe_segment(seg) is None


def test_normalize_rel():
    assert normalize_rel("小说/三体.epub") == "小说/三体.epub"
    assert normalize_rel("a/b/c.txt") == "a/b/c.txt"
    for bad in ["", "/abs.txt", "a//b", "a/../b", "../x", "a/./b", "a\\b", "C:/x", "a/CON/b.txt",
                "a/b.txt.", "a/ b /c ", "a/b\x01", "a/b:c", "x/" + "y" * 256, "a/" * 600, None]:
        assert normalize_rel(bad) is None, bad


# ---------------- PYREADER_HOME ----------------
def test_resolve_app_dir_override_and_default():
    assert config.resolve_app_dir({"PYREADER_HOME": str(ROOT)}) == os.path.abspath(ROOT)
    assert config.resolve_app_dir({"PYREADER_HOME": "  "}) == os.path.join(os.path.expanduser("~"), ".pyreader")
    assert config.resolve_app_dir({}) == os.path.join(os.path.expanduser("~"), ".pyreader")
    assert config.resolve_app_dir({"PYREADER_HOME": "~/x"}) == os.path.join(os.path.expanduser("~"), "x")


def test_conftest_isolates_all_module_level_paths():
    """conftest 顶部设置的 PYREADER_HOME 生效：所有从 APP_DIR 派生的路径都在临时目录里，而不是真实 home。"""
    from pyreader import sync, graph
    real = os.path.join(os.path.expanduser("~"), ".pyreader")
    home = os.environ["PYREADER_HOME"]
    assert os.path.abspath(config.APP_DIR) == os.path.abspath(home) != os.path.abspath(real)
    for p in (config.CONFIG_PATH, config.BOOKMARKS_PATH, config.CACHE_DIR, sync.REMOTE_BOOKS_DIR,
              str(graph._VIS_CACHE)):
        ap = os.path.abspath(p)
        assert ap.startswith(os.path.abspath(home) + os.sep), p
        assert not ap.startswith(os.path.abspath(real) + os.sep), p
    assert os.path.isdir(config.CACHE_DIR)


def test_import_with_override_does_not_touch_home(tmp_path):
    """全新解释器：HOME/USERPROFILE 指向空目录，PYREADER_HOME 指向别处；import 所有无 Qt 界面的模块后，
    空 home 里不能出现 .pyreader，且 APP_DIR 是在 import 时就被读到的。"""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    target = tmp_path / "elsewhere" / "data"
    env = dict(os.environ, PYREADER_HOME=str(target), HOME=str(fake_home), USERPROFILE=str(fake_home))
    env.pop("PYREADER_PROXY", None)
    code = ("import sys; sys.path.insert(0, %r)\n"
            "import pyreader.config as c, pyreader.store, pyreader.webdav, pyreader.sync, pyreader.cache, pyreader.graph\n"
            "print(c.APP_DIR)" % ROOT)
    out = subprocess.run([sys.executable, "-I", "-c", code], env=env, capture_output=True, text=True,
                         timeout=120)
    assert out.returncode == 0, out.stderr
    assert os.path.abspath(out.stdout.strip()) == os.path.abspath(target)
    assert (target / "cache").is_dir()
    assert os.listdir(fake_home) == []
