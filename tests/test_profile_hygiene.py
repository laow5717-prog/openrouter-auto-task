"""持久化 profile 卫生：孤儿进程回收与缓存清理。

守的是一类白屏故障：Chrome 异常退出留下孤儿进程仍占着 user-data-dir，而
_clear_singleton_locks 无条件删掉 Singleton 锁，于是两个 Chrome 争抢同一份
leveldb，渲染进程起不来；叠加从不清理的 Service Worker 缓存腐坏后返回空响应，
页面 URL 正常却全白。

回归重点是 test_prune_keeps_credentials —— 清缓存一旦误删登录态，所有账号都要
重新登录，比白屏本身更糟。
"""

import os
import subprocess
import sys
import tempfile

import pytest

import src.browser.driver as driver
from src.browser.driver import (
    _chrome_pids_for_profile,
    _kill_chrome_for_profile,
    _prune_profile_cache,
)

# 登录态所在文件。清缓存绝不能碰这几个，否则账号全部掉线。
_CREDENTIAL_FILES = (
    'Default/Cookies',
    'Default/Login Data',
    'Default/Local Storage/leveldb.log',
    'Local State',
)


@pytest.fixture
def fake_chrome(tmp_path):
    """产出「假 Chrome 进程」的工厂：命令行带 --user-data-dir，不会自行退出。

    真实 Chrome 在父进程死后会自己退出（remote-debugging-pipe 断开），
    没法稳定复现顽固孤儿，所以用长睡进程代替——本模块要验的是按 user-data-dir
    匹配与回收的逻辑，与进程是不是真 Chrome 无关。
    """
    script = tmp_path / 'fake_chrome.py'
    script.write_text('import time\ntime.sleep(120)\n')
    spawned = []

    def _spawn(user_data_dir, *extra_args):
        p = subprocess.Popen([sys.executable, str(script),
                              f'--user-data-dir={user_data_dir}', *extra_args])
        spawned.append(p)
        return p

    yield _spawn

    for p in spawned:
        try:
            p.kill()
        except OSError:
            pass


def _wait_for_pids(user_data_dir, expected_count, timeout=5):
    """等到匹配进程数达到预期，返回 pid 列表。进程启动有延迟，不能裸断言。"""
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        pids = _chrome_pids_for_profile(user_data_dir)
        if len(pids) == expected_count:
            return pids
        time.sleep(0.1)
    return _chrome_pids_for_profile(user_data_dir)


# ==================== _chrome_pids_for_profile ====================


def test_matches_only_exact_profile_dir(fake_chrome, tmp_path):
    """/a 不能匹配 /ab —— 否则启动一个账号会误杀名字相近的兄弟 profile。"""
    a, ab = str(tmp_path / 'a'), str(tmp_path / 'ab')
    fake_chrome(a)
    proc_ab = fake_chrome(ab)

    assert _wait_for_pids(ab, 1) == [proc_ab.pid]
    assert proc_ab.pid not in _chrome_pids_for_profile(a)


def test_main_process_sorts_before_helpers(fake_chrome, tmp_path):
    """主进程排在 helper 前面：SIGTERM 先送主进程，helper 会跟着退。"""
    d = str(tmp_path / 'p')
    main = fake_chrome(d)
    helper = fake_chrome(d, '--type=renderer')

    assert _wait_for_pids(d, 2) == [main.pid, helper.pid]


def test_returns_empty_when_ps_unavailable(monkeypatch, tmp_path):
    """ps 不可用（非 macOS/Linux 等）时降级为空列表，绝不能把浏览器启动搞挂。"""
    def _boom(*_a, **_kw):
        raise FileNotFoundError('ps')

    monkeypatch.setattr(subprocess, 'run', _boom)
    assert _chrome_pids_for_profile(str(tmp_path / 'p')) == []


# ==================== _kill_chrome_for_profile ====================


def test_kill_is_noop_without_occupants(tmp_path):
    assert _kill_chrome_for_profile(str(tmp_path / 'empty'), '测试') == 0


def test_kill_reclaims_all_occupants(fake_chrome, tmp_path):
    d = str(tmp_path / 'p')
    fake_chrome(d)
    fake_chrome(d, '--type=renderer')
    _wait_for_pids(d, 2)

    assert _kill_chrome_for_profile(d, '测试') == 2
    assert _wait_for_pids(d, 0) == []


def test_kill_grace_lets_process_exit_on_its_own(fake_chrome, tmp_path):
    """宽限期内自行退出的进程不算「回收」。

    quit() 里 context.close() 返回时 Chrome 常常还在优雅退出、正把 Cookies 落盘。
    此刻抢着发信号会截断落盘，白屏没修成反而丢登录态。
    """
    d = str(tmp_path / 'p')
    proc = fake_chrome(d)
    _wait_for_pids(d, 1)

    import threading
    threading.Timer(0.5, proc.kill).start()      # 模拟 Chrome 自行退出

    assert _kill_chrome_for_profile(d, '测试', grace=5) == 0


def test_kill_grace_still_reclaims_stubborn_process(fake_chrome, tmp_path):
    """宽限期过后仍赖着不走的照样回收，否则又变成泄漏。"""
    d = str(tmp_path / 'p')
    fake_chrome(d)
    _wait_for_pids(d, 1)

    assert _kill_chrome_for_profile(d, '测试', grace=1) == 1
    assert _wait_for_pids(d, 0) == []


def test_kill_spares_neighbour_profile(fake_chrome, tmp_path):
    """回收 /a 时 /ab 必须毫发无损。"""
    a, ab = str(tmp_path / 'a'), str(tmp_path / 'ab')
    neighbour = fake_chrome(ab)
    _wait_for_pids(ab, 1)

    assert _kill_chrome_for_profile(a, '测试') == 0
    assert neighbour.poll() is None


# ==================== _prune_profile_cache ====================


def _build_profile(root, cache_mb):
    """造一个带缓存和登录态的 profile 目录。"""
    for name in driver._PROFILE_CACHE_DIRS:
        path = os.path.join(root, 'Default', name)
        os.makedirs(path, exist_ok=True)
        chunk = cache_mb * 1024 * 1024 // len(driver._PROFILE_CACHE_DIRS)
        with open(os.path.join(path, 'blob'), 'wb') as f:
            f.write(b'\0' * chunk)

    for rel in _CREDENTIAL_FILES:
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write('SECRET')
    return root


def _credentials_intact(root):
    return all(
        os.path.exists(os.path.join(root, rel))
        and open(os.path.join(root, rel)).read() == 'SECRET'
        for rel in _CREDENTIAL_FILES
    )


def test_prune_skips_when_under_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(driver, 'PROFILE_CACHE_LIMIT_MB', 64)
    root = _build_profile(str(tmp_path / 'p'), cache_mb=8)

    _prune_profile_cache(root)

    assert os.path.isdir(os.path.join(root, 'Default', 'Cache'))
    assert _credentials_intact(root)


def test_prune_clears_all_cache_dirs_over_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(driver, 'PROFILE_CACHE_LIMIT_MB', 8)
    root = _build_profile(str(tmp_path / 'p'), cache_mb=32)

    _prune_profile_cache(root)

    remaining = [n for n in driver._PROFILE_CACHE_DIRS
                 if os.path.isdir(os.path.join(root, 'Default', n))]
    assert remaining == []


def test_prune_keeps_credentials(tmp_path, monkeypatch):
    """回归核心：清缓存后账号必须仍处登录态。

    Service Worker 目录被删是修白屏的手段（强制 dash 重新注册 SW），但
    Cookies / Login Data / Local Storage / Local State 一个都不许丢。
    """
    monkeypatch.setattr(driver, 'PROFILE_CACHE_LIMIT_MB', 8)
    root = _build_profile(str(tmp_path / 'p'), cache_mb=32)

    _prune_profile_cache(root)

    assert _credentials_intact(root)


def test_prune_tolerates_missing_cache_dirs(tmp_path, monkeypatch):
    """全新 profile 还没有任何缓存目录时不应抛异常。"""
    monkeypatch.setattr(driver, 'PROFILE_CACHE_LIMIT_MB', 8)
    root = str(tmp_path / 'fresh')
    os.makedirs(os.path.join(root, 'Default'))

    _prune_profile_cache(root)     # 不抛即通过
