"""运行时配置：DB 覆盖值 + config.yaml 默认值。

两层的分工是本模块的全部内容：

    config.yaml   默认值。手写、注释密集，**只读**——不回写它，
                  否则 yaml.safe_dump 会把每一项的说明注释抹掉。
    settings 表   覆盖值。UI 上改的东西落这里，只存**用户真的设过**的项。

「只存设过的项」是刻意的，而不是启动时把 yaml 的值全量灌进表里。灌进去的话，
此后改 config.yaml 就再也不生效了——用户改了文件、重启、发现毫无变化，
而 UI 上显示的还是当初灌进去的旧值，这种「配置有两个真相且互相打架」的状态
排查起来极其痛苦。没设过就回落 yaml，两边的职责才是清楚的。

值一律以 TEXT 存取，类型由读取方按 key 解释（见 as_bool）。配置项本来就是异构的，
为它们造一套类型系统不值当。
"""

# 已知配置键。集中列出是为了让「UI 能配什么」在代码里有一处可查，
# 而不是散落在路由和前端里靠字符串对暗号。
KEY_ADSPOWER_ENABLED = 'adspower.enabled'
KEY_ADSPOWER_API_KEY = 'adspower.api_key'
KEY_ADSPOWER_BASE_URL = 'adspower.base_url'

_TRUE = ('1', 'true', 'yes', 'on')


def as_bool(text, default=False):
    """把 TEXT 值解释成 bool。None / 空串一律回落 default —— 空串是「没设过」，
    不是「设成了 False」，这两者必须区分，否则 UI 上从没碰过开关也会把它按关处理。"""
    if text is None or str(text).strip() == '':
        return default
    return str(text).strip().lower() in _TRUE


class SettingsModel:
    def __init__(self, db):
        self.db = db

    def get(self, key, default=None):
        """取覆盖值。没设过返回 default（**不是**空串）。"""
        row = self.db.fetchone("SELECT value FROM settings WHERE key=?", (key,))
        if row is None:
            return default
        val = row['value']
        return default if val is None else val

    def set(self, key, value):
        """写覆盖值。value 为 None 表示删除该项（回落 yaml 默认值）。"""
        if value is None:
            self.db.execute("DELETE FROM settings WHERE key=?", (key,))
            return
        self.db.execute(
            "INSERT INTO settings (key, value, updated_at) "
            "VALUES (?, ?, datetime('now','localtime')) "
            "ON CONFLICT(key) DO UPDATE SET "
            "  value=excluded.value, updated_at=excluded.updated_at",
            (key, str(value)),
        )

    def get_many(self, keys):
        """批量取，返回 {key: value}，只含设过的项。"""
        if not keys:
            return {}
        ph = ','.join('?' * len(keys))
        rows = self.db.fetchall(
            f"SELECT key, value FROM settings WHERE key IN ({ph})", list(keys))
        return {r['key']: r['value'] for r in rows}

    # ---------- AdsPower 生效配置 ----------

    def adspower_effective(self, cfg_adspower):
        """AdsPower 三项的**生效值**：DB 设过就用 DB，否则用 yaml。

        cfg_adspower 传 cfg.adspower（而不是在这里 import 全局 cfg）——让默认值从
        参数进来，这个函数才能被测试直接喂一个假配置，不必去动进程级单例。
        """
        got = self.get_many([KEY_ADSPOWER_ENABLED,
                             KEY_ADSPOWER_API_KEY,
                             KEY_ADSPOWER_BASE_URL])
        enabled_raw = got.get(KEY_ADSPOWER_ENABLED)
        return {
            'enabled': (as_bool(enabled_raw) if enabled_raw not in (None, '')
                        else bool(cfg_adspower.enabled)),
            'api_key': got.get(KEY_ADSPOWER_API_KEY) or cfg_adspower.api_key,
            'base_url': got.get(KEY_ADSPOWER_BASE_URL) or cfg_adspower.base_url,
        }


# API Key 在界面上**明文**回显、明文提交。
#
# 早先做过掩码（回显 `d6c9…4f8f`、提交掩码原值视为未修改），后来去掉了：这是本机
# 单人使用的工具，同一个库里 GitHub 密码、邮箱密码本来就明文躺着，单给这一个字段
# 打码挡不住任何真实威胁，却带来一套「提交上来的到底是新 key 还是掩码」的判断——
# 判错一次就把用户的真 key 覆盖成 `d6c9…4f8f` 这种串，反倒是真实的破坏。
# 明文回显还有个实际好处：填错时一眼能看出来。
