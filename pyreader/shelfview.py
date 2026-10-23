"""阶段 2 / 3：书架界面（阶段 3 起云端书库并入：后台列 library/、角标、带 token 的下载后打开）。设计见 c:\\tmp\\pyreader-bookshelf-design.md §6、§7 阶段 2。

结构：
  - ScanWorker  后台线程：scan_local + build_books（纯逻辑，不碰 Qt 对象），结果经 Signal 回主线程
  - BookModel   QAbstractTableModel：网格视图用第 0 列，列表视图用全部列
  - BookDelegate 网格卡片（色块封面 + 书名 + 进度）
  - ShelfView   侧栏（智能书架 + 本机文件夹）、搜索 / 排序 / 视图切换、状态条、index.json 缓存

线程规则：Qt 对象只在主线程创建 / 访问；扫描线程只拿到快照（路径列表、书签快照、进度 / 打开记录副本）。
所有来自磁盘的字符串都按纯文本绘制（QLabel 一律 PlainText；delegate 用 drawText）。
"""
import html
import os
import threading
import time
from typing import Callable, Dict, List, Optional, Set

from PySide6.QtCore import (QAbstractTableModel, QModelIndex, QObject, QRect, QSize, Qt, QTimer, QUrl, Signal)
from PySide6.QtGui import QColor, QDesktopServices, QFont, QFontMetrics, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QListView, QListWidget, QListWidgetItem, QMenu, QPushButton, QStackedWidget,
                               QStyle, QStyledItemDelegate, QToolButton, QTreeView, QVBoxLayout, QWidget)

from . import shelf as sh
from . import shelfsync as ss
from .webdav import client_from_cfg

SMART = (("all", "全部"), ("recent", "最近阅读"), ("reading", "在读"), ("done", "已读完"))   # 阶段 2 的智能书架
SORT_LABELS = (("last_read", "最近阅读"), ("title", "书名"), ("added", "最近添加"), ("progress", "进度"), ("size", "大小"))
COLUMNS = ("书名", "格式", "进度", "最近阅读", "大小", "位置")
CLOUD_SMART = (("uncategorized", "未分类"), ("removed", "云端已移除"))     # 有书时才出现在侧栏
CLOUD_TIMER_MS = 10 * 60 * 1000      # 书架可见期间每 10 分钟刷新一次云端列表
CLOUD_STALE_SECONDS = 60             # 打开书架视图时，距上次云端刷新不足这个秒数则不重复
BADGES = {"cloud": "仅云端", "synced": "已下载", "local": "仅本地", "removed": "云端已移除"}
BADGE_COLORS = {"cloud": "#2f7fd8", "synced": "#2e9a5b", "local": "#6b7280", "removed": "#d9822b"}
CARD_W, CARD_H = 144, 240
COVER_W, COVER_H = 116, 160


# ================================================================== 小工具（纯函数，便于测试）
def fmt_size(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError, OverflowError):
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def fmt_ago(ts: float, now: Optional[float] = None) -> str:
    """最近阅读时间的口语化显示。小于 1e9 的是 recent 菜单的排名种子（不是真实时间）→「最近」。"""
    if not ts or ts <= 0:
        return ""
    if ts < 1e9:
        return "最近"
    d = (now if now is not None else time.time()) - ts
    if d < 0:
        return "刚刚"
    if d < 60:
        return "刚刚"
    if d < 3600:
        return f"{int(d // 60)} 分钟前"
    if d < 86400:
        return f"{int(d // 3600)} 小时前"
    if d < 86400 * 30:
        return f"{int(d // 86400)} 天前"
    try:
        return time.strftime("%Y-%m-%d", time.localtime(ts))
    except (OverflowError, OSError, ValueError):
        return ""


def progress_text(b: sh.Book) -> str:
    """进度列 / 卡片上的进度文字：已知百分比 → 'NN%'（读完 → 已读完）；只知道有本地进度偏移 → 在读。"""
    if b.percent is not None:
        if b.status == "done":
            return "已读完"
        return f"{b.percent:.0f}%"
    return "在读" if b.has_progress else ""


def plain_tip(text: str) -> str:
    """工具提示按纯文本显示：Qt 的 ToolTip 会自动识别富文本，来自磁盘 / 服务器的名字不能被当成 HTML 解释。"""
    return "<p style='white-space:pre-wrap'>" + html.escape(str(text), quote=False) + "</p>"


def badge_text(b: sh.Book, busy: bool = False) -> str:
    """卡片 / 列表「位置」列的角标文字（design §4.9）：下载中 > 仅云端 / 已下载 / 仅本地 / 云端已移除；占位卡片没有。"""
    if busy:
        return "下载中…"
    return BADGES.get(b.where, "")


def count_shelves(books) -> Dict[str, int]:
    """一次遍历得到侧栏所有计数：智能书架 + 每个文件夹书架（与 filter_books 的可见性规则一致）。
    removed（云端已移除）含默认隐藏的；uncategorized 是云端根目录的书。"""
    out: Dict[str, int] = {"all": 0, "recent": 0, "reading": 0, "done": 0, "removed": 0, "uncategorized": 0}
    for b in (books.values() if isinstance(books, dict) else books):
        if b.removed and not b.hidden:
            out["removed"] += 1
        if b.hidden or b.auto_hidden:
            continue
        out["all"] += 1
        if b.shelf == sh.CLOUD:
            out["uncategorized"] += 1
        if b.last_read > 0:
            out["recent"] += 1
        st = b.status
        if st == "reading":
            out["reading"] += 1
        elif st == "done":
            out["done"] += 1
        out[b.shelf] = out.get(b.shelf, 0) + 1
    return out


class ScanResult:
    def __init__(self, books: Dict[str, sh.Book], truncated: bool, nfiles: int, files=None, scanned: bool = True):
        self.books, self.truncated, self.nfiles = books, truncated, nfiles
        self.files, self.scanned = files, scanned       # scanned=False：只用上一轮的本地文件重建（云端列表变了）


# ================================================================== 后台扫描
class ScanWorker(QObject):
    """后台线程里跑 scan_local + build_books。只处理传入的快照；结果经 Signal 回主线程（QueuedConnection）。"""
    done = Signal(int, object)        # (token, ScanResult)
    failed = Signal(int, str)

    def __init__(self, token: int, roots: List[str], bookmarks: dict, recent: List[str], progress: dict, opened: dict,
                 remote_files=None, removed=(), removed_files=(), files=None):
        """remote_files 为 None = 云端未启用 / 列表未知。files 不为 None 时复用上一轮的本地文件（不再遍历磁盘）。"""
        super().__init__()
        self.token = token
        self.roots, self.bookmarks, self.recent = roots, bookmarks, recent
        self.progress, self.opened = progress, opened
        self.remote_files, self.removed, self.removed_files = remote_files, set(removed or ()), list(removed_files or ())
        self.files = files
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        try:
            scanned = self.files is None
            files = sh.scan_local(self.roots, should_stop=self._stop.is_set) if scanned else self.files
            if self._stop.is_set():
                return
            books = sh.build_books(files, self.remote_files, sh.progress_overlay(self.progress), bookmarks=self.bookmarks,
                                   recent=self.recent, opened=self.opened, removed=self.removed,
                                   removed_files=self.removed_files)
            if self._stop.is_set():
                return
            self.done.emit(self.token, ScanResult(books, bool(getattr(files, "truncated", False)), len(files),
                                                  files, scanned))
        except Exception as e:      # 扫描失败不能抛进 UI
            self.failed.emit(self.token, str(e))


# ================================================================== 模型 / 委托
class BookModel(QAbstractTableModel):
    BookRole = Qt.ItemDataRole.UserRole + 1

    def __init__(self, parent=None):
        super().__init__(parent)
        self._books: List[sh.Book] = []
        self.busy: Set[str] = set()          # 正在下载的 book_id
        self.show_badges = False             # 云端未启用时没有任何云相关角标

    def set_busy(self, busy):
        self.busy = set(busy)
        if self._books:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self._books) - 1, len(COLUMNS) - 1))

    def badge(self, b: sh.Book) -> str:
        return badge_text(b, b.id in self.busy) if self.show_badges else ""

    def set_books(self, books: List[sh.Book]):
        self.beginResetModel()
        self._books = list(books)
        self.endResetModel()

    def books(self) -> List[sh.Book]:
        return list(self._books)

    def book_at(self, row: int) -> Optional[sh.Book]:
        return self._books[row] if 0 <= row < len(self._books) else None

    def row_of(self, bid: str) -> int:
        for i, b in enumerate(self._books):
            if b.id == bid:
                return i
        return -1

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._books)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole and 0 <= section < len(COLUMNS):
            return COLUMNS[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._books)):
            return None
        b = self._books[index.row()]
        if role == self.BookRole:
            return b
        c = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            if c == 0:
                return b.title
            if c == 1:
                return b.fmt.upper()
            if c == 2:
                return progress_text(b)
            if c == 3:
                return fmt_ago(b.last_read)
            if c == 4:
                return fmt_size(b.size)
            if c == 5:
                return self.badge(b)
        elif role == Qt.ItemDataRole.ToolTipRole:
            if b.local_paths:
                return plain_tip(b.local_paths[0])
            return plain_tip(("云端：" + b.remote_rel) if b.remote_rel else b.title)
        elif role == Qt.ItemDataRole.TextAlignmentRole and c in (2, 4):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None


class BookDelegate(QStyledItemDelegate):
    """网格卡片：fp 哈希取色的色块封面 + 大字 + 进度条；封面下是书名（最多两行）和进度文字。"""

    def sizeHint(self, option, index):
        return QSize(CARD_W, CARD_H)

    def paint(self, p: QPainter, option, index):
        b = index.data(BookModel.BookRole)
        if not isinstance(b, sh.Book):
            return
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if b.removed:
            p.setOpacity(0.55)                            # 云端已移除：灰显
        r = option.rect
        if option.state & QStyle.StateFlag.State_Selected:
            p.fillRect(r.adjusted(2, 2, -2, -2), option.palette.highlight())
            txt = option.palette.highlightedText().color()
        else:
            txt = option.palette.text().color()
        cover = QRect(r.left() + (r.width() - COVER_W) // 2, r.top() + 8, COVER_W, COVER_H)
        h, s, l = sh.cover_hsl(b.fp, b.title)
        col = QColor.fromHsl(h, s, l)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(col)
        p.drawRoundedRect(cover, 6, 6)
        p.setBrush(col.lighter(125))                      # 书脊
        p.drawRoundedRect(QRect(cover.left(), cover.top(), 8, cover.height()), 3, 3)
        glyph = sh.cover_glyph(b.title)
        f = QFont(p.font())
        f.setBold(True)
        f.setPixelSize(40 if len(glyph) <= 2 else 28)
        p.setFont(f)
        p.setPen(QColor("#ffffff"))
        p.drawText(cover.adjusted(12, 6, -6, -6), int(Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWrapAnywhere), glyph)
        m = index.model()
        bt = m.badge(b) if hasattr(m, "badge") else ""
        if bt:                                            # 角标（纯文字小药丸，drawText 按纯文本绘制）
            bf = QFont(option.font)
            bf.setBold(False)
            bf.setPixelSize(11)
            bfm = QFontMetrics(bf)
            pill = QRect(0, cover.top() + 4, bfm.horizontalAdvance(bt) + 10, bfm.height() + 2)
            pill.moveRight(cover.right() - 4)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#8b5cf6" if bt.startswith("下载中") else BADGE_COLORS.get(b.where, "#6b7280")))
            p.drawRoundedRect(pill, 4, 4)
            p.setFont(bf)
            p.setPen(QColor("#ffffff"))
            p.drawText(pill, int(Qt.AlignmentFlag.AlignCenter), bt)
        if b.percent is not None:                         # 封面底部的进度条
            track = QRect(cover.left() + 12, cover.bottom() - 10, cover.width() - 20, 4)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 90))
            p.drawRoundedRect(track, 2, 2)
            p.setBrush(QColor("#ffffff"))
            w = int(track.width() * max(0.0, min(100.0, b.percent)) / 100.0)
            if w > 0:
                p.drawRoundedRect(QRect(track.left(), track.top(), w, track.height()), 2, 2)
        base = QFont(option.font)
        base.setPointSize(max(8, base.pointSize()))
        p.setFont(base)
        fm = QFontMetrics(base)
        p.setPen(QPen(txt))
        tr = QRect(r.left() + 6, cover.bottom() + 6, r.width() - 12, fm.height() * 2 + 2)
        p.setClipRect(tr)
        title = fm.elidedText(b.title, Qt.TextElideMode.ElideRight, tr.width() * 2 - fm.horizontalAdvance("W"))
        p.drawText(tr, int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWrapAnywhere), title)
        p.setClipping(False)
        pt = progress_text(b)
        if pt:
            small = QFont(base)
            small.setPointSize(max(7, base.pointSize() - 1))
            p.setFont(small)
            c2 = QColor(txt)
            c2.setAlpha(170)
            p.setPen(c2)
            p.drawText(QRect(r.left() + 6, tr.bottom() + 2, r.width() - 12, fm.height()),
                       int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop), pt)
        p.restore()


# ================================================================== 书架页
class ShelfView(QWidget):
    openRequested = Signal(str)          # 要打开的本地文件路径（由 MainWindow 调 load_book）
    addLibraryRequested = Signal()       # 空状态里的「添加书库目录…」
    scanFinished = Signal(int)           # 本轮扫描得到的书数（测试 / 状态用）
    cloudSynced = Signal(int)            # 一轮云端列表已并入书架（参数：云端书数）；测试 / 状态用

    STALE_SECONDS = 60                   # 书架显示时，距上次扫描超过这个秒数才重新扫描
    INDEX_SAVE_MS = 5000                 # index.json 防抖

    def __init__(self, context: Optional[Callable[[], dict]] = None, root: Optional[str] = None,
                 default_view: str = "grid", default_sort: str = "last_read", parent=None,
                 cloud: Optional[ss.ShelfSyncWorker] = None):
        """context() 在主线程调用，返回 {"library_dirs": [...], "recent": [...], "bookmarks": {...}, "cfg": {...}}
        （引用 MainWindow 的活数据；cfg 里的 webdav_* 决定云端是否启用）。
        root：书架数据根（默认 ~/.pyreader，测试传临时目录）。cloud：云端后台任务（测试可注入）。"""
        super().__init__(parent)
        self._context = context or (lambda: {})
        self._root = root
        self._books: Dict[str, sh.Book] = {}
        self._opened: Dict[str, float] = {}
        self._progress: Dict[str, dict] = {}
        self._token = 0
        self._scanner: Optional[ScanWorker] = None
        self._last_scan: Optional[float] = None       # time.monotonic()
        self._truncated = False
        self._scan_error = ""
        self._notice = ""                             # 一次性提示（如「找不到文件」），下一轮扫描完成后清除
        self._index_dirty = False
        # ---- 云端（阶段 3）
        self._cloud = cloud or ss.ShelfSyncWorker(self)
        self._cloud.listed.connect(self._on_cloud_listed, Qt.ConnectionType.QueuedConnection)
        self._cloud.listFailed.connect(self._on_cloud_failed, Qt.ConnectionType.QueuedConnection)
        self._cloud.downloadedFor.connect(self._on_downloaded_for, Qt.ConnectionType.QueuedConnection)
        self._cloud.downloadFailed.connect(self._on_download_failed, Qt.ConnectionType.QueuedConnection)
        self._cloud_busy = False                      # 列表读取中
        self._cloud_error = ""                        # 上次读取失败的原因（离线时显示本地缓存）
        self._cloud_ok_at: Optional[float] = None     # 上次成功的时间（time.time()）
        self._cloud_tick: Optional[float] = None      # 上次发起的时间（time.monotonic()，60 秒防重复）
        self._listing: Optional[ss.Listing] = None
        self._local_files = None                      # 上一轮扫描的本地文件（云端列表变化时复用，不再遍历磁盘）
        self._local_roots: List[str] = []
        self._dl: Dict[int, tuple] = {}               # token -> (book_id, rel, open_when_done)
        self._dl_seq = 0
        self._open_token: Optional[int] = None        # 只有最新的「下载后打开」请求有资格打开；其它都是迟到 / 过期

        # ---- 本机 UI 状态（state.json）：首次（无 state.json）用 config 里的默认视图 / 排序
        fresh = not os.path.exists(sh.state_path(root))
        self._state = sh.load_state(root)
        ui = self._state["ui"]
        if fresh:
            if default_view in ("grid", "list"):
                ui["view"] = default_view
            if default_sort in sh.SORT_KEYS:
                ui["sort"] = default_sort
            try:
                sh.save_state(self._state, root)      # 立刻固定下来：以后 config 默认值只在设置页改动时才再应用
            except OSError:
                pass
        self._want_shelf = ui.get("last_shelf") or "all"
        self._shelf = "all"

        # ---- index.json 缓存：先显示上次的结果，后台扫描再刷新
        self._index_readonly = sh.index_readonly(root)
        cached = sh.load_index(root)
        if cached:
            self._books, self._opened, self._progress = cached

        self._catalog = ss.CloudCatalog(ss.known_from_books(self._books.values()), self._state["cloud"]["missing"])
        self._build_ui()
        self._cloud_timer = QTimer(self)
        self._cloud_timer.setInterval(CLOUD_TIMER_MS)
        self._cloud_timer.timeout.connect(self._on_cloud_timer)
        self._cloud_timer.start()
        self._index_timer = QTimer(self)
        self._index_timer.setSingleShot(True)
        self._index_timer.setInterval(self.INDEX_SAVE_MS)
        self._index_timer.timeout.connect(self._save_index)
        self.set_view_mode(ui["view"], save=False)
        i = self.sort_box.findData(ui["sort"])
        self.sort_box.setCurrentIndex(i if i >= 0 else 0)
        self.refresh()

    # ------------------------------------------------------------ 界面
    def _build_ui(self):
        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索书名 / 作者 / 文件名  Ctrl+F")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _t: self._apply_filter())
        self.sort_box = QComboBox()
        for key, label in SORT_LABELS:
            self.sort_box.addItem(label, key)
        self.sort_box.currentIndexChanged.connect(self._on_sort_changed)
        self.btn_grid = QToolButton(); self.btn_grid.setText("▦"); self.btn_grid.setToolTip("网格")
        self.btn_list = QToolButton(); self.btn_list.setText("☰"); self.btn_list.setToolTip("列表")
        for b in (self.btn_grid, self.btn_list):
            b.setCheckable(True)
            b.setAutoExclusive(True)
        self.btn_grid.clicked.connect(lambda: self.set_view_mode("grid"))
        self.btn_list.clicked.connect(lambda: self.set_view_mode("list"))
        self.btn_refresh = QToolButton(); self.btn_refresh.setText("⟳"); self.btn_refresh.setToolTip("重新扫描书库并刷新云端列表")
        self.btn_refresh.clicked.connect(self._on_refresh_clicked)
        top.addWidget(self.search, 1)
        top.addWidget(QLabel("排序"))
        top.addWidget(self.sort_box)
        top.addWidget(self.btn_grid)
        top.addWidget(self.btn_list)
        top.addWidget(self.btn_refresh)

        self.sidebar = QListWidget()
        self.sidebar.setFixedWidth(190)
        self.sidebar.currentItemChanged.connect(self._on_sidebar_changed)

        self.model = BookModel(self)
        self.grid = QListView()
        self.grid.setViewMode(QListView.ViewMode.IconMode)
        self.grid.setResizeMode(QListView.ResizeMode.Adjust)
        self.grid.setMovement(QListView.Movement.Static)
        self.grid.setUniformItemSizes(True)
        self.grid.setGridSize(QSize(CARD_W + 8, CARD_H + 8))
        self.grid.setLayoutMode(QListView.LayoutMode.Batched)
        self.grid.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.grid.setItemDelegate(BookDelegate(self.grid))
        self.table = QTreeView()
        self.table.setRootIsDecorated(False)
        self.table.setItemsExpandable(False)
        self.table.setUniformRowHeights(True)
        self.table.setAlternatingRowColors(True)
        for v in (self.grid, self.table):
            v.setModel(self.model)
            v.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            v.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
            v.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            v.customContextMenuRequested.connect(lambda pos, v=v: self._show_menu(v, pos))
            v.activated.connect(self._on_activated)       # 双击 / 回车
        hdr = self.table.header()
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for c in range(1, len(COLUMNS)):
            hdr.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)

        self.empty_label = QLabel()
        self.empty_label.setTextFormat(Qt.TextFormat.PlainText)
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.empty_btn = QPushButton("添加书库目录…")
        self.empty_btn.clicked.connect(self.addLibraryRequested.emit)
        empty = QWidget()
        ev = QVBoxLayout(empty)
        ev.addStretch(1)
        ev.addWidget(self.empty_label)
        ev.addWidget(self.empty_btn, 0, Qt.AlignmentFlag.AlignHCenter)
        ev.addStretch(1)
        self.content = QStackedWidget()
        self.content.addWidget(self.grid)       # 0
        self.content.addWidget(self.table)      # 1
        self.content.addWidget(empty)           # 2

        body = QHBoxLayout()
        body.addWidget(self.sidebar)
        body.addWidget(self.content, 1)
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.model.show_badges = False
        root = QVBoxLayout(self)
        root.addLayout(top)
        root.addLayout(body, 1)
        root.addWidget(self.status)

    # ------------------------------------------------------------ 状态 / 偏好
    @property
    def books(self) -> Dict[str, sh.Book]:
        return self._books

    @property
    def view_mode(self) -> str:
        return self._state["ui"]["view"]

    @property
    def sort_key(self) -> str:
        return self.sort_box.currentData()

    @property
    def current_shelf(self) -> str:
        return self._shelf

    @property
    def scanning(self) -> bool:
        return self._scanner is not None

    def visible_books(self) -> List[sh.Book]:
        return self.model.books()

    def _save_ui(self):
        try:
            sh.save_state(self._state, self._root)
        except OSError:
            pass

    def set_view_mode(self, mode: str, save: bool = True):
        mode = mode if mode in ("grid", "list") else "grid"
        self._state["ui"]["view"] = mode
        self.btn_grid.setChecked(mode == "grid")
        self.btn_list.setChecked(mode == "list")
        self._update_content_page()
        if save:
            self._save_ui()

    def set_sort(self, key: str, save: bool = True):
        i = self.sort_box.findData(key)
        if i < 0:
            return
        self._state["ui"]["sort"] = key
        if self.sort_box.currentIndex() != i:
            self.sort_box.blockSignals(True)
            self.sort_box.setCurrentIndex(i)
            self.sort_box.blockSignals(False)
        self._apply_filter()
        if save:
            self._save_ui()

    def _on_sort_changed(self, _i):
        self.set_sort(self.sort_box.currentData())

    def set_shelf(self, key: str, save: bool = True):
        """选择侧栏条目（智能书架 key 或 local:<路径>）。不存在的 key 退回「全部」，但保留用户的选择（书架重新出现时恢复）。"""
        self._want_shelf = key
        if save:
            self._state["ui"]["last_shelf"] = key
            self._save_ui()
        self._select_sidebar(key)

    def set_query(self, text: str):
        self.search.setText(text)

    def focus_search(self):
        self.search.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.search.selectAll()

    def handle_escape(self) -> bool:
        """Esc 在书架上：搜索框非空则清空并返回 True；否则返回 False（交给调用者）。"""
        if self.search.text():
            self.search.clear()
            return True
        return False

    def apply_defaults(self, view: str, sort: str, start_in_shelf: bool):
        """设置页改了「默认视图 / 默认排序 / 启动时显示书架」：立即应用并写入 state.json。"""
        self._state["ui"]["start_in_shelf"] = bool(start_in_shelf)
        self.set_view_mode(view, save=False)
        self.set_sort(sort, save=False)
        self._save_ui()

    def reset_state(self, default_view: str, default_sort: str, start_in_shelf: bool = False):
        """设置页「重置书架状态」（阶段 2：仅本机界面偏好 + index.json 缓存；
        overlay.json / peers/ / 远端设备文件的重置属于阶段 5，此处不碰）。device_id 保持不变。"""
        ui = dict(sh.DEFAULT_UI, collapsed=[])
        if default_view in ("grid", "list"):
            ui["view"] = default_view
        if default_sort in sh.SORT_KEYS:
            ui["sort"] = default_sort
        ui["start_in_shelf"] = bool(start_in_shelf)
        self._state["ui"] = ui
        self._save_ui()
        if not self._index_readonly:
            sh.reset_index(self._root)
        self._index_timer.stop()
        self._index_dirty = False
        self._opened.clear()
        self._progress.clear()
        self._books = {}
        self._catalog = ss.CloudCatalog({}, self._state["cloud"]["missing"])     # 云端缓存是派生的；「缺失」计数保留
        self._want_shelf = "all"
        self.search.clear()
        self.set_view_mode(ui["view"], save=False)
        self.set_sort(ui["sort"], save=False)
        self.refresh()
        self.rescan(force=True)
        self.sync_cloud(force=True)

    # ------------------------------------------------------------ 过滤 / 刷新
    def refresh(self):
        """重建侧栏（含计数）并按当前书架 / 搜索 / 排序刷新主区。"""
        self.model.show_badges = self.cloud_enabled()
        self._rebuild_sidebar()
        self._apply_filter()

    def _roots(self) -> List[str]:
        try:
            dirs = self._context().get("library_dirs") or []
        except Exception:
            dirs = []
        return [d for d in dirs if isinstance(d, str)]

    def _shelf_label(self, key: str) -> str:
        """local:<根>/<子文件夹> → 显示名：子文件夹相对书库根（缩进表示层级）；根目录显示根的末段。"""
        path = key[len(sh.LOCAL):]
        best = ""
        for r in self._roots():
            rr = r.replace("\\", "/").rstrip("/") or r
            if (path == rr or path.startswith(rr + "/")) and len(rr) > len(best):
                best = rr
        if not best:
            return path
        sub = path[len(best):].strip("/")
        base = best.rsplit("/", 1)[-1] or best
        if not sub:
            return base
        return "　" * sub.count("/") + "└ " + sub.rsplit("/", 1)[-1]

    @staticmethod
    def _cloud_label(key: str, counts: dict) -> str:
        """cloud:<文件夹路径> → 显示名：父文件夹也在侧栏里时缩进显示最后一段，否则显示完整相对路径。"""
        path = key[len(sh.CLOUD):]
        if "/" in path and (sh.CLOUD + path.rsplit("/", 1)[0]) in counts:
            return "　" * path.count("/") + "└ " + path.rsplit("/", 1)[-1]
        return path

    def _rebuild_sidebar(self):
        counts = count_shelves(self._books)
        self.sidebar.blockSignals(True)
        self.sidebar.clear()
        keys = []
        for key, label in SMART + tuple((k, l) for k, l in CLOUD_SMART if counts.get(k, 0) > 0):
            it = QListWidgetItem(f"{label}    {counts.get(key, 0)}")
            it.setData(Qt.ItemDataRole.UserRole, key)
            self.sidebar.addItem(it)
            keys.append(key)
        cloud = sorted((k for k in counts if k.startswith(sh.CLOUD) and k != sh.CLOUD), key=lambda k: k.casefold())
        if cloud:
            hdr = QListWidgetItem("云端书库")
            hdr.setFlags(Qt.ItemFlag.NoItemFlags)
            f = hdr.font()
            f.setBold(True)
            hdr.setFont(f)
            self.sidebar.addItem(hdr)
            for k in cloud:
                it = QListWidgetItem(f"{self._cloud_label(k, counts)}    {counts[k]}")
                it.setData(Qt.ItemDataRole.UserRole, k)
                it.setToolTip(plain_tip("library/" + k[len(sh.CLOUD):]))
                self.sidebar.addItem(it)
                keys.append(k)
        local = sorted((k for k in counts if k.startswith(sh.LOCAL)), key=lambda k: k.casefold())
        if local:
            hdr = QListWidgetItem("本机书库")
            hdr.setFlags(Qt.ItemFlag.NoItemFlags)
            f = hdr.font()
            f.setBold(True)
            hdr.setFont(f)
            self.sidebar.addItem(hdr)
            for k in local:
                it = QListWidgetItem(f"{self._shelf_label(k)}    {counts[k]}")
                it.setData(Qt.ItemDataRole.UserRole, k)
                it.setToolTip(plain_tip(k[len(sh.LOCAL):]))
                self.sidebar.addItem(it)
                keys.append(k)
        self._shelf = self._want_shelf if self._want_shelf in keys else "all"
        for i in range(self.sidebar.count()):
            if self.sidebar.item(i).data(Qt.ItemDataRole.UserRole) == self._shelf:
                self.sidebar.setCurrentRow(i)
                break
        self.sidebar.blockSignals(False)

    def _select_sidebar(self, key: str):
        for i in range(self.sidebar.count()):
            if self.sidebar.item(i).data(Qt.ItemDataRole.UserRole) == key:
                self.sidebar.setCurrentRow(i)      # 触发 _on_sidebar_changed
                return
        self.refresh()                            # 目标书架当前不存在：按「退回全部」重建侧栏

    def _on_sidebar_changed(self, cur, _prev):
        key = cur.data(Qt.ItemDataRole.UserRole) if cur is not None else None
        if not key or key == self._shelf and self._want_shelf == key:
            return
        self._shelf = key
        self._want_shelf = key
        self._state["ui"]["last_shelf"] = key
        self._save_ui()
        self._apply_filter()

    def _current_view(self):
        return self.table if self.view_mode == "list" else self.grid

    def _apply_filter(self):
        shown = sh.sort_books(sh.filter_books(self._books, self._shelf, self.search.text()), self.sort_key or "last_read")
        v = self._current_view()
        cur = v.currentIndex()
        cur_bid = cur.data(BookModel.BookRole).id if cur.isValid() and isinstance(cur.data(BookModel.BookRole), sh.Book) else None
        scroll = v.verticalScrollBar().value()
        self.model.set_books(shown)
        if cur_bid:
            row = self.model.row_of(cur_bid)
            if row >= 0:
                v.setCurrentIndex(self.model.index(row, 0))
        v.verticalScrollBar().setValue(scroll)
        self._update_content_page()
        self._update_status()

    def _update_content_page(self):
        if not hasattr(self, "content"):
            return
        if self.model.rowCount() == 0:
            self.content.setCurrentIndex(2)
            self._set_empty_text()
        else:
            self.content.setCurrentIndex(1 if self.view_mode == "list" else 0)

    def _set_empty_text(self):
        if not self._books:
            if self.scanning or self._cloud_busy:
                self.empty_label.setText("正在扫描书库…" if self.scanning else "正在读取云端书库…")
                self.empty_btn.hide()
            elif self.cloud_enabled() and not self._roots():
                self.empty_label.setText("云端 library/ 里还没有书，也没有添加本机书库目录。")
                self.empty_btn.show()
            elif not self._roots():
                self.empty_label.setText("尚未添加书库目录。\n选择存放电子书的文件夹后，书会出现在这里。")
                self.empty_btn.show()
            else:
                self.empty_label.setText("书库目录中没有找到电子书。")
                self.empty_btn.hide()
        else:
            self.empty_label.setText("没有匹配的书。")
            self.empty_btn.hide()

    def _update_status(self):
        n_all = sum(1 for b in self._books.values() if not b.hidden and not b.auto_hidden)
        cloud = self.cloud_enabled()
        parts = [f"{'共' if cloud else '本机'} {n_all} 本", f"当前显示 {self.model.rowCount()} 本"]
        if cloud:
            parts.append(self.cloud_status_text())
        if self.scanning:
            parts.append("扫描中…")
        if self._truncated:
            parts.append(f"书库超过 {sh.SCAN_LIMIT} 本，只显示前 {sh.SCAN_LIMIT} 本")
        if self._scan_error:
            parts.append("扫描失败：" + self._scan_error)
        if self._notice:
            parts.append(self._notice)
        self.status.setText(" · ".join(parts))

    # ------------------------------------------------------------ 扫描（后台线程）
    def rescan_if_stale(self) -> bool:
        if self._scanner is None and (self._last_scan is None or time.monotonic() - self._last_scan > self.STALE_SECONDS):
            return self.rescan()
        return False

    def rescan(self, force: bool = False, reuse: bool = False) -> bool:
        """后台扫描本机书库并与云端目录归并。已有扫描在跑时（且非 force）忽略；force 会作废旧扫描的结果。
        reuse=True：云端列表变了但磁盘没变，复用上一轮的本地文件，只重建（不再遍历磁盘）。"""
        if self._scanner is not None and not force:
            return False
        try:
            ctx = self._context() or {}
        except Exception:
            ctx = {}
        roots = [d for d in (ctx.get("library_dirs") or []) if isinstance(d, str)]
        remote_files, removed, removed_files = None, (), ()
        if self.cloud_enabled():
            from . import sync as syncmod
            rb = syncmod.REMOTE_BOOKS_DIR              # 缓存副本也要扫：云端移除后本机副本仍要留在书架上
            if not any(sh.norm_path(r) == sh.norm_path(rb) for r in roots):
                roots.append(rb)
            remote_files, removed, removed_files = (self._catalog.display_files(), self._catalog.removed_ids(),
                                                    self._catalog.removed_files())
        files = self._local_files if (reuse and self._local_files is not None and self._local_roots == roots) else None
        recent = [p for p in (ctx.get("recent") or []) if isinstance(p, str)]
        snap = sh.bookmark_snapshot(ctx.get("bookmarks"))
        if self._scanner is not None:
            self._scanner.stop()
        self._token += 1
        w = ScanWorker(self._token, roots, snap, recent, dict(self._progress), dict(self._opened),
                       remote_files, removed, removed_files, files)
        w.done.connect(self._on_scan_done, Qt.ConnectionType.QueuedConnection)
        w.failed.connect(self._on_scan_failed, Qt.ConnectionType.QueuedConnection)
        self._scanner = w
        self._scan_error = ""
        threading.Thread(target=w.run, daemon=True).start()
        self._update_status()
        if not self._books:
            self._update_content_page()
        return True

    def _on_scan_done(self, token: int, res: ScanResult):
        if token != self._token:
            return                                # 被更新的扫描作废
        if res.scanned:
            self._local_roots = list(self._scanner.roots) if self._scanner is not None else []
            self._local_files = res.files
            self._last_scan = time.monotonic()
        self._scanner = None
        self._notice = ""
        self._books = res.books
        self._truncated = res.truncated
        for bid, ts in self._opened.items():      # 扫描期间发生的打开 / 进度记录不能丢
            b = self._books.get(bid)
            if b:
                sh.note_opened(b, ts)
        for bid, rec in self._progress.items():
            b = self._books.get(bid)
            if b:
                sh.note_progress(b, rec["percent"], rec["updated"], rec.get("device", ""))
        self.refresh()
        self._schedule_index_save()
        self.scanFinished.emit(len(self._books))

    def _on_scan_failed(self, token: int, msg: str):
        if token != self._token:
            return
        self._scanner = None
        self._scan_error = msg[:200]
        self._last_scan = time.monotonic()
        self._update_status()
        self._update_content_page()

    def showEvent(self, e):
        super().showEvent(e)
        self.rescan_if_stale()
        self.sync_cloud_if_stale()

    # ------------------------------------------------------------ 云端（阶段 3）：全部网络在 ShelfSyncWorker 的后台线程
    def _cfg(self) -> dict:
        try:
            cfg = (self._context() or {}).get("cfg")
        except Exception:
            cfg = None
        return cfg if isinstance(cfg, dict) else {}

    def cloud_enabled(self) -> bool:
        """WebDAV 已启用且地址非空。未启用时整个云端流程都是空操作，界面上也没有云相关角标 / 提示。"""
        return client_from_cfg(self._cfg()) is not None

    def cloud_status_text(self) -> str:
        if self._cloud_busy:
            return "云端：同步中…"
        if self._cloud_error:
            return f"云端：离线（显示本地缓存）· 上次同步失败：{self._cloud_error}（点 ⟳ 重试）"
        if self._cloud_ok_at is None:
            return "云端：尚未同步"
        s = "云端：已同步 " + time.strftime("%H:%M", time.localtime(self._cloud_ok_at))
        lst = self._listing
        if lst is not None:
            if not lst.complete:
                s += f" · ⚠ 云端列表不完整（{lst.problem()}），暂不判断「云端已移除」"
            if lst.unstable:
                s += f" · {len(lst.unstable)} 本正在上传，暂不显示"
        return s

    def sync_cloud_if_stale(self) -> bool:
        return self.sync_cloud(force=False)

    def sync_cloud(self, force: bool = False) -> bool:
        """后台读取云端 library/ 并并入书架。未启用云端是空操作；非 force 时 60 秒内不重复。"""
        cfg = self._cfg()
        if client_from_cfg(cfg) is None:
            return False
        now = time.monotonic()
        if not force and self._cloud_tick is not None and now - self._cloud_tick < CLOUD_STALE_SECONDS:
            return False
        self._cloud_tick = now
        self._cloud_busy = True
        self._cloud.refresh(cfg)
        self._update_status()
        return True

    def _on_refresh_clicked(self):
        self.rescan(force=True)
        self.sync_cloud(force=True)

    def _on_cloud_timer(self):
        if self.isVisible():
            self.sync_cloud(force=True)

    def _on_cloud_listed(self, lst):
        self._cloud_busy = False
        if not self.cloud_enabled():
            return
        self._cloud_error = ""
        self._listing = lst
        self._cloud_ok_at = time.time()
        self._catalog.apply(lst, ss.extra_candidates(self._books.values()))
        self._state["cloud"] = self._catalog.to_state()
        self._save_ui()
        # 刚扫过磁盘（几秒内）就只重建；否则连磁盘一起重扫（后台线程，成本很小），以免错过本机的新缓存副本
        recent = self._last_scan is not None and time.monotonic() - self._last_scan < 5
        self.rescan(force=True, reuse=recent)
        self.cloudSynced.emit(len(lst.files))

    def _on_cloud_failed(self, msg: str):
        """离线 / 认证失败 / 服务器错误：保留本地缓存（书、索引、已移除计数都不动），只在状态条报告。"""
        self._cloud_busy = False
        self._cloud_error = (msg or "未知错误")[:120]
        self._update_status()
        self._update_content_page()

    def request_download(self, b: sh.Book, open_when_done: bool = False) -> bool:
        """后台下载云端书到 remote_books/。open_when_done：完成后打开（仅当这个请求仍是最新的打开意图、
        书架仍在前台时才会打开：迟到 / 过期的完成事件只缓存文件，不会劫持当前窗口）。"""
        cfg = self._cfg()
        if client_from_cfg(cfg) is None:
            self._notice = "未启用云同步，无法下载"
            self._update_status()
            return False
        if not b.remote_rel or b.removed:
            self._notice = f"云端已移除，无法下载：{b.title}" if b.removed else f"这本书不在云端：{b.title}"
            self._update_status()
            return False
        self._dl_seq += 1
        tok = self._dl_seq
        self._dl[tok] = (b.id, b.remote_rel, bool(open_when_done))
        if open_when_done:
            self._open_token = tok
        self.model.set_busy({v[0] for v in self._dl.values()})
        self._notice = f"正在下载：{b.title}"
        self._update_status()
        self._cloud.download(cfg, b.remote_rel, b.size, open_when_done, tok)
        return True

    def _on_downloaded_for(self, tok, path: str):
        ent = self._dl.pop(tok, None)
        if ent is None:
            return                                # 不是本视图发起的请求
        self.model.set_busy({v[0] for v in self._dl.values()})
        want = ent[2] and tok == self._open_token
        if tok == self._open_token:
            self._open_token = None
        self._notice = ""
        self.rescan(force=True)                   # 新文件落地：重新归并（仅云端 → 已下载）
        if want and self.isVisible() and os.path.isfile(path):
            self.openRequested.emit(path)

    def _on_download_failed(self, tok, msg: str):
        ent = self._dl.pop(tok, None)
        if ent is None:
            return
        self.model.set_busy({v[0] for v in self._dl.values()})
        if tok == self._open_token:
            self._open_token = None
        self._notice = msg
        self._update_status()

    # ------------------------------------------------------------ 来自阅读页的记录（主线程）
    @staticmethod
    def _book_id(path: str) -> Optional[str]:
        try:
            from . import sync
            return sync.book_id(path)
        except Exception:
            return None

    def note_opened(self, path: str, ts: Optional[float] = None):
        """阅读页打开了一本书：记 last_opened（只存本机索引，不创建覆盖层条目）。"""
        self._open_token = None                   # 用户已经打开了别的书：还没完成的「下载后打开」不得再抢窗口
        bid = self._book_id(path)
        if not bid or not sh.valid_book_id(bid):
            return
        ts = float(ts if ts is not None else time.time())
        self._opened[bid] = max(self._opened.get(bid, 0.0), ts)
        b = self._books.get(bid)
        if b:
            sh.note_opened(b, ts)
            self.refresh()
        self._schedule_index_save()

    def note_progress(self, path: str, percent: float, updated: Optional[float] = None, device: str = ""):
        """阅读页保存了进度：记百分比（书签里只有字符偏移，没有百分比；已读完 / 进度条靠这条）。"""
        bid = self._book_id(path)
        if not bid or not sh.valid_book_id(bid):
            return
        u = float(updated if updated is not None else time.time())
        p = sh._num(percent)
        if p is None or p < 0:
            return
        old = self._progress.get(bid)
        if old and old["updated"] > u:
            return
        self._progress[bid] = {"percent": min(100.0, p), "updated": u, "device": str(device or "")[:64]}
        b = self._books.get(bid)
        if b and sh.note_progress(b, p, u, device):
            self.refresh()
        self._schedule_index_save()

    # ------------------------------------------------------------ index.json（主线程写，防抖）
    def _schedule_index_save(self):
        self._index_dirty = True
        self._index_timer.start()

    def _save_index(self):
        self._index_timer.stop()
        if not self._index_dirty or self._index_readonly:
            return
        self._index_dirty = False
        try:
            sh.save_index(self._books, self._opened, self._progress, self._root)
        except OSError:
            self._index_dirty = True

    def flush(self):
        """关闭窗口 / 退出前：立刻落盘未保存的索引与 UI 状态。"""
        self._save_index()
        self._save_ui()

    # ------------------------------------------------------------ 交互
    def _on_activated(self, index: QModelIndex):
        b = index.data(BookModel.BookRole)
        if isinstance(b, sh.Book):
            self.open_book(b)

    def open_book(self, b: sh.Book) -> bool:
        """打开一本书：取第一个仍存在的本地副本发 openRequested（MainWindow 先切页再 load_book）。
        没有可用文件（索引过期）时提示并重新扫描。"""
        for p in b.local_paths:
            if os.path.isfile(p):
                self._open_token = None
                self.openRequested.emit(p)
                return True
        if b.remote_rel and b.where in ("cloud", "synced") and self.cloud_enabled():
            return self.request_download(b, open_when_done=True)      # 仅云端（或缓存副本被删了）：下载后再打开
        if b.removed and not b.local_paths:
            self._notice = f"云端已移除，本机没有副本：{b.title}"
            self._update_status()
            return False
        if b.remote_rel and not self.cloud_enabled():
            self._notice = f"未启用云同步，无法下载：{b.title}"
            self._update_status()
            return False
        self._notice = f"找不到文件：{b.title}（已重新扫描）"
        self.rescan(force=True)
        return False

    def _show_menu(self, view, pos):
        idx = view.indexAt(pos)
        b = idx.data(BookModel.BookRole) if idx.isValid() else None
        if not isinstance(b, sh.Book):
            return
        view.setCurrentIndex(idx)
        m = QMenu(self)
        m.addAction("打开", lambda: self.open_book(b))
        if b.remote_rel and not b.local_paths and not b.removed and self.cloud_enabled():
            m.addAction("下载到本机", lambda: self.request_download(b, open_when_done=False))
        if b.remote_rel:
            m.addAction("复制云端路径", lambda: QGuiApplication.clipboard().setText("library/" + b.remote_rel))
        if b.local_paths:
            m.addAction("在文件夹中显示", lambda: QDesktopServices.openUrl(
                QUrl.fromLocalFile(os.path.dirname(b.local_paths[0]))))
        m.exec(view.viewport().mapToGlobal(pos))
