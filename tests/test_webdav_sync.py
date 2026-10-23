import base64
import email.utils
import http.server
import json
import os
import threading
import time
from urllib.parse import unquote, quote

import pytest

from PySide6.QtWidgets import QApplication

from pyreader import sync, webdav
from pyreader.webdav import WebDavClient, WebDavError, client_from_cfg


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_app = QApplication.instance() or QApplication([])


def _pump():
    """worker 线程 emit 的信号是排队投递的，需要事件循环处理后回调才会触发。"""
    for _ in range(3):
        _app.processEvents()


class _Handler(http.server.BaseHTTPRequestHandler):
    """进程内最小 WebDAV 桩：PROPFIND/GET/PUT/MKCOL，可选 Basic 认证。"""
    protocol_version = "HTTP/1.1"
    files = None      # path -> bytes
    dirs = None       # set of paths
    auth = None       # "user:pass" or None
    seen_auth = None  # list of Authorization header values

    def log_message(self, *a):
        pass

    def _ok(self):
        type(self).seen_auth.append(self.headers.get("Authorization"))
        if self.auth:
            want = "Basic " + base64.b64encode(self.auth.encode()).decode()
            if self.headers.get("Authorization") != want:
                self._send(401, b"", {"WWW-Authenticate": 'Basic realm="t"'})
                return False
        return True

    def _send(self, code, body=b"", headers=None):
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _path(self):
        return unquote(self.path.split("?")[0]).rstrip("/") or "/"

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        if not self._ok():
            return
        p = self._path()
        if p in self.files:
            self._send(200, self.files[p])
        else:
            self._send(404)

    def do_PUT(self):
        data = self._body()
        if not self._ok():
            return
        p = self._path()
        parent = p.rsplit("/", 1)[0] or "/"
        if parent != "/" and parent not in self.dirs:
            return self._send(409)
        self.files[p] = data
        self._send(201)

    def do_MKCOL(self):
        self._body()
        if not self._ok():
            return
        p = self._path()
        if p in self.dirs:
            return self._send(405)
        parent = p.rsplit("/", 1)[0] or "/"
        if parent != "/" and parent not in self.dirs:
            return self._send(409)
        self.dirs.add(p)
        self._send(201)

    def _entry(self, p, is_dir):
        href = quote(p + ("/" if is_dir else ""))
        if is_dir:
            props = "<d:resourcetype><d:collection/></d:resourcetype>"
        else:
            props = ("<d:resourcetype/><d:getcontentlength>%d</d:getcontentlength>"
                     "<d:getlastmodified>%s</d:getlastmodified>"
                     % (len(self.files[p]), email.utils.formatdate(1700000000, usegmt=True)))
        return ("<d:response><d:href>%s</d:href><d:propstat><d:prop>%s</d:prop>"
                "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>" % (href, props))

    def do_PROPFIND(self):
        self._body()
        if not self._ok():
            return
        p = self._path()
        depth = self.headers.get("Depth", "1")
        if p in self.dirs or p == "/":
            parts = [self._entry(p, True)]
            if depth != "0":
                pref = p.rstrip("/") + "/"
                for d in sorted(self.dirs):
                    if d.startswith(pref) and "/" not in d[len(pref):]:
                        parts.append(self._entry(d, True))
                for f in sorted(self.files):
                    if f.startswith(pref) and "/" not in f[len(pref):]:
                        parts.append(self._entry(f, False))
        elif p in self.files:
            parts = [self._entry(p, False)]
        else:
            return self._send(404)
        xml = '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">%s</d:multistatus>' % "".join(parts)
        self._send(207, xml.encode(), {"Content-Type": "application/xml"})


def _start(auth=None):
    h = type("H", (_Handler,), {"files": {}, "dirs": set(), "auth": auth, "seen_auth": []})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), h)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, h, "http://127.0.0.1:%d/dav" % srv.server_address[1]


@pytest.fixture
def server():
    srv, h, url = _start()
    h.dirs.add("/dav")
    yield h, url
    srv.shutdown()


@pytest.fixture
def auth_server():
    srv, h, url = _start("bob:secret")
    h.dirs.add("/dav")
    yield h, url
    srv.shutdown()


# ---------------- client ----------------
def test_client_ops_no_auth_sends_no_header(server):
    h, url = server
    c = WebDavClient(url)
    assert c.exists("")
    assert not c.exists("a")
    c.mkdirs("a/b c")                      # mkdir -p，且对已存在目录幂等
    c.mkdirs("a/b c")
    assert c.exists("a/b c")
    c.put("a/b c/书.txt", "你好".encode())
    assert c.get("a/b c/书.txt") == "你好".encode()
    ents = {e.name: e for e in c.propfind("a/b c", 1)}
    assert set(ents) == {"书.txt"} and not ents["书.txt"].is_dir
    assert ents["书.txt"].size == len("你好".encode()) and ents["书.txt"].mtime == 1700000000
    top = {e.name: e.is_dir for e in c.propfind("a", 1)}
    assert top == {"b c": True}
    assert [e.name for e in c.propfind("a", 0)] == []   # depth 0 只有自身，被过滤
    assert all(a is None for a in h.seen_auth)


def test_client_basic_auth(auth_server):
    h, url = auth_server
    good = WebDavClient(url, "bob", "secret")
    good.mkdirs("x")
    good.put("x/f.txt", b"1")
    assert good.get("x/f.txt") == b"1"
    with pytest.raises(WebDavError):
        WebDavClient(url, "bob", "wrong").get("x/f.txt")
    with pytest.raises(WebDavError):
        WebDavClient(url).get("x/f.txt")                # 无凭据 -> 401


def test_list_tree_recursive(server):
    h, url = server
    c = WebDavClient(url)
    c.mkdirs("library/sub")
    c.put("library/a.txt", b"a")
    c.put("library/sub/b.epub", b"bb")
    got = sorted((r, e.size) for r, e in c.list_tree("library"))
    assert got == [("a.txt", 1), ("sub/b.epub", 2)]


# ---------------- book id ----------------
def test_book_id_stability(tmp_path, monkeypatch):
    a = tmp_path / "x" / "novel.txt"
    b = tmp_path / "y" / "novel.txt"
    a.parent.mkdir(); b.parent.mkdir()
    a.write_text("hello"); b.write_text("hello")
    assert sync.book_id(str(a)) == sync.book_id(str(b))     # 与路径无关
    b.write_text("hello!")
    assert sync.book_id(str(a)) != sync.book_id(str(b))     # 大小变了
    rb = tmp_path / "remote_books"
    monkeypatch.setattr(sync, "REMOTE_BOOKS_DIR", str(rb))
    (rb / "sci").mkdir(parents=True)
    p = rb / "sci" / "dune.epub"
    p.write_text("z")
    assert sync.book_id(str(p)) == "remote:sci/dune.epub"


# ---------------- merge / roundtrip ----------------
def test_merge_newer_wins():
    old = sync.make_record("b", 10, 1, "c", "A", updated=100)
    new = sync.make_record("b", 20, 2, "c", "B", updated=200)
    assert sync.merge_progress(old, new) is new
    assert sync.merge_progress(new, old) is new
    assert sync.merge_progress(old, None) is old
    assert sync.merge_progress(None, new) is new
    tie = sync.make_record("b", 99, 9, "c", "C", updated=100)
    assert sync.merge_progress(old, tie) is old             # 相等偏向本地


def test_resolve_offset_falls_back_to_percent():
    rec = sync.make_record("b", 5000, 50.0, "", "d")
    assert sync.resolve_offset(rec, 10000) == 5000
    assert sync.resolve_offset(rec, 1000) == 499            # offset 越界 -> 按百分比


def test_progress_push_pull_roundtrip(server):
    h, url = server
    c = WebDavClient(url)
    rec = sync.make_record("local:abc", 123, 4.5, "第一章", "pc", updated=1234.5)
    sync.push_progress(c, "alice", rec)
    key = "/dav/" + sync.progress_path("alice", "local:abc")
    assert key in h.files
    assert sync.pull_progress(c, "alice", "local:abc") == rec
    assert sync.pull_progress(c, "alice", "local:none") is None
    assert sync.pull_progress(c, "bob", "local:abc") is None   # 用户隔离
    # 远端文件里不得出现配置/密码字段
    blob = h.files[key].decode()
    assert set(json.loads(blob)) == {"book_id", "offset", "percent", "chapter", "updated", "device"}


def test_progress_filename_no_collision_for_non_ascii_titles(server):
    """不同的中文书名必须得到不同的进度文件，且文件内 book_id 不符时被忽略。"""
    h, url = server
    c = WebDavClient(url)
    ids = ["remote:小说/三体.epub", "remote:小说/红楼.epub", "remote:科幻/沙丘.epub"]
    assert len({sync.progress_path("alice", b) for b in ids}) == 3
    for i, b in enumerate(ids):
        sync.push_progress(c, "alice", sync.make_record(b, 100 + i, 1.0, "", "pc", updated=10.0 + i))
    for i, b in enumerate(ids):
        assert sync.pull_progress(c, "alice", b)["offset"] == 100 + i
    # 文件名碰撞/被篡改：文件体 book_id 与请求不符 -> 忽略
    h.files["/dav/" + sync.progress_path("alice", ids[0])] = json.dumps(
        sync.make_record(ids[1], 1, 1.0, "", "pc")).encode()
    assert sync.pull_progress(c, "alice", ids[0]) is None


# ---------------- no-op / failure ----------------
@pytest.mark.parametrize("cfg", [
    {}, {"webdav_enabled": False, "webdav_url": "http://x"},
    {"webdav_enabled": True, "webdav_url": "  "},
])
def test_noop_when_disabled(cfg):
    assert client_from_cfg(cfg) is None
    w = sync.SyncWorker()
    assert w.pull(cfg, __file__) is None
    assert w.push(cfg, sync.make_record("b", 1, 1, "", "d")) is None
    assert w.list_library(cfg, (".txt",)) is None
    assert w.download(cfg, "a.txt", 1) is None
    assert w.upload_book(cfg, __file__) is None


def test_failure_does_not_raise():
    cfg = {"webdav_enabled": True, "webdav_url": "http://127.0.0.1:9/dav"}   # 端口 9：连不上
    with pytest.raises(WebDavError):
        WebDavClient(cfg["webdav_url"], timeout=2).get("x")
    w = sync.SyncWorker()
    msgs, pulled = [], []
    w.status.connect(msgs.append)
    w.pulled.connect(lambda *a: pulled.append(a))
    for t in (w.pull(cfg, __file__),
              w.push(cfg, sync.make_record("b", 1, 1, "", "d")),
              w.list_library(cfg, (".txt",)),
              w.download(cfg, "a.txt", 1),
              w.upload_book(cfg, __file__)):
        t.join(30)
        assert not t.is_alive()
    _pump()
    assert not pulled
    assert len(msgs) == 5 and all("失败" in m or "下载" in m for m in msgs)   # pull/push/list/download/upload 各报状态


def test_download_skips_when_size_matches_and_blocks_traversal(server, tmp_path, monkeypatch):
    h, url = server
    rb = tmp_path / "rb"
    monkeypatch.setattr(sync, "REMOTE_BOOKS_DIR", str(rb))
    c = WebDavClient(url)
    c.mkdirs("library/s")
    c.put("library/s/a.txt", b"12345")
    cfg = {"webdav_enabled": True, "webdav_url": url}
    w = sync.SyncWorker()
    got = []
    w.downloaded.connect(got.append)
    w.download(cfg, "s/a.txt", 5).join(10)
    dest = rb / "s" / "a.txt"
    _pump()
    assert got == [str(dest)]
    dest.write_bytes(b"XXXXX")                  # 大小相同 -> 不应重新下载
    w.download(cfg, "s/a.txt", 5).join(10)
    assert dest.read_bytes() == b"XXXXX"
    msgs = []
    w.status.connect(msgs.append)
    w.download(cfg, "../evil.txt", 1).join(10)
    _pump()
    assert msgs and not (tmp_path / "evil.txt").exists()


def test_upload_book(server, tmp_path):
    h, url = server
    f = tmp_path / "up.txt"
    f.write_bytes(b"data")
    cfg = {"webdav_enabled": True, "webdav_url": url}
    sync.SyncWorker().upload_book(cfg, str(f)).join(10)
    assert h.files["/dav/library/up.txt"] == b"data"


def test_wsgidav_real_server(tmp_path):
    """可选：用真实 wsgidav 服务器验证客户端的互操作性（未安装则跳过）。"""
    pytest.importorskip("wsgidav")
    cheroot = pytest.importorskip("cheroot.wsgi")
    from wsgidav.wsgidav_app import WsgiDAVApp
    app = WsgiDAVApp({"provider_mapping": {"/": str(tmp_path)}, "simple_dc": {"user_mapping": {"*": True}},
                      "verbose": 0, "logging": {"enable": False}})
    srv = cheroot.Server(("127.0.0.1", 0), app)
    srv.prepare()
    port = srv.bind_addr[1]
    threading.Thread(target=srv.serve, daemon=True).start()
    try:
        c = WebDavClient("http://127.0.0.1:%d" % port)
        sync.push_progress(c, "u", sync.make_record("local:q", 7, 1.0, "", "d", updated=5))
        assert sync.pull_progress(c, "u", "local:q")["offset"] == 7
        c.put("library/x.txt", b"1") if c.mkcol("library") is not None else None
        assert [r for r, _ in c.list_tree("library")] == ["x.txt"]
    finally:
        srv.stop()
