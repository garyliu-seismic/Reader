# 必须最先执行：把 PYREADER_HOME 指到临时目录，早于任何 `import pyreader`（config.py 在 import 时定义 APP_DIR
# 并创建目录）。否则测试会读写真实的 ~/.pyreader（config.json、bookmarks.json、remote_books、cache）。
import atexit
import os
import shutil
import sys
import tempfile

_TEST_HOME = tempfile.mkdtemp(prefix="pyreader-test-home-")
os.environ["PYREADER_HOME"] = os.path.join(_TEST_HOME, ".pyreader")   # 强制覆盖，不沿用外部已设置的值
atexit.register(shutil.rmtree, _TEST_HOME, True)

# 让测试直接 import pyreader（不依赖安装），且不需要 Qt：textio / cache 都是纯逻辑模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import fakedav  # noqa: E402


@pytest.fixture
def server():
    srv, h, url = fakedav.start()
    h.dirs.add("/dav")
    yield h, url
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def auth_server():
    srv, h, url = fakedav.start("bob:secret")
    h.dirs.add("/dav")
    yield h, url
    srv.shutdown()
    srv.server_close()
