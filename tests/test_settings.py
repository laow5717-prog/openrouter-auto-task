"""运行时配置：DB 覆盖值 + config.yaml 默认值。

两条性质决定这个功能是「能用」还是「看着能用」：

  1. **回落**：没在 UI 上设过的项要等于 config.yaml 的值，不能因为 DB 里没有就变成空串。
  2. **缓存失效**：AdsPowerClient 是惰性创建后缓存的。改了 key 不重建的话，进程会
     继续用旧的，界面显示保存成功、行为毫无变化——这类 bug 从现象上看不出根因。

API Key 明文回显、明文提交。早先做过掩码，去掉了：本机单人使用，同库里 GitHub 密码、
邮箱密码本来就明文躺着，单给这一个字段打码挡不住任何真实威胁，却要引入「提交上来的
是新 key 还是掩码」的判断——判错就把真 key 覆盖成掩码串，那才是真实的破坏。
"""

import tempfile

import pytest

from src.models import settings as S
from src.models.settings import SettingsModel, as_bool


class _FakeCfg:
    """假的 cfg.adspower，用来喂默认值——不去动进程级单例。"""

    def __init__(self, enabled=True, api_key='YAMLKEY', base_url='http://yaml.local:1'):
        self.enabled = enabled
        self.api_key = api_key
        self.base_url = base_url


@pytest.fixture
def sm(db):
    return SettingsModel(db)


# ---------- 键值层 ----------


def test_unset_key_returns_the_default(sm):
    assert sm.get('nope') is None
    assert sm.get('nope', 'fallback') == 'fallback'


def test_set_then_get(sm):
    sm.set('k', 'v')
    assert sm.get('k') == 'v'


def test_set_is_idempotent_and_overwrites(sm):
    sm.set('k', 'v1')
    sm.set('k', 'v2')
    assert sm.get('k') == 'v2'


def test_set_none_removes_the_override(sm):
    """删除覆盖值 = 回落 yaml，与「设成空串」是两回事。"""
    sm.set('k', 'v')
    sm.set('k', None)
    assert sm.get('k') is None


def test_get_many_only_returns_stored_keys(sm):
    sm.set('a', '1')
    assert sm.get_many(['a', 'b']) == {'a': '1'}


# ---------- bool 解释 ----------


@pytest.mark.parametrize('raw,expect', [
    ('1', True), ('true', True), ('True', True), ('yes', True), ('on', True),
    ('0', False), ('false', False), ('no', False), ('随便', False),
])
def test_as_bool_reads_the_usual_spellings(raw, expect):
    assert as_bool(raw) is expect


@pytest.mark.parametrize('raw', [None, '', '   '])
def test_blank_means_unset_not_false(raw):
    """空 = 「没设过」，不是「设成了 False」。混淆的话，从没碰过开关也会被当成关。"""
    assert as_bool(raw, default=True) is True
    assert as_bool(raw, default=False) is False


# ---------- 覆盖层 ----------


def test_falls_back_to_yaml_when_nothing_is_set(sm):
    """AC2：DB 没设过 → 用 config.yaml 的值，不是空串。"""
    eff = sm.adspower_effective(_FakeCfg())
    assert eff == {'enabled': True, 'api_key': 'YAMLKEY', 'base_url': 'http://yaml.local:1'}


def test_db_value_wins_over_yaml(sm):
    """AC3：设过就用 DB 的。"""
    sm.set(S.KEY_ADSPOWER_API_KEY, 'DBKEY')
    sm.set(S.KEY_ADSPOWER_BASE_URL, 'http://db.local:2')
    sm.set(S.KEY_ADSPOWER_ENABLED, '0')

    eff = sm.adspower_effective(_FakeCfg())
    assert eff == {'enabled': False, 'api_key': 'DBKEY', 'base_url': 'http://db.local:2'}


def test_each_field_falls_back_independently(sm):
    """只设一项时，其余仍走 yaml——不能因为表里有行就整组当成已配置。"""
    sm.set(S.KEY_ADSPOWER_API_KEY, 'DBKEY')
    eff = sm.adspower_effective(_FakeCfg())
    assert eff['api_key'] == 'DBKEY'
    assert eff['base_url'] == 'http://yaml.local:1'
    assert eff['enabled'] is True


def test_enabled_false_in_db_overrides_true_in_yaml(sm):
    """开关的关是「真的关」，不能被当成空值回落成 yaml 的 True。

    这是整个覆盖层最容易写错的一处：用 `or` 兜底的话 '0' 是真值没问题，
    但 False 会被 or 掉，于是永远关不掉。
    """
    sm.set(S.KEY_ADSPOWER_ENABLED, '0')
    assert sm.adspower_effective(_FakeCfg(enabled=True))['enabled'] is False


# ---------- 缓存失效（AC4/AC5） ----------


def _state(tmpdb):
    """建一个 AdsPower 相关行为挂在 AppState 上（经属性代理到 SharedResources），
    所以这里要的是 AppState 而不是裸的共享资源。"""
    from src.models.database import Database
    from src.web.app import AppState, build_models
    db = Database(db_path=tmpdb)
    return db, AppState(db, build_models(db))


def test_client_is_rebuilt_when_the_key_changes(monkeypatch):
    """AC4：换 key 后不重启也要用新 key。

    不重建的话，界面显示保存成功、进程却一直拿着旧 client——最难查的一类 bug。
    """
    from src.config import cfg
    monkeypatch.setattr(cfg.adspower, 'enabled', True)
    monkeypatch.setattr(cfg.adspower, 'api_key', 'OLD')

    db, shared = _state(tempfile.mktemp(suffix='.db'))
    try:
        built = []

        class _Client:
            def __init__(self, base_url, api_key):
                built.append(api_key)

        class _Pool:
            def __init__(self, *a, **kw):
                pass

        monkeypatch.setattr('src.services.adspower.AdsPowerClient', _Client)
        monkeypatch.setattr('src.browser.adspower_driver.AdsPowerProfilePool', _Pool)

        shared._ensure_adspower()
        shared._ensure_adspower()          # 第二次应命中缓存，不重建
        assert built == ['OLD']

        shared.models['settings'].set(S.KEY_ADSPOWER_API_KEY, 'NEW')
        shared._ensure_adspower()
        assert built == ['OLD', 'NEW'], '改 key 后没有重建客户端'
    finally:
        db.close()


def test_client_is_rebuilt_when_the_base_url_changes(monkeypatch):
    """AC5：地址变了同样要重建。"""
    from src.config import cfg
    monkeypatch.setattr(cfg.adspower, 'enabled', True)

    db, shared = _state(tempfile.mktemp(suffix='.db'))
    try:
        built = []

        class _Client:
            def __init__(self, base_url, api_key):
                built.append(base_url)

        class _Pool:
            def __init__(self, *a, **kw):
                pass

        monkeypatch.setattr('src.services.adspower.AdsPowerClient', _Client)
        monkeypatch.setattr('src.browser.adspower_driver.AdsPowerProfilePool', _Pool)

        shared._ensure_adspower()
        shared.models['settings'].set(S.KEY_ADSPOWER_BASE_URL, 'http://other:9')
        shared._ensure_adspower()

        assert len(built) == 2 and built[1] == 'http://other:9'
    finally:
        db.close()


def test_disabling_from_the_ui_turns_off_the_whole_integration(monkeypatch):
    """AC9：UI 关掉开关后 browser_factory 返回 None，全链路回退本地 profile。"""
    from src.config import cfg
    monkeypatch.setattr(cfg.adspower, 'enabled', True)

    db, shared = _state(tempfile.mktemp(suffix='.db'))
    try:
        assert shared.adspower_enabled is True
        shared.models['settings'].set(S.KEY_ADSPOWER_ENABLED, '0')
        assert shared.adspower_enabled is False
        assert shared._ensure_adspower() == (None, None)
    finally:
        db.close()


def test_connectivity_check_reuses_the_shared_client(monkeypatch):
    """连通性检测必须复用共享客户端，不能每次新建。

    AdsPowerClient 的 _throttle 是**实例级**的（见 SharedResources 的 docstring）：
    多一个实例 = 多一倍请求速率，会撞 AdsPower 本地接口的频率限制。任务正在跑时
    点一下检测，可能把正在跑的任务一起撞挂——2026-08-05 连点四次就复现过，
    第四次被拒、还被错报成「API Key 不对」。
    """
    import inspect
    from src.api import routes

    src = inspect.getsource(routes.test_adspower_settings)
    assert 'state._ensure_adspower()' in src, '没有复用共享客户端'
    # 新建实例只允许出现在「开关关着、没有共享实例」那条分支里，所以那行 import
    # 必须排在取共享实例之后。比对 import 而不是类名——类名在注释里也会出现。
    assert (src.index('client, _pool = state._ensure_adspower()')
            < src.index('from src.services.adspower import AdsPowerClient')), \
        '在拿到共享实例之前就新建了客户端'


def test_api_key_is_returned_in_plaintext(monkeypatch):
    """GET 直接回明文 key，不做掩码。

    钉住它是因为「密钥要打码」是个很强的直觉，将来很容易有人顺手加回去；
    加回去就得同时处理「提交上来的是不是掩码」，而那正是当初去掉它的原因。
    """
    import inspect
    from src.api import routes

    src = inspect.getsource(routes.get_adspower_settings)
    assert '"api_key": eff[\'api_key\']' in src, 'GET 没有返回明文 key'
    assert 'mask' not in src.lower(), '又把掩码加回来了'

    save_src = inspect.getsource(routes.save_adspower_settings)
    assert 'mask' not in save_src.lower(), 'PUT 里还有掩码判断'
