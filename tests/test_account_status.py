"""AccountModel 状态修正测试（R3：误标 failed → registered）。"""

from src.models.account import AccountModel


def test_reset_failed_to_registered_only_touches_failed(db):
    """被误标 'failed' 的账号批量改回 'registered'，其它状态一律不动。"""
    acct = AccountModel(db)
    acct.upsert('a@x.com', login_password='pw', status='failed')
    acct.upsert('b@x.com', login_password='pw', status='failed')
    acct.upsert('c@x.com', login_password='pw', status='registered')
    acct.upsert('d@x.com', login_password='pw', status='recharged')
    acct.upsert('e@x.com', login_password='pw', status='archived')

    n = acct.reset_failed_to_registered()

    assert n == 2
    by_email = {a['email']: a['status'] for a in acct.get_all()}
    assert by_email['a@x.com'] == 'registered'
    assert by_email['b@x.com'] == 'registered'
    assert by_email['c@x.com'] == 'registered'   # 本就 registered，不变
    assert by_email['d@x.com'] == 'recharged'    # 其它状态不动
    assert by_email['e@x.com'] == 'archived'     # 归档状态不动


def test_reset_failed_to_registered_idempotent(db):
    """无 failed 时返回 0，可重复运行。"""
    acct = AccountModel(db)
    acct.upsert('a@x.com', login_password='pw', status='registered')
    assert acct.reset_failed_to_registered() == 0
    assert acct.reset_failed_to_registered() == 0
