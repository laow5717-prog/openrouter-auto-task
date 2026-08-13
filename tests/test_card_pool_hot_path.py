"""取卡热路径的两项提速：过期刷新节流 + 可选卡短缓存。

回归 2026-08-13 的现场：用了一个 3.2 万张卡的分组，`_eligible_cards` 单次要 715ms
（过期刷新 309ms + 可选卡查询 + 成功卡查询 306ms），而它在 8 个 worker 的取卡热路径
上、全部压在 Database 那把**全局互斥锁**上。后果是 worker 大半时间在排队等锁——8 个
worker 只跑得动 4 个浏览器，连每秒轮询的 /api/status 都被挤到超时、整个服务看起来
像卡死。

这里守的是两条不变式：
  1. 提速手段（节流、缓存）不能让「卡的可选性」变脏——那会让已判废的卡被反复刷；
  2. 手动入口（界面刷新）要能绕过节流拿到最新结果。
"""

import time

from src.models.card_pool import CardPoolModel
from src.models.card_group import CardGroupModel
from src.models.card_payment_state import CardPaymentStateModel
from src.utils import CARD_STATUS_EXPIRED

OC = 'opencode'


def _card(number, month='12', year='2030'):
    return {
        'number': number, 'expiry_month': month, 'expiry_year': year,
        'cvc': '123', 'first_name': 'T', 'last_name': 'U', 'country': 'US',
        'address': 'a', 'address2': '', 'city': 'c', 'state': 's',
        'zip': '10001', 'company': '',
    }


def _setup(db, n=3):
    # 缓存与节流都是**类级**状态，测试之间必须清干净，否则互相污染
    CardPoolModel._usable_cache.clear()
    CardPoolModel._expiry_refreshed_at.clear()
    pool = CardPoolModel(db)
    gid = CardGroupModel(db).create('g1', 'payment', '')
    pool.add_cards(gid, [_card(f'411111111111{1000 + i}') for i in range(n)])
    return pool, gid


# ---------- 过期刷新的节流 ----------

def test_expiry_refresh_is_throttled(db):
    """同一分组在 TTL 内只真扫一次——它在热路径上且握着全局 DB 锁。"""
    pool, gid = _setup(db)
    calls = []
    orig = pool.db.fetchall
    pool.db.fetchall = lambda *a, **k: (calls.append(a), orig(*a, **k))[1]

    pool.refresh_expired_status(gid)          # 第一次：真扫
    first = len(calls)
    pool.refresh_expired_status(gid)          # TTL 内：应直接返回
    pool.refresh_expired_status(gid)
    assert len(calls) == first, '节流没生效，热路径上又在重复全表扫描'

    pool.db.fetchall = orig


def test_force_bypasses_the_throttle(db):
    """界面手动刷新要能拿到最新结果，不受节流限制。"""
    pool, gid = _setup(db)
    pool.refresh_expired_status(gid)

    calls = []
    orig = pool.db.fetchall
    pool.db.fetchall = lambda *a, **k: (calls.append(a), orig(*a, **k))[1]
    pool.refresh_expired_status(gid, force=True)
    assert calls, 'force=True 仍被节流挡住了'
    pool.db.fetchall = orig


def test_throttle_still_marks_expired_cards(db):
    """节流是降频不是跳过：该标的过期卡还得标。

    这里要手动把 status 清空来构造用例——add_cards 入库时就已经判过一次过期，
    正常路径下过期卡进库即是 expired，refresh 扫不到它反而是对的（它只看未标记的行）。
    真正需要 refresh 兜底的是「入库时还没到期、后来才过期」的存量卡，形状就是这样。
    """
    pool, gid = _setup(db, n=0)
    pool.add_cards(gid, [_card('4111111111119999', month='01', year='2020')])
    db.execute("UPDATE card_pool SET status='' WHERE group_id=?", (gid,))
    CardPoolModel._expiry_refreshed_at.clear()

    assert pool.refresh_expired_status(gid, force=True) == 1
    cards = pool.get_cards_as_list(gid, OC)
    assert cards[0]['status'] == CARD_STATUS_EXPIRED


# ---------- 可选卡缓存 ----------

def test_usable_cache_is_used_within_ttl(db):
    """TTL 内重复取卡不该反复查库——3.2 万行的分组一次就是 200ms+。"""
    pool, gid = _setup(db)
    pool.get_usable_cards_as_list(OC, gid)

    calls = []
    orig = pool.db.fetchall
    pool.db.fetchall = lambda *a, **k: (calls.append(a), orig(*a, **k))[1]
    pool.get_usable_cards_as_list(OC, gid)
    pool.get_usable_cards_as_list(OC, gid)
    assert not calls, '缓存没命中，仍在重复查库'
    pool.db.fetchall = orig


def test_marking_invalid_invalidates_the_cache(db):
    """判废必须立刻可见。读到过期快照 = 拿一张已废的卡反复去刷，既烧账号也烧风控额度。"""
    pool, gid = _setup(db)
    before = pool.get_usable_cards_as_list(OC, gid)[0]
    target = before[0]['number']

    pool.mark_invalid_by_number(OC, target)

    after = pool.get_usable_cards_as_list(OC, gid)[0]
    assert len(after) == len(before) - 1
    assert target not in {c['number'] for c in after}


def test_adding_cards_invalidates_the_cache(db):
    """新导入的卡要立刻能被选中，否则用户会以为导入没生效。"""
    pool, gid = _setup(db)
    before = pool.get_usable_cards_as_list(OC, gid)[0]

    pool.add_cards(gid, [_card('4111111111117777')])

    after = pool.get_usable_cards_as_list(OC, gid)[0]
    assert len(after) == len(before) + 1


def test_deleting_cards_invalidates_the_cache(db):
    pool, gid = _setup(db)
    before = pool.get_usable_cards_as_list(OC, gid)[0]

    pool.delete_card(before[0]['id'])

    after = pool.get_usable_cards_as_list(OC, gid)[0]
    assert len(after) == len(before) - 1


def test_cache_does_not_leak_across_platforms(db):
    """缓存键必须含 platform：卡的可选性是按平台算的，串了就是跨平台污染。"""
    pool, gid = _setup(db)
    oc = pool.get_usable_cards_as_list(OC, gid)[0]
    pool.mark_invalid_by_number(OC, oc[0]['number'])

    # 在 opencode 判废，不该影响 infron 的可选集
    other = pool.get_usable_cards_as_list('infron', gid)[0]
    assert len(other) == len(oc)


def test_caller_mutation_does_not_poison_the_cache(db):
    """调用方对返回的列表做增删，不能影响下一次读到的内容。"""
    pool, gid = _setup(db)
    first = pool.get_usable_cards_as_list(OC, gid)[0]
    n = len(first)
    first.pop()                      # 调用方改自己那份

    second = pool.get_usable_cards_as_list(OC, gid)[0]
    assert len(second) == n, '缓存被调用方的修改污染了'


def test_cooldown_is_never_cached(db):
    """冷却状态变化最频繁，必须每次实时算——它不属于被缓存的那部分。"""
    pool, gid = _setup(db)
    from src.web.app import AppState, build_models
    models = build_models(db)
    st = AppState(db, models)
    st.platform = OC

    before = st._eligible_cards(gid, exclude_used=False)
    target = before[0]['number']
    CardPaymentStateModel(db).set_cooldown(OC, target, hours=12, reason='test')

    after = st._eligible_cards(gid, exclude_used=False)
    assert target not in {c['number'] for c in after}, '冷却没有实时生效'


# ---------- 索引 ----------

def test_hot_path_indexes_exist(db):
    """按 group_id 取卡的索引。缺了就是全表扫——3.2 万行的分组上 300ms 起步。

    card_pool 原先只有 UNIQUE(card_number, group_id) 的自动索引，它的前导列是
    card_number，按 group_id 过滤完全用不上。
    """
    names = {r['name'] for r in db.fetchall(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert 'idx_card_pool_group' in names
    assert 'idx_card_pool_group_status' in names
