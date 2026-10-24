"""设置对话框：按功能分页签 + 搜索过滤。控件属性名是 mainwin 读取设置的接口，必须保持不变。"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from pyreader.config import DEFAULT_CONFIG  # noqa: E402
from pyreader.settings import SettingsDialog  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def qapp():
    return QApplication.instance() or QApplication([])


def titles(d):
    return [d.tabs.tabText(i) for i in range(d.tabs.count())]


def test_tabs_group_settings_by_function():
    d = SettingsDialog(dict(DEFAULT_CONFIG))
    assert titles(d) == ["AI", "排版", "朗读", "同步与网络", "书架"]


def test_every_widget_mainwin_reads_still_exists_and_is_on_some_tab():
    d = SettingsDialog(dict(DEFAULT_CONFIG))
    names = ["preset", "key", "base", "model", "api_version", "font_family", "theme", "flip_effect", "single_page",
             "font_size", "line_spacing", "para_spacing", "margin_x", "margin_y", "outer", "gutter", "tts_voice",
             "azure_key", "azure_region", "azure_key2", "azure_region2", "tts_rate", "proxy", "webdav_enabled",
             "webdav_url", "webdav_user", "webdav_password", "webdav_allow_insecure", "sync_user_name", "device_name",
             "shelf_view", "shelf_sort", "shelf_start", "shelf_reset", "bilingual", "bilingual_target"]
    for n in names:
        w = getattr(d, n)
        owner = w
        while owner is not None and owner not in d._pages:
            owner = owner.parentWidget()
        assert owner is not None, f"{n} 没有放进任何页签"


def test_values_roundtrip_through_tabs():
    cfg = dict(DEFAULT_CONFIG, api_key="k1", webdav_url="https://x/", webdav_user="gary", font_size=20,
               azure_tts_key="az", shelf_default_view="list")
    d = SettingsDialog(cfg)
    assert d.key.text() == "k1" and d.webdav_url.text() == "https://x/" and d.webdav_user.text() == "gary"
    assert d.font_size.value() == 20 and d.azure_key.text() == "az" and d.shelf_view.currentData() == "list"


def test_search_filters_rows_counts_matches_and_jumps_to_first_hit_tab():
    d = SettingsDialog(dict(DEFAULT_CONFIG))
    d.search.setText("key")
    assert titles(d) == ["AI (1)", "排版 (0)", "朗读 (2)", "同步与网络 (0)", "书架 (0)"]
    assert d.tabs.currentIndex() == 0
    d.search.setText("代理")
    assert d.tabs.currentIndex() == 3 and titles(d)[3] == "同步与网络 (1)"
    assert not d.tabs.isTabEnabled(0) and d.tabs.isTabEnabled(3)
    d.search.setText("azure key")                              # 多个词：都要命中
    assert titles(d)[2] == "朗读 (2)" and d.tabs.currentIndex() == 2


def test_search_matches_placeholder_and_checkbox_text_and_combo_items():
    d = SettingsDialog(dict(DEFAULT_CONFIG))
    d.search.setText("sing-box")                               # 只出现在代理输入框的占位符里
    assert titles(d)[3] == "同步与网络 (1)"
    d.search.setText("明文")                                    # 复选框文字
    assert titles(d)[3] == "同步与网络 (1)"
    d.search.setText("网格")                                    # 下拉项
    assert titles(d)[4] == "书架 (1)"


def test_clearing_search_restores_everything():
    d = SettingsDialog(dict(DEFAULT_CONFIG))
    d.search.setText("zzzz-no-such-setting")
    assert all(not d.tabs.isTabEnabled(i) for i in range(d.tabs.count()))
    d.search.setText("")
    assert titles(d) == ["AI", "排版", "朗读", "同步与网络", "书架"]
    assert all(d.tabs.isTabEnabled(i) for i in range(d.tabs.count()))
    assert all(form.isRowVisible(row) for form, row, _i, _h in d._rows)


def test_dialog_fits_on_screen_and_old_configs_still_open():
    d = SettingsDialog({})                                      # 老版本 config.json 什么键都没有
    assert d.width() >= 560 and d.height() > 200
