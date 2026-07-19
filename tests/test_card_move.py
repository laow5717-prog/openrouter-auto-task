"""卡池跨分组移动 /api/card-pool/<id>/move 的契约测试。

重点：桶口径（「待验证」是派生桶，不是 status 值）、重复卡跳过而非删除、
非法入参零数据变更。
"""

import tempfile

import pytest

from src.web.app import create_app

from conftest import make_cards


@pytest.fixture
def client():
    app = create_app(db_path=tempfile.mktemp(suffix='.db'))
    yield app.test_client(), app.config['MODELS']
    app.config['REAPER'].stop()
    app.config['DB'].close()


@pytest.fixture
def groups(client):
    """源分组（payment，20 张全新卡）+ 空目标分组（bind）。"""
    _, models = client
    src = models['card_group'].create('源-支付卡', group_type='payment')
    dst = models['card_group'].create('目标-绑定卡', group_type='bind')
    models['card_pool'].add_cards(src, make_cards(20))
    return src, dst


def _bucket(models, gid):
    return models['card_pool'].count_buckets(gid)


def test_moves_requested_count(client, groups):
    c, models = client
    src, dst = groups
    r = c.post(f'/api/card-pool/{src}/move',
               json={'target_group_id': dst, 'bucket': 'unverified', 'limit': 5})
    assert r.status_code == 200
    assert r.get_json()['moved'] == 5
    assert _bucket(models, src)['unverified'] == 15
    assert _bucket(models, dst)['unverified'] == 5


def test_limit_over_available_moves_all(client, groups):
    c, models = client
    src, dst = groups
    r = c.post(f'/api/card-pool/{src}/move',
               json={'target_group_id': dst, 'bucket': 'unverified', 'limit': 9999})
    assert r.get_json()['moved'] == 20
    assert _bucket(models, src)['unverified'] == 0


def test_invalid_cards_never_move(client, groups):
    c, models = client
    src, dst = groups
    nums = [r['card_number'] for r in models['card_pool'].get_all_by_group(src)][:3]
    for n in nums:
        models['card_pool'].mark_status_by_number(n, 'invalid')

    c.post(f'/api/card-pool/{src}/move',
           json={'target_group_id': dst, 'bucket': 'unverified', 'limit': 9999})
    assert _bucket(models, dst)['invalid'] == 0
    assert _bucket(models, src)['invalid'] == 3, '无效卡应原地不动'


def test_valid_cards_excluded_from_unverified_bucket(client, groups):
    """已登记 valid_cards 的卡属于 valid 桶，不该被 unverified 移走。"""
    c, models = client
    src, dst = groups
    card = models['card_pool'].get_all_by_group(src)[0]
    models['valid_card'].record(card, source_type='payment', source_group_id=src)

    c.post(f'/api/card-pool/{src}/move',
           json={'target_group_id': dst, 'bucket': 'unverified', 'limit': 9999})
    assert _bucket(models, src)['valid'] == 1, '有效卡应留在源组'
    assert _bucket(models, dst)['valid'] == 0


def test_duplicate_card_is_skipped_not_deleted(client, groups):
    c, models = client
    src, dst = groups
    dup = models['card_pool'].get_all_by_group(src)[0]['card_number']
    # 绕过 add_cards 的跨组冲突检查直插——这里要构造的正是它平时拦下的重复态
    models['card_pool'].db.execute(
        """INSERT INTO card_pool (group_id, card_number, expiry_month, expiry_year,
                                  cvc, first_name, last_name)
           VALUES (?, ?, '12', '2030', '123', 'Dup', 'Card')""", (dst, dup))

    body = c.post(f'/api/card-pool/{src}/move',
                  json={'target_group_id': dst, 'bucket': 'unverified', 'limit': 9999}).get_json()
    assert body['skipped'] == 1
    survivors = [r['card_number'] for r in models['card_pool'].get_all_by_group(src)]
    assert dup in survivors, '重复卡必须留在源组，不能像 merge 那样被删掉'


@pytest.mark.parametrize('payload,code', [
    ({'bucket': 'unverified', 'limit': 5}, 400),                            # 缺目标分组
    ({'target_group_id': 'x', 'bucket': 'unverified', 'limit': 5}, 400),    # 目标非整数
    ({'target_group_id': 999, 'bucket': 'bogus', 'limit': 5}, 400),         # 桶非法
    ({'target_group_id': 999, 'bucket': 'unverified', 'limit': 0}, 400),    # 数量非正
    ({'target_group_id': 999, 'bucket': 'unverified', 'limit': -1}, 400),
    ({'target_group_id': 999, 'bucket': 'unverified', 'limit': 5}, 404),    # 目标不存在
])
def test_rejects_bad_input_without_touching_data(client, groups, payload, code):
    c, models = client
    src, dst = groups
    before = _bucket(models, src)

    r = c.post(f'/api/card-pool/{src}/move', json=payload)
    assert r.status_code == code, r.get_json()
    assert _bucket(models, src) == before, '非法入参不得产生任何数据变更'
    assert _bucket(models, dst)['total'] == 0


def test_rejects_same_source_and_target(client, groups):
    c, models = client
    src, _ = groups
    r = c.post(f'/api/card-pool/{src}/move',
               json={'target_group_id': src, 'bucket': 'unverified', 'limit': 5})
    assert r.status_code == 400
    assert _bucket(models, src)['unverified'] == 20


def test_merge_endpoint_still_works(client, groups):
    """回归：新端点不得影响原有 /merge（合并到新建分组）。"""
    c, models = client
    src, _ = groups
    body = c.post('/api/card-pool/merge',
                  json={'source_group_ids': [src], 'name': '合并组', 'type': 'bind'}).get_json()
    assert body['status'] == 'ok'
    assert body['moved'] == 20
    assert _bucket(models, body['group_id'])['unverified'] == 20
