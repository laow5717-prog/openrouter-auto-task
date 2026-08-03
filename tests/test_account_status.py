"""AccountModel 身份状态修正测试（R3：误标 failed → registered）。"""

from src.models.account import AccountModel


def test_reset_failed_to_registered_only_touches_failed(db):
    """被误标 'failed' 的账号批量改回 'registered'，其它状态一律不动。"""
    acct = AccountModel(db)
    acct.upsert('a@x.com', login_password='pw', identity_status='failed')
    acct.upsert('b@x.com', login_password='pw', identity_status='failed')
    acct.upsert('c@x.com', login_password='pw', identity_status='registered')
    acct.upsert('d@x.com', login_password='pw', identity_status='pending')
    acct.upsert('e@x.com', login_password='pw', identity_status='suspended')

    n = acct.reset_failed_to_registered()

    assert n == 2
    by_email = {a['email']: a['identity_status'] for a in acct.get_all()}
    assert by_email['a@x.com'] == 'registered'
    assert by_email['b@x.com'] == 'registered'
    assert by_email['c@x.com'] == 'registered'   # 本就 registered，不变
    assert by_email['d@x.com'] == 'pending'      # 其它身份状态不动
    assert by_email['e@x.com'] == 'suspended'


def test_reset_failed_to_registered_idempotent(db):
    """无 failed 时返回 0，可重复运行。"""
    acct = AccountModel(db)
    acct.upsert('a@x.com', login_password='pw', identity_status='registered')
    assert acct.reset_failed_to_registered() == 0
    assert acct.reset_failed_to_registered() == 0


def test_platform_status_is_not_touched_by_identity_reset(db):
    """身份层的批量修正绝不能碰平台状态——两层是独立的。"""
    from src.models.platform_account import PlatformAccountModel

    acct = AccountModel(db)
    pa = PlatformAccountModel(db)
    acct.upsert('a@x.com', login_password='pw', identity_status='failed')
    pa.update_status('opencode', 'a@x.com', 'recharged')

    acct.reset_failed_to_registered()

    assert pa.get_status('opencode', 'a@x.com') == 'recharged'
