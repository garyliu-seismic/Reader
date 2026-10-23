"""极简 WebDAV 客户端：只用标准库 + config.net_urlopen（沿用 Reader 的代理策略）。

用户名/密码都为空时不发送 Authorization 头（匿名 WebDAV，如 rclone serve webdav 默认）。"""
import base64
import email.utils
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import List, Optional

from .config import net_urlopen

_DAV = "{DAV:}"
_PROPFIND_BODY = (b'<?xml version="1.0" encoding="utf-8"?>'
                  b'<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/>'
                  b'<d:getcontentlength/><d:getlastmodified/></d:prop></d:propfind>')


class WebDavError(Exception):
    pass


@dataclass
class DavEntry:
    name: str
    is_dir: bool
    size: int = 0
    mtime: float = 0.0


class WebDavClient:
    def __init__(self, base_url: str, user: str = "", password: str = "", timeout: float = 20):
        self.base = (base_url or "").strip().rstrip("/")
        self.user = user or ""
        self.password = password or ""
        self.timeout = timeout

    # ---- 内部 ----
    def _url(self, path: str = "") -> str:
        parts = [p for p in (path or "").replace("\\", "/").split("/") if p]
        quoted = "/".join(urllib.parse.quote(p, safe="") for p in parts)
        return self.base + ("/" + quoted if quoted else "")

    def _headers(self, extra=None) -> dict:
        h = dict(extra or {})
        if self.user or self.password:
            tok = base64.b64encode(f"{self.user}:{self.password}".encode("utf-8")).decode("ascii")
            h["Authorization"] = "Basic " + tok
        return h

    def _request(self, method, path, data=None, headers=None, ok_codes=None):
        """返回 (status, body_bytes)。HTTP 错误码在 ok_codes 内则不抛。"""
        req = urllib.request.Request(self._url(path), data=data, method=method,
                                     headers=self._headers(headers))
        try:
            with net_urlopen(req, timeout=self.timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            if ok_codes and e.code in ok_codes:
                try:
                    body = e.read()
                except Exception:
                    body = b""
                return e.code, body
            raise WebDavError(f"{method} {path or '/'} -> HTTP {e.code}") from e
        except Exception as e:
            raise WebDavError(f"{method} {path or '/'} failed: {e}") from e

    # ---- 操作 ----
    def propfind(self, path: str = "", depth: int = 1) -> List[DavEntry]:
        _st, body = self._request("PROPFIND", path, data=_PROPFIND_BODY,
                                  headers={"Depth": str(depth),
                                           "Content-Type": "application/xml; charset=utf-8"})
        try:
            root = ET.fromstring(body)
        except ET.ParseError as e:
            raise WebDavError(f"bad multistatus XML: {e}") from e
        base_path = urllib.parse.unquote(urllib.parse.urlparse(self._url(path)).path).rstrip("/")
        out = []
        for resp in root.iter(_DAV + "response"):
            href = resp.findtext(_DAV + "href") or ""
            hp = urllib.parse.unquote(urllib.parse.urlparse(href).path)
            is_dir = False
            size = 0
            mtime = 0.0
            for ps in resp.findall(_DAV + "propstat"):
                st = ps.findtext(_DAV + "status") or "HTTP/1.1 200 OK"
                if " 200" not in st:
                    continue
                prop = ps.find(_DAV + "prop")
                if prop is None:
                    continue
                rt = prop.find(_DAV + "resourcetype")
                if rt is not None and rt.find(_DAV + "collection") is not None:
                    is_dir = True
                cl = (prop.findtext(_DAV + "getcontentlength") or "").strip()
                if cl.isdigit():
                    size = int(cl)
                lm = (prop.findtext(_DAV + "getlastmodified") or "").strip()
                if lm:
                    try:
                        mtime = email.utils.parsedate_to_datetime(lm).timestamp()
                    except Exception:
                        pass
            if hp.rstrip("/") == base_path:
                continue  # 自身
            name = hp.rstrip("/").rsplit("/", 1)[-1]
            if name:
                out.append(DavEntry(name, is_dir, size, mtime))
        return out

    def get(self, path: str) -> bytes:
        return self._request("GET", path)[1]

    def put(self, path: str, data: bytes):
        self._request("PUT", path, data=data,
                      headers={"Content-Type": "application/octet-stream"})

    def mkcol(self, path: str) -> bool:
        """创建单层目录；已存在（405）视为成功（返回 False 表示原本就在）。"""
        st, _ = self._request("MKCOL", path, ok_codes=(405,))
        return st != 405

    def mkdirs(self, path: str):
        """mkdir -p。"""
        cur = ""
        for p in [x for x in path.replace("\\", "/").split("/") if x]:
            cur = f"{cur}/{p}" if cur else p
            self.mkcol(cur)

    def exists(self, path: str = "") -> bool:
        st, _ = self._request("PROPFIND", path, data=_PROPFIND_BODY,
                              headers={"Depth": "0", "Content-Type": "application/xml"},
                              ok_codes=(404,))
        return st != 404

    def list_tree(self, path: str = "", max_files: int = 2000) -> List[tuple]:
        """递归列出文件，返回 [(相对路径, DavEntry)]；根不存在则抛 WebDavError。"""
        res = []
        stack = [""]
        while stack and len(res) < max_files:
            rel = stack.pop()
            try:
                entries = self.propfind("/".join(x for x in (path, rel) if x), 1)
            except WebDavError:
                if rel == "":
                    raise
                continue
            for e in entries:
                r = f"{rel}/{e.name}" if rel else e.name
                if e.is_dir:
                    stack.append(r)
                else:
                    res.append((r, e))
        return res


def client_from_cfg(cfg: dict) -> Optional[WebDavClient]:
    """未启用或 url 为空 → None（调用方一律视为 no-op）。"""
    if not cfg.get("webdav_enabled") or not (cfg.get("webdav_url") or "").strip():
        return None
    return WebDavClient(cfg["webdav_url"], cfg.get("webdav_user", ""), cfg.get("webdav_password", ""))
