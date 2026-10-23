"""阶段 4：进度索引（设备文件摘要 + 逐书补缺）、「继续阅读」条、本设备摘要的写入与推送。
纯逻辑部分用进程内 fake WebDAV；界面部分用 offscreen Qt 在模型 / 信号层验证（真实点击 / 外观无法在此验证）。"""
import json
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from pyreader import shelf as sh  # noqa: E402
from pyreader import shelfsync as ss  # noqa: E402
from pyreader import shelfview as sv  # noqa: E402
from pyreader import sync  # noqa: E402
from pyreader.webdav import DavEntry, WebDavClient, WebDavError  # noqa: E402

OLD = 1000
T0 = 1_700_000_000          # 固定的「服务器时间」：mtime = int(T0)
USER = "gary"


@pytest.fixture(scope="module", autouse=True)
def qapp():
    return QApplication.instance() or QApplication([])


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
    """直接往 fake 服务器放文件（不经 HTTP；mtime 可控），自动建父目录。"""
    parts = path.strip("/").split("/")
    for i in range(1, len(parts)):
        h.dirs.add("/" + "/".join(parts[:i]))
    h.files[path] = data
    h.mtimes[path] = mtime
    h.etags[path] = '"e%d-%d"' % (next(h.counter), len(data))


def dev_doc(dev, host=None, progress=None):
    d = {"version": 1, "device_id": dev, "entries": {},
         "progress": {bid: {"percent": p, "updated": u} for bid, (p, u) in (progress or {}).items()}}
    if host:
        d["hostname"] = host
    return json.dumps(d, ensure_ascii=False).encode("utf-8")


def put_dev(h, dev, host=None, progress=None, mtime=OLD, user=USER):
    raw_put(h, f"/dav/users/{user}/shelf/{dev}.json", dev_doc(dev, host, progress), mtime)


def gets(h, suffix=""):
    return [p for m, p in h.log if m == "GET" and p.endswith(suffix)]


def n_requests(h):
    return len(h.log)


@pytest.fixture
def home(tmp_path):
    return str(tmp_path / "shelfroot")


# ================================================================== 游标规则（纯函数）
def ent(mtime=OLD, size=10, etag='"a"'):
    return DavEntry("x.json", False, size, float(mtime), etag)


def cur(mtime=OLD, size=10, etag='"a"', fetched_at=OLD + 100):
    return {"mtime": float(mtime), "size": size, "etag": etag, "fetched_at": float(fetched_at)}


def test_needs_fetch_cursor_rules():
    assert ss.needs_fetch(None, ent()) is True                            # 没有游标
    assert ss.needs_fetch(cur(), ent()) is False                          # 戳全同、游标可信
    assert ss.needs_fetch(cur(etag='"b"'), ent()) is True                 # etag 变了
    assert ss.needs_fetch(cur(size=11), ent()) is True                    # 大小变了
    assert ss.needs_fetch(cur(mtime=OLD - 5), ent()) is True              # mtime 变了
    assert ss.needs_fetch(cur(fetched_at=OLD + 1.5), ent()) is True       # racy：抓取时距 mtime < 2 秒
    assert ss.needs_fetch(cur(fetched_at=OLD + 2.0), ent()) is False      # 刚好够 2 秒
    assert ss.needs_fetch(cur(fetched_at=OLD - 3), ent()) is True         # mtime 在抓取时刻之后（时钟偏差）：不可信
    assert ss.needs_fetch({"mtime": OLD, "size": 10, "etag": '"a"'}, ent()) is True    # 没有 fetched_at
    assert ss.needs_fetch(cur(etag=""), ent(etag="")) is False            # 服务器不给 etag：靠 mtime + size
    assert ss.needs_fetch(cur(etag="", mtime=0), ent(etag="", mtime=0)) is True        # 什么戳都不给：永远重新 GET
    assert ss.needs_fetch(cur(etag=""), ent(etag='"new"')) is True        # 服务器开始给 etag 了


# ================================================================== pull_peers
def test_pull_peers_fetches_then_trusts_cursor(server, home):
    h, url = server
    put_dev(h, "gary-phone-1", "gary-phone", {"remote:a.txt": (37.0, 5000.0)})
    put_dev(h, "gary-pad-2", "gary-pad", {"remote:b.txt": (80.0, 6000.0)})
    put_dev(h, "gary-pc-3", "gary-pc", {"remote:c.txt": (5.0, 7000.0)})      # 本设备自己的文件：不读
    c = WebDavClient(url)
    r1 = ss.pull_peers(c, home, USER, "gary-pc-3", None)
    assert sorted(r1.views) == ["gary-pad-2", "gary-phone-1"] and sorted(r1.fetched) == ["gary-pad-2", "gary-phone-1"]
    assert r1.requests == 3 and n_requests(h) == 3                          # 1 次 PROPFIND + 每个其它设备 1 次 GET
    assert not any("gary-pc-3" in p for p in gets(h))
    assert os.path.isfile(ss.peer_cache_path(home, "gary-phone-1")) and os.path.isfile(ss.peer_cache_path(home, "gary-pad-2"))
    assert set(r1.cursors["peers"]) == {"gary-phone-1", "gary-pad-2"} and r1.cursors["rounds_since_full"] == 1
    h.log.clear()
    r2 = ss.pull_peers(c, home, USER, "gary-pc-3", r1.cursors)               # 游标可信：只剩一次 PROPFIND
    assert [m for m, _p in h.log] == ["PROPFIND"] and sorted(r2.unchanged) == ["gary-pad-2", "gary-phone-1"]
    assert not r2.fetched and r2.views["gary-phone-1"]["progress"]["remote:a.txt"]["percent"] == 37.0
    # 对端改了内容（etag / mtime 变）：重新 GET
    put_dev(h, "gary-phone-1", "gary-phone", {"remote:a.txt": (55.0, 9000.0)}, mtime=OLD + 50)
    h.log.clear()
    r3 = ss.pull_peers(c, home, USER, "gary-pc-3", r2.cursors)
    assert r3.fetched == ["gary-phone-1"] and r3.views["gary-phone-1"]["progress"]["remote:a.txt"]["percent"] == 55.0
    assert len(gets(h)) == 1


def test_racy_two_puts_in_same_second_are_not_lost(server, home):
    """服务器 mtime 只有 1 秒精度、不给 etag：同一秒内两次等长 PUT 的戳完全相同，靠 racy 规则不丢更新。"""
    h, url = server
    h.no_etag = True
    h.now = staticmethod(lambda: T0 + 0.2)
    c = WebDavClient(url)
    c.mkdirs(f"users/{USER}/shelf")
    doc = lambda pct: dev_doc("gary-phone-1", "p", {"remote:a.txt": (pct, 5000.0)})    # noqa: E731
    c.put(f"users/{USER}/shelf/gary-phone-1.json", doc(11.0))
    r1 = ss.pull_peers(c, home, USER, "me-1", None, now=T0 + 0.8)             # 抓取时距 mtime 只有 0.8 秒：游标 racy
    assert r1.views["gary-phone-1"]["progress"]["remote:a.txt"]["percent"] == 11.0
    c.put(f"users/{USER}/shelf/gary-phone-1.json", doc(22.0))                 # 同一秒内第二次 PUT，大小相同
    assert h.mtimes[f"/dav/users/{USER}/shelf/gary-phone-1.json"] == T0
    r2 = ss.pull_peers(c, home, USER, "me-1", r1.cursors, now=T0 + 0.9)       # 戳完全相同，但游标是 racy 的 -> 必须重新 GET
    assert r2.fetched == ["gary-phone-1"]
    assert r2.views["gary-phone-1"]["progress"]["remote:a.txt"]["percent"] == 22.0
    # 抓取时刻仍在同一秒的 2 秒窗口内：游标仍 racy，继续重新 GET；够 2 秒之后那次 GET 才产生可信游标
    r3 = ss.pull_peers(c, home, USER, "me-1", r2.cursors, now=T0 + 10)
    assert r3.fetched == ["gary-phone-1"]
    r4 = ss.pull_peers(c, home, USER, "me-1", r3.cursors, now=T0 + 20)
    assert r4.unchanged == ["gary-phone-1"] and not r4.fetched


def test_forced_full_fetch_every_n_rounds(server, home):
    h, url = server
    h.no_etag = True
    c = WebDavClient(url)
    put_dev(h, "gary-phone-1", "p", {"remote:a.txt": (11.0, 5000.0)}, mtime=OLD)
    r = ss.pull_peers(c, home, USER, "me-1", None)
    # 对端用同样的 mtime / size 偷偷换了内容（服务器戳不可靠）：游标可信时看不到……
    raw_put(h, f"/dav/users/{USER}/shelf/gary-phone-1.json", dev_doc("gary-phone-1", "p", {"remote:a.txt": (22.0, 5000.0)}), OLD)
    r = ss.pull_peers(c, home, USER, "me-1", r.cursors)
    assert not r.fetched and r.views["gary-phone-1"]["progress"]["remote:a.txt"]["percent"] == 11.0
    # ……每 FULL_EVERY 轮强制全量 GET 一次，兜底补上
    cursors, seen_at, n = r.cursors, None, 2                                  # 已经拉了 2 轮
    while n < 3 * ss.FULL_EVERY:
        r = ss.pull_peers(c, home, USER, "me-1", cursors)
        n += 1
        cursors = r.cursors
        if r.fetched:
            seen_at = n
            break
    assert seen_at == ss.FULL_EVERY and r.views["gary-phone-1"]["progress"]["remote:a.txt"]["percent"] == 22.0
    assert r.cursors["rounds_since_full"] == 0
    # force_full 参数立即全量
    raw_put(h, f"/dav/users/{USER}/shelf/gary-phone-1.json", dev_doc("gary-phone-1", "p", {"remote:a.txt": (33.0, 5000.0)}), OLD)
    r = ss.pull_peers(c, home, USER, "me-1", r.cursors, force_full=True)
    assert r.views["gary-phone-1"]["progress"]["remote:a.txt"]["percent"] == 33.0


def test_corrupt_peer_file_is_skipped_and_last_good_copy_kept(server, home):
    h, url = server
    c = WebDavClient(url)
    put_dev(h, "gary-phone-1", "p", {"remote:a.txt": (40.0, 5000.0)})
    r = ss.pull_peers(c, home, USER, "me-1", None)
    assert r.views["gary-phone-1"]["progress"]["remote:a.txt"]["percent"] == 40.0
    p = f"/dav/users/{USER}/shelf/gary-phone-1.json"
    for i, bad in enumerate((b"{not json", b"[1, 2, 3]", b'{"version": 99, "device_id": "x"}', b"\xff\xfe\x00", b"[" * 5000)):
        raw_put(h, p, bad, OLD + 10 + i)
        h.log.clear()
        r = ss.pull_peers(c, home, USER, "me-1", r.cursors)
        assert r.corrupt == ["gary-phone-1"], bad
        assert r.views["gary-phone-1"]["progress"]["remote:a.txt"]["percent"] == 40.0       # 保留上次好副本
        h.log.clear()
        r = ss.pull_peers(c, home, USER, "me-1", r.cursors)                                  # 同一份坏内容不重复 GET
        assert not gets(h) and r.unchanged == ["gary-phone-1"]
    assert ss.load_peer_view(home, "gary-phone-1")["progress"]["remote:a.txt"]["percent"] == 40.0
    put_dev(h, "gary-phone-1", "p", {"remote:a.txt": (41.0, 5001.0)}, mtime=OLD + 99)       # 修好了：恢复
    r = ss.pull_peers(c, home, USER, "me-1", r.cursors)
    assert r.views["gary-phone-1"]["progress"]["remote:a.txt"]["percent"] == 41.0


class FlakyClient(WebDavClient):
    fail_get = ()

    def get_ex(self, path, *a, **k):
        if any(path.endswith(s) for s in self.fail_get):
            raise WebDavError("boom", status=500)
        return super().get_ex(path, *a, **k)


def test_get_failure_keeps_cache_and_cursor(server, home):
    h, url = server
    put_dev(h, "gary-phone-1", "p", {"remote:a.txt": (40.0, 5000.0)})
    c = FlakyClient(url)
    r = ss.pull_peers(c, home, USER, "me-1", None)
    old_cur = dict(r.cursors["peers"]["gary-phone-1"])
    put_dev(h, "gary-phone-1", "p", {"remote:a.txt": (60.0, 6000.0)}, mtime=OLD + 5)
    c.fail_get = ("gary-phone-1.json",)
    r2 = ss.pull_peers(c, home, USER, "me-1", r.cursors)
    assert r2.failed == ["gary-phone-1"] and not r2.fetched
    assert r2.views["gary-phone-1"]["progress"]["remote:a.txt"]["percent"] == 40.0          # 上次好副本
    assert r2.cursors["peers"]["gary-phone-1"] == old_cur                                    # 游标不动：下一轮会再试
    c.fail_get = ()
    r3 = ss.pull_peers(c, home, USER, "me-1", r2.cursors)
    assert r3.fetched == ["gary-phone-1"] and r3.views["gary-phone-1"]["progress"]["remote:a.txt"]["percent"] == 60.0


def test_cache_written_before_cursor(server, home, monkeypatch):
    """写缓存失败时游标不能前进（否则下一轮会信任一个不存在的缓存）。"""
    h, url = server
    put_dev(h, "gary-phone-1", "p", {"remote:a.txt": (40.0, 5000.0)})
    monkeypatch.setattr(ss, "save_json", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    r = ss.pull_peers(WebDavClient(url), home, USER, "me-1", None)
    assert r.failed == ["gary-phone-1"] and "gary-phone-1" not in r.cursors["peers"] and not r.views


def test_disappeared_device_file_drops_cache(server, home):
    h, url = server
    put_dev(h, "gary-phone-1", "p", {"remote:a.txt": (40.0, 5000.0)})
    put_dev(h, "gary-pad-2", "q", {"remote:b.txt": (50.0, 5000.0)})
    c = WebDavClient(url)
    r = ss.pull_peers(c, home, USER, "me-1", None)
    assert sorted(ss.load_cached_peers(home)) == ["gary-pad-2", "gary-phone-1"]
    del h.files[f"/dav/users/{USER}/shelf/gary-pad-2.json"]
    r = ss.pull_peers(c, home, USER, "me-1", r.cursors)
    assert r.dropped == ["gary-pad-2"] and sorted(r.views) == ["gary-phone-1"]
    assert not os.path.exists(ss.peer_cache_path(home, "gary-pad-2")) and "gary-pad-2" not in r.cursors["peers"]
    assert sorted(ss.load_cached_peers(home)) == ["gary-phone-1"]
    # 整个目录都没了（404）= 一个设备文件都没有
    h.files.clear()
    h.dirs.discard(f"/dav/users/{USER}/shelf")
    r = ss.pull_peers(c, home, USER, "me-1", r.cursors)
    assert r.dropped == ["gary-phone-1"] and not r.views and ss.load_cached_peers(home) == {}


def test_failed_listing_leaves_cache_untouched(server, home):
    h, url = server
    put_dev(h, "gary-phone-1", "p", {"remote:a.txt": (40.0, 5000.0)})
    c = WebDavClient(url)
    r = ss.pull_peers(c, home, USER, "me-1", None)
    h.fail_propfind.add(f"/dav/users/{USER}/shelf")
    with pytest.raises(WebDavError):
        ss.pull_peers(c, home, USER, "me-1", r.cursors)                      # 5xx / 离线：抛，不当作「已删除」
    assert sorted(ss.load_cached_peers(home)) == ["gary-phone-1"]


def test_hostile_device_names_are_never_read(server, home):
    h, url = server
    c = WebDavClient(url)
    for n in ("CON.json", "a b.json", ".hidden.json", "x" * 80 + ".json", "..json", "evil;rm.json", "readme.txt"):
        raw_put(h, f"/dav/users/{USER}/shelf/{n}", dev_doc("zzz"))
    h.extra_entries[f"/dav/users/{USER}/shelf"] = [("/dav/users/%s/shelf/..%%5Cx.json" % USER, False),
                                                   ("/dav/users/%s/shelf/../../etc.json" % USER, False)]
    put_dev(h, "ok-dev-1", "p", {"remote:a.txt": (10.0, 5000.0)})
    h.log.clear()
    r = ss.pull_peers(c, home, USER, "me-1", None)
    assert sorted(r.views) == ["ok-dev-1"] and len(gets(h)) == 1
    assert sorted(os.listdir(sh.peers_dir(home))) == ["ok-dev-1.json"]
    assert r.skipped >= 5


def test_device_id_comes_from_file_name_not_body(server, home):
    h, url = server
    raw_put(h, f"/dav/users/{USER}/shelf/gary-phone-1.json",
            json.dumps({"version": 1, "device_id": "totally-other", "hostname": "gary-phone",
                        "progress": {"remote:a.txt": {"percent": 12.0, "updated": 5000.0, "device": "spoofed"}}}).encode())
    r = ss.pull_peers(WebDavClient(url), home, USER, "me-1", None)
    assert list(r.views) == ["gary-phone-1"]
    assert r.views["gary-phone-1"]["progress"]["remote:a.txt"]["device"] == "gary-phone-1"
    assert ss.digest_progress(r.views)["remote:a.txt"]["device"] == "gary-phone"     # 显示名取 hostname


def test_malicious_peer_content_is_validated(server, home):
    h, url = server
    far = time.time() + 5 * 86400
    raw_put(h, f"/dav/users/{USER}/shelf/gary-phone-1.json", json.dumps({
        "version": 1, "hostname": "<script>x</script>\x00‮",
        "progress": {"remote:a.txt": {"percent": 1e9, "updated": 5000.0},          # 越界百分比：截断到 100
                     "remote:b.txt": {"percent": "abc", "updated": 5000.0},         # 非数字：丢弃
                     "remote:c.txt": {"percent": 5.0, "updated": far},              # 超前 24 小时：截断为最旧
                     "../../etc": {"percent": 5.0, "updated": 5000.0},              # 非法 book_id：丢弃
                     "local:" + "0" * 40: {"percent": 5.0, "updated": 5000.0}}}).encode())
    r = ss.pull_peers(WebDavClient(url), home, USER, "me-1", None)
    prog = r.views["gary-phone-1"]["progress"]
    assert prog["remote:a.txt"]["percent"] == 100.0 and "remote:b.txt" not in prog and "../../etc" not in prog
    assert prog["remote:c.txt"]["updated"] < 1 and "local:" + "0" * 40 in prog
    assert "\x00" not in r.views["gary-phone-1"].get("hostname", "") and "‮" not in r.views["gary-phone-1"].get("hostname", "")


def test_unsafe_user_name_is_refused_without_requests(server, home):
    h, url = server
    for u in ("../x", "a/b", "a b", ".", ".."):
        with pytest.raises(WebDavError):
            ss.pull_peers(WebDavClient(url), home, u, "me-1", None)
    assert n_requests(h) == 0
    assert ss.check_user("") == "default" and ss.check_user(None) == "default" and ss.check_user("gary_1") == "gary_1"


def test_digest_progress_merge_rules():
    v1 = {"hostname": "alpha", "progress": {"remote:a": {"percent": 10.0, "updated": 100.0}, "remote:t": {"percent": 1.0, "updated": 50.0}}}
    v2 = {"hostname": "beta", "progress": {"remote:a": {"percent": 20.0, "updated": 200.0}, "remote:t": {"percent": 2.0, "updated": 50.0}}}
    d = ss.digest_progress({"dev-a": v1, "dev-b": v2})
    assert d["remote:a"] == {"percent": 20.0, "updated": 200.0, "device": "beta"}         # updated 大者胜
    assert d["remote:t"]["device"] == "beta"                                              # 平局：device_id 大者胜
    assert ss.digest_progress({"dev-a": v1, "dev-b": v2}) == ss.digest_progress({"dev-b": v2, "dev-a": v1})


# ================================================================== 逐书补缺：pull_progress_index
def prog_file(h, bid, pct=50.0, upd=5000.0, body_bid=None, user=USER, device="gary-phone"):
    body = {"book_id": body_bid or bid, "offset": 10, "percent": pct, "chapter": "", "updated": upd, "device": device}
    raw_put(h, "/dav/" + sync.progress_path(user, bid), json.dumps(body, ensure_ascii=False).encode())


def test_pull_progress_index_validates_body_book_id(server):
    h, url = server
    A, B, C, D, E, F = ("remote:小说/甲.txt", "remote:小说/乙.txt", "remote:丙.txt", "local:" + "a" * 40,
                       "remote:戊.txt", "remote:己.txt")
    prog_file(h, A, 33.0)
    prog_file(h, B, 44.0, body_bid=A)                      # B 的文件里装的是 A 的记录（碰撞 / 篡改）：忽略，且不能给 A 重复记账
    raw_put(h, "/dav/" + sync.progress_path(USER, D), b"{not json")
    prog_file(h, E, 10.0, upd=time.time() + 10 * 86400)         # 更新时间在遥远的未来：坏记录
    raw_put(h, "/dav/" + sync.progress_path(USER, F), json.dumps({"book_id": F, "percent": "NaN?", "updated": 5.0}).encode())
    res = ss.pull_progress_index(WebDavClient(url), USER, [A, B, C, D, E, F])
    assert res.found == {A: {"percent": 33.0, "updated": 5000.0, "device": "gary-phone"}}
    assert res.absent == [C] and sorted(res.bad) == sorted([B, D, E, F]) and not res.failed
    assert res.requests == 6 and len(gets(h)) == 6                          # 每本恰好 1 次请求（404 不再补 exists）
    assert not any(m == "PROPFIND" for m, _p in h.log)


def test_pull_progress_index_keys_by_body_not_file_name(server):
    """文件名是 sha1(book_id)[:20]：即使有人把别的书的记录放到这本书的文件名下，结果也不会按文件名认书。"""
    h, url = server
    X, Y = "remote:x.txt", "remote:y.txt"
    prog_file(h, X, 10.0, body_bid=Y)                      # 放在 X 的文件名下，但文件体说自己是 Y
    res = ss.pull_progress_index(WebDavClient(url), USER, [X])
    assert res.found == {} and res.bad == [X]
    res = ss.pull_progress_index(WebDavClient(url), USER, [Y])                # 想要 Y 时，Y 自己的路径上没有文件
    assert res.found == {} and res.absent == [Y]


def test_500_books_request_count_is_bounded(server, home):
    h, url = server
    bids = [f"remote:书库/第{i:03d}本.txt" for i in range(500)]
    for i, b in enumerate(bids):
        if i % 4 == 0:
            prog_file(h, b, 10.0 + i % 80, upd=5000.0 + i)
    c = WebDavClient(url)
    res = ss.pull_progress_index(c, USER, bids)
    assert res.requests <= ss.FILL_PER_ROUND and len(gets(h)) == ss.FILL_PER_ROUND and len(h.log) == ss.FILL_PER_ROUND
    assert len(res.found) + len(res.absent) == ss.FILL_PER_ROUND
    # 整轮（Worker）：1 次 PROPFIND + 每个设备 1 次 GET + 至多 50 次补缺
    put_dev(h, "gary-phone-1", "p", {"remote:别的.txt": (5.0, 100.0)})
    put_dev(h, "gary-pad-2", "q", {})
    h.log.clear()
    w = ss.ShelfSyncWorker()
    results, errs = [], []
    w.progressPulled.connect(results.append)
    w.progressFailed.connect(errs.append)
    cfg = {"webdav_enabled": True, "webdav_url": url, "sync_user_name": USER}
    want = list(bids)
    seen_gets = set()
    for rnd in range(11):
        h.log.clear()
        n = len(results)
        w.sync_progress(cfg, home, "me-1", {"peers": {}, "rounds_since_full": 0}, want)
        assert wait_for(lambda: len(results) > n or errs), errs
        assert not errs
        assert len(h.log) <= 1 + 2 + ss.FILL_PER_ROUND, (rnd, len(h.log))
        round_gets = {p for p in gets(h) if "/progress/" in p}
        assert not (round_gets & seen_gets), "已探测过的书（含 404）不应在后续轮次被重复探测"
        seen_gets |= round_gets
        want = [b for b in want if b not in results[-1].found]                # 主线程会把已有进度的书从 want 里去掉
        if not round_gets:
            break
    assert len(seen_gets) == 500                                              # 十来轮后 500 本都探测过一遍


# ================================================================== 摘要 / 继续阅读 / 推送：纯逻辑
def book(bid, title, pct, upd, where="synced", dev="d"):
    b = sh.Book(bid, title, "txt", 1, "cloud:", "fp", where, bid[7:] if bid.startswith("remote:") else "")
    b.percent, b.progress_updated, b.progress_device, b.last_read = pct, upd, dev, upd
    if where in ("synced", "local"):
        b.local_paths = ["/x/" + title]
    return b


def test_continue_candidate_and_text():
    a = book("remote:a", "甲", 37.0, 1_700_000_000.0, dev="gary-phone")
    b = book("remote:b", "乙", 80.0, 1_700_000_500.0, dev="gary-pc")
    done = book("remote:c", "丙", 99.0, 1_700_001_000.0)
    unread = book("remote:d", "丁", 0.0, 1_700_002_000.0)
    hidden = book("remote:e", "戊", 10.0, 1_700_003_000.0)
    hidden.hidden = True
    ph = book("remote:f", "己", 10.0, 1_700_004_000.0, where="missing")
    assert sh.continue_candidate({x.id: x for x in (a, b, done, unread, hidden, ph)}) is b            # 在读里最近的一本
    assert sh.continue_candidate([done, unread, hidden, ph]) is None                                 # 没有在读的：不显示
    assert sh.continue_candidate({}) is None
    now = 1_700_000_500.0 + 3 * 3600
    assert sv.continue_text(a, now=1_700_000_000.0 + 3 * 3600) == "继续阅读：《甲》  在 gary-phone 上读到 37%（3 小时前）"
    assert sv.continue_text(b, local_names={"gary-pc"}, now=now) == "继续阅读：《乙》  在 本机 上读到 80%（3 小时前）"
    b.progress_device = ""
    assert "读到 80%" in sv.continue_text(b, now=now) and " 上读到" not in sv.continue_text(b, now=now)


def test_prune_progress_keeps_newest():
    ov = sh.new_overlay("dev-a")
    for i in range(10):
        sh.set_progress(ov, f"remote:{i}", 10.0, updated=1000.0 + i)
    rev = ov["_rev"]
    assert sh.prune_progress(ov, 20) == 0 and ov["_rev"] == rev
    assert sh.prune_progress(ov, 4) == 6
    assert sorted(ov["progress"]) == ["remote:6", "remote:7", "remote:8", "remote:9"] and ov["_rev"] == rev + 1


# ================================================================== 界面层：偏好 / 摘要写入 / 推送 / 继续阅读
def cfg_for(url, user=USER, name="gary-pc"):
    return {"webdav_enabled": True, "webdav_url": url, "sync_user_name": user, "device_name": name}


def make_view(tmp_path, url, dirs=(), root="home", cfg=None, confirm=True):
    cfg = cfg if cfg is not None else (cfg_for(url) if url else {})
    ctx = {"library_dirs": list(dirs), "recent": [], "bookmarks": {}, "cfg": cfg}
    v = sv.ShelfView(lambda: ctx, root=str(tmp_path / root))
    v.ctx = ctx
    v.synced, v.psynced, v.opened = [], [], []
    v.cloudSynced.connect(v.synced.append)
    v.progressSynced.connect(v.psynced.append)
    v.openRequested.connect(v.opened.append)
    if confirm is not None:
        v.confirm_push = (lambda: confirm)
    return v


def settle(v):
    assert wait_for(lambda: not v.scanning and not v._cloud_busy and not v._cloud._running
                    and not v._cloud._prog_running and not v._progress_busy), "云端同步 / 扫描没有在时限内结束"


def sync_wait(v):
    n, m = len(v.synced), len(v.psynced)
    assert v.sync_cloud(force=True) is True
    assert wait_for(lambda: len(v.synced) > n and len(v.psynced) > m), "云端列表 / 进度没有并入"
    settle(v)


def put_book(h, rel, data=b"x", mtime=OLD):
    segs = rel.split("/")
    h.dirs.update(["/dav", "/dav/library"] + ["/dav/library/" + "/".join(segs[:i]) for i in range(1, len(segs))])
    raw_put(h, "/dav/library/" + rel, data, mtime)


@pytest.fixture
def rb(tmp_path, monkeypatch):
    d = tmp_path / "remote_books"
    monkeypatch.setattr(sync, "REMOTE_BOOKS_DIR", str(d))
    return d


def local_book(tmp_path, name="我的书.txt", data=b"hello world"):
    d = tmp_path / "mine"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_bytes(data)
    return str(p)


def test_disabled_cloud_creates_no_overlay_and_no_requests(tmp_path, rb):
    v = make_view(tmp_path, None)
    p = local_book(tmp_path)
    v.note_progress(p, 40.0, 5000.0, "me")
    assert v._overlay is None and v.push_pending() is False and v.maybe_push() is False and v.sync_progress() is False
    v.flush()
    assert not os.path.exists(sh.overlay_path(str(tmp_path / "home")))
    assert v._cloud.progress_generation == 0 and not v._cloud._prog_running


def test_reading_events_write_digest_and_push_after_confirmation(server, tmp_path, rb):
    h, url = server
    v = make_view(tmp_path, url, confirm=None)
    asked = []
    v.confirm_push = lambda: asked.append(1) or True
    p = local_book(tmp_path)
    bid = sync.book_id(p)
    v.note_progress(p, 37.0, 5000.0, "gary-pc")
    assert v._overlay["progress"][bid]["percent"] == 37.0 and v.push_pending() is True
    assert not any(m == "PUT" for m, _x in h.log)                          # 没有确认、也没触发推送之前：云端什么都没写
    assert v.maybe_push() is True and asked == [1]
    assert wait_for(lambda: not v.push_pending()), v._push_error
    dev_path = f"/dav/users/{USER}/shelf/{v._state['device_id']}.json"
    body = json.loads(h.files[dev_path].decode("utf-8"))
    assert body["progress"][bid]["percent"] == 37.0 and body["device_id"] == v._state["device_id"]
    assert set(body) <= {"version", "device_id", "hostname", "user", "entries", "progress"}      # to_wire 白名单
    txt = h.files[dev_path].decode("utf-8")
    assert str(tmp_path) not in txt and "我的书" not in txt and "webdav_password" not in txt and "api_key" not in txt
    assert v._state["confirmed_upload"] is True and v._state["pushed_hash"] == sh.wire_hash(v._overlay)
    st = sh.load_state(str(tmp_path / "home"))
    assert st["confirmed_upload"] is True and st["pushed_hash"] == v._state["pushed_hash"]      # 已落盘
    # 之后不再询问
    v.note_progress(p, 50.0, 6000.0, "gary-pc")
    assert v.maybe_push() is True and asked == [1]
    assert wait_for(lambda: not v.push_pending())
    assert json.loads(h.files[dev_path].decode("utf-8"))["progress"][bid]["percent"] == 50.0
    assert v.maybe_push() is False                                          # 没有待推送内容


def test_declined_or_deferred_confirmation_never_uploads(server, tmp_path, rb):
    h, url = server
    v = make_view(tmp_path, url, confirm=None)
    answers = iter([None, False])
    v.confirm_push = lambda: next(answers)
    p = local_book(tmp_path)
    v.note_progress(p, 37.0, 5000.0, "gary-pc")
    assert v.maybe_push() is False and v._push_declined is False            # 「现在不问」：保持待推送，以后再问
    assert v.maybe_push() is False and v._push_declined is True             # 拒绝
    assert v.maybe_push() is False                                          # 本次会话不再问
    assert v._state["confirmed_upload"] is False and not any(m == "PUT" for m, _x in h.log)
    assert v.push_pending() is True                                         # 进度只存本机，仍标记待上传


def test_default_confirm_is_deferred_when_shelf_not_visible(server, tmp_path, rb):
    h, url = server
    v = make_view(tmp_path, url, confirm=None)                              # 默认确认：书架没显示 -> 返回 None，不弹窗
    assert v.isVisible() is False and v.confirm_push() is None
    v.note_progress(local_book(tmp_path), 37.0, 5000.0, "me")
    assert v.maybe_push() is False and not any(m == "PUT" for m, _x in h.log)


def test_edit_during_push_keeps_pending(server, tmp_path, rb, monkeypatch):
    h, url = server
    v = make_view(tmp_path, url)
    sent = []
    monkeypatch.setattr(v._cloud, "push_device", lambda cfg, dev, payload, rev, digest: sent.append((rev, digest, payload)))
    p = local_book(tmp_path)
    v.note_progress(p, 10.0, 5000.0, "me")
    assert v.maybe_push() is True and len(sent) == 1
    rev1, h1, _ = sent[0]
    v.note_progress(p, 20.0, 5100.0, "me")                                   # 推送期间又有新的阅读事件
    assert v.maybe_push() is True and len(sent) == 2 and sent[1][1] != h1    # 新内容单独再推一份，不会被「已有推送在途」吞掉
    v._on_pushed(rev1, h1, "")                                               # 先到的完成通知是旧内容的
    assert v._state["pushed_hash"] == h1 and v.push_pending() is True and v._push_timer.isActive()
    v._on_pushed(sent[1][0], sent[1][1], "")
    assert v.push_pending() is False
    v.maybe_push()
    assert len(sent) == 2


def test_other_device_progress_is_not_written_into_own_digest(server, tmp_path, rb):
    h, url = server
    v = make_view(tmp_path, url)
    p = local_book(tmp_path)
    v.note_progress(p, 60.0, 5000.0, "gary-phone", local=False)             # mainwin 应用远端较新进度时
    assert v._overlay is None or not v._overlay["progress"]
    assert v.books == {} and v._progress[sync.book_id(p)]["percent"] == 60.0   # 但显示用的进度记下了
    assert v.push_pending() is False


def test_invalid_user_name_does_not_sync_progress(server, tmp_path, rb):
    h, url = server
    v = make_view(tmp_path, url, cfg=cfg_for(url, user="../evil"))
    v.note_progress(local_book(tmp_path), 60.0, 5000.0, "me")
    assert v._overlay is None and "用户名" in v._notice and v.maybe_push() is False
    assert v.sync_progress() is True
    assert wait_for(lambda: v._progress_error != "")
    assert not any(m in ("PUT", "MKCOL") for m, _x in h.log) and not any("evil" in x for _m, x in h.log)


def test_pending_digest_is_pushed_after_restart_when_cloud_reachable(server, tmp_path, rb):
    h, url = server
    dead = cfg_for("http://127.0.0.1:9/dav")
    v1 = make_view(tmp_path, None, cfg=dead)                                  # 先离线读书
    p = local_book(tmp_path)
    v1.note_progress(p, 42.0, 5000.0, "me")
    assert v1.maybe_push() is True                                            # 推送失败（连不上）
    assert wait_for(lambda: v1._push_error != "") and v1.push_pending() is True
    v1.flush()
    assert os.path.isfile(sh.overlay_path(str(tmp_path / "home")))
    dev = v1._state["device_id"]
    v2 = make_view(tmp_path, url)                                             # 重启后云端可达
    sync_wait(v2)
    assert wait_for(lambda: f"/dav/users/{USER}/shelf/{dev}.json" in h.files), "重启后没有补推之前没推上去的摘要"
    assert json.loads(h.files[f"/dav/users/{USER}/shelf/{dev}.json"].decode())["progress"][sync.book_id(p)]["percent"] == 42.0


def test_other_devices_progress_shows_in_bars_smart_shelves_and_continue_bar(server, tmp_path, rb):
    h, url = server
    now = time.time()
    put_book(h, "小说/三体.txt", b"x" * 10)
    put_book(h, "小说/沙丘.txt", b"y" * 11)
    put_book(h, "杂项.txt", b"z" * 12)
    put_book(h, "旧书.txt", b"w" * 13)              # 书架功能上线前读过的：不在任何摘要里，只有逐书进度文件
    put_book(h, "没读过.txt", b"q" * 14)
    put_book(h, "撞名.txt", b"v" * 15)              # 逐书进度文件的内容属于别的书：忽略
    put_dev(h, "gary-phone-1", "gary-phone", {"remote:小说/三体.txt": (37.0, now - 3 * 3600),
                                              "remote:小说/沙丘.txt": (99.0, now - 86400)})
    put_dev(h, "gary-pad-2", "gary-pad", {"remote:小说/三体.txt": (20.0, now - 10 * 3600),     # 较旧的记录不会盖掉较新的
                                          "remote:杂项.txt": (5.0, now - 5 * 86400)})
    prog_file(h, "remote:旧书.txt", 61.0, now - 40 * 86400, device="old-pc")
    prog_file(h, "remote:撞名.txt", 50.0, now - 100, body_bid="remote:旧书.txt")
    v = make_view(tmp_path, url)
    sync_wait(v)
    B = lambda rel: v.books["remote:" + rel]    # noqa: E731
    assert B("小说/三体.txt").percent == 37.0 and B("小说/三体.txt").progress_device == "gary-phone"
    assert B("小说/沙丘.txt").status == "done" and B("杂项.txt").status == "reading"
    assert B("旧书.txt").percent == 61.0 and B("旧书.txt").status == "reading"
    assert B("没读过.txt").percent is None and B("没读过.txt").status == "unread"
    assert B("撞名.txt").percent is None                                       # 文件体 book_id 不符：忽略
    ids = lambda shelf: sorted(b.id for b in sh.filter_books(v.books, shelf))   # noqa: E731
    assert ids("reading") == sorted(["remote:小说/三体.txt", "remote:杂项.txt", "remote:旧书.txt"])
    assert ids("done") == ["remote:小说/沙丘.txt"]
    assert "remote:小说/三体.txt" in ids("recent") and "remote:没读过.txt" not in ids("recent")
    counts = sv.count_shelves(v.books)
    assert counts["reading"] == 3 and counts["done"] == 1
    # 继续阅读条：最近一本在任一设备上读过的（在读状态）
    assert v.continue_bar.isHidden() is False and v._continue_book is B("小说/三体.txt")
    assert v.continue_label.textFormat().name == "PlainText"
    assert "《三体》" in v.continue_label.text() and "gary-phone" in v.continue_label.text() and "37%" in v.continue_label.text()
    assert "3 小时前" in v.continue_label.text()
    # 点「继续」：云端书先下载再打开（带 token，迟到的不劫持窗口）
    v.show()
    v.continue_btn.click()
    assert wait_for(lambda: v.opened), "继续阅读没有触发打开"
    assert v.opened[-1].replace("\\", "/").endswith("remote_books/小说/三体.txt")
    v.hide()
    # 第二次同步：游标可信，只剩 PROPFIND + 没有进度的两本书（有 neg 缓存，不再重复探测）
    h.log.clear()
    sync_wait(v)
    dev_gets = [p for p in gets(h) if "/shelf/" in p]
    assert dev_gets == [], "设备文件游标可信时不应再 GET"
    assert [p for p in gets(h) if "/progress/" in p] == []


def test_progress_survives_restart_offline_from_peers_cache_and_index(server, tmp_path, rb):
    h, url = server
    now = time.time()
    put_book(h, "三体.txt", b"x" * 10)
    put_dev(h, "gary-phone-1", "gary-phone", {"remote:三体.txt": (37.0, now - 100)})
    v = make_view(tmp_path, url)
    sync_wait(v)
    v.flush()
    v2 = make_view(tmp_path, None)                                           # 云端没启用 / 离线冷启动：先用缓存画界面
    b = v2.books["remote:三体.txt"]
    assert b.percent == 37.0 and b.status == "reading"
    assert v2._peers["gary-phone-1"]["hostname"] == "gary-phone"
    assert v2.continue_bar.isHidden() is False and "三体" in v2.continue_label.text()


def test_progress_sync_failure_keeps_cache_and_never_raises(server, tmp_path, rb):
    h, url = server
    put_book(h, "三体.txt", b"x" * 10)
    put_dev(h, "gary-phone-1", "gary-phone", {"remote:三体.txt": (37.0, time.time() - 100)})
    v = make_view(tmp_path, url)
    sync_wait(v)
    assert v.books["remote:三体.txt"].percent == 37.0
    h.fail_propfind.add(f"/dav/users/{USER}/shelf")
    assert v.sync_progress() is True
    assert wait_for(lambda: v._progress_error != "")
    assert v.books["remote:三体.txt"].percent == 37.0 and "进度同步失败" in v.cloud_status_text()
    assert ss.load_cached_peers(str(tmp_path / "home")).get("gary-phone-1") is not None
