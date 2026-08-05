"""
配置加载模块
从 config.yaml 文件加载配置，支持数据类的类型安全访问
"""

import random
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, Dict, Any

try:
    import yaml
except ImportError:
    print("Missing PyYAML dependency. Install: pip install pyyaml")
    sys.exit(1)



# === 浏览器 profile 相关常量 ===
# 单个持久化 profile 的缓存类目录（Cache / Code Cache / Service Worker / GPUCache 等）
# 合计上限（MB），超限则在浏览器启动前整体清理。这些目录不含登录态
# （Cookies / Login Data / Local Storage / Local State 均不在其中），清理后仍保持登录。
# 缓存腐坏——尤其 Service Worker——会让站点导航请求拿到空响应，
# 表现为「地址栏 URL 正常但页面全白」，且 Chrome 不会自愈。
PROFILE_CACHE_LIMIT_MB = 200


@dataclass
class RegistrationConfig:
    total_accounts: int = 1


@dataclass
class EmailConfig:
    worker_url: str = ""
    domain: str = ""
    prefix_length: int = 10
    wait_timeout: int = 120
    poll_interval: int = 3
    admin_password: str = ""


@dataclass
class BrowserConfig:
    max_wait_time: int = 600
    short_wait_time: int = 120
    user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


@dataclass
class PasswordConfig:
    length: int = 16
    charset: str = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!@#$%"


@dataclass
class RetryConfig:
    http_max_retries: int = 5
    http_timeout: int = 30
    error_page_max_retries: int = 5
    button_click_max_retries: int = 3


@dataclass
class BatchConfig:
    interval_min: int = 5
    interval_max: int = 15


@dataclass
class CaptchaConfig:
    api_key: str = ""
    service: str = "2captcha"


@dataclass
class DatabaseConfig:
    path: str = "data/openrouter_auto.db"


@dataclass
class FilesConfig:
    accounts_file: str = "registered_accounts.txt"


@dataclass
class CreditCardConfig:
    number: str = ""
    expiry_month: str = ""
    expiry_year: str = ""
    cvc: str = ""
    first_name: str = ""
    last_name: str = ""
    country: str = "United States"
    address: str = ""
    address2: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""
    company: str = ""


@dataclass
class PaymentConfig:
    max_bindable_cards: int = 2
    credit_cards: list = field(default_factory=list)


@dataclass
class ConcurrencyConfig:
    """每日流水线的并发执行配置。

    max_workers: **每个平台**同时驱动的浏览器 worker 数，作为未单独配置的平台的
                 默认值。有效范围 1-4，越界由 WorkerPool 夹紧；上限 4 源于有头
                 Chrome 每实例约 300-500MB 内存的开销。
                 多平台并发时总数要按平台累加，还受 adspower.total_quota 约束。
    platform_workers: 按平台覆盖，如 {opencode: 4, infron: 2}。没列到的平台用
                 max_workers。分开配的理由很实际——不同平台单账号耗时差很多，
                 慢的那个多开几个才不至于拖住整体。
    claim_timeout_minutes: 卡被领取（processing 态）后多久无进展视为 worker 失联，
                 由回收线程重置回 pending。
    """
    max_workers: int = 1
    platform_workers: dict = field(default_factory=dict)
    claim_timeout_minutes: int = 20

    def workers_for(self, platform):
        """该平台的并发度。未单独配置时回落到 max_workers。"""
        try:
            return int(self.platform_workers[platform])
        except (KeyError, TypeError, ValueError):
            return self.max_workers


@dataclass
class RechargeConfig:
    """充值策略参数。四个字段各对应一条业务规则，全部可在 config.yaml 里调。

    amount_min / amount_max: 每笔充值的随机金额区间（美元，取整数）。
             **按笔取值**而非按账号——同一个账号连充几笔时每笔金额都不同，更像真人。
             两个平台的充值页都接受任意金额：infron 命中不了预设档位会回退自定义
             输入框，opencode 直接把数字敲进输入框。

    balance_cap: 单账号余额上限。一个账号在一次会话里连续充值，余额达到此值就换下
             一个账号。**不能拿 adapter.recharge_skip_balance 代替**——那是登录时的
             归档预检阈值（两平台都是 20），拿来当循环上限的话第一笔充完就会停，
             「成功后继续充」这条规则直接失效。

    max_fail_streak: 一张卡在某平台**连续**失败多少次才判废。中间成功一次即清零。
             此前的口径是首次被拒即永久 invalid，一次发卡行瞬时抖动就能烧掉一张好卡。

    fail_cooldown_hours: 失败卡冷却时长的**下限**，不是时长本身。实际到期时刻是
             max(次日 00:00, now + 本值)——规则是「一张卡当日付款失败，当日就不再
             被选用」，跟着自然日走才对得上发卡行按自然日算的 velocity 风控。
             取 max 是因为只用次日零点的话，23:59 失败的卡一分钟后就能再刷。
             默认 12：中午前失败的卡次日零点回来，中午后失败的按 12h 滑动。
             注意**只有失败才冷却**，成功的卡可立即复用，否则「成功后继续充」无从谈起。
    """
    amount_min: int = 20
    amount_max: int = 100
    balance_cap: float = 200.0
    max_fail_streak: int = 3
    fail_cooldown_hours: int = 12

    # 金额的绝对边界。夹紧而非报错：一个越界的配置值不该让整条充值流水线停摆。
    AMOUNT_FLOOR = 1
    AMOUNT_CEILING = 1000

    def bounds(self):
        """夹紧后的 (min, max)。min > max 时以 min 为准，两端都取它。"""
        lo = max(self.AMOUNT_FLOOR, min(self.AMOUNT_CEILING, int(self.amount_min)))
        hi = max(self.AMOUNT_FLOOR, min(self.AMOUNT_CEILING, int(self.amount_max)))
        return (lo, hi) if lo <= hi else (lo, lo)

    def fail_threshold(self):
        """判废所需的连续失败次数，至少为 1。

        下限不是形式主义：编排层判的是 `streak >= max_fail_streak`，而 streak 从 1 起数。
        配成 0 或负数会让这个条件恒真——**连计数都不用读**，第一次拒付就判废，
        比改造前还激进。手改 config.yaml 时把它当成开关写个 0 是很自然的笔误，
        而后果（整池好卡被一次性烧掉）不可逆。
        """
        try:
            return max(1, int(self.max_fail_streak))
        except (TypeError, ValueError):
            return 1

    def pick_amount(self):
        """取一笔充值金额（整数美元）。

        这里再夹一次而不是信任调用方：API 层虽然会校验，但 config.yaml 是人手写的，
        也可能直接构造实例。让它永远返回一个可用的数，而不是抛异常打断充值。
        """
        lo, hi = self.bounds()
        return random.randint(lo, hi)

    def with_overrides(self, **kw):
        """按传入的非 None 值产出一个**新实例**（UI 覆盖 yaml 默认值）。

        必须返回新对象、绝不原地改：cfg 是进程级全局单例，两个平台并发跑时原地改
        会让后启动的那个平台把前一个的参数覆盖掉——这类竞态不报错、不留日志。
        """
        vals = {f: getattr(self, f) for f in
                ('amount_min', 'amount_max', 'balance_cap',
                 'max_fail_streak', 'fail_cooldown_hours')}
        for k, v in kw.items():
            if k in vals and v is not None:
                vals[k] = v
        return RechargeConfig(**vals)


@dataclass
class AdsPowerConfig:
    """AdsPower 指纹浏览器接入配置。

    enabled: 总开关。为 False 时每日任务走原来的本地 Chrome 持久 profile 路径，
             代码路径与接入前完全一致——这是唯一的回退手段，不需要改代码。
    base_url: 本地 API 地址。AdsPower 客户端默认监听 50325。
    api_key:  客户端「自动化 - API - API Key」里复制。走 Authorization: Bearer 头。
    group_id: 新建环境归入的分组，"0" 是未分组。
    reclaim_batch: 环境配额满时单次回收几个环境。环境上限很低（实测 12），
             删多了浪费登录态，删少了并发下不够分。
    ua_systems: 新建环境的指纹允许哪些操作系统。**只能是桌面系统**——AdsPower 不设此项
             时会在所有系统里随机（含 Android/iOS/Linux），移动端 UA 会让 GitHub 与
             Stripe 返回移动版页面，而本项目所有选择器都按桌面版编写。留空用代码默认值。
    total_quota: 同时可用的环境总数硬上限。**11 而不是 12**——AdsPower 的配额是 12，
             但它自带的 "Default Profile" 也占一个名额，实测会卡在 11/12。
    platform_quota: 各平台的自有额度（如 {opencode: 7, infron: 4}）。对方空闲时可以
             借用，但总数始终受 total_quota 约束。留空用代码默认值。
    quota_wait_seconds: 等配额的最长时间。配额是两平台共用的资源，对方跑完就会释放，
             所以拿不到时应当等待而非报错——直接判失败会让账号白白进失败集合。
    """
    enabled: bool = False
    base_url: str = "http://local.adspower.net:50325"
    api_key: str = ""
    group_id: str = "0"
    reclaim_batch: int = 3
    ua_systems: list = field(default_factory=list)
    total_quota: int = 11
    platform_quota: dict = field(default_factory=dict)
    quota_wait_seconds: int = 300


@dataclass
class AppConfig:
    registration: RegistrationConfig = field(default_factory=RegistrationConfig)
    email: EmailConfig = field(default_factory=EmailConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    password: PasswordConfig = field(default_factory=PasswordConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)
    batch: BatchConfig = field(default_factory=BatchConfig)
    captcha: CaptchaConfig = field(default_factory=CaptchaConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    files: FilesConfig = field(default_factory=FilesConfig)
    payment: PaymentConfig = field(default_factory=PaymentConfig)
    concurrency: ConcurrencyConfig = field(default_factory=ConcurrencyConfig)
    adspower: AdsPowerConfig = field(default_factory=AdsPowerConfig)
    recharge: RechargeConfig = field(default_factory=RechargeConfig)


def get_base_dir():
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent
    return Path(__file__).parent.parent


def get_data_dir() -> Path:
    """获取数据目录。
    打包后使用用户家目录下的固定路径，升级程序不会丢失数据；
    开发时使用项目根目录下的 data/ 目录。
    """
    if getattr(sys, 'frozen', False):
        data_dir = Path.home() / ".openrouter-auto-task"
    else:
        data_dir = get_base_dir() / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


class ConfigLoader:
    CONFIG_FILES = [
        "config.yaml",
        "config.yml",
        "config.local.yaml",
        "config.local.yml",
    ]

    def __init__(self, config_path: Optional[str] = None):
        self.config_path = config_path
        self.raw_config: Dict[str, Any] = {}
        self.config = AppConfig()
        self._load_config()

    def _find_config_file(self) -> Optional[Path]:
        if getattr(sys, 'frozen', False):
            # 打包模式：config 与数据库放在同一目录，升级不丢配置
            data_dir = get_data_dir()
            config_file = data_dir / "config.yaml"
            if not config_file.exists():
                # 首次运行：从程序目录的模板自动复制
                example = get_base_dir() / "config.example.yaml"
                if example.exists():
                    import shutil
                    shutil.copy2(example, config_file)
                    print(f"已创建配置文件: {config_file}")
                    print("请编辑配置文件后重新启动程序")
            return config_file if config_file.exists() else None
        else:
            base_dir = get_base_dir()
            for filename in self.CONFIG_FILES:
                config_file = base_dir / filename
                if config_file.exists():
                    return config_file
            return None

    def _load_config(self) -> None:
        if self.config_path:
            config_file = Path(self.config_path)
        else:
            config_file = self._find_config_file()

        if config_file is None or not config_file.exists():
            print("Warning: config.yaml not found, using defaults")
            return

        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                self.raw_config = yaml.safe_load(f) or {}
            self.config_path = str(config_file)
            self._parse_config()
        except yaml.YAMLError as e:
            print(f"Config parse error: {e}")
            sys.exit(1)

    def _parse_config(self) -> None:
        if 'registration' in self.raw_config:
            reg = self.raw_config['registration']
            self.config.registration = RegistrationConfig(
                total_accounts=reg.get('total_accounts', 1),
            )

        if 'email' in self.raw_config:
            email = self.raw_config['email']
            self.config.email = EmailConfig(
                worker_url=email.get('worker_url', ''),
                domain=email.get('domain', ''),
                prefix_length=email.get('prefix_length', 10),
                wait_timeout=email.get('wait_timeout', 120),
                poll_interval=email.get('poll_interval', 3),
                admin_password=email.get('admin_password', ''),
            )

        if 'browser' in self.raw_config:
            browser = self.raw_config['browser']
            self.config.browser = BrowserConfig(
                max_wait_time=browser.get('max_wait_time', 600),
                short_wait_time=browser.get('short_wait_time', 120),
                user_agent=browser.get('user_agent', ''),
            )

        if 'password' in self.raw_config:
            pwd = self.raw_config['password']
            self.config.password = PasswordConfig(
                length=pwd.get('length', 16),
                charset=pwd.get('charset', ''),
            )

        if 'retry' in self.raw_config:
            retry = self.raw_config['retry']
            self.config.retry = RetryConfig(
                http_max_retries=retry.get('http_max_retries', 5),
                http_timeout=retry.get('http_timeout', 30),
                error_page_max_retries=retry.get('error_page_max_retries', 5),
                button_click_max_retries=retry.get('button_click_max_retries', 3),
            )

        if 'batch' in self.raw_config:
            batch = self.raw_config['batch']
            self.config.batch = BatchConfig(
                interval_min=batch.get('interval_min', 5),
                interval_max=batch.get('interval_max', 15),
            )

        if 'concurrency' in self.raw_config:
            conc = self.raw_config['concurrency']
            self.config.concurrency = ConcurrencyConfig(
                max_workers=conc.get('max_workers', 1),
                platform_workers=dict(conc.get('platform_workers') or {}),
                claim_timeout_minutes=conc.get('claim_timeout_minutes', 20),
            )

        if 'adspower' in self.raw_config:
            ads = self.raw_config['adspower']
            self.config.adspower = AdsPowerConfig(
                enabled=bool(ads.get('enabled', False)),
                base_url=ads.get('base_url', 'http://local.adspower.net:50325'),
                api_key=ads.get('api_key', ''),
                group_id=str(ads.get('group_id', '0')),
                reclaim_batch=ads.get('reclaim_batch', 3),
                ua_systems=list(ads.get('ua_systems') or []),
                total_quota=int(ads.get('total_quota', 11)),
                platform_quota=dict(ads.get('platform_quota') or {}),
                quota_wait_seconds=int(ads.get('quota_wait_seconds', 300)),
            )

        if 'recharge' in self.raw_config:
            rc = self.raw_config['recharge']
            self.config.recharge = RechargeConfig(
                amount_min=int(rc.get('amount_min', 20)),
                amount_max=int(rc.get('amount_max', 100)),
                balance_cap=float(rc.get('balance_cap', 200)),
                max_fail_streak=int(rc.get('max_fail_streak', 3)),
                fail_cooldown_hours=int(rc.get('fail_cooldown_hours', 12)),
            )

        if 'captcha' in self.raw_config:
            captcha = self.raw_config['captcha']
            self.config.captcha = CaptchaConfig(
                api_key=captcha.get('api_key', ''),
                service=captcha.get('service', '2captcha'),
            )

        if 'database' in self.raw_config:
            database = self.raw_config['database']
            self.config.database = DatabaseConfig(
                path=database.get('path', 'data/openrouter_auto.db'),
            )

        if 'files' in self.raw_config:
            files = self.raw_config['files']
            self.config.files = FilesConfig(
                accounts_file=files.get('accounts_file', 'registered_accounts.txt'),
            )

        if 'payment' in self.raw_config:
            payment = self.raw_config['payment']
            cards_raw = payment.get('credit_cards', [])
            credit_cards = []
            if isinstance(cards_raw, list):
                for cc in cards_raw:
                    if isinstance(cc, dict) and cc.get('number'):
                        credit_cards.append(CreditCardConfig(
                            number=cc.get('number', ''),
                            expiry_month=cc.get('expiry_month', ''),
                            expiry_year=cc.get('expiry_year', ''),
                            cvc=cc.get('cvc', ''),
                            first_name=cc.get('first_name', ''),
                            last_name=cc.get('last_name', ''),
                            country=cc.get('country', 'United States'),
                            address=cc.get('address', ''),
                            address2=cc.get('address2', ''),
                            city=cc.get('city', ''),
                            state=cc.get('state', ''),
                            zip=cc.get('zip', ''),
                            company=cc.get('company', ''),
                        ))
            self.config.payment = PaymentConfig(
                max_bindable_cards=payment.get('max_bindable_cards', 2),
                credit_cards=credit_cards,
            )

    def reload(self) -> None:
        self._load_config()


_loader = ConfigLoader()
cfg = _loader.config
