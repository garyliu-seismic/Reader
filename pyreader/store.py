"""持久化 JSON 的原子读写，以及对「来自外部的名字」的校验（safe_component / rel 规范化）。

纯标准库，不依赖 Qt / 网络 / config（config 反过来从这里导入 load_json / save_json）。

写入规则：
  - save_json：先序列化，再写同目录的临时文件 + fsync + os.replace。任何一步失败都不会动旧文件。
  - load_json：解析失败时把坏文件改名为 <name>.bad-<时间戳> 并记日志，再返回 default。
    这样随后的 save_json 写的是新文件，坏文件原样留在磁盘上供人工恢复，而不是被默认值悄悄覆盖。
    文件不存在 / 读不了（OSError）不算损坏，直接返回 default，不改名。

名字校验（全工程只此一份）：
  - safe_component：device_id / fp / sync_user_name 这类要拼进路径或 URL 的单段名字。
  - safe_segment / normalize_rel：取自 PROPFIND 列表、要拼进本地路径的相对路径。"""
import json
import os
import re
import threading
import time
import unicodedata
import uuid
from typing import Optional


# ---------------------------------------------------------------- 原子读写
def _log(msg: str):
    """尽力写日志；config 在导入时反过来依赖本模块，所以这里延迟导入。"""
    try:
        from .config import _log_net
        _log_net(msg)
    except Exception:
        pass


def _replace(src: str, dst: str):
    """os.replace；Windows 上目标被杀毒/索引器短暂占用会抛 PermissionError，重试几次。"""
    for i in range(6):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == 5 or os.name != "nt":
                raise
            time.sleep(0.02 * (i + 1))


def _fsync_dir(d: str):
    """尽力 fsync 目录，让 rename 本身也落盘（Windows 不支持，直接跳过）。"""
    if os.name == "nt":
        return
    try:
        fd = os.open(d, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def save_json(path, obj):
    """原子写 JSON（tmp + fsync + os.replace）。失败时旧文件保持原样，临时文件被清理，异常照常抛出。"""
    path = os.fspath(path)
    data = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")   # 先序列化：对象有问题时连临时文件都不建
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, f".{os.path.basename(path)}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        _replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(d)


_q_lock = threading.Lock()


def quarantine(path) -> Optional[str]:
    """把损坏的文件改名为 <path>.bad-<时间戳>[-n]（不覆盖已有的 .bad 文件）。返回新路径，失败返回 None。"""
    path = os.fspath(path)
    ts = time.strftime("%Y%m%d-%H%M%S")
    with _q_lock:
        for n in range(1000):
            dst = f"{path}.bad-{ts}" + (f"-{n}" if n else "")
            if os.path.exists(dst):
                continue
            try:
                os.rename(path, dst)
                return dst
            except FileExistsError:
                continue
            except OSError as e:
                _log(f"[存储] 无法隔离损坏文件 {path}: {e}")
                return None
    return None


def load_json(path, default, expect=None):
    """读 JSON。不存在/读不了 → default；内容损坏（或 expect 给定而类型不符）→ 隔离为 .bad-<ts> 后返回 default。"""
    path = os.fspath(path)
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return default
    try:
        obj = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError) as e:
        bad = quarantine(path)
        _log(f"[存储] {path} 解析失败（{e}），已隔离为 {bad}")
        return default
    if expect is not None and not isinstance(obj, expect):
        bad = quarantine(path)
        _log(f"[存储] {path} 内容类型不符（{type(obj).__name__}），已隔离为 {bad}")
        return default
    return obj


# ---------------------------------------------------------------- 名字校验
_SAFE_RE = re.compile(r"[A-Za-z0-9._-]{1,64}", re.ASCII)
_RESERVED = ({"CON", "PRN", "AUX", "NUL"}
             | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)})
# 双向文本控制字符：用来伪装文件名/书名（RLO 等），一律拒绝
_BIDI = set("؜‎‏‪‫‬‭‮⁦⁧⁨⁩")
MAX_SEGMENT = 255
MAX_REL = 1024


def _is_reserved(name: str) -> bool:
    """Windows 保留设备名：CON、con.txt、COM1.epub、NUL.tar.gz 都算（取第一个点之前的部分）。"""
    stem = unicodedata.normalize("NFKC", name.split(".", 1)[0]).rstrip(" ").upper()
    return stem in _RESERVED


def safe_component(name) -> Optional[str]:
    """单段安全名字：只允许 [A-Za-z0-9._-]{1,64}；拒绝 '.'、'..'、点开头、点结尾、Windows 保留名。
    通过则原样返回，否则返回 None。"""
    if not isinstance(name, str) or not _SAFE_RE.fullmatch(name):
        return None
    if name.startswith(".") or name.endswith("."):
        return None
    if _is_reserved(name):
        return None
    return name


def safe_segment(name) -> Optional[str]:
    """相对路径中的一段（允许中文等非 ASCII）。拒绝：空、'.'、'..'、含 / \\ :、控制字符、
    双向控制字符、结尾为点或空格、Windows 保留名、过长。通过则原样返回，否则 None。"""
    if not isinstance(name, str) or not name or len(name) > MAX_SEGMENT:
        return None
    if name in (".", ".."):
        return None
    if any(c in name for c in "/\\:"):
        return None
    if any(unicodedata.category(c) == "Cc" or c in _BIDI for c in name):
        return None
    if name[-1] in ". ":
        return None
    if _is_reserved(name):
        return None
    return name


def normalize_rel(rel) -> Optional[str]:
    """校验用 '/' 分隔的相对路径：每一段都要通过 safe_segment，不允许前导 '/' 或空段。
    通过则原样返回（不改写内容，book_id 依赖原始 rel），否则 None。"""
    if not isinstance(rel, str) or not rel or len(rel) > MAX_REL:
        return None
    for seg in rel.split("/"):
        if safe_segment(seg) is None:
            return None
    return rel


def is_hidden_name(name: str) -> bool:
    """书库里要忽略的名字：以 '.' 或 '~$' 开头（隐藏文件、.part 上传中文件、Office 临时锁文件）。"""
    return name.startswith((".", "~$"))
