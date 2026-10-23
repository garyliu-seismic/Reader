"""阅读进度 WebDAV 同步（纯逻辑 + 后台 worker）。

远端布局： <base>/users/<sync_user_name>/progress/<book_id>.json
          <base>/library/<相对路径>
只同步进度；config.json（含密钥）与 bookmarks.json 永不上传。"""
import hashlib
import json
import os
import re
import socket
import threading
import time
from typing import Optional

from PySide6.QtCore import QObject, Signal

from .config import APP_DIR, _log_net
from .webdav import WebDavClient, WebDavError, client_from_cfg

REMOTE_BOOKS_DIR = os.path.join(APP_DIR, "remote_books")


def default_device_name() -> str:
    try:
        return socket.gethostname() or "device"
    except Exception:
        return "device"


def _remote_rel(path: str) -> Optional[str]:
    """若 path 位于 remote_books 下，返回相对路径（/ 分隔），否则 None。"""
    try:
        rel = os.path.relpath(os.path.abspath(path), os.path.abspath(REMOTE_BOOKS_DIR))
    except ValueError:
        return None
    if rel.startswith(".."):
        return None
    return rel.replace(os.sep, "/")


def book_id(path: str) -> str:
    """远程书库的书 → 远端相对路径；本地书 → sha1(文件名 + 大小)（与本机路径无关）。"""
    rel = _remote_rel(path)
    if rel:
        return "remote:" + rel
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    h = hashlib.sha1(f"{os.path.basename(path)}{size}".encode("utf-8")).hexdigest()
    return "local:" + h


def _safe_name(bid: str) -> str:
    return re.sub(r"[^0-9A-Za-z._-]", "_", bid)[:180]


def progress_path(user: str, bid: str) -> str:
    return f"users/{user or 'default'}/progress/{_safe_name(bid)}.json"


def make_record(bid, offset, percent, chapter, device, updated=None) -> dict:
    return {"book_id": bid, "offset": int(offset), "percent": float(percent),
            "chapter": chapter, "updated": float(updated if updated is not None else time.time()),
            "device": device}


def merge_progress(local: Optional[dict], remote: Optional[dict]) -> Optional[dict]:
    """较新的 updated 获胜；相等时偏向本地。"""
    if not remote:
        return local
    if not local:
        return remote
    return remote if float(remote.get("updated", 0)) > float(local.get("updated", 0)) else local


def push_progress(client: WebDavClient, user: str, rec: dict):
    client.mkdirs(f"users/{user or 'default'}/progress")
    client.put(progress_path(user, rec["book_id"]), json.dumps(rec, ensure_ascii=False).encode("utf-8"))


def pull_progress(client: WebDavClient, user: str, bid: str) -> Optional[dict]:
    p = progress_path(user, bid)
    if not client.exists(p):
        return None
    try:
        d = json.loads(client.get(p).decode("utf-8"))
        return d if isinstance(d, dict) and "offset" in d else None
    except (ValueError, UnicodeDecodeError):
        return None


def resolve_offset(rec: dict, text_len: int) -> int:
    """优先用 offset；文件/解析器不同导致越界时退回按 percent 换算。"""
    off = int(rec.get("offset", 0))
    if 0 <= off < max(1, text_len):
        return off
    return int(max(0.0, min(1.0, float(rec.get("percent", 0)) / 100.0)) * max(0, text_len - 1))


class SyncWorker(QObject):
    """后台线程执行同步任务；结果经 Signal 回主线程。所有异常只记日志，不外抛。"""
    pulled = Signal(str, object)    # (book_path, remote_record_or_None)
    status = Signal(str)
    downloaded = Signal(str)        # 本地路径
    remoteList = Signal(object)     # [(rel, size)]

    @staticmethod
    def _spawn(fn):
        t = threading.Thread(target=fn, daemon=True)
        t.start()
        return t

    def pull(self, cfg: dict, book_path: str):
        c = client_from_cfg(cfg)
        if not c:
            return None
        user, bid = cfg.get("sync_user_name") or "default", book_id(book_path)

        def run():
            try:
                self.pulled.emit(book_path, pull_progress(c, user, bid))
            except Exception as e:
                _log_net(f"[同步] 拉取进度失败: {e}")
                self.status.emit("同步进度失败（已忽略）")
        return self._spawn(run)

    def push(self, cfg: dict, rec: dict):
        c = client_from_cfg(cfg)
        if not c:
            return None
        user = cfg.get("sync_user_name") or "default"

        def run():
            try:
                push_progress(c, user, rec)
            except Exception as e:
                _log_net(f"[同步] 上传进度失败: {e}")
                self.status.emit("同步进度失败（已忽略）")
        return self._spawn(run)

    def list_library(self, cfg: dict, book_exts):
        c = client_from_cfg(cfg)
        if not c:
            return None

        def run():
            try:
                files = [(r, e.size) for r, e in c.list_tree("library")
                         if r.lower().endswith(tuple(book_exts))
                         and not os.path.basename(r).startswith((".", "~$"))]
                files.sort()
                self.remoteList.emit(files)
            except Exception as e:
                _log_net(f"[同步] 列远程书库失败: {e}")
                self.status.emit("读取远程书库失败")
        return self._spawn(run)

    def download(self, cfg: dict, rel: str, size: int):
        c = client_from_cfg(cfg)
        if not c:
            return None

        def run():
            try:
                root = os.path.abspath(REMOTE_BOOKS_DIR)
                dest = os.path.abspath(os.path.join(root, *rel.split("/")))
                if not dest.startswith(root + os.sep):
                    raise WebDavError("illegal path")
                if not (os.path.isfile(dest) and os.path.getsize(dest) == size):
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    data = c.get("library/" + rel)
                    tmp = dest + ".part"
                    with open(tmp, "wb") as f:
                        f.write(data)
                    os.replace(tmp, dest)
                self.downloaded.emit(dest)
            except Exception as e:
                _log_net(f"[同步] 下载失败 {rel}: {e}")
                self.status.emit("下载远程书失败")
        return self._spawn(run)

    def upload_book(self, cfg: dict, path: str):
        c = client_from_cfg(cfg)
        if not c:
            return None

        def run():
            try:
                with open(path, "rb") as f:
                    data = f.read()
                c.mkdirs("library")
                c.put("library/" + os.path.basename(path), data)
                self.status.emit(f"已上传到远程书库：{os.path.basename(path)}")
            except Exception as e:
                _log_net(f"[同步] 上传书失败: {e}")
                self.status.emit("上传失败")
        return self._spawn(run)
