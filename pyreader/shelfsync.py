"""阶段 3：云端书库并入书架。设计见 c:\\tmp\\pyreader-bookshelf-design.md §3、§4.3 步骤 1、§4.8、§4.9、§6.3。

分三层，前两层是纯逻辑（不碰 Qt，用进程内 fake WebDAV 即可测试）：
  - fetch_library(client, prev_sizes, now)   列 <base>/library，名字校验 + 稳定性过滤（上传中的半截文件）
  - CloudCatalog                             云端已移除的判断：只信「完整」列表，且连续两轮完整列表都缺失
  - ShelfSyncWorker                          后台线程 + Signal：单飞的列表刷新、带 token 的下载

线程规则：所有网络都在后台线程；结果只经 Signal 回主线程；任何网络失败都不会抛进 UI（只发 Signal / 记日志）。
本阶段不碰覆盖层和设备文件（阶段 4 / 5），也不做任何上传。
"""
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Set

from PySide6.QtCore import QObject, Signal

from . import shelf as sh
from .config import _log_net
from .sync import MAX_BOOK_BYTES, download_book
from .webdav import MAX_FILES, WebDavClient, WebDavError, client_from_cfg

LIBRARY = "library"
STABLE_SECONDS = 60          # mtime 距今不足这个秒数的文件视为「还在上传」
CLOCK_SLACK = 300            # mtime 比本机时钟超前超过这个秒数：当作服务器时钟偏差，忽略 mtime 只看大小变化
REMOVED_ROUNDS = 2           # 连续这么多轮「完整」列表都缺失才算云端已移除
MB = 1024 * 1024


# ================================================================== 列表
@dataclass
class Listing:
    files: List[sh.RemoteFile] = field(default_factory=list)    # 通过校验的全部云端书（含上传中的）
    unstable: Set[str] = field(default_factory=set)             # 上传中的 rel（不显示、不算指纹）
    complete: bool = False       # 无子树错误、未触到上限、library 目录存在：只有完整列表才能用来判断「消失」
    truncated: bool = False      # 触到 文件数 / 目录数 / 深度 上限
    errors: List[str] = field(default_factory=list)             # 读取失败的子目录
    skipped: int = 0             # 被名字过滤丢掉的条目数
    root_missing: bool = False   # 服务器上没有 library/ 目录
    fetched_at: float = 0.0

    @property
    def stable_files(self) -> List[sh.RemoteFile]:
        return [f for f in self.files if f.rel not in self.unstable]

    def problem(self) -> str:
        """列表不完整时给用户看的一句话（完整则为空）。"""
        if self.complete:
            return ""
        if self.root_missing:
            return "云端没有 library 文件夹"
        parts = []
        if self.truncated:
            parts.append(f"书库超过读取上限（{MAX_FILES} 本），只显示了一部分")
        if self.errors:
            parts.append(f"{len(self.errors)} 个文件夹读取失败")
        return "；".join(parts) or "云端列表不完整"


def _is_unstable(rf: sh.RemoteFile, prev_sizes: Optional[Dict[str, int]], now: float) -> bool:
    """半截上传判断（design §4.3 步骤 1）：mtime 距今 < 60s，或大小与上一轮列表不同。"""
    if rf.mtime and rf.mtime > 0:
        age = now - rf.mtime
        if -CLOCK_SLACK < age < STABLE_SECONDS:
            return True
    prev = (prev_sizes or {}).get(rf.rel)
    return prev is not None and prev != rf.size


def fetch_library(client: WebDavClient, prev_sizes: Optional[Dict[str, int]] = None,
                  now: Optional[float] = None, max_files: int = MAX_FILES) -> Listing:
    """列 <base>/library。阻塞调用，只能在后台线程用。网络 / 协议错误抛 WebDavError（调用方负责接住）；
    library 目录不存在（404）不抛，返回 root_missing 的不完整列表。

    名字过滤：BOOK_EXTS、点 / ~$ 开头、rel 校验（valid_remote_rel）。稳定性：见 _is_unstable。"""
    now = time.time() if now is None else now
    try:
        tree = client.list_tree_ex(LIBRARY, max_files=max_files)
    except WebDavError as e:
        if e.status == 404:
            return Listing(complete=False, root_missing=True, fetched_at=now)
        raise
    out: List[sh.RemoteFile] = []
    seen = set()
    skipped = tree.skipped
    for rel, ent in tree.files:
        if not sh.valid_remote_rel(rel) or rel in seen:
            skipped += 1
            continue
        seen.add(rel)
        out.append(sh.RemoteFile(rel, int(ent.size or 0), float(ent.mtime or 0.0)))
    out.sort(key=lambda f: f.rel)
    unstable = {f.rel for f in out if _is_unstable(f, prev_sizes, now)}
    return Listing(out, unstable, bool(tree.complete), bool(tree.truncated), list(tree.errors), skipped, False, now)


# ================================================================== 云端目录：已移除的判断
def _bid(rel: str) -> str:
    return "remote:" + rel


class CloudCatalog:
    """记住「云端有哪些书」，并按设计判断「云端已移除」。纯逻辑。

    known    最近一次列表里（含上传中的）见过的书 {book_id: RemoteFile}；冷启动时由 index.json 缓存灌入
    shown    当前应当作为「云端书」显示的（稳定的、或还不能下结论的）{book_id: RemoteFile}
    missing  连续缺失的完整列表轮数 {book_id: {"n", "size", "mtime"}}；n >= REMOVED_ROUNDS 即「云端已移除」

    规则：
      - 不完整的列表（子树失败 / 截断 / library 不存在）不产生任何「消失」结论：计数不动，书继续显示。
      - 只在完整列表里缺失才计数；再次出现（含 rclone 式「先复制、再删旧」的旧路径之外的新路径）不影响别的书。
      - 计数在书重新出现时清零；n >= 2 才标记，单轮缺失（包括单轮子树失败）永远不会标记。
    """

    def __init__(self, known: Optional[Dict[str, sh.RemoteFile]] = None, missing: Optional[dict] = None):
        self.known: Dict[str, sh.RemoteFile] = dict(known or {})
        self.missing: Dict[str, dict] = {k: dict(v) for k, v in (missing or {}).items()
                                         if isinstance(v, dict) and isinstance(v.get("n"), int)}
        for bid in self.missing:                     # 已在 missing 里的不再算「已知存在」
            self.known.pop(bid, None)
        self.shown: Dict[str, sh.RemoteFile] = dict(self.known)

    @staticmethod
    def _ghost(bid: str, meta: dict) -> sh.RemoteFile:
        return sh.RemoteFile(bid[7:], int(meta.get("size", 0) or 0), float(meta.get("mtime", 0.0) or 0.0))

    def apply(self, listing: Listing, extra: Optional[Dict[str, sh.RemoteFile]] = None) -> None:
        """吸收一轮列表。extra：调用方额外知道的、本机有缓存副本的 remote: 书（索引丢失时也能判断它们的移除）。"""
        prev_shown = set(self.shown)
        listed = {_bid(f.rel): f for f in listing.files}
        cands: Dict[str, sh.RemoteFile] = dict(self.known)
        for bid, meta in self.missing.items():
            cands.setdefault(bid, self._ghost(bid, meta))
        for bid, rf in (extra or {}).items():
            cands.setdefault(bid, rf)
        new_missing: Dict[str, dict] = {}
        carried: Dict[str, sh.RemoteFile] = {}
        for bid, rf in cands.items():
            if bid in listed:
                continue                             # 出现了（含上传中）：计数清零
            n = self.missing.get(bid, {}).get("n", 0)
            if listing.complete:
                n += 1
            elif n == 0:
                carried[bid] = rf                    # 不完整列表里没看到：不下结论，仍当作存在
            if n > 0:
                new_missing[bid] = {"n": min(n, 99), "size": rf.size, "mtime": rf.mtime}
        self.missing = new_missing
        self.known = dict(listed)
        self.known.update(carried)
        self.shown = {bid: f for bid, f in listed.items() if f.rel not in listing.unstable}
        for bid, rf in carried.items():
            if bid in prev_shown:
                self.shown[bid] = rf

    def display_files(self) -> List[sh.RemoteFile]:
        """build_books 的 remote_files：稳定的云端书 + 还不能下结论的「缺失过一轮」的书。"""
        out = dict(self.shown)
        for bid, meta in self.missing.items():
            if meta["n"] < REMOVED_ROUNDS:
                out.setdefault(bid, self._ghost(bid, meta))
        return sorted(out.values(), key=lambda f: f.rel)

    def removed_ids(self) -> Set[str]:
        return {bid for bid, meta in self.missing.items() if meta["n"] >= REMOVED_ROUNDS}

    def removed_files(self) -> List[sh.RemoteFile]:
        return [self._ghost(bid, meta) for bid, meta in sorted(self.missing.items()) if meta["n"] >= REMOVED_ROUNDS]

    def to_state(self) -> dict:
        """写进 state.json 的 cloud 段。"""
        return {"missing": {bid: dict(m) for bid, m in self.missing.items()}}


def known_from_books(books: Iterable[sh.Book]) -> Dict[str, sh.RemoteFile]:
    """冷启动：从 index.json 还原出的书里取云端书目（where 为 cloud / synced）作为「已知存在」。"""
    out = {}
    for b in books:
        if b.remote_rel and b.id == _bid(b.remote_rel) and b.where in ("cloud", "synced"):
            out[b.id] = sh.RemoteFile(b.remote_rel, b.size, b.mtime)
    return out


def extra_candidates(books: Iterable[sh.Book]) -> Dict[str, sh.RemoteFile]:
    """本机有缓存副本的 remote: 书（不管索引里有没有）：它们的移除也要能判断。"""
    out = {}
    for b in books:
        if b.remote_rel and b.id == _bid(b.remote_rel) and b.where in ("synced", "removed"):
            out[b.id] = sh.RemoteFile(b.remote_rel, b.size, b.mtime)
    return out


# ================================================================== 后台线程
class ShelfSyncWorker(QObject):
    """书架的云端后台任务。refresh / download 都立刻返回（线程句柄仅供测试 join），结果经 Signal 回主线程。
    所有异常只记日志并发失败信号，绝不外抛。"""
    listed = Signal(object)              # Listing
    listFailed = Signal(str)             # 列表读取失败（离线 / 认证 / 服务器错误）：调用方保留本地缓存
    downloadedFor = Signal(object, str)  # (token, 本地路径)：与永久接到 load_book 的 SyncWorker.downloaded 无关
    downloadFailed = Signal(object, str)  # (token, 提示)
    status = Signal(str)

    def __init__(self, parent=None, client_factory: Callable[[dict], Optional[WebDavClient]] = client_from_cfg):
        super().__init__(parent)
        self._factory = client_factory
        self._lock = threading.Lock()
        self._running = False
        self._rerun = False
        self._cfg: Optional[dict] = None
        self._prev_sizes: Dict[str, int] = {}
        self._downloads: Dict[str, List[object]] = {}      # rel -> 等这次下载的 token 们（同一文件只下载一次）
        self.generation = 0                                # 每完成一轮列表 +1（测试用）

    # ------------------------------------------------------------ 列表（单飞：同一时间只有一个，后到者合并为「再跑一次」）
    def refresh(self, cfg: dict) -> Optional[threading.Thread]:
        """未启用 WebDAV（工厂返回 None）时是空操作，返回 None。已有一轮在跑则登记「再跑一次」，也返回 None。"""
        if self._factory(cfg) is None:
            return None
        with self._lock:
            self._cfg = cfg
            if self._running:
                self._rerun = True
                return None
            self._running = True
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()
        return t

    def _loop(self):
        while True:
            with self._lock:
                cfg = self._cfg
            try:
                client = self._factory(cfg)
                if client is None:
                    raise WebDavError("WebDAV 未启用")
                lst = fetch_library(client, self._prev_sizes)
                self._prev_sizes = {f.rel: f.size for f in lst.files}
                self.generation += 1
                self.listed.emit(lst)
            except Exception as e:                   # 离线 / 401 / 5xx / 解析错误：只报告，绝不抛
                _log_net(f"[书架] 读取云端书库失败: {e}")
                try:
                    self.listFailed.emit(str(e)[:200])
                except Exception:
                    pass
            with self._lock:
                if self._rerun:
                    self._rerun = False
                    continue
                self._running = False
                return

    # ------------------------------------------------------------ 下载（与打开解耦，带 token）
    def download(self, cfg: dict, rel: str, size: int, open_when_done: bool, token) -> Optional[threading.Thread]:
        """下载 library/<rel> 到 remote_books/<rel>。完成经 downloadedFor(token, path)，失败经 downloadFailed(token, msg)。
        open_when_done 只影响状态提示文字：「要不要打开」由调用方按 token 是否仍然有效决定（迟到的完成不会劫持窗口）。
        同一个 rel 已在下载时，新的 token 并入同一次下载（返回 None）。未启用 WebDAV 返回 None。"""
        client = self._factory(cfg)
        if client is None:
            return None
        try:
            mb = int(cfg.get("webdav_max_book_mb") or 0)
        except (TypeError, ValueError):
            mb = 0
        max_bytes = mb * MB if mb > 0 else MAX_BOOK_BYTES
        with self._lock:
            waiters = self._downloads.get(rel)
            if waiters is not None:
                waiters.append(token)
                return None
            self._downloads[rel] = [token]

        def run():
            path, err = "", ""
            try:
                path = download_book(client, rel, size, max_bytes=max_bytes)
            except Exception as e:
                _log_net(f"[书架] 下载失败 {rel}: {e}")
                err = f"下载失败：{rel.rsplit('/', 1)[-1]}（{str(e)[:120]}）"
            with self._lock:
                tokens = self._downloads.pop(rel, [])
            name = rel.rsplit("/", 1)[-1]
            for tok in tokens:
                try:
                    if err:
                        self.downloadFailed.emit(tok, err)
                    else:
                        self.downloadedFor.emit(tok, path)
                except Exception:
                    pass
            try:
                self.status.emit(err or (f"已下载，正在打开：{name}" if open_when_done else f"已下载：{name}"))
            except Exception:
                pass
        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t
