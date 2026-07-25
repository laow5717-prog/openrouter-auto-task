"""
配置加载模块
从 config.yaml 文件加载配置，支持数据类的类型安全访问
"""

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

    max_workers: 同时驱动的浏览器 worker 数。默认 1（串行，与改造前等价）。
                 有效范围 1-4，越界由 WorkerPool 夹紧；上限 4 源于有头 Chrome
                 每实例约 300-500MB 内存的开销。
    claim_timeout_minutes: 卡被领取（processing 态）后多久无进展视为 worker 失联，
                 由回收线程重置回 pending。
    """
    max_workers: int = 1
    claim_timeout_minutes: int = 20


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
                claim_timeout_minutes=conc.get('claim_timeout_minutes', 20),
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
