import os
import pytest
from pyreader import textio
from pyreader.textio import cn2int, decode_text, scan_chapters


def titles(chs):
    return [t for _, t in chs]


def body(n=6):
    return "他沿着走廊慢慢往前走，心里反复想着刚才发生的一切，却始终得不到答案。\n\n" * n


def novel(headings, filler=None):
    filler = body() if filler is None else filler
    return "书名\n\n" + "".join(f"{h}\n\n{filler}" for h in headings)


@pytest.mark.parametrize("s,n", [
    ("十", 10), ("十一", 11), ("二十", 20), ("二十三", 23), ("一百零五", 105), ("一千二百三十四", 1234),
    ("一万二千", 12000), ("廿一", 21), ("卅", 30), ("两百", 200), ("壹贰叁", 123), ("拾壹", 11),
    ("柒佰捌拾玖", 789), ("二〇二", 202), ("０１２", 12), ("37", 37), ("零", 0),
])
def test_cn2int(s, n):
    assert cn2int(s) == n


@pytest.mark.parametrize("s", ["", "章", "第三", "abc"])
def test_cn2int_invalid(s):
    assert cn2int(s) is None


def test_decode_gbk():
    text = "第一章 开始\n\n" + "这是正文。" * 50 + "\n\n第二章 继续\n\n" + "更多正文。" * 50
    assert decode_text(text.encode("gbk")) == text


def test_decode_utf8_bom():
    assert decode_text("﻿第一章 起\n正文".encode("utf-8")) == "第一章 起\n正文"
    assert decode_text(b"\xef\xbb\xbf" + b"x") == "x"


def test_decode_normalizes_newlines():
    assert decode_text("a\r\nb\rc\nd".encode("utf-8")) == "a\nb\nc\nd"
    assert decode_text("a\r\nb\rc".encode("gbk")) == "a\nb\nc"


def test_lone_cr_offsets_match_text():
    b = body().replace("\n", "\r")
    raw = f"楔子\r\r{b}第一章 残骸\r\r{b}第二章 回声\r\r{b}".encode("utf-8")
    text = decode_text(raw)
    chs = scan_chapters(text)
    assert "\r" not in text
    assert titles(chs) == ["楔子", "第一章 残骸", "第二章 回声"]
    for off, t in chs:
        assert text[off:].startswith(t)


def test_sample_novel_unchanged():
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "示例小说.txt")
    if not os.path.exists(p):
        pytest.skip("sample novel absent")
    with open(p, "rb") as f:
        chs = scan_chapters(decode_text(f.read()))
    assert titles(chs) == ["开篇", "楔子", "第一章 残骸", "第二章 回声", "第三章 蓝纹", "第四章 出价",
                           "第五章 追猎", "第六章 通道", "第七章 坠落", "尾声"]


def test_toc_page_ads_and_body_reference():
    nums = "一二三四五六七八"
    toc = "目录\n" + "".join(f"第{n}章 标题{n}\n" for n in nums) + "\n"
    ads = "本书由XX网整理上传，仅供试读，请于24小时内删除\n更多好书请访问 www.example.com\n\n"
    parts = []
    for n in nums:
        extra = ""
        if n == "三":
            extra = "第三章的内容是…\n\n第三章讲的是另一个故事\n\n"
        parts.append(f"第{n}章 标题{n}\n\n{extra}{body(4)}")
    text = "书名\n" + ads + toc + "\n".join(parts)
    chs = scan_chapters(text)
    assert titles(chs) == ["开篇"] + [f"第{n}章 标题{n}" for n in nums]
    # 每个章节起点必须落在正文里（目录页之后），而不是目录行
    assert chs[1][0] >= text.index("目录") + len(toc)


def test_body_line_with_chapter_prefix_is_not_chapter():
    text = novel([f"第{n}章 名{n}" for n in "一二三四"])
    text += "\n第三章讲的是另一个故事\n\n" + body(2)
    assert len(scan_chapters(text)) == 5          # 开篇 + 4


def test_closing_bracket_title_kept_but_quote_rejected():
    text = novel(["第一章 重生（上）", "第二章 重生（下）", "第三章 归来"])
    assert titles(scan_chapters(text))[1:] == ["第一章 重生（上）", "第二章 重生（下）", "第三章 归来"]
    text2 = novel(["第一章 甲", "第二章 乙", "第三章 丙"]) + "\n第四章 他说走吧”\n\n" + body(1)
    assert len(scan_chapters(text2)) == 4


def test_english_chapters():
    filler = "He walked slowly down the long corridor, thinking about everything. " * 8 + "\n\n"
    hs = ["Chapter 1: The Beginning", "Chapter 2", "CHAPTER III", "Chapter Four - Rain"]
    assert titles(scan_chapters(novel(hs, filler)))[1:] == hs


def test_dun_style_numbering():
    hs = ["一、少年", "二、远行", "三、归来", "四、尾声之前"]
    assert titles(scan_chapters(novel(hs, body(15))))[1:] == hs


def test_numdot_style_numbering_and_list_in_body_ignored():
    hs = ["1. Arrival", "2. Departure", "3. Return"]
    assert titles(scan_chapters(novel(hs, body(15))))[1:] == hs
    lst = body() + "1. 苹果\n2. 香蕉\n\n" + body()          # 正文里零散的列表不构成章节
    assert titles(scan_chapters(lst)) == ["第 1 部分"]


def test_specials_fullwidth_and_traditional_numbers():
    hs = ["序章", "第１章 起", "第２章 承", "第３章 转", "尾声", "番外 小事"]
    assert titles(scan_chapters(novel(hs, body(15))))[1:] == hs
    assert len(scan_chapters(novel(["第壹章 甲", "第贰章 乙", "第叁章 丙"]))) == 4
    assert len(scan_chapters(novel(["第廿一章 甲", "第廿二章 乙", "第廿三章 丙"]))) == 4


def test_bracketed_and_hui_units():
    assert len(scan_chapters(novel(["【第一章】 起", "【第二章】 承", "【第三章】 转"]))) == 4
    assert len(scan_chapters(novel(["第一回 宴桃园", "第二回 张翼德", "第三回 议温明"]))) == 4


def test_no_chapter_text_chunks_on_paragraph_boundary(monkeypatch):
    monkeypatch.setattr(textio, "CHUNK", 1000)
    para = "这是一个没有任何章节标记的段落，内容很长很长很长很长。" * 4
    text = "\n\n".join([para] * 60)
    chs = scan_chapters(text)
    assert len(chs) > 3
    assert titles(chs)[0] == "第 1 部分"
    for off, _ in chs[1:]:
        assert text[off - 1] == "\n"                  # 不在句子中间切
    assert [o for o, _ in chs] == sorted(o for o, _ in chs)


def test_empty_and_short_text():
    assert scan_chapters("") == []
    assert scan_chapters("很短的一段文字") == [(0, "第 1 部分")]


def test_markdown_headings_still_chapters():
    text = "# 书\n\n" + "正文。" * 100 + "\n\n## 第二节\n\n" + "正文。" * 100
    assert titles(scan_chapters(text)) == ["书", "第二节"]
