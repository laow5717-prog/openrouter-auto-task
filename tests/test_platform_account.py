"""平台账号隔离测试（AC1）。

核心命题：同一个邮箱可以在多个平台各有一套独立的账号数据，互不覆盖；而身份层
（邮箱凭据 + GitHub 密码 + GitHub 注册结果）是这些平台共用的同一行。
"""

import pytest

from src.models.account import AccountModel
from src.models.platform_account import PlatformAccountModel


@pytest.fixture
def acct(db):
    return AccountModel(db)


@pytest.fixture
def pa(db):
    return PlatformAccountModel(db)


def test_same_email_two_platforms_independent(acct, pa):
    """AC1：同一邮箱在两个平台各有一行，状态/余额/apikey/密码互不覆盖。"""
    acct.upsert('a@x.com', login_password='github-pw', email_password='mail-pw',
                identity_status='registered')

    pa.ensure('opencode', 'a@x.com', status='recharged')
    pa.update_balance('opencode', 'a@x.com', 20.0)
    pa.update_apikey('opencode', 'a@x.com', 'sk-opencode')

    pa.ensure('infron', 'a@x.com', login_password='infron-random-pw', status='')
    pa.update_balance('infron', 'a@x.com', 3.5)

    oc = pa.get('opencode', 'a@x.com')
    inf = pa.get('infron', 'a@x.com')

    assert oc['status'] == 'recharged' and inf['status'] == ''
    assert oc['credits_balance'] == 20.0 and inf['credits_balance'] == 3.5
    assert oc['apikey'] == 'sk-opencode' and not inf['apikey']
    # 平台密码各自独立：opencode 走 OAuth 没有密码，infron 有自己的随机密码
    assert oc['login_password'] is None
    assert inf['login_password'] == 'infron-random-pw'


def test_identity_is_shared_across_platforms(acct, pa):
    """身份层只有一份：两个平台共用同一套邮箱凭据与 GitHub 密码。"""
    acct.upsert('a@x.com', login_password='github-pw', email_password='mail-pw',
                identity_status='registered')
    pa.ensure('opencode', 'a@x.com', status='recharged')
    pa.ensure('infron', 'a@x.com', status='')

    rows = acct.get_all()
    assert len(rows) == 1
    assert rows[0]['login_password'] == 'github-pw'
    assert rows[0]['email_password'] == 'mail-pw'


def test_platform_write_never_touches_identity(acct, pa):
    """平台侧写状态不得改动身份层——否则一个平台跑完会把别的平台也带停。"""
    acct.upsert('a@x.com', login_password='pw', identity_status='registered')

    pa.update_status('opencode', 'a@x.com', 'recharged')

    assert acct.get_all()[0]['identity_status'] == 'registered'


def test_identity_flag_does_not_write_platform_row(acct, pa):
    """GitHub 被 flag 是身份层的事，不该凭空造出平台账号行。"""
    acct.upsert('a@x.com', login_password='pw', identity_status='registered')
    acct.update_identity_status('a@x.com', 'flagged')

    assert pa.get('opencode', 'a@x.com') is None


def test_missing_row_means_platform_not_opened(pa):
    """「没有行」表示该邮箱尚未在此平台开通，与「有行但状态为空」不是一回事。"""
    assert pa.get('opencode', 'a@x.com') is None
    assert pa.get_status('opencode', 'a@x.com') == ''

    pa.ensure('opencode', 'a@x.com')
    assert pa.get('opencode', 'a@x.com') is not None
    assert pa.get_status('opencode', 'a@x.com') == ''


def test_ensure_does_not_clobber_omitted_fields(pa):
    """只想改状态的调用不能把密码清空（同 AccountModel.upsert 语义）。"""
    pa.ensure('infron', 'a@x.com', login_password='secret', status='pending')
    pa.ensure('infron', 'a@x.com', status='registered')

    row = pa.get('infron', 'a@x.com')
    assert row['login_password'] == 'secret'
    assert row['status'] == 'registered'


def test_update_tenant_id_ignores_empty(pa):
    """空 tenant_id 跳过，不覆盖已抓到的值。"""
    pa.update_tenant_id('opencode', 'a@x.com', 'wrk_123')
    pa.update_tenant_id('opencode', 'a@x.com', '')
    assert pa.get('opencode', 'a@x.com')['tenant_id'] == 'wrk_123'


def test_statuses_by_email_spans_all_platforms(pa):
    """跨平台状态汇总——AdsPower 回收判据的数据来源。"""
    pa.update_status('opencode', 'a@x.com', 'recharged')
    pa.update_status('infron', 'a@x.com', '')
    pa.update_status('opencode', 'b@x.com', 'subscribed')

    got = pa.statuses_by_email()
    assert got['a@x.com'] == {'opencode': 'recharged', 'infron': ''}
    assert got['b@x.com'] == {'opencode': 'subscribed'}


def test_delete_all_for_emails_clears_every_platform(pa):
    """删身份时要连带清掉它在所有平台的行，否则留下孤儿。"""
    pa.update_status('opencode', 'a@x.com', 'recharged')
    pa.update_status('infron', 'a@x.com', 'pending')

    pa.delete_all_for_emails(['a@x.com'])

    assert pa.get('opencode', 'a@x.com') is None
    assert pa.get('infron', 'a@x.com') is None
