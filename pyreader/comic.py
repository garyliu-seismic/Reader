"""漫画 / 扫描书压缩包（.zip / .cbz）的纯逻辑：列页、读页、分章，以及翻页 / 跨页 / 缩放的纯函数。不依赖 Qt（见 comicview.py）。
逻辑参照 ImageViewer（viewer_core.py / media_source.py），改进点：自然排序按「完整相对路径」逐级比较（ImageViewer 只比文件名），
所以 ch2/ 排在 ch10/ 之前，且同一目录的页挨在一起。

规则：只认 IMAGE_EXTS；跳过目录条目、__MACOSX、任何以 . 开头的路径段（.DS_Store、._xxx）；
条目只读字节，不落盘，路径穿越（../）无影响。防御：坏 zip / 加密 / 超大条目（zip 炸弹）都转成 ComicError。
"""
import os
import re
import threading
import zipfile
from typing import Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")
COMIC_EXTS = (".zip", ".cbz")
MAX_ENTRY_SIZE = 64 * 1024 * 1024     # 单页解压后上限；声明的大小和实际读到的字节数都要过这一关
MAX_ENTRIES = 20000                   # 压缩包条目数上限
MIN_ZOOM, MAX_ZOOM = 0.02, 40.0
FIT_MODES = ("width", "height", "window")


class ComicError(Exception):
    """压缩包无法作为漫画读取：损坏 / 加密 / 条目过多 / 单页过大 / 已关闭。"""


class Chapter(NamedTuple):
    title: str      # 目录路径（页都在根目录时为 ""）
    start: int      # 起始页序号（0 起）
    count: int


# ================================================================== 排序 / 过滤
def natural_key(s: str) -> list:
    """自然排序：page2 排在 page10 之前。split 的结果恒为 str, int, str, ... 交替，所以两个 key 可直接比较。"""
    return [int(t) if i % 2 else t.lower() for i, t in enumerate(re.split(r"(\d+)", s))]     # 按奇偶位判断：isdigit() 对 ①/² 也为真，int() 会抛 ValueError


def path_key(name: str) -> list:
    """完整相对路径的排序键：按路径段逐级做自然排序。"""
    return [natural_key(p) for p in name.replace("\\", "/").split("/")]


def is_page(name: str) -> bool:
    """压缩包里的条目是否算一页：图片扩展名，且不是目录 / 苹果垃圾 / 点文件。"""
    n = name.replace("\\", "/")
    if not n or n.endswith("/"):
        return False
    parts = n.split("/")
    for p in parts:
        if not p or p == "__MACOSX" or p.startswith("."):
            return False
    return os.path.splitext(parts[-1])[1].lower() in IMAGE_EXTS


def display_name(name: str) -> str:
    """没标 UTF-8 的 zip 条目名会被 zipfile 按 cp437 解码；中文 Windows 打的包实际是 GBK，能还原就还原（只用于显示，排序不用）。"""
    try:
        name.encode("cp437")
    except UnicodeEncodeError:
        return name                   # 已经含非 cp437 字符，说明 zipfile 按 UTF-8 解的
    try:
        return name.encode("cp437").decode("gbk")
    except UnicodeDecodeError:
        return name


def group_chapters(pages: Sequence[str], title: Callable[[str], str] = str) -> List[Chapter]:
    """按所在目录把（已排序的）页切成章；只是数据，是否显示由界面决定。目录不同的相邻页开新章。title 把目录路径转成显示名。"""
    chapters: List[Chapter] = []
    cur, start = None, 0
    for i, name in enumerate(pages):
        d = name.replace("\\", "/").rpartition("/")[0]
        if i == 0 or d != cur:
            if i:
                chapters.append(Chapter(title(cur), start, i - start))
            cur, start = d, i
    if pages:
        chapters.append(Chapter(title(cur), start, len(pages) - start))
    return chapters


# ================================================================== 压缩包
class ComicArchive:
    """打开的漫画压缩包。多线程可共享：所有读取都在锁里，close() 之后再读抛 ComicError。"""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._zf: Optional[zipfile.ZipFile] = None
        try:
            zf = zipfile.ZipFile(path)
        except (zipfile.BadZipFile, OSError, EOFError, ValueError, NotImplementedError) as e:
            raise ComicError(f"无法打开压缩包：{e}") from e
        try:
            infos = zf.infolist()
            if len(infos) > MAX_ENTRIES:
                raise ComicError(f"压缩包条目过多（{len(infos)}）")
            self._sizes: Dict[str, int] = {i.filename: i.file_size for i in infos if is_page(i.filename)}
            self._repair_names = not any(i.flag_bits & 0x800 for i in infos)   # 没有任何条目标了 UTF-8 才尝试按 GBK 还原
        except BaseException:
            zf.close()
            raise
        self._zf = zf
        self.pages: List[str] = sorted(self._sizes, key=path_key)

    def read(self, entry: str) -> bytes:
        size = self._sizes.get(entry)
        if size is None:
            raise ComicError(f"压缩包里没有这一页：{entry}")
        if size > MAX_ENTRY_SIZE:
            raise ComicError(f"页面过大（{size} 字节）：{entry}")
        with self._lock:
            if self._zf is None:
                raise ComicError("压缩包已关闭")
            try:
                with self._zf.open(entry) as f:
                    data = f.read(MAX_ENTRY_SIZE + 1)      # 头里声明的大小可以造假，按实际读到的再卡一次
            except (zipfile.BadZipFile, RuntimeError, OSError, EOFError, NotImplementedError, ValueError) as e:
                raise ComicError(f"读取失败（损坏或加密）：{entry}：{e}") from e
        if len(data) > MAX_ENTRY_SIZE:
            raise ComicError(f"页面过大：{entry}")
        return data

    def chapters(self) -> List[Chapter]:
        return group_chapters(self.pages, display_name if self._repair_names else str)

    def close(self):
        with self._lock:
            zf, self._zf = self._zf, None
        if zf is not None:
            zf.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def list_pages(path: str) -> List[str]:
    """按自然顺序列出 .zip / .cbz 里的页（条目名）。坏 zip 抛 ComicError；没有图片返回 []。"""
    with ComicArchive(path) as a:
        return list(a.pages)


def read_page_bytes(path: str, entry: str) -> bytes:
    """读一页的原始字节（每次打开 / 关闭压缩包，线程安全；连续读很多页请用 ComicArchive）。"""
    with ComicArchive(path) as a:
        return a.read(entry)


# ================================================================== 跨页 / 方向 / 缩放（纯函数）
def is_portrait(w: float, h: float) -> bool:
    return h > w > 0


def spread_pages(index: int, count: int, portrait: Callable[[int], bool],
                 spread: bool = True, cover_alone: bool = True) -> Tuple[int, ...]:
    """index 起始的这一屏要显示哪几页（1 或 2 页）。只有两页都是竖页才合成跨页；cover_alone 时第 0 页（封面）单独显示。"""
    if not spread or index >= count - 1 or (cover_alone and index == 0):
        return (index,)
    if portrait(index) and portrait(index + 1):
        return (index, index + 1)
    return (index,)


def next_start(index: int, count: int, portrait: Callable[[int], bool],
               spread: bool = True, cover_alone: bool = True) -> Optional[int]:
    """下一屏的起始页；已经是最后一屏返回 None。"""
    nxt = spread_pages(index, count, portrait, spread, cover_alone)[-1] + 1
    return nxt if nxt < count else None


def prev_start(index: int, count: int, portrait: Callable[[int], bool],
               spread: bool = True, cover_alone: bool = True) -> Optional[int]:
    """上一屏的起始页；已经是第一屏返回 None。往回退时，前两页都是竖页（且不碰封面）就退成一个跨页。"""
    if index <= 0:
        return None
    s = index - 2
    if spread and s >= (1 if cover_alone else 0) and portrait(s) and portrait(index - 1):
        return s
    return index - 1


def display_order(pages: Sequence[int], rtl: bool) -> Tuple[int, ...]:
    """跨页从左到右的绘制顺序：从右往左阅读时，先读的页在右边。"""
    return tuple(reversed(pages)) if rtl else tuple(pages)


def click_side(x: float, width: float) -> str:
    """点击位置落在左 / 中 / 右三分之一。"""
    if width <= 0:
        return "middle"
    if x < width / 3:
        return "left"
    if x > width * 2 / 3:
        return "right"
    return "middle"


def nav_delta(side: str, rtl: bool) -> int:
    """点击 / 方向键的左右侧对应的翻页方向：+1 下一页，-1 上一页，0 无动作。从右往左阅读时左侧是下一页。"""
    if side == "left":
        return 1 if rtl else -1
    if side == "right":
        return -1 if rtl else 1
    return 0


def spread_size(sizes: Sequence[Tuple[float, float]]) -> Tuple[float, float]:
    """一屏的合成尺寸：各页先缩放到同一高度（最高那页的高度）再并排。单页就是原尺寸。"""
    h = max(s[1] for s in sizes)
    return sum(w * h / ph for w, ph in sizes), h


def compute_zoom(fit: str, cw: float, ch: float, rw: float, rh: float) -> float:
    """按适配模式算缩放倍率（参照 ImageViewer compute_zoom）。cw/ch 视口，rw/rh 图片（或跨页）尺寸。"""
    cw, ch = max(cw, 1), max(ch, 1)
    rw, rh = max(rw, 1), max(rh, 1)
    if fit == "width":
        z = cw / rw
    elif fit == "height":
        z = ch / rh
    else:
        z = min(cw / rw, ch / rh)
    return min(max(z, MIN_ZOOM), MAX_ZOOM)
