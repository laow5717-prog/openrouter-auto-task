"""AppState 拆分后的共享/隔离边界。

多平台并发的正确性全建立在「哪些资源共享、哪些按平台隔离」这条线上，而画错线的
后果是**静默错误**：共享了本该隔离的 → 两个平台互相覆盖状态；隔离了本该共享的 →
排他保护失效（同一张卡在两个平台同时扣款、同一个代理发给两边）。

两种错法都不会报错，所以边界必须被测试钉死。
"""

import tempfile

import pytest

from src.web.app import AppState, SharedResources, create_app


@pytest.fixture
def app():
    a = create_app(db_path=tempfile.mktemp(suffix='.db'))
    yield a
    a.config['REAPER'].stop()
    a.config['DB'].close()


# ---------- 共享的必须真共享 ----------

SHARED_ATTRS = [
    ('db', 'Database 自带锁，本身线程安全'),
    ('models', 'model 方法已全部显式收 platform 参数'),
    ('open_browsers', 'Chrome profile 目录按 email，同一 email 不能开两次'),
    ('account_registry', '账号排他是 email 级的，跨平台也必须互斥'),
    ('payment_registry', '同卡在两处同时授权是盗刷特征，会触发发卡行风控'),
    ('proxy_registry', '出口 IP 是物理资源，反关联的意义就在于不重复'),
]


@pytest.mark.parametrize('attr,why', SHARED_ATTRS)
def test_shared_resources_are_the_same_object(app, attr, why):
    ctxs = app.config['RUN_CONTEXTS']
    assert len(ctxs) >= 2, '至少要有两个平台才谈得上并发'
    objs = [getattr(c, attr) for c in ctxs.values()]
    first = objs[0]
    for o in objs[1:]:
        assert o is first, f'{attr} 必须跨平台共享：{why}'


def test_adspower_client_and_pool_are_shared(app):
    """AdsPower 客户端与池必须单例。

    客户端的 _throttle 限流状态是**实例级**的，两个实例等于两倍请求速率；
    池的 _lock 串行化「挑代理→建环境→撞配额→回收→重试」整条链，拆开会活锁
    （A 刚删出的配额被 B 抢走）。
    """
    ctxs = list(app.config['RUN_CONTEXTS'].values())
    a, b = ctxs[0], ctxs[1]

    a._adspower_client = object()
    assert b._adspower_client is a._adspower_client, '客户端被拆开了，会撞接口频率限制'

    a._adspower_pool = object()
    assert b._adspower_pool is a._adspower_pool, '池被拆开了，会活锁'

    assert a._adspower_lock is b._adspower_lock, '惰性构造锁必须共享'


# ---------- 隔离的必须真隔离 ----------

ISOLATED_ATTRS = ['is_running', 'stop_requested', 'success_count',
                  'fail_count', 'current_action', 'parallel_mode',
                  'current_card_task_id']


@pytest.mark.parametrize('attr', ISOLATED_ATTRS)
def test_run_state_does_not_leak_across_platforms(app, attr):
    """一个平台的运行状态改动不能影响另一个平台。

    这些字段曾经是全局单例属性——两个平台同时跑会互相覆盖，
    第二个任务一启动，第一个的选卡与记账就全串到另一个平台去了。
    """
    ctxs = list(app.config['RUN_CONTEXTS'].values())
    a, b = ctxs[0], ctxs[1]
    before = getattr(b, attr)

    setattr(a, attr, 'SENTINEL')
    assert getattr(b, attr) == before, f'{attr} 串台了'


def test_each_context_knows_its_own_platform(app):
    for slug, ctx in app.config['RUN_CONTEXTS'].items():
        assert ctx.platform == slug


def test_logs_and_workers_are_per_platform(app):
    ctxs = list(app.config['RUN_CONTEXTS'].values())
    a, b = ctxs[0], ctxs[1]

    assert a.logs is not b.logs, '日志缓冲共享会让两个平台的日志串在一起'
    assert a.workers is not b.workers
    assert a.primary_worker is not b.primary_worker, \
        '两个平台各自要有主 worker，否则截图与 active driver 会互相覆盖'


def test_adspower_started_is_per_platform(app):
    """收尾时只该关自己起的环境，不能连另一个平台正在用的一起关掉。"""
    ctxs = list(app.config['RUN_CONTEXTS'].values())
    a, b = ctxs[0], ctxs[1]

    a._adspower_started.add('pid-a')
    assert 'pid-a' not in b._adspower_started
    assert a._adspower_started_lock is not b._adspower_started_lock, \
        '各自的集合要用各自的锁，共用会让两个平台互相阻塞'


# ---------- 向后兼容 ----------

def test_app_state_still_constructible_without_shared():
    """不传 shared 时自建一个——保持既有调用方（含大量测试）不用改。"""
    st = AppState(db=None, models={})
    assert isinstance(st.shared, SharedResources)
    assert st.platform == AppState.DEFAULT_PLATFORM


def test_app_state_config_points_at_the_default_platform(app):
    assert app.config['APP_STATE'] is app.config['RUN_CONTEXTS'][AppState.DEFAULT_PLATFORM]


# ---------- 并发度是「每平台」的 ----------

def test_each_platform_gets_its_own_worker_pool(app):
    """max_workers 是**每个平台**的并发度，不是全局的。

    两个平台各建各的 WorkerPool（构造时传的是自己的 ctx），所以
    max_workers=2 意味着总共 4 个浏览器。把池挪到 SharedResources 会让
    两个平台抢同一批 worker——那不是并发，是把并发度砍半。
    """
    import inspect
    from src.web.app import AppState

    src = inspect.getsource(AppState.run_daily_pipeline)
    assert 'WorkerPool(self,' in src, '池必须用本平台的 ctx 构造'
    assert 'WorkerPool(self.shared' not in src, '池挪到共享层会让两个平台抢同一批 worker'

    # 两个 ctx 的 worker 实体确实是分开的
    ctxs = list(app.config['RUN_CONTEXTS'].values())
    a, b = ctxs[0], ctxs[1]
    a.ensure_workers(2)
    b.ensure_workers(2)
    assert set(map(id, a.workers.values())).isdisjoint(map(id, b.workers.values()))


def test_total_browsers_stay_within_the_quota():
    """各平台并发度**累加**后不能超过 AdsPower 总配额。

    注意是累加不是相乘——并发度现在按平台单独配（opencode 4 / infron 2），
    合计 6。哪天有人把某个平台拉高或再接一个平台，这条会先红，
    而不是等生产上撞配额。
    """
    import src.platforms as platforms
    from src.browser.adspower_quota import AdsPowerQuota
    from src.config import cfg

    slugs = list(platforms.all_slugs())
    worst = sum(cfg.concurrency.workers_for(s) for s in slugs)
    assert worst <= AdsPowerQuota.TOTAL, (
        f'满负荷需要 {worst} 个环境，超过配额 {AdsPowerQuota.TOTAL}——'
        '要么降并发度，要么这个组合跑不起来')


def test_per_platform_workers_fit_their_reserved_quota():
    """单个平台的并发度不能超过它自己的配额额度。

    超了的话它每轮都得去借，而借用要看对方脸色——对方满负荷时就只能干等，
    表现是「配了 4 个 worker 却总有一个卡在等环境」。
    """
    import src.platforms as platforms
    from src.config import cfg

    reserved = cfg.adspower.platform_quota or {}
    for slug in platforms.all_slugs():
        want = cfg.concurrency.workers_for(slug)
        have = reserved.get(slug)
        if have is None:
            continue
        assert want <= have, (
            f'{slug} 配了 {want} 个 worker 但自有额度只有 {have}——'
            '会长期依赖借用，对方忙时就卡住')


def test_workers_for_falls_back_to_the_default():
    """没单独配的平台用 max_workers，不能拿不到 worker。"""
    from src.config import ConcurrencyConfig

    c = ConcurrencyConfig(max_workers=3, platform_workers={'opencode': 4})
    assert c.workers_for('opencode') == 4
    assert c.workers_for('从没听说过的平台') == 3


def test_pipeline_uses_the_per_platform_worker_count():
    """流水线必须按**自己这个平台**取并发度，不能读全局 max_workers。"""
    import inspect
    from src.web.app import AppState

    src = inspect.getsource(AppState.run_daily_pipeline)
    assert 'workers_for(self.platform)' in src, \
        '仍在用全局 max_workers，按平台配的值不会生效'
