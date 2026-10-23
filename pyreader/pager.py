import bisect
from typing import List, Tuple, Optional
from PySide6.QtCore import QSizeF
from PySide6.QtGui import QFont, QFontMetricsF
from .config import LINE_SPACING, MARGIN_X, MARGIN_Y, PARA_SPACING, CHUNK, MAX_CACHE
from .model import Line, Page, paginate
from .markdown import render_md_lines, _pack_lines
from .pdf import render_pdf_lines

# ============ 惰性分页器 ============
class _Chapter:
    __slots__ = ("index", "title", "start", "end")
    def __init__(self, index, title, start, end):
        self.index, self.title, self.start, self.end = index, title, start, end

class LazyPager:
    """按章节惰性分页，只缓存最近几章，其余按需计算。"""
    def __init__(self, text: str, chapters: List[Tuple[int, str]], md: bool = False, pdf_blocks=None, scan_pages=None):
        self.text = text
        self.md = md
        self.pdf_blocks = pdf_blocks
        self.scan_pages = scan_pages
        self._params = {"font": QFont(), "page_size": QSizeF(400, 500),
                        "line_spacing": LINE_SPACING, "margin_x": MARGIN_X,
                        "margin_y": MARGIN_Y, "para_spacing": PARA_SPACING}
        # 归一化：把超大章节再切小，保证单次分页永远轻量
        self.chapters = []
        for i, (off, title) in enumerate(chapters):
            end = chapters[i + 1][0] if i + 1 < len(chapters) else len(text)
            if end - off <= CHUNK:
                self.chapters.append(_Chapter(len(self.chapters), title, off, end))
            else:
                k, p = 1, off
                while p < end:
                    e = min(p + CHUNK, end)
                    self.chapters.append(_Chapter(len(self.chapters), f"{title} ({k})", p, e))
                    p, k = e, k + 1
        self._cache = {}
        self._npages = {}            # 章节索引 -> 已排版页数（不随缓存淘汰，供页码估算）

    def set_params(self, params):
        self._params = params
        self._cache.clear()          # 布局变了，缓存全部失效
        self._npages.clear()

    def chapter_index_at(self, offset: int) -> int:
        starts = [c.start for c in self.chapters]
        return max(0, bisect.bisect_right(starts, offset) - 1)

    def page_info(self, ci: int, pi: int):
        """(当前页码, 总页数)：已排版章节用真实页数，未排版章节按已知章节的字数/页估算（至少 1 页）。
        用于「每页字数很少」的版式文档（如 PPTX 整页图片幻灯片），避免全文字数/章均字数虚高。"""
        known = self._npages
        chars = sum(self.chapters[i].end - self.chapters[i].start for i in known)
        pgs = sum(known.values())
        cpp = max(200.0, chars / pgs) if pgs else 400.0   # 下限：每页实际容量不会低于 ~200 字，避免被图片页拉低
        total = before = 0
        for i, c in enumerate(self.chapters):
            n = known.get(i) or max(1, round((c.end - c.start) / cpp))
            if i < ci:
                before += n
            total += n
        return before + pi + 1, max(total, before + pi + 1)

    def pages_of(self, ci: int) -> List[Page]:
        pages = self._cache.get(ci)
        if pages is None:
            c = self.chapters[ci]
            p = self._params
            if self.scan_pages is not None:
                # 扫描版：一章 = 一页，直接构造含整页位图的 Page
                spec = self.scan_pages[c.start]
                max_w = p["page_size"].width() - 2 * p["margin_x"]
                max_h = p["page_size"].height() - 2 * p["margin_y"]
                iw, ih = spec["w"], spec["h"]
                scale = min(max_w / max(1.0, iw), max_h / max(1.0, ih)) if iw and ih else 1.0
                scale = min(scale, 1.0)   # 不放大超过原始分辨率，避免模糊
                dw, dh = iw * scale, ih * scale
                ln = Line("", c.start, c.end, indent=0.0, style="image",
                          height=dh, ascent=0.0, image=(spec["img_id"], dw, dh))
                pages = [Page(0, c.start, c.end, [ln])]
            elif self.pdf_blocks is not None:
                max_w = p["page_size"].width() - 2 * p["margin_x"]
                usable_h = p["page_size"].height() - 2 * p["margin_y"]
                line_h = QFontMetricsF(p["font"]).height() * p["line_spacing"]
                if c.end == len(self.text):
                    # 文档末尾的图片等无文本块（start == len(text)）也要归入最后一章
                    bs = [b for b in self.pdf_blocks if c.start <= b.get("start", 0) <= c.end]
                else:
                    bs = [b for b in self.pdf_blocks if c.start <= b.get("start", 0) < c.end]
                lines = render_pdf_lines(bs, p["font"], max_w, p["line_spacing"],
                                         p.get("para_spacing", PARA_SPACING), line_h,
                                         max_h=usable_h * 0.92)
                pages = _pack_lines(lines, usable_h, line_h)
            elif self.md:
                max_w = p["page_size"].width() - 2 * p["margin_x"]
                line_h = QFontMetricsF(p["font"]).height() * p["line_spacing"]
                lines = render_md_lines(self.text[c.start:c.end], c.start, p["font"],
                                        max_w, p["line_spacing"],
                                        p.get("para_spacing", PARA_SPACING), line_h)
                pages = _pack_lines(lines, p["page_size"].height() - 2 * p["margin_y"], line_h)
            else:
                pages = paginate(self.text[c.start:c.end], p["font"], p["page_size"],
                                 base_offset=c.start, line_spacing=p["line_spacing"],
                                 margin_x=p["margin_x"], margin_y=p["margin_y"],
                                 para_spacing=p.get("para_spacing", PARA_SPACING))
            self._cache[ci] = pages
            self._npages[ci] = len(pages)
            # 淘汰最远的章节，保留当前章节及其邻居（跨章翻页无感）
            if len(self._cache) > MAX_CACHE:
                far = sorted((abs(k - ci), k) for k in self._cache if k != ci)
                for _, k in far:
                    if len(self._cache) <= MAX_CACHE:
                        break
                    del self._cache[k]
        return pages
