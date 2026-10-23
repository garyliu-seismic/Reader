"""进程内最小 WebDAV 桩（测试专用，永远不连真实服务器）。

支持：PROPFIND / GET / PUT / MKCOL，可选 Basic 认证。
每个文件的 mtime 与 etag 随 PUT 变化；GET/PUT 解析 If-None-Match；响应带 ETag / Date 头。
故障注入（均为 start() 返回的 handler 类上的属性）：
  now             可替换的时钟（默认 time.time）→ 控制 mtime
  log             [(method, path)] 请求记录
  fail_propfind   {path} 这些目录的 PROPFIND 返回 500
  extra_entries   {dir_path: [(raw_href, is_dir)]} 往该目录的列表里注入任意 href（'..'、反斜杠名等）
  raw_propfind    {path: bytes} 直接返回这段原始 multistatus 内容（恶意 XML）
  links           {link_path: target_path} 目录符号链接（可造出环）
  redirects       {path: absolute_url} GET 该路径时返回 302
  ignore_inm      True：PUT 忽略 If-None-Match（模拟不支持条件请求的服务器）
  move_status     非 None：MOVE 一律返回这个状态码（如 405 / 501：模拟不支持 MOVE）
  no_etag         True：PROPFIND / 响应都不带 getetag（模拟不给 etag 的服务器）
  put_hooks       [f(path, data)]：每次 PUT 落盘之前依次调用（造「PUT 期间发生别的事」的竞态）；返回 bytes 则替换落盘内容（造截断）
  move_hooks      [f(src, dest)]：每次 MOVE 处理之前依次调用
  req_headers     [(method, path, {If-None-Match, Overwrite, Destination})] 请求头记录
MOVE 支持 Destination / Overwrite: F（目标已存在回 412）。
"""
import base64
import email.utils
import http.server
import itertools
import threading
import time
from urllib.parse import unquote, quote, urlparse


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    files = None      # path -> bytes
    dirs = None       # set of paths
    auth = None       # "user:pass" or None
    seen_auth = None  # list of Authorization header values
    mtimes = None
    etags = None
    log = None
    fail_propfind = None
    extra_entries = None
    raw_propfind = None
    links = None
    redirects = None
    ignore_inm = False
    move_status = None
    no_etag = False
    put_hooks = None
    move_hooks = None
    req_headers = None
    counter = None
    now = staticmethod(time.time)

    def log_message(self, *a):
        pass

    def _ok(self):
        type(self).seen_auth.append(self.headers.get("Authorization"))
        type(self).log.append((self.command, self._path()))
        type(self).req_headers.append((self.command, self._path(), {
            k: self.headers.get(k) for k in ("If-None-Match", "Overwrite", "Destination") if self.headers.get(k)}))
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

    def _resolve(self, p):
        """把目录符号链接前缀替换成目标路径。"""
        for _ in range(50):
            for link, tgt in self.links.items():
                if p == link or p.startswith(link + "/"):
                    p = tgt + p[len(link):]
                    break
            else:
                return p
        return p

    def _match_inm(self, p):
        """If-None-Match：'*' 匹配任何已存在资源；否则与当前 etag 比较。"""
        inm = (self.headers.get("If-None-Match") or "").strip()
        if not inm or p not in self.files or (self.command == "PUT" and self.ignore_inm):
            return False
        return inm == "*" or self.etags.get(p) in [x.strip() for x in inm.split(",")]

    def do_GET(self):
        if not self._ok():
            return
        p = self._path()
        if p in self.redirects:
            return self._send(302, b"", {"Location": self.redirects[p]})
        if p in self.files:
            if self._match_inm(p):
                return self._send(304, b"", {"ETag": self.etags[p]})
            self._send(200, self.files[p], {} if self.no_etag else {"ETag": self.etags[p]})
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
        if self._match_inm(p):
            return self._send(412)
        for hook in list(self.put_hooks):
            data = hook(p, data) or data
        self.files[p] = data
        self.mtimes[p] = int(type(self).now())
        self.etags[p] = '"%x-%d"' % (next(self.counter), len(data))
        self._send(201, b"", {} if self.no_etag else {"ETag": self.etags[p]})

    def do_MOVE(self):
        self._body()
        if not self._ok():
            return
        src = self._path()
        dest = unquote(urlparse(self.headers.get("Destination") or "").path).rstrip("/")
        for hook in list(self.move_hooks):
            hook(src, dest)
        if self.move_status:
            return self._send(self.move_status)
        if src not in self.files:
            return self._send(404)
        parent = dest.rsplit("/", 1)[0] or "/"
        if parent != "/" and parent not in self.dirs:
            return self._send(409)
        if dest in self.files and (self.headers.get("Overwrite") or "T").upper() == "F":
            return self._send(412)
        existed = dest in self.files
        self.files[dest] = self.files.pop(src)
        self.mtimes[dest] = self.mtimes.pop(src, int(type(self).now()))
        self.etags[dest] = self.etags.pop(src, "")
        self._send(204 if existed else 201)

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

    def _entry(self, href_path, real, is_dir):
        href = quote(href_path + ("/" if is_dir else ""))
        if is_dir:
            props = "<d:resourcetype><d:collection/></d:resourcetype>"
        else:
            props = ("<d:resourcetype/><d:getcontentlength>%d</d:getcontentlength>"
                     "<d:getlastmodified>%s</d:getlastmodified>%s"
                     % (len(self.files[real]),
                        email.utils.formatdate(self.mtimes.get(real, int(type(self).now())), usegmt=True),
                        "" if self.no_etag else "<d:getetag>%s</d:getetag>" % self.etags.get(real, "")))
        return self._raw_entry(href, props)

    @staticmethod
    def _raw_entry(href, props):
        return ("<d:response><d:href>%s</d:href><d:propstat><d:prop>%s</d:prop>"
                "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>" % (href, props))

    def do_PROPFIND(self):
        self._body()
        if not self._ok():
            return
        p = self._path()
        if p in self.fail_propfind:
            return self._send(500)
        if p in self.raw_propfind:
            return self._send(207, self.raw_propfind[p], {"Content-Type": "application/xml"})
        depth = self.headers.get("Depth", "1")
        real = self._resolve(p)
        if real in self.dirs or real == "/":
            parts = [self._entry(p, real, True)]
            if depth != "0":
                pref = real.rstrip("/") + "/"
                hpref = p.rstrip("/") + "/"
                for d in sorted(self.dirs | set(self.links)):
                    if d.startswith(pref) and "/" not in d[len(pref):]:
                        parts.append(self._entry(hpref + d[len(pref):], d, True))
                for f in sorted(self.files):
                    if f.startswith(pref) and "/" not in f[len(pref):]:
                        parts.append(self._entry(hpref + f[len(pref):], f, False))
                for raw_href, is_dir in self.extra_entries.get(p, []):
                    props = ("<d:resourcetype><d:collection/></d:resourcetype>" if is_dir
                             else "<d:resourcetype/><d:getcontentlength>1</d:getcontentlength>")
                    parts.append(self._raw_entry(raw_href, props))
        elif real in self.files:
            parts = [self._entry(p, real, False)]
        else:
            return self._send(404)
        xml = '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">%s</d:multistatus>' % "".join(parts)
        self._send(207, xml.encode(), {"Content-Type": "application/xml"})


def start(auth=None):
    h = type("H", (Handler,), {
        "files": {}, "dirs": set(), "auth": auth, "seen_auth": [], "mtimes": {}, "etags": {},
        "log": [], "fail_propfind": set(), "extra_entries": {}, "raw_propfind": {}, "links": {},
        "redirects": {}, "put_hooks": [], "move_hooks": [], "req_headers": [], "counter": itertools.count(1), "now": staticmethod(time.time)})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), h)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, h, "http://127.0.0.1:%d/dav" % srv.server_address[1]
