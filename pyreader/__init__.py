"""PyReader —— 按功能拆分的阅读器包。"""

__all__ = ["MainWindow"]


def __getattr__(name):
    # 惰性导入：只用 textio / cache 等纯逻辑模块（如单元测试）时不必加载 Qt
    if name == "MainWindow":
        from .mainwin import MainWindow
        return MainWindow
    raise AttributeError(name)
