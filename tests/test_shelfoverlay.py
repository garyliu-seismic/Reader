"""阶段 5：覆盖层同步（fav / hidden 编辑、合并、rev + 内容哈希驱动的推送与自愈、单实例锁、重置、关闭 flush、恶意 JSON）。
进程内 fake WebDAV（绝不连真实服务器）+ offscreen Qt：在模型 / 信号层验证；GUI 的真实点击 / 外观无法在此验证。"""
import json
import os
import subprocess
import sys
import threading
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QItemSelectionModel  # noqa: E402
from PySide6.QtGui import QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication, QStyleOptionViewItem  # noqa: E402

from pyreader import shelf as sh  # noqa: E402
from pyreader import shelfsync as ss  # noqa: E402
from pyreader import shelfview as sv  # noqa: E402
from pyreader import sync  # noqa: E402
from pyreader.webdav import WebDavClient  # noqa: E402

OLD = 1000
T0 = 1_700_000_000.0
USER = "gary"
DEAD = "http://127.0.0.1:9/dav"          # 连接被拒绝：模拟离线
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module", autouse=True)
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def rb(tmp_path, monkeypatch):
    d = tmp_path / "remote_books"
    monkeypatch.setattr(sync, "REMOTE_BOOKS_DIR", str(d))
    return d


def wait_for(cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        QApplication.processEvents()
        if cond():
            return True
        time.sleep(0.005)
    QApplication.processEvents()
    return cond()


def raw_put(h, path, data, mtime=OLD):
    parts = path.strip("/").split("/")
    for i in range(1, len(parts)):
        h.dirs.add("/" + "/".join(parts[:i]))
    h.files[path] = data
    h.mtimes[path] = mtime
    h.etags[path] = '"e%d-%d"' % (next(h.counter), len(data))


def raw_del(h, path):
    for d in (h.files, h.mtimes, h.etags):
        d.pop(path, None)


def put_book(h, rel, data=b"x"):
    segs = rel.split("/")
    h.dirs.update(["/dav", "/dav/library"] + ["/dav/library/" + "/".join(segs[:i]) for i in range(1, len(segs))])
    raw_put(h, "/dav/library/" + rel, data, OLD)


def ms_now():
    return int(time.time() * 1000)


def ov_doc(dev, entries=None, progress=None, host=None, **extra):
    d = {"version": 1, "device_id": dev, "entries": entries or {}, "progress": progress or {}}
    if host:
        d["hostname"] = host
    d.update(extra)
    return json.dumps(d, ensure_ascii=False).encode("utf-8")


def put_dev(h, dev, entries=None, progress=None, mtime=OLD, user=USER, raw=None):
    raw_put(h, f"/dav/users/{user}/shelf/{dev}.json", raw if raw is not None else ov_doc(dev, entries, progress), mtime)


def dev_path(dev, user=USER):
    return f"/dav/users/{user}/shelf/{dev}.json"


def server_doc(h, dev):
    return json.loads(h.files[dev_path(dev)].decode("utf-8"))


def gets(h, suffix=""):
    return [p for m, p in h.log if m == "GET" and p.endswith(suffix)]


def cfg_for(url, user=USER, **extra):
    d = {"webdav_enabled": True, "webdav_url": url, "sync_user_name": user, "device_name": "pc"}
    d.update(extra)
    return d


def mk(tmp_path, url, name="a", dev=None, read_only=False, cfg=None, confirm=True, dirs=()):
    root = tmp_path / name
    if dev:
        sh.save_state(sh.default_state(dev), str(root))        # 固定 device_id（克隆 / 多设备场景）
    cfg = cfg if cfg is not None else (cfg_for(url) if url else {})
    ctx = {"library_dirs": list(dirs), "recent": [], "bookmarks": {}, "cfg": cfg}
    v = sv.ShelfView(lambda: ctx, root=str(root), read_only=read_only)
    v.ctx, v.root = ctx, str(root)
    v.synced, v.psynced = [], []
    v.cloudSynced.connect(v.synced.append)
    v.progressSynced.connect(v.psynced.append)
    if confirm is not None:
        v.confirm_push = lambda: confirm
    return v


def online(v, url):
    v.ctx["cfg"] = cfg_for(url)


def offline(v):
    v.ctx["cfg"] = cfg_for(DEAD)


def settle(v):
    assert wait_for(lambda: not v.scanning and not v._cloud_busy and not v._cloud._running
                    and not v._cloud._prog_running and not v._progress_busy), "云端同步 / 扫描没有在时限内结束"


def settle_push(v):
    assert wait_for(lambda: not v._cloud._push_running and v._push_inflight is None), "推送没有在时限内结束"


def sync_wait(v, push=True):
    n, m = len(v.synced), len(v.psynced)
    assert v.sync_cloud(force=True) is True
    assert wait_for(lambda: len(v.synced) > n and len(v.psynced) > m), "云端列表 / 进度没有并入"
    settle(v)
    if push:
        settle_push(v)
        settle(v)


def flags(v, ids):
    m = v.merged_view()
    return {b: (bool(sh.field_value(m, b, "fav", False)), bool(sh.field_value(m, b, "hidden", False))) for b in ids}


def cloud_books(h, *rels):
    for r in rels:
        put_book(h, r, ("内容:" + r).encode())
    return ["remote:" + r for r in rels]


@pytest.fixture
def fixed_clock(server):
    h, url = server
    h.now = staticmethod(lambda: T0)          # 服务器写入的 mtime 固定为很久以前：自己的文件游标可信，不会一直 racy
    return h, url


# ================================================================== 纯逻辑：reconcile_own / 状态 / 锁
def _own(dev="dev-a", fav=(), prog=None):
    ov = sh.new_overlay(dev, "pc", "gary")
    for bid, ts in fav:
        ov["entries"].setdefault(bid, {})["fav"] = [True, ts, dev]
    for bid, (p, u) in (prog or {}).items():
        ov["progress"][bid] = {"percent": p, "updated": u, "device": dev}
    return ov


def test_reconcile_own_cases():
    A, B = "remote:甲.txt", "remote:乙.txt"
    now = ms_now()
    local = _own(fav=[(A, now - 5000)])
    same, _ = sh.validate_overlay(json.loads(ov_doc("dev-a", {A: {"fav": [True, now - 5000]}})), device_id="dev-a")
    adopted, behind = sh.reconcile_own(local, same)
    assert adopted is None and behind is False                                  # 完全一致
    behind_remote, _ = sh.validate_overlay(json.loads(ov_doc("dev-a")), device_id="dev-a")
    adopted, behind = sh.reconcile_own(local, behind_remote)
    assert adopted is None and behind is True                                   # 云端落后（回滚 / 空文件）：要重推
    newer, _ = sh.validate_overlay(json.loads(ov_doc("dev-a", {A: {"fav": [False, now]}, B: {"hidden": [True, now]}})),
                                   device_id="dev-a")
    adopted, behind = sh.reconcile_own(local, newer)                            # 克隆设备写了更新的值：并入本地
    assert adopted is not None and behind is False
    assert adopted["entries"][A]["fav"][:2] == [False, now] and adopted["entries"][B]["hidden"][0] is True
    assert local["entries"][A]["fav"][0] is True                                # 不修改传入的 local
    other, _ = sh.validate_overlay(json.loads(ov_doc("dev-a", {B: {"fav": [True, now]}})), device_id="dev-a")
    adopted, behind = sh.reconcile_own(local, other)                            # 各有对方没有的：并入后云端仍落后 -> 两样都要
    assert adopted is not None and behind is True and set(adopted["entries"]) == {A, B}
    # hostname 不同不算不一致（克隆设备不会因为元信息互相覆盖而死循环）
    h2, _ = sh.validate_overlay(json.loads(ov_doc("dev-a", {A: {"fav": [True, now - 5000]}}, host="other-pc")), device_id="dev-a")
    assert sh.reconcile_own(local, h2) == (None, False)
    # 进度摘要：云端更新的并入，本地更新的要重推；并入后仍按 DIGEST_KEEP 修剪（不拉锯）
    lp = _own(prog={A: (10.0, 1000.0)})
    rp, _ = sh.validate_overlay(json.loads(ov_doc("dev-a", progress={A: {"percent": 50.0, "updated": 2000.0}})), device_id="dev-a")
    adopted, behind = sh.reconcile_own(lp, rp)
    assert adopted["progress"][A]["percent"] == 50.0 and behind is False


def test_state_persists_unpushed_reset_pending_and_stamp_fetched_at(tmp_path):
    root = str(tmp_path / "r")
    st = sh.load_state(root)
    assert st["unpushed"] == [] and st["reset_pending"] is False
    st["unpushed"] = ["remote:a.txt", "../evil", 5, "remote:a.txt", "local:" + "a" * 40]
    st["reset_pending"] = True
    st["remote_stamp"] = {"mtime": 5.0, "size": 7, "etag": '"x"', "fetched_at": 99.5}
    assert sh.save_state(st, root)
    st2 = sh.load_state(root)
    assert st2["unpushed"] == ["remote:a.txt", "local:" + "a" * 40] and st2["reset_pending"] is True
    assert st2["remote_stamp"]["fetched_at"] == 99.5
    raw = json.load(open(sh.state_path(root), encoding="utf-8"))
    raw["unpushed"], raw["reset_pending"] = "bad", "yes"
    json.dump(raw, open(sh.state_path(root), "w", encoding="utf-8"))
    st3 = sh.load_state(root)
    assert st3["unpushed"] == [] and st3["reset_pending"] is False


def test_lock_second_holder_is_refused_until_released(tmp_path):
    root = str(tmp_path / "r")
    a = sh.acquire_lock(root)
    assert a is not None and a.held
    assert sh.acquire_lock(root) is None                                       # 同一把锁，第二个持有者拿不到
    a.release()
    assert not a.held
    b = sh.acquire_lock(root)
    assert b is not None
    b.release()


def test_lock_blocks_another_process(tmp_path):
    root = str(tmp_path / "r")
    code = ("import sys; sys.path.insert(0, sys.argv[1])\n"
            "from pyreader import shelf as sh\n"
            "l = sh.acquire_lock(sys.argv[2])\n"
            "print('held' if l else 'none')\n"
            "l and l.release()\n")

    def other():
        r = subprocess.run([sys.executable, "-I", "-c", code, REPO, root], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    lock = sh.acquire_lock(root)
    assert lock is not None
    assert other() == "none"                                                   # 另一个进程拿不到：它会以只读模式打开书架
    lock.release()
    assert other() == "held"


def test_pid_lock_fallback(tmp_path):
    path = str(tmp_path / "x.lock")
    a = sh._pid_lock(path)
    assert a is not None and a.kind == "pid"
    assert sh._pid_lock(path) is None                                          # 进程（就是我们自己）还活着：被占用
    a.release()
    assert not os.path.exists(path + ".pid")
    open(path + ".pid", "w").write("99999999")                                 # 残留（进程不存在）：接管
    b = sh._pid_lock(path)
    assert b is not None
    b.release()


# ================================================================== 两台设备离线各自编辑再同步，收敛一致
def test_two_devices_edit_offline_then_converge(server, tmp_path, rb):
    h, url = server
    A_, B_, C_ = cloud_books(h, "甲.txt", "乙.txt", "丙.txt")
    A = mk(tmp_path, url, "a", dev="dev-a")
    B = mk(tmp_path, url, "b", dev="dev-b")
    sync_wait(A)
    sync_wait(B)
    offline(A)
    offline(B)
    assert A.set_fav([A.books[A_]]) == 1 and A.set_hidden([A.books[B_]]) == 1
    assert B.set_fav([B.books[C_]]) == 1 and B.set_fav([B.books[A_]]) == 1
    assert B.set_fav([B.books[A_]], False) == 1                               # B 之后又取消了甲的收藏（B 的更晚）
    assert A.books[A_].fav is True and A.books[B_].hidden is True              # 立即生效（离线）
    assert A.push_pending() and B.push_pending() and A.pending_count() == 2 and B.pending_count() == 2
    assert not any(m == "PUT" and "/shelf/" in p for m, p in h.log)            # 离线：什么都没发
    online(A, url)
    online(B, url)
    sync_wait(A)                                                               # A 先同步：拉不到 B，推出自己的
    assert set(server_doc(h, "dev-a")["entries"]) == {A_, B_}
    sync_wait(B)                                                               # B 拉到 A，合并后推出自己的
    sync_wait(A)                                                               # A 拉到 B
    ids = [A_, B_, C_]
    want = {A_: (False, False), B_: (False, True), C_: (True, False)}          # 甲：B 更晚的取消收藏获胜
    assert flags(A, ids) == want and flags(B, ids) == want
    assert A.books[C_].fav is True and A.books[B_].hidden is True and B.books[A_].fav is False
    assert [b.id for b in sh.filter_books(A.books, "fav")] == [C_] == [b.id for b in sh.filter_books(B.books, "fav")]
    assert not A.push_pending() and not B.push_pending() and not A._unpushed and not B._unpushed
    # 收敛之后没有新的 PUT（幂等）
    n = len([1 for m, p in h.log if m == "PUT" and "/shelf/" in p])
    sync_wait(A)
    sync_wait(B)
    assert len([1 for m, p in h.log if m == "PUT" and "/shelf/" in p]) == n
    # 云端文件只含白名单字段、只含本设备自己的寄存器
    for dev in ("dev-a", "dev-b"):
        doc = server_doc(h, dev)
        assert set(doc) <= {"version", "device_id", "hostname", "user", "entries", "progress"} and doc["device_id"] == dev
        for ent in doc["entries"].values():
            assert all(len(reg) == 2 for k, reg in ent.items() if k in ("fav", "hidden", "meta"))


def test_same_field_conflict_follows_ts_then_device_id(fixed_clock, tmp_path, rb):
    h, url = fixed_clock
    (A_,) = cloud_books(h, "甲.txt")
    t = ms_now() - 60_000
    put_dev(h, "dev-a", {A_: {"fav": [True, t]}})
    put_dev(h, "dev-z", {A_: {"fav": [False, t]}})                             # ts 相同：device_id 大者（dev-z）胜
    v = mk(tmp_path, url, "m", dev="dev-m")
    sync_wait(v)
    assert flags(v, [A_])[A_][0] is False and v.books[A_].fav is False
    put_dev(h, "dev-a", {A_: {"fav": [False, t]}}, mtime=OLD + 1)
    put_dev(h, "dev-z", {A_: {"fav": [True, t]}}, mtime=OLD + 1)
    sync_wait(v)
    assert flags(v, [A_])[A_][0] is True and v.books[A_].fav is True           # 与文件顺序无关，只看 device_id
    put_dev(h, "dev-a", {A_: {"fav": [False, t + 1]}}, mtime=OLD + 2)          # ts 不同：大者胜，不管 device_id
    sync_wait(v)
    assert flags(v, [A_])[A_][0] is False
    # 用户之后的点击总能压过他看到的值（观察合并值的混合时钟）：ts >= 合并值 + 1
    assert v.set_fav([v.books[A_]]) == 1
    assert v._overlay["entries"][A_]["fav"][1] >= t + 2 and flags(v, [A_])[A_][0] is True


# ================================================================== rev + 内容哈希
def test_edit_during_push_keeps_pending_and_dots(server, tmp_path, rb, monkeypatch):
    h, url = server
    A_, B_ = cloud_books(h, "甲.txt", "乙.txt")
    v = mk(tmp_path, url)
    sync_wait(v)
    sent = []
    monkeypatch.setattr(v._cloud, "push_device", lambda cfg, dev, payload, rev, digest: sent.append((rev, digest, payload)))
    assert v.set_fav([v.books[A_]]) == 1
    assert v.books[A_].unpushed is True and v.books[B_].unpushed is False
    assert v.maybe_push() is True and len(sent) == 1
    rev1, h1, _ = sent[0]
    assert v.set_fav([v.books[B_]]) == 1                                       # 推送期间又有编辑
    assert v.maybe_push() is True and len(sent) == 2 and sent[1][1] != h1
    v._on_pushed(rev1, h1, "", None)                                           # 先到的完成通知只覆盖到 rev1
    assert v._state["pushed_hash"] == h1 and v._state["pushed_rev"] == 0       # 当前 rev 已不等于 rev1：不记 pushed_rev
    assert v.push_pending() is True and v._push_timer.isActive()               # 没有被误清
    assert sorted(v._unpushed) == [B_] and v.books[A_].unpushed is False and v.books[B_].unpushed is True
    v._on_pushed(sent[1][0], sent[1][1], "", None)
    assert v.push_pending() is False and v._unpushed == {} and v._state["pushed_rev"] == sent[1][0]
    assert v.books[B_].unpushed is False
    assert v.maybe_push() is False and len(sent) == 2                          # 幂等：没有新内容就不再推


# ================================================================== 自愈（design §4.3）
def _seed_fav_pushed(h, url, tmp_path, name="a", dev="dev-a", n=1):
    rels = [f"书{i}.txt" for i in range(n + 2)]
    ids = cloud_books(h, *rels)
    v = mk(tmp_path, url, name, dev=dev)
    sync_wait(v)
    for i in range(n):
        assert v.set_fav([v.books[ids[i]]]) == 1
    assert v.maybe_push() is True
    settle_push(v)
    assert wait_for(lambda: not v.push_pending())
    return v, ids


def test_selfheal_server_rollback_is_repaired_next_round(fixed_clock, tmp_path, rb):
    h, url = fixed_clock
    v, ids = _seed_fav_pushed(h, url, tmp_path)
    assert server_doc(h, "dev-a")["entries"][ids[0]]["fav"][0] is True
    raw_put(h, dev_path("dev-a"), ov_doc("dev-a"), mtime=OLD + 7)              # 服务器快照回滚：丢了最近的修改
    sync_wait(v)                                                               # 下一轮：戳变了 -> GET -> 云端落后 -> 重推
    assert server_doc(h, "dev-a")["entries"][ids[0]]["fav"][0] is True
    assert not v.push_pending() and v._heal is False and v.books[ids[0]].fav is True
    h.log.clear()
    sync_wait(v)                                                               # 修复后戳已确认：不再 GET 自己的文件、不再 PUT
    assert gets(h, "dev-a.json") == [] and not any(m == "PUT" for m, _p in h.log)


def test_selfheal_deleted_own_file_is_recreated_but_empty_device_does_not_create_one(fixed_clock, tmp_path, rb):
    h, url = fixed_clock
    v, ids = _seed_fav_pushed(h, url, tmp_path)
    raw_del(h, dev_path("dev-a"))                                              # 手工误删 users/<me>/shelf/<id>.json
    sync_wait(v)
    assert dev_path("dev-a") in h.files and server_doc(h, "dev-a")["entries"][ids[0]]["fav"][0] is True
    # 从没有过任何内容的设备：文件不存在不是问题，不创建
    w = mk(tmp_path, url, "fresh", dev="dev-fresh")
    n = len([1 for m, _p in h.log if m == "PUT"])
    sync_wait(w)
    assert dev_path("dev-fresh") not in h.files and len([1 for m, _p in h.log if m == "PUT"]) == n


def test_selfheal_cloned_device_id_merges_and_restores_cloud(fixed_clock, tmp_path, rb):
    h, url = fixed_clock
    v, ids = _seed_fav_pushed(h, url, tmp_path)
    t = ms_now()
    # 另一台克隆了同一个 device_id 的机器只推了它自己的内容（覆盖了云端文件，丢了本机的收藏）
    raw_put(h, dev_path("dev-a"), ov_doc("dev-a", {ids[1]: {"fav": [True, t]}}), mtime=OLD + 9)
    sync_wait(v)
    doc = server_doc(h, "dev-a")
    assert doc["entries"][ids[0]]["fav"][0] is True and doc["entries"][ids[1]]["fav"][0] is True     # 云端恢复为两边的并集
    assert v.books[ids[0]].fav is True and v.books[ids[1]].fav is True                              # 本地也采纳了对方的更新
    assert not v.push_pending()
    # 克隆那一台下一轮也能收敛：不会来回拉锯
    c = mk(tmp_path, url, "clone", dev="dev-a")
    sync_wait(c)
    assert c.books[ids[0]].fav is True and c.books[ids[1]].fav is True
    n = len([1 for m, p in h.log if m == "PUT" and "/shelf/" in p])
    sync_wait(v)
    sync_wait(c)
    assert len([1 for m, p in h.log if m == "PUT" and "/shelf/" in p]) == n


def test_selfheal_corrupt_own_file_is_overwritten(fixed_clock, tmp_path, rb):
    h, url = fixed_clock
    v, ids = _seed_fav_pushed(h, url, tmp_path)
    raw_put(h, dev_path("dev-a"), b"<html>502 bad gateway</html>", mtime=OLD + 3)
    sync_wait(v)
    assert server_doc(h, "dev-a")["entries"][ids[0]]["fav"][0] is True


def test_unchanged_own_file_is_not_refetched(fixed_clock, tmp_path, rb):
    h, url = fixed_clock
    v, ids = _seed_fav_pushed(h, url, tmp_path)
    sync_wait(v)                                                               # 第一次核对：确认戳
    h.log.clear()
    sync_wait(v)
    assert gets(h, "dev-a.json") == []                                         # 戳没变、游标可信：自己的文件不再 GET
    assert v._state["remote_stamp"]["size"] == len(h.files[dev_path("dev-a")])


# ================================================================== 对端文件消失 / 重置
def test_peer_file_vanishing_drops_cache_and_its_flags(server, tmp_path, rb):
    h, url = server
    A_, B_ = cloud_books(h, "甲.txt", "乙.txt")
    b = mk(tmp_path, url, "b", dev="dev-b")
    sync_wait(b)
    b.set_fav([b.books[B_]])
    b.maybe_push()
    settle_push(b)
    a = mk(tmp_path, url, "a", dev="dev-a")
    sync_wait(a)
    assert a.books[B_].fav is True and "dev-b" in a._peers
    cache = ss.peer_cache_path(a.root, "dev-b")
    assert os.path.isfile(cache)
    raw_del(h, dev_path("dev-b"))                                              # 对端文件在服务器上消失（权威的「已删除」）
    sync_wait(a)
    assert not os.path.exists(cache) and "dev-b" not in a._peers
    assert a.books[B_].fav is False and flags(a, [B_])[B_] == (False, False)
    # 重启后冷启动也不会从缓存里把它复活
    a.flush()
    a2 = mk(tmp_path, None, "a")
    assert "dev-b" not in a2._peers and a2.merged_view()["entries"] == {}


def test_reset_is_not_resurrected_by_remote_file_or_caches(fixed_clock, tmp_path, rb):
    h, url = fixed_clock
    v, ids = _seed_fav_pushed(h, url, tmp_path, n=2)
    assert len(server_doc(h, "dev-a")["entries"]) == 2
    put_dev(h, "dev-b", {ids[2]: {"fav": [True, ms_now()]}})                    # 另一台设备的状态：重置只清本设备
    sync_wait(v)
    assert os.path.isfile(ss.peer_cache_path(v.root, "dev-b"))
    with open(os.path.join(sh.peers_dir(v.root), "dev-stale.json"), "wb") as f:                # 一份过期的 peers 缓存（对应的设备文件早就不在了）
        f.write(ov_doc("dev-stale", {ids[3]: {"fav": [True, ms_now()]}}))
    offline(v)                                                                 # 离线重置：云端旧内容还在，之后联网不能把它「自愈」回来
    v.reset_state("grid", "last_read", False)
    wait_for(lambda: not v.scanning)
    settle(v)
    settle_push(v)
    assert v._overlay["entries"] == {} and v._overlay["progress"] == {}
    assert json.load(open(sh.overlay_path(v.root), encoding="utf-8"))["entries"] == {}      # 本地 overlay.json 被截断
    assert os.listdir(sh.peers_dir(v.root)) == [] and v._peers == {}
    st = sh.load_state(v.root)
    assert st["reset_pending"] is True and st["pushed_hash"] == "" and st["remote_stamp"] is None and st["unpushed"] == []
    assert st["device_id"] == "dev-a"
    assert server_doc(h, "dev-a")["entries"]                                    # 云端还没动（离线）
    online(v, url)
    sync_wait(v)
    assert server_doc(h, "dev-a")["entries"] == {} and server_doc(h, "dev-a")["progress"] == {}      # 先把空文件推上去
    assert sh.load_state(v.root)["reset_pending"] is False
    assert v.books[ids[0]].fav is False and v.books[ids[1]].fav is False
    assert v._overlay["entries"] == {}                                          # 云端旧内容没有被采纳
    assert v.books[ids[2]].fav is True                                          # 其它设备（dev-b）的状态是它自己的，不受影响
    sync_wait(v)
    sync_wait(v)
    assert v._overlay["entries"] == {} and server_doc(h, "dev-a")["entries"] == {}
    assert "dev-stale" not in v._peers and not os.path.exists(ss.peer_cache_path(v.root, "dev-stale"))


def test_reset_online_pushes_empty_device_file(fixed_clock, tmp_path, rb):
    h, url = fixed_clock
    v, ids = _seed_fav_pushed(h, url, tmp_path)
    v.reset_state("list", "title", True)
    assert wait_for(lambda: server_doc(h, "dev-a")["entries"] == {} and not v.push_pending())
    settle(v)
    assert v.books[ids[0]].fav is False and v.view_mode == "list"


# ================================================================== 单实例锁：只有持锁者写
def _local_lib(tmp_path, names=("甲.txt", "乙.txt", "丙.txt")):
    d = tmp_path / "lib"
    d.mkdir(exist_ok=True)
    for n in names:
        (d / n).write_text("内容 " + n, encoding="utf-8")
    return str(d)


def test_lock_holder_only_writes(server, tmp_path, rb):
    h, url = server
    lib = _local_lib(tmp_path)
    lock = sh.acquire_lock(str(tmp_path / "shared"))
    assert lock is not None and sh.acquire_lock(str(tmp_path / "shared")) is None
    holder = mk(tmp_path, url, "shared", dev="dev-s", dirs=[lib])
    holder.rescan()
    assert wait_for(lambda: not holder.scanning)
    bids = sorted(holder.books)
    assert holder.set_fav([holder.books[bids[0]]]) == 1
    holder.flush()
    holder.maybe_push()
    settle_push(holder)
    assert wait_for(lambda: dev_path("dev-s") in h.files)
    files = {n: open(os.path.join(sh.shelf_dir(holder.root), n), "rb").read() for n in ("overlay.json", "state.json")}
    puts = len([1 for m, _p in h.log if m in ("PUT", "MKCOL")])
    ro = mk(tmp_path, url, "shared", read_only=True, dirs=[lib])               # 没拿到锁的第二个实例
    ro.rescan()
    assert wait_for(lambda: not ro.scanning)
    assert ro.read_only and ro.books[bids[0]].fav is True                      # 能浏览，也能看到持锁者保存的收藏
    assert ro.set_fav([ro.books[bids[1]]]) == 0 and "只读" in ro._notice       # 不能改
    assert ro.set_hidden([ro.books[bids[1]]]) == 0 and ro.books[bids[1]].hidden is False
    ro.note_progress(list(ro.books[bids[1]].local_paths)[0], 40.0, time.time(), "pc")     # 阅读事件也不写摘要
    assert ro._overlay is None and ro.push_pending() is False and ro.maybe_push() is False
    assert "只读" in ro.status.text()
    sync_wait(ro, push=False)                                                  # 只读实例可以读云端（进度 / 其它设备），但不碰自己的文件
    ro.flush()
    assert ro.flush_cloud(1.0) is True
    ro.reset_state("grid", "last_read", False)                                 # 重置也不动持锁者的数据
    wait_for(lambda: not ro.scanning)
    settle(ro)
    for n, data in files.items():
        assert open(os.path.join(sh.shelf_dir(holder.root), n), "rb").read() == data, n + " 被只读实例改写了"
    assert len([1 for m, _p in h.log if m in ("PUT", "MKCOL")]) == puts        # 只读实例没有发出任何写请求
    assert server_doc(h, "dev-s")["entries"] and holder.books[bids[0]].fav is True
    lock.release()


# ================================================================== 恶意 JSON
def test_malicious_peer_json_is_contained(server, tmp_path, rb):
    h, url = server
    (A_,) = cloud_books(h, "甲.txt")
    now = ms_now()
    evil = {
        "version": 1, "device_id": "../../etc/passwd",                          # 体内的 device_id 被忽略（只取自文件名）
        "hostname": "<b>pwn</b>\x07" + "h" * 500,
        "entries": {
            A_: {"fav": [True, 9 * 10 ** 18], "hidden": ["yes", now],            # 超大 ts -> 截断为最旧；非布尔 -> 跳过
                 "meta": [{"title": "<script>alert(1)</script>\x00\x1b[31m" + "长" * 600, "fp": "../x"}, now],
                 "status": [{"__proto__": 1}, now], "tags": ["a" * 1000]},
            "remote:../../x.txt": {"fav": [True, now]},                          # 非法 book_id
            "remote:乙.txt": {"fav": [True, "abc"], "hidden": [True, 1e400], "meta": [{"title": 5}, -5]},   # 非数字 / 非有限 / 负 ts / 非字符串标题
            "remote:丙.txt": {"fav": [1, now]},                                  # 整数不是布尔
            "bogus": 5,
        },
        "progress": {A_: {"percent": "NaN", "updated": 5}, "remote:丁.txt": {"percent": 1e999, "updated": now}},
    }
    put_dev(h, "dev-evil", raw=json.dumps(evil).encode("utf-8"))
    put_dev(h, "dev-huge", raw=b"[" * 100000)                                    # 嵌套炸弹 / 非法
    put_dev(h, "dev-array", raw=b"[1,2,3]")
    raw_put(h, f"/dav/users/{USER}/shelf/..%2f..%2fevil.json".replace("%2f", "_"), ov_doc("x"))   # 路径字符的文件名
    for bad in ("a b", "a;b", "dev id", "a\\b", "..a", ".hidden", "CON"):
        raw_put(h, f"/dav/users/{USER}/shelf/{bad}.json", ov_doc("whatever", {A_: {"fav": [True, now]}}))
    v = mk(tmp_path, url, "v", dev="dev-v")
    sync_wait(v)                                                                 # 不抛、不崩
    assert "dev-evil" in v._peers and not any(d for d in v._peers if d not in ("dev-evil",))
    ent = v.merged_view()["entries"]
    assert set(ent) == {A_}                                                      # 只剩合法 id 的合法字段
    assert "hidden" not in ent[A_]                                               # 非布尔的 hidden 被跳过
    assert ent[A_]["fav"][1] == sh.CLAMPED_TS                                    # 9e18 的 ts 被截断成最旧值
    title = ent[A_]["meta"][0]["title"]
    assert len(title) <= sh.MAX_STR and "\x00" not in title and "\x1b" not in title
    assert "<script>" in title                                                   # 不做 HTML 过滤：靠纯文本显示（见下一个测试）
    assert "fp" not in ent[A_]["meta"][0]                                        # fp 不是安全名字：丢弃
    assert v.clock_anomalies() == ["dev-evil"] and "时钟异常" in v.status.text()
    # 用户的点击能压过被截断的异常值；未知字段（status / tags）不影响任何东西，也永远不会被上传
    assert v.books[A_].fav is True
    assert v.set_fav([v.books[A_]], False) == 1 and flags(v, [A_])[A_][0] is False and v.books[A_].fav is False
    v.maybe_push()
    settle_push(v)
    assert wait_for(lambda: dev_path("dev-v") in h.files and not v.push_pending())
    mine = server_doc(h, "dev-v")
    assert set(mine["entries"]) == {A_} and set(mine["entries"][A_]) <= {"fav", "hidden", "meta"}
    assert mine["entries"][A_]["fav"][0] is False and "pwn" not in json.dumps(mine) and "script" not in json.dumps(mine)
    # 事关路径的名字：既没有被读取，也没有被拼进本地路径
    assert not [p for p in gets(h) if "evil" in p and p.endswith("evil.json") and "dev-evil" not in p]
    for bad in ("a b", "a;b", "dev id", "..a", ".hidden", "CON"):
        assert not [p for p in gets(h) if p.endswith(f"/{bad}.json")]
    assert sorted(os.listdir(sh.peers_dir(v.root))) == ["dev-evil.json"]


def test_hostile_text_is_never_rendered_as_rich_text(server, tmp_path, rb):
    h, url = server
    (A_,) = cloud_books(h, "甲.txt")
    put_dev(h, "dev-evil", {A_: {"meta": [{"title": "<img src=x onerror=1><b>粗</b>"}, ms_now()], "fav": [True, ms_now()]}})
    v = mk(tmp_path, url, "v", dev="dev-v")
    sync_wait(v)
    b = v.books[A_]
    assert b.title == "<img src=x onerror=1><b>粗</b>"                           # 书名以字面文本保留
    assert v.status.textFormat().name == "PlainText" and v.continue_label.textFormat().name == "PlainText"
    assert v.empty_label.textFormat().name == "PlainText"
    tip = sv.plain_tip(b.title)                                                  # 工具提示是转义后的富文本壳，里面没有活的标签
    assert "<img" not in tip and "<b>" not in tip and "&lt;img" in tip


# ================================================================== 未启用 WebDAV：全部空操作，但本机收藏仍然可用
def test_disabled_webdav_is_a_noop_but_local_flags_work(tmp_path, rb):
    lib = _local_lib(tmp_path)
    v = mk(tmp_path, None, "a", dirs=[lib])
    v.rescan()
    assert wait_for(lambda: not v.scanning)
    bids = sorted(v.books)
    assert v.cloud_enabled() is False
    assert v.set_fav([v.books[bids[0]]]) == 1 and v.set_hidden([v.books[bids[1]]]) == 1
    assert v.push_pending() is False and v.pending_count() == 0 and v._unpushed == {}
    assert v.maybe_push() is False and v.sync_progress() is False and v.sync_cloud(force=True) is False
    assert v.flush_cloud(1.0) is True
    assert v._cloud._running is False and v._cloud._prog_running is False and v._cloud._push_running is False
    assert v._cloud.sync_progress({}, v.root, "d", {}, []) is None and v._cloud.push_device({}, "d", b"", 0, "") is None
    assert "待同步" not in v.status.text() and "云端" not in v.status.text()
    v.flush()
    ov = json.load(open(sh.overlay_path(v.root), encoding="utf-8"))
    assert ov["entries"][bids[0]]["fav"][0] is True and ov["entries"][bids[1]]["hidden"][0] is True
    assert sh.load_state(v.root)["pushed_hash"] == ""
    # 重启：收藏 / 隐藏仍在，并反映到智能书架
    v2 = mk(tmp_path, None, "a", dirs=[lib])
    v2.rescan()
    assert wait_for(lambda: not v2.scanning)
    assert [b.id for b in sh.filter_books(v2.books, "fav")] == [bids[0]]
    assert bids[1] not in [b.id for b in sh.filter_books(v2.books, "all")]
    assert [b.id for b in sh.filter_books(v2.books, "hidden")] == [bids[1]]
    v2.set_hidden([v2.books[bids[1]]], False)
    assert bids[1] in [b.id for b in sh.filter_books(v2.books, "all")]


def test_invalid_user_name_blocks_cloud_flags_but_not_requests(server, tmp_path, rb):
    h, url = server
    lib = _local_lib(tmp_path)
    v = mk(tmp_path, url, "a", dirs=[lib], cfg=cfg_for(url, user="../evil"))
    v.rescan()
    assert wait_for(lambda: not v.scanning)
    b = v.books[sorted(v.books)[0]]
    assert v.set_fav([b]) == 0 and "用户名" in v._notice
    assert not any(m in ("PUT", "MKCOL") for m, _p in h.log) and not any("evil" in p for _m, p in h.log)


# ================================================================== PUT 请求体里没有本机路径 / 密钥
def test_put_bodies_contain_no_local_paths_or_secrets(server, tmp_path, rb):
    h, url = server
    secret_dir = tmp_path / "私人目录" / "sk-secret-dir"
    secret_dir.mkdir(parents=True)
    (secret_dir / "秘密书.txt").write_text("内容", encoding="utf-8")
    (C_,) = cloud_books(h, "公共书.txt")
    bodies = []
    h.put_hooks.append(lambda p, d: bodies.append((p, d)))
    cfg = cfg_for(url, api_key="sk-live-ABCDEF", webdav_password="hunter2-pass",
                  api_base="https://api.example.com", proxy="http://proxy.local:3128")
    v = mk(tmp_path, url, "a", dev="dev-p", cfg=cfg, dirs=[str(secret_dir)])
    sync_wait(v)
    local_id = next(b for b in v.books if b.startswith("local:"))
    assert v.set_fav([v.books[local_id], v.books[C_]]) == 2
    assert v.set_hidden([v.books[C_]]) == 1
    v.note_progress(v.books[local_id].local_paths[0], 12.5, time.time(), "pc")
    assert v.maybe_push() is True
    settle_push(v)
    assert wait_for(lambda: not v.push_pending())
    pushed = [(p, d) for p, d in bodies if "/shelf/" in p]
    assert pushed
    for p, d in pushed:
        txt = d.decode("utf-8")
        for needle in (str(tmp_path), "私人目录", "sk-secret-dir", "sk-live-ABCDEF", "hunter2-pass",
                       "api.example.com", "proxy.local", "api_key", "webdav_password", "webdav_user", "\\\\", "C:/", "C:\\\\"):
            assert needle not in txt, (needle, txt)
        doc = json.loads(txt)
        assert set(doc) <= {"version", "device_id", "hostname", "user", "entries", "progress"}
        assert set(doc["entries"]) == {local_id, C_} and doc["entries"][local_id]["meta"][0]["title"] == "秘密书"
        assert not any(k.startswith("_") for ent in doc["entries"].values() for k in ent)


# ================================================================== 首次写入确认 / 关闭时 flush
def test_first_write_confirmation_covers_flags(server, tmp_path, rb):
    h, url = server
    (A_,) = cloud_books(h, "甲.txt")
    v = mk(tmp_path, url, "a", dev="dev-a", confirm=None)
    sync_wait(v)
    answers = iter([None, False])
    asked = []
    v.confirm_push = lambda: asked.append(1) or next(answers)
    v.set_fav([v.books[A_]])
    assert v.maybe_push() is False and v._push_declined is False                 # 现在不问
    assert v.maybe_push() is False and v._push_declined is True                  # 拒绝
    assert v.maybe_push() is False and len(asked) == 2
    assert dev_path("dev-a") not in h.files and v.books[A_].fav is True          # 没确认之前云端什么都没写；本机编辑仍保留
    assert v.push_pending() is True and v.books[A_].unpushed is True
    assert v._state["confirmed_upload"] is False
    assert "书名" in sv.CONFIRM_TEXT and "收藏" in sv.CONFIRM_TEXT and "隐藏" in sv.CONFIRM_TEXT
    assert "不会上传：本机文件路径、账号密码" in sv.CONFIRM_TEXT
    v._push_declined = False
    v.confirm_push = lambda: True
    assert v.maybe_push() is True
    settle_push(v)
    assert wait_for(lambda: not v.push_pending()) and v._state["confirmed_upload"] is True
    assert server_doc(h, "dev-a")["entries"][A_]["fav"][0] is True and v.books[A_].unpushed is False


def test_flush_cloud_on_close_is_bounded_and_never_prompts(server, tmp_path, rb):
    h, url = server
    (A_,) = cloud_books(h, "甲.txt")
    v = mk(tmp_path, url, "a", dev="dev-a", confirm=None)
    sync_wait(v)
    v.confirm_push = lambda: (_ for _ in ()).throw(AssertionError("关闭窗口时不能弹确认框"))
    v.set_fav([v.books[A_]])
    assert v.flush_cloud(1.0) is True                                            # 没确认过上传：什么都不推、不弹窗
    assert dev_path("dev-a") not in h.files
    v._state["confirmed_upload"] = True
    t0 = time.time()
    assert v.flush_cloud(3.0) is True                                            # 确认过：有界地推一次
    assert time.time() - t0 < 3.0
    assert server_doc(h, "dev-a")["entries"][A_]["fav"][0] is True
    assert v._state["pushed_hash"] == sh.wire_hash(v._overlay) and not v.push_pending()   # 排队的完成信号已投递、状态已记录
    assert sh.load_state(v.root)["pushed_hash"] == v._state["pushed_hash"]       # 并已落盘
    # 云端不可达：不会卡住关闭
    v.set_hidden([v.books[A_]])
    offline(v)
    t0 = time.time()
    assert v.flush_cloud(2.0) is False and time.time() - t0 < 4.0
    assert sh.load_overlay(v.root)["entries"][A_]["hidden"][0] is True           # 但本机已落盘，下次启动补推


# ================================================================== 单飞 / 时钟偏差
def test_push_and_pull_never_interleave(server, tmp_path, rb):
    h, url = server
    (A_,) = cloud_books(h, "甲.txt")
    v = mk(tmp_path, url, "a", dev="dev-a")
    sync_wait(v)
    v.set_fav([v.books[A_]])
    slow = threading.Event()

    def hook(p, d):
        if p.endswith("dev-a.json"):
            slow.set()
            time.sleep(0.4)
    h.put_hooks.append(hook)
    h.log.clear()
    assert v.maybe_push() is True
    assert slow.wait(5)
    v.sync_progress()                                                            # 推送正在 PUT 时又来一轮进度同步：要排队等 IO 锁
    settle_push(v)
    settle(v)
    i = next(k for k, (m, p) in enumerate(h.log) if m == "PUT" and p.endswith("dev-a.json"))
    assert [m for m, _p in h.log[i + 1:i + 3]] == ["PROPFIND", "GET"], h.log[i:i + 6]     # PUT 之后紧跟推送自己的读回校验


def test_many_sync_requests_coalesce(server, tmp_path, rb):
    h, url = server
    cloud_books(h, "甲.txt")
    v = mk(tmp_path, url, "a", dev="dev-a")
    sync_wait(v)
    g0 = v._cloud.progress_generation
    for _ in range(8):
        v.sync_progress()
    settle(v)
    assert 1 <= v._cloud.progress_generation - g0 <= 2                           # 后到者合并成「再跑一次」


def test_clock_skew_estimate_is_optional_and_only_trusted_when_large(server, tmp_path, rb):
    h, url = server
    put_dev(h, "dev-x")                                                          # 目录存在，PROPFIND 才有响应头
    r = ss.pull_peers(WebDavClient(url), str(tmp_path / "r"), USER, "dev-a", None)
    assert r.skew_ms is not None and abs(r.skew_ms) < 5000                       # 同一台机器：偏差只是秒级舍入
    (A_,) = cloud_books(h, "甲.txt")
    v = mk(tmp_path, url, "a", dev="dev-a")
    sync_wait(v)
    v._note_skew(900)
    assert v._state["clock_skew_ms"] == 0 and v._clock_note == ""                # 小偏差不采信
    v._note_skew(10 * 60 * 1000)
    assert v._state["clock_skew_ms"] == 600_000 and "10 分钟" in v._clock_note and "自动对时" in v.status.text()
    before = ms_now()
    v.set_fav([v.books[A_]])
    assert v._overlay["entries"][A_]["fav"][1] >= before + 590_000               # 时间戳按服务器时间写
    v._note_skew(-10 ** 12)
    assert v._state["clock_skew_ms"] == -sh.FUTURE_LIMIT_MS
    v._note_skew(0)
    assert v._state["clock_skew_ms"] == 0 and v._clock_note == ""


# ================================================================== 界面层：智能书架 / 多选 / 角标
def test_fav_shelf_hidden_shelf_multiselect_and_badges(server, tmp_path, rb):
    h, url = server
    ids = cloud_books(h, "甲.txt", "乙.txt", "丙.txt", "丁.txt")
    v = mk(tmp_path, url, "a", dev="dev-a")
    sync_wait(v)
    texts = lambda: [v.sidebar.item(i).text() for i in range(v.sidebar.count())]    # noqa: E731
    assert any(t.startswith("收藏") and t.endswith("0") for t in texts())
    assert not any(t.startswith("我隐藏的") for t in texts())
    v.set_shelf("all")
    assert v.model.rowCount() == 4
    sel = v.grid.selectionModel()
    sel.select(v.model.index(0, 0), QItemSelectionModel.SelectionFlag.Select)
    sel.select(v.model.index(2, 0), QItemSelectionModel.SelectionFlag.Select)
    picked = v.selected_books(v.grid)
    assert len(picked) == 2 and [b.id for b in picked] == [v.model.book_at(0).id, v.model.book_at(2).id]
    assert v.set_fav(picked) == 2 and v.set_fav(picked) == 0                      # 幂等
    assert any(t.startswith("收藏") and t.endswith("2") for t in texts())
    counts = sv.count_shelves(v.books)
    assert counts["fav"] == 2 and counts["hidden"] == 0
    v.set_shelf("fav")
    assert sorted(b.id for b in v.visible_books()) == sorted(b.id for b in picked)
    assert all(b.unpushed for b in picked)                                        # 橙点：已改、未推送
    assert sv.flags_text(picked[0], True) == " ★ ●" and sv.flags_text(picked[0], False) == " ★"
    assert "★" in v.model.data(v.model.index(0, 5)) and "●" in v.model.data(v.model.index(0, 5))
    # 画一遍卡片：星标 / 橙点不抛异常
    pm = QPixmap(sv.CARD_W, sv.CARD_H)
    pm.fill()
    p = QPainter(pm)
    opt = QStyleOptionViewItem()
    opt.rect = pm.rect()
    opt.font = v.font()
    opt.palette = v.palette()
    v.grid.itemDelegate().paint(p, opt, v.model.index(0, 0))
    p.end()
    # 隐藏：从「全部」消失，出现「我隐藏的」书架，可取消
    assert v.set_hidden([picked[0]]) == 1
    v.set_shelf("all")
    assert picked[0].id not in [b.id for b in v.visible_books()]
    assert any(t.startswith("我隐藏的") and t.endswith("1") for t in texts())
    assert picked[0].id not in [b.id for b in sh.filter_books(v.books, "fav")]    # 隐藏的书不进「收藏」书架
    v.set_shelf("hidden")
    assert [b.id for b in v.visible_books()] == [picked[0].id]
    assert v.set_hidden(v.visible_books(), False) == 1
    v.set_shelf("fav")
    assert len(v.visible_books()) == 2
    # 推送后橙点消失
    assert v.maybe_push() is True
    settle_push(v)
    assert wait_for(lambda: not v.push_pending() and not any(b.unpushed for b in v.books.values()))
    assert sv.flags_text(picked[0], True) == " ★"
    assert "●" not in v.model.data(v.model.index(0, 5))


def test_flags_survive_cold_start_before_scan_and_rescans(server, tmp_path, rb):
    h, url = server
    ids = cloud_books(h, "甲.txt", "乙.txt")
    v = mk(tmp_path, url, "a", dev="dev-a")
    sync_wait(v)
    v.set_fav([v.books[ids[0]]])
    v.set_hidden([v.books[ids[1]]])
    v.maybe_push()
    settle_push(v)
    v.flush()
    v.rescan(force=True)
    assert wait_for(lambda: not v.scanning)
    assert v.books[ids[0]].fav is True and v.books[ids[1]].hidden is True        # 重建后仍带着标记（来自合并视图）
    v2 = mk(tmp_path, url, "a")                                                  # 冷启动：index.json 缓存先画界面，标记马上就在
    assert v2.books[ids[0]].fav is True and v2.books[ids[1]].hidden is True
    assert [b.id for b in sh.filter_books(v2.books, "fav")] == [ids[0]]


def test_other_device_flags_arrive_without_losing_own_pending(server, tmp_path, rb):
    h, url = server
    A_, B_ = cloud_books(h, "甲.txt", "乙.txt")
    v = mk(tmp_path, url, "a", dev="dev-a")
    sync_wait(v)
    offline(v)
    v.set_hidden([v.books[A_]])
    put_dev(h, "dev-b", {B_: {"fav": [True, ms_now()]}})
    online(v, url)
    sync_wait(v)
    assert v.books[B_].fav is True and v.books[A_].hidden is True
    assert server_doc(h, "dev-a")["entries"][A_]["hidden"][0] is True and not v.push_pending()


def test_shelf_dir_has_no_stray_files_and_to_wire_whitelist(server, tmp_path, rb):
    h, url = server
    (A_,) = cloud_books(h, "甲.txt")
    v = mk(tmp_path, url, "a", dev="dev-a")
    sync_wait(v)
    v.set_fav([v.books[A_]])
    v.maybe_push()
    settle_push(v)
    v.flush()
    names = sorted(os.listdir(sh.shelf_dir(v.root)))
    assert not [n for n in names if n.endswith(".tmp") or ".bad-" in n]
    ov = json.load(open(sh.overlay_path(v.root), encoding="utf-8"))
    assert set(ov) <= {"version", "device_id", "hostname", "user", "entries", "progress"}


def test_context_menu_offers_fav_hide_and_applies_to_multiselection(server, tmp_path, rb, monkeypatch):
    h, url = server
    ids = cloud_books(h, "甲.txt", "乙.txt", "丙.txt")
    v = mk(tmp_path, url, "a", dev="dev-a")
    sync_wait(v)
    v.resize(900, 600)
    v.show()
    wait_for(lambda: v.grid.visualRect(v.model.index(0, 0)).isValid() and v.grid.visualRect(v.model.index(0, 0)).width() > 0)
    seen = []
    chosen = {}

    def fake_exec(menu, _gpos):
        seen.append([x.text() for x in menu.actions() if x.text()])
        for x in menu.actions():
            if x.text() == chosen.get("pick"):
                x.trigger()
    v.run_menu = fake_exec
    view = v.grid
    sel = view.selectionModel()
    sel.clear()
    sel.select(v.model.index(0, 0), QItemSelectionModel.SelectionFlag.Select)
    sel.select(v.model.index(1, 0), QItemSelectionModel.SelectionFlag.Select)
    pos = view.visualRect(v.model.index(1, 0)).center()                         # 右键点在选区之内：作用于整个选区
    chosen["pick"] = "收藏"
    v._show_menu(view, pos)
    assert "收藏" in seen[-1] and any(t.startswith("从我的书架隐藏") for t in seen[-1])
    assert [b.fav for b in map(v.model.book_at, range(3))] == [True, True, False]
    chosen["pick"] = None
    v._show_menu(view, pos)
    assert "取消收藏" in seen[-1]                                               # 都已收藏：菜单变成「取消收藏」
    third = v.model.book_at(2).id
    pos3 = view.visualRect(v.model.index(2, 0)).center()                        # 右键点在选区之外：只作用于被点的那一本
    chosen["pick"] = "从我的书架隐藏（只隐藏，不删文件）"
    v._show_menu(view, pos3)
    hidden = [b.id for b in v.books.values() if b.hidden]
    assert hidden == [third] and v.books[third].fav is False and all(v.books[i].fav for i in ids if i != third)
    # 只读实例：没有收藏 / 隐藏菜单项
    ro = mk(tmp_path, url, "a", read_only=True)
    ro.resize(900, 600)
    ro.show()
    ro.rescan()
    assert wait_for(lambda: not ro.scanning and ro.model.rowCount() > 0)
    wait_for(lambda: ro.grid.visualRect(ro.model.index(0, 0)).width() > 0)
    ro.run_menu = fake_exec
    ro._show_menu(ro.grid, ro.grid.visualRect(ro.model.index(0, 0)).center())
    assert "收藏" not in seen[-1] and "取消收藏" not in seen[-1] and not any("隐藏" in t for t in seen[-1]) and "打开" in seen[-1]
