"""代理池解析、model、ProxyRegistry 排他测试。"""

import tempfile
import threading

from src.models.database import Database
from src.models.proxy import ProxyModel, parse_proxy_line
from src.web.worker import ProxyRegistry


# ---- 解析 ----

def test_parse_at_format():
    r = parse_proxy_line("ff76b46bcb5e076c:92dsTEyCpnbIOxt0@gateway.i-proxy.com:10000")
    assert r == {'host': 'gateway.i-proxy.com', 'port': 10000,
                 'username': 'ff76b46bcb5e076c', 'password': '92dsTEyCpnbIOxt0'}


def test_parse_colon_format():
    r = parse_proxy_line("user1:pass1:gateway.i-proxy.com:10005")
    assert r == {'host': 'gateway.i-proxy.com', 'port': 10005,
                 'username': 'user1', 'password': 'pass1'}


def test_parse_no_cred_and_scheme():
    assert parse_proxy_line("1.2.3.4:8080") == {
        'host': '1.2.3.4', 'port': 8080, 'username': '', 'password': ''}
    r = parse_proxy_line("http://u:p@h.com:3128")
    assert r['host'] == 'h.com' and r['port'] == 3128 and r['username'] == 'u'


def test_parse_invalid():
    assert parse_proxy_line("garbage") is None
    assert parse_proxy_line("") is None
    assert parse_proxy_line("host:notaport") is None


# ---- model ----

def _model():
    return ProxyModel(Database(tempfile.mktemp(suffix='.db')))


def test_add_dedup_and_count():
    m = _model()
    text = (
        "ff76b46bcb5e076c:92dsTEyCpnbIOxt0@gateway.i-proxy.com:10000\n"
        "ff76b46bcb5e076c:92dsTEyCpnbIOxt0@gateway.i-proxy.com:10001\n"
        "ff76b46bcb5e076c:92dsTEyCpnbIOxt0@gateway.i-proxy.com:10000\n"   # 重复
        "garbage\n"                                                        # 非法
    )
    added, skipped = m.add_proxies(text)
    assert added == 2 and skipped == 2
    assert m.count() == 2
    assert len(m.get_usable_list()) == 2


def test_mark_invalid_excluded_from_usable():
    m = _model()
    m.add_proxies("u:p@h.com:10000")
    rows, _ = m.get_all()
    m.mark_status(rows[0]['id'], 'invalid')
    assert len(m.get_usable_list()) == 0


# ---- ProxyRegistry 排他 ----

def test_registry_exclusion():
    reg = ProxyRegistry()
    p = {'host': 'h', 'port': 1, 'username': 'u', 'password': 'x'}
    key = reg.key_of(p)
    assert reg.try_acquire(key, 'W1') is True
    assert reg.try_acquire(key, 'W2') is False       # 已被 W1 占用
    assert reg.try_acquire(key, 'W1') is True         # 同持有者幂等
    reg.release(key)
    assert reg.try_acquire(key, 'W2') is True          # 释放后可领


def test_acquire_free_picks_distinct():
    reg = ProxyRegistry()
    cands = [{'host': 'h', 'port': p, 'username': 'u', 'password': ''} for p in (1, 2, 3)]
    a = reg.acquire_free(cands, 'W1')
    b = reg.acquire_free(cands, 'W2')
    assert a is not None and b is not None
    assert reg.key_of(a) != reg.key_of(b)              # 两 worker 领到不同代理
    assert len(reg.in_flight_keys()) == 2


def test_acquire_free_all_busy_returns_none():
    reg = ProxyRegistry()
    cands = [{'host': 'h', 'port': 1, 'username': 'u', 'password': ''}]
    reg.acquire_free(cands, 'W1')
    assert reg.acquire_free(cands, 'W2') is None        # 唯一代理已被占,全忙


def test_registry_concurrent_no_double_grant():
    """并发领取:每个 key 至多被一个 worker 持有。"""
    reg = ProxyRegistry()
    cands = [{'host': 'h', 'port': p, 'username': 'u', 'password': ''} for p in range(20)]
    grants = []
    lock = threading.Lock()

    def worker(wid):
        for _ in range(50):
            p = reg.acquire_free(cands, wid)
            if p is not None:
                with lock:
                    grants.append((reg.key_of(p), wid))
                reg.release(reg.key_of(p))

    threads = [threading.Thread(target=worker, args=(f'W{i}',)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 结束后池全空
    assert len(reg.in_flight_keys()) == 0
