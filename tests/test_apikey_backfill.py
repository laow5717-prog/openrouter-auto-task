"""任务跑到账号、登录成功时的 API key 补漏（registration.ensure_apikey）。

锁的是 2026-08-16 暴露的那个覆盖缺口：key 原本只在「充值成功」后抓一次，于是
  - 一个早就充过、这次全程拒付的账号（卡池质量差时是常态）永远走不到成功分支；
  - 只跑订阅、从不走充值的账号，链路里压根没有抓 key 这一步。
两类账号的 apikey 列会一直是空的，且没有任何日志提示。

现在改成登录成功就补一次，但**只对库里为空的账号**——已有 key 的账号必须连页面
都不导航，否则每个账号每轮平白多一次 keys 页往返。
"""

import pytest

from src.models.platform_account import PlatformAccountModel
from src.services.registration import ensure_apikey


class _Adapter:
    """假适配器。fetch_apikey 记调用次数，可配置返回值或抛异常。"""

    def __init__(self, key='sk-abcdefghijklmnopqrstuvwxyz', boom=False, fetchable=True):
        self.key = key
        self.boom = boom
        self.apikey_fetchable = fetchable
        self.calls = 0

    def fetch_apikey(self, session, wid, monitor=None):
        self.calls += 1
        if self.boom:
            raise RuntimeError("keys 页打不开")
        return self.key


@pytest.fixture
def pam(db):
    m = PlatformAccountModel(db)
    db.execute("INSERT INTO accounts (email, identity_status) VALUES (?, 'registered')",
               ('a@x.com',))
    m.ensure('opencode', 'a@x.com', status='recharged', tenant_id='wrk_1')
    return m


def _run(adapter, pam, **kw):
    return ensure_apikey(adapter, pam, 'opencode', 'a@x.com',
                         session=object(), wid='wrk_1', **kw)


def test_backfills_when_missing(pam):
    """库里为空 → 抓一次并落库。"""
    ad = _Adapter()
    assert _run(ad, pam, only_if_missing=True) is True
    assert ad.calls == 1
    assert pam.get('opencode', 'a@x.com')['apikey'] == ad.key


def test_skips_navigation_when_already_present(pam):
    """已有 key → 一次页面都不导航。

    这条断言的是成本：漏掉它，每个账号每轮都要白跑一趟 keys 页。
    """
    pam.update_apikey('opencode', 'a@x.com', 'sk-existing-key-0000000000')
    ad = _Adapter()
    assert _run(ad, pam, only_if_missing=True) is False
    assert ad.calls == 0, '库里已经有 key 了还去导航'
    assert pam.get('opencode', 'a@x.com')['apikey'] == 'sk-existing-key-0000000000'


def test_unconditional_mode_overwrites(pam):
    """only_if_missing=False（充值成功后的常规抓取）照旧覆盖写。"""
    pam.update_apikey('opencode', 'a@x.com', 'sk-old-key-000000000000000')
    ad = _Adapter(key='sk-new-key-111111111111111')
    assert _run(ad, pam) is True
    assert ad.calls == 1
    assert pam.get('opencode', 'a@x.com')['apikey'] == 'sk-new-key-111111111111111'


def test_platform_without_plaintext_key_is_skipped(pam):
    """apikey_fetchable=False（infron）直接跳过，不导航也不刷屏。"""
    ad = _Adapter(fetchable=False)
    assert _run(ad, pam, only_if_missing=True) is False
    assert ad.calls == 0


def test_fetch_failure_never_breaks_the_caller(pam):
    """抓取抛异常 → 返回 False，不外溢；库里那格保持原样。"""
    ad = _Adapter(boom=True)
    assert _run(ad, pam, only_if_missing=True) is False
    assert (pam.get('opencode', 'a@x.com')['apikey'] or '') == ''


def test_empty_result_is_not_written(pam):
    """抓不到（返回 None）不能写空值进去。"""
    ad = _Adapter(key=None)
    assert _run(ad, pam, only_if_missing=True) is False
    assert (pam.get('opencode', 'a@x.com')['apikey'] or '') == ''


def test_missing_tenant_id_is_reported_not_crashed(pam):
    """没有 wid 时安全返回——上层不该因为抓 key 而中断。"""
    ad = _Adapter()
    assert ensure_apikey(ad, pam, 'opencode', 'a@x.com', object(), None,
                         only_if_missing=True) is False
    assert ad.calls == 0
