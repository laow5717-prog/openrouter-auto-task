"""账号归档（retired）与「注册失败重置为 imported」的回归测试。

## 为什么判据类测试要逐个入口钉

`retired` 之所以只需在 `utils.IDENTITY_TERMINAL_STATUSES` 加一项就能生效，靠的是
四处「这账号还能不能跑」的判据**共用** `is_identity_terminal()` 这一个谓词：

    app.py::_payable_now()          可充值账号
    app.py::_reusable_recharged()   余额未满的复用池
    routes.py::_usable()            充值启动门 + 复用池计数
    routes.py 订阅启动门

这是既有设计的红利，但也是脆的：将来有人新写一个入口、自己硬编码一组状态而不调这个
谓词，归档就会在那条路径上静默失效。所以这里逐个入口钉住语义，而不是只测谓词本身。

## 为什么 hotmail.xlsx 缺失的用例是常态而非边界

本机实测：`hotmail.xlsx` **不存在**，而 38 个 failed 账号全都有 `email_verify_link`。
所以「xlsx 读不到 → 空集」是当前的主路径。若哪天有人把它改成抛异常或判成「无收码
数据」，重置功能会对全部账号失效——点了没反应，不报错。
"""

import pytest

from src.services.account_reset import (
    RESETTABLE, classify_for_reset, load_hotmail_emails,
)
from src.utils import IDENTITY_TERMINAL_STATUSES, is_identity_terminal


def _acct(email, status, link=''):
    return {"email": email, "identity_status": status, "email_verify_link": link}


# --- retired 是身份层终态 ---------------------------------------------------

def test_retired_is_identity_terminal():
    assert is_identity_terminal('retired') is True
    assert 'retired' in IDENTITY_TERMINAL_STATUSES


def test_retired_sits_alongside_broken_account_states():
    """retired 与 banned/suspended 同组，但语义不同：那些是账号坏了，它是用户主动停用。

    同组的唯一理由是下游只问「还能不能跑」。这条测试锁住集合内容，防有人为了
    「语义更清晰」把 retired 拆出去——拆了就得同步改四处判据，而那四处正是靠
    共用谓词才只改了一处。
    """
    for s in ('banned', 'suspended', 'rejected', 'flagged', 'retired'):
        assert is_identity_terminal(s) is True
    for s in ('registered', 'imported', 'failed', 'pending', ''):
        assert is_identity_terminal(s) is False


def test_registered_and_imported_still_runnable():
    """反向保护：正常账号不能被误判成终态。"""
    assert is_identity_terminal('registered') is False
    assert is_identity_terminal('imported') is False


# --- 四处判据逐个钉住 -------------------------------------------------------
#
# 这些复刻各入口的过滤表达式，而不是调用真实函数——那几个函数深嵌在 run_daily_pipeline
# 的闭包里，起真流水线的代价远超收益。复刻的是**判据本身**，一旦有人改了入口却没改
# 这里，check 阶段的人工比对会发现；一旦有人删了谓词调用，这里立刻红。

def test_retired_excluded_from_payable():
    """_payable_now: 有密码 + 身份非终态 + 平台非终态。"""
    accounts = [
        {"email": "ok@x.com", "identity_status": "registered", "login_password": "p"},
        {"email": "gone@x.com", "identity_status": "retired", "login_password": "p"},
    ]
    payable = [a for a in accounts
               if a.get('login_password')
               and not is_identity_terminal(a.get('identity_status'))]
    assert [a['email'] for a in payable] == ["ok@x.com"]


def test_retired_excluded_from_reusable():
    """_reusable_recharged: 同样先过 is_identity_terminal 才看平台状态与余额。

    归档一个 recharged 账号后，它必须立刻退出复用池——否则「归档」对当前最主要的
    轮转来源（余额未满的已充值账号）完全无效。
    """
    accounts = [
        {"email": "reuse@x.com", "identity_status": "registered", "login_password": "p"},
        {"email": "gone@x.com", "identity_status": "retired", "login_password": "p"},
    ]
    platform_status = {"reuse@x.com": {"status": "recharged", "credits_balance": 20.0},
                       "gone@x.com": {"status": "recharged", "credits_balance": 20.0}}
    out = []
    for a in accounts:
        if not a.get('login_password'):
            continue
        if is_identity_terminal(a.get('identity_status')):
            continue
        if (platform_status.get(a['email']) or {}).get('status') != 'recharged':
            continue
        out.append(a['email'])
    assert out == ["reuse@x.com"]


def test_retired_excluded_from_registerable():
    """_registerable_imported 只认 identity_status == 'imported'，retired 天然不在内。"""
    accounts = [_acct("new@x.com", "imported", "link"),
                _acct("gone@x.com", "retired", "link")]
    registerable = [a for a in accounts
                    if (a.get('identity_status') or '') == 'imported']
    assert [a['email'] for a in registerable] == ["new@x.com"]


def test_retired_excluded_from_start_gates():
    """两个启动门（充值 routes._usable / 订阅）都只调 is_identity_terminal。"""
    accounts = [_acct("ok@x.com", "registered"), _acct("gone@x.com", "retired")]
    count = sum(1 for a in accounts
                if not is_identity_terminal(a.get('identity_status')))
    assert count == 1


# --- 重置资格判定 -----------------------------------------------------------

def test_only_failed_and_pending_are_resettable():
    accounts = [_acct("a@x.com", "failed", "l"), _acct("b@x.com", "pending", "l")]
    ready, bad, no_mail = classify_for_reset(accounts, set())
    assert [a['email'] for a in ready] == ["a@x.com", "b@x.com"]
    assert bad == [] and no_mail == []


def test_suspended_is_not_resettable():
    """刻意排除，不是遗漏。

    suspended = 注册出来就被 GitHub 挂起，同一邮箱重注册大概率还是同样下场，
    退回 imported 只会让它每轮白跑一次。
    """
    assert 'suspended' not in RESETTABLE
    ready, bad, _ = classify_for_reset([_acct("s@x.com", "suspended", "l")], set())
    assert ready == []
    assert [a['email'] for a in bad] == ["s@x.com"]


@pytest.mark.parametrize("status", ["registered", "retired", "imported", ""])
def test_other_statuses_are_skipped_not_reset(status):
    """批量接口会收到用户随手勾选的各种账号，全都要安全跳过。"""
    ready, bad, _ = classify_for_reset([_acct("x@x.com", status, "l")], set())
    assert ready == []
    assert len(bad) == 1


def test_account_without_mailbox_is_skipped():
    """无收码数据的账号重置了也领不走，只会让列表多几行看着能用其实不能用的账号。"""
    ready, bad, no_mail = classify_for_reset([_acct("n@x.com", "failed", "")], set())
    assert ready == [] and bad == []
    assert [a['email'] for a in no_mail] == ["n@x.com"]


def test_verify_link_alone_is_enough():
    """DB 自带 email_verify_link 即可——这是当前 38 个 failed 账号走的路径。"""
    ready, _, _ = classify_for_reset([_acct("a@x.com", "failed", "https://ruoanzhu/x")], set())
    assert len(ready) == 1


def test_xlsx_membership_alone_is_enough():
    """没有 link 但邮箱在 hotmail.xlsx 里也算有收码数据（订阅任务那批）。"""
    ready, _, _ = classify_for_reset([_acct("a@x.com", "failed", "")], {"a@x.com"})
    assert len(ready) == 1


def test_missing_xlsx_does_not_block_db_linked_accounts():
    """**当前主路径**：本机没有 hotmail.xlsx，load_hotmail_emails 返回空集。

    有 email_verify_link 的账号必须照样可重置。把 xlsx 缺失当错误或当作
    「无收码数据」，会让这个功能对当前全部 38 个 failed 账号失效。
    """
    emails = load_hotmail_emails(base_dir="/nonexistent-dir-for-test")
    assert emails == set()
    ready, _, no_mail = classify_for_reset(
        [_acct("a@x.com", "failed", "https://ruoanzhu/x")], emails)
    assert len(ready) == 1 and no_mail == []


def test_include_suspended_override():
    """命令行脚本的 --include-suspended：显式传 statuses 可以放行 suspended。"""
    ready, bad, _ = classify_for_reset(
        [_acct("s@x.com", "suspended", "l")], set(),
        statuses=RESETTABLE + ('suspended',))
    assert [a['email'] for a in ready] == ["s@x.com"]
    assert bad == []


def test_classify_is_pure_and_partitions_input():
    """三组不重不漏——漏一个就意味着有账号既没被重置也没被报告，静默消失。"""
    accounts = [_acct("a@x.com", "failed", "l"), _acct("b@x.com", "registered", "l"),
                _acct("c@x.com", "failed", ""), _acct("d@x.com", "pending", "l")]
    ready, bad, no_mail = classify_for_reset(accounts, set())
    got = {a['email'] for a in ready + bad + no_mail}
    assert got == {"a@x.com", "b@x.com", "c@x.com", "d@x.com"}
    assert len(ready) + len(bad) + len(no_mail) == len(accounts)


# --- 端点层 -----------------------------------------------------------------

import tempfile

from src.web.app import create_app


@pytest.fixture
def client(monkeypatch):
    """本文件只验状态流转，AdsPower 一律关掉。

    ⚠️ 不关的话这些测试会**真的调本机 AdsPower 接口**——开发机上客户端是常驻的，
    `_release_adspower_for` 会连上去。归档的环境释放行为在
    test_account_delete_adspower.py 里用假 pool 测（那边有全套基建）。
    """
    app = create_app(db_path=tempfile.mktemp(suffix='.db'))
    state = app.config['APP_STATE']
    monkeypatch.setattr(type(state), 'adspower_enabled',
                        property(lambda self: False), raising=False)
    yield app.test_client(), app.config['MODELS']
    app.config['REAPER'].stop()
    app.config['DB'].close()


def _seed(models, email, status, link=''):
    models['account'].upsert(email)
    models['account'].update_identity_status(email, status)
    if link:
        models['account'].backfill_email_verify_link(email, link)


def test_archive_sets_retired(client):
    c, models = client
    _seed(models, 'a@x.com', 'registered')
    body = c.post('/api/accounts/archive', json={'emails': ['a@x.com']}).get_json()

    assert body['retired'] == 1
    assert models['account'].get_by_emails(['a@x.com'])[0]['identity_status'] == 'retired'
    # AdsPower 未启用时归档照样成功，返回里说明原因（best-effort，不阻断）
    assert 'adspower' in body


def test_archive_succeeds_when_adspower_disabled(client):
    """AdsPower 关着时归档照样成功，并说明环境为什么没释放。

    与删账号同一条红线：外部依赖故障不许卡住账号管理。
    """
    c, models = client
    _seed(models, 'a@x.com', 'registered')
    body = c.post('/api/accounts/archive', json={'emails': ['a@x.com']}).get_json()
    assert body['retired'] == 1
    assert '未启用' in body['adspower']['reason']


def test_unarchive_only_touches_retired(client):
    """误传的正常账号不能被改状态。"""
    c, models = client
    _seed(models, 'gone@x.com', 'retired')
    _seed(models, 'ok@x.com', 'registered')
    _seed(models, 'bad@x.com', 'failed')

    body = c.post('/api/accounts/unarchive',
                  json={'emails': ['gone@x.com', 'ok@x.com', 'bad@x.com']}).get_json()

    assert body['restored'] == 1
    got = {a['email']: a['identity_status']
           for a in models['account'].get_by_emails(['gone@x.com', 'ok@x.com', 'bad@x.com'])}
    assert got == {'gone@x.com': 'registered', 'ok@x.com': 'registered',
                   'bad@x.com': 'failed'}


def test_reset_endpoint_partitions_results(client):
    """三类结果都要回传——只报数字会让用户以为功能坏了。"""
    c, models = client
    _seed(models, 'ok@x.com', 'failed', 'https://ruoanzhu/x')
    _seed(models, 'nolink@x.com', 'failed')
    _seed(models, 'wrong@x.com', 'registered', 'https://ruoanzhu/y')

    body = c.post('/api/accounts/reset-imported',
                  json={'emails': ['ok@x.com', 'nolink@x.com', 'wrong@x.com']}).get_json()

    assert body['reset'] == ['ok@x.com']
    assert body['skipped_no_mailbox'] == ['nolink@x.com']
    assert [s['email'] for s in body['skipped_status']] == ['wrong@x.com']
    assert models['account'].get_by_emails(['ok@x.com'])[0]['identity_status'] == 'imported'
    # 跳过的两个状态必须原样不动
    assert models['account'].get_by_emails(['nolink@x.com'])[0]['identity_status'] == 'failed'
    assert models['account'].get_by_emails(['wrong@x.com'])[0]['identity_status'] == 'registered'


@pytest.mark.parametrize("path", [
    '/api/accounts/archive', '/api/accounts/unarchive', '/api/accounts/reset-imported',
])
def test_empty_emails_returns_400(client, path):
    """空数组必须 400——这三个都是批量写操作，漏传参数就是全表事故。"""
    c, _ = client
    assert c.post(path, json={'emails': []}).status_code == 400
    assert c.post(path, json={}).status_code == 400
