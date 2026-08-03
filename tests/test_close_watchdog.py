"""关闭浏览器的看门狗测试。

真实故障：日志停在「正在关闭浏览器...」后再无任何输出，整条任务线程静默。
原因是 Chrome 卡死时 context.close() 无限期阻塞——且不抛异常，所以
close_driver 里的 try/except 救不了，后续账号与「注册新号」阶段全部不再执行。
"""

import threading
import time

import src.browser.driver as D


class _HangingContext:
    """模拟卡死的 Chrome：close() 一直阻塞，直到被外部事件放行。"""

    def __init__(self, released):
        self.released = released
        self.close_called = threading.Event()

    def close(self):
        self.close_called.set()
        # 等看门狗放行；超时上限防止测试本身挂死
        self.released.wait(timeout=10)


class _Playwright:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


def _make_session(context, playwright, user_data_dir='/tmp/fake-profile'):
    s = D.BrowserSession.__new__(D.BrowserSession)
    s.context = context
    s.playwright = playwright
    s._closed = False
    s._temp_profile = None
    s._user_data_dir = user_data_dir
    # 本地模式：remote_* 均为 None，quit() 走本地分支（杀进程 + 清目录）。
    # 这两个字段来自 AdsPower 接入，本文件全部用例都只覆盖本地分支。
    s._remote_browser = None
    s._remote_stop = None
    return s


def test_watchdog_kills_chrome_when_close_hangs(monkeypatch):
    """close() 卡住时，看门狗应在时限后强杀 Chrome，使 quit() 得以返回。"""
    monkeypatch.setattr(D, '_CLOSE_WATCHDOG_SEC', 0.3)

    released = threading.Event()
    killed = []

    def fake_kill(user_data_dir, reason, grace=0):
        killed.append((user_data_dir, reason))
        released.set()          # 杀掉进程后，阻塞的 close() 随之解开
        return 1

    monkeypatch.setattr(D, '_kill_chrome_for_profile', fake_kill)

    ctx = _HangingContext(released)
    pw = _Playwright()
    session = _make_session(ctx, pw, '/tmp/profile-x')

    start = time.time()
    session.quit()
    elapsed = time.time() - start

    assert killed, "看门狗未触发强杀"
    assert killed[0][0] == '/tmp/profile-x'
    assert elapsed < 5, f"quit() 未及时返回，耗时 {elapsed:.1f}s"
    assert pw.stopped, "解除阻塞后仍应继续停止 playwright"


def test_watchdog_does_not_fire_on_normal_close(monkeypatch):
    """正常关闭时不得强杀——误杀会截断 Chrome 落盘，把登录态搞丢。"""
    monkeypatch.setattr(D, '_CLOSE_WATCHDOG_SEC', 5)
    killed = []
    monkeypatch.setattr(D, '_kill_chrome_for_profile',
                        lambda *a, **kw: killed.append(a) or 0)

    class _FastContext:
        def close(self):
            pass

    session = _make_session(_FastContext(), _Playwright())
    session.quit()
    time.sleep(0.3)

    # 正常路径本就有一次「关闭后残留」核查（带 grace，自行退干净则不杀），
    # 那是既有行为；这里只断言看门狗的超时强杀没有触发。
    timeout_kills = [a for a in killed if len(a) > 1 and a[1] == '关闭超时']
    assert timeout_kills == [], "正常关闭不应触发超时强杀"


def test_quit_is_idempotent(monkeypatch):
    """重复 quit 只执行一次关闭。"""
    monkeypatch.setattr(D, '_CLOSE_WATCHDOG_SEC', 5)
    monkeypatch.setattr(D, '_kill_chrome_for_profile', lambda *a, **kw: 0)

    calls = []

    class _CountingContext:
        def close(self):
            calls.append(1)

    session = _make_session(_CountingContext(), _Playwright())
    session.quit()
    session.quit()

    assert len(calls) == 1
