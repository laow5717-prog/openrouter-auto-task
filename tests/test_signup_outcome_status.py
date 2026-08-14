"""注册结果 → identity_status 的映射，以及注册没跑成时的环境回收。

这里锁住的是一条口径：**注册跑了但没成，就是 failed**，不再写 'pending'。
'pending' 那档曾经指望「下一轮换个代理自动重试」，代价是账号列表的「待处理」页签
里混着「仅导入还没轮到」和「跑过一遍没成」两种账号，人看不出差别。改判后要重跑
只能显式走「重置为待注册」。

同时锁住：只要结果不是 registered，就当场释放该账号的 AdsPower 环境——那种环境里
没有任何 GitHub 授权态，留着纯占 12 格硬配额里的一格。
"""

import pytest

import src.services.github_signup_service as gss
from src.browser.github_signup import BLOCK_RESTRICTED
from src.web.app import AppState, build_models


@pytest.fixture
def state(db, monkeypatch):
    """带一个 imported 账号（自带收码链接）的 AppState，环境释放被打桩记录。"""
    models = build_models(db)
    db.execute("INSERT INTO accounts (email, email_password, email_verify_link, "
               "identity_status) VALUES (?,?,?,?)",
               ('imp0@example.com', 'ep', 'https://ruoanzhu.example/s?e=x', 'imported'))
    st = AppState(db, models)
    st.released = []
    monkeypatch.setattr(st, '_release_adspower_profile',
                        lambda email, reason='': st.released.append(email) or True)
    return st


def _acct(state):
    return state.models['account'].get_by_emails(['imp0@example.com'])[0]


def _run(state, monkeypatch, outcome, **extra):
    payload = {'outcome': outcome, 'ok': outcome == 'signup_complete'}
    payload.update(extra)
    monkeypatch.setattr(gss, 'signup_one', lambda **kw: payload)
    return state._register_one_account(_acct(state))


def _status(state):
    return _acct(state)['identity_status']


@pytest.mark.parametrize('outcome', [
    'reached_captcha',        # 碰 Arkose，全自动模式不等人工
    BLOCK_RESTRICTED,         # 注册页没打开：GitHub 按出口 IP 拦了
    'signup_page_unavailable',
    'no_verification_email',  # 其它注册未完成
])
def test_signup_not_completed_marks_failed(state, monkeypatch, outcome):
    result, _detail = _run(state, monkeypatch, outcome)
    assert result == 'failed', f'{outcome} 应计注册失败'
    assert _status(state) == 'failed', \
        f"{outcome} 写了 '{_status(state)}'，「待处理」这档已废弃"


@pytest.mark.parametrize('outcome', [
    'reached_captcha', BLOCK_RESTRICTED, 'no_verification_email', 'account_suspended',
])
def test_failed_signup_releases_browser_profile(state, monkeypatch, outcome):
    _run(state, monkeypatch, outcome, github_password='pw')
    assert state.released == ['imp0@example.com'], \
        f'{outcome} 之后环境没释放，那一格配额要等下次撞满才回收'


def test_successful_signup_keeps_profile(state, monkeypatch):
    result, _detail = _run(state, monkeypatch, 'signup_complete', github_password='pw')
    assert result == 'registered'
    assert _status(state) == 'registered'
    # 环境里的 GitHub 授权态正是下一步登录平台要用的东西，绝不能删
    assert state.released == []


def test_no_mailbox_touches_nothing(state, monkeypatch):
    """没有收码数据时压根没起浏览器，账号状态不动、也没有环境可释放。"""
    monkeypatch.setattr(gss, 'signup_one',
                        lambda **kw: pytest.fail('不该走到注册'))
    acct = dict(_acct(state), email_verify_link='')
    monkeypatch.setattr(state, '_hotmail_by_email', lambda email: None)
    result, _detail = state._register_one_account(acct)
    assert result == 'skipped'
    assert _status(state) == 'imported'
    assert state.released == []
