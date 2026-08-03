# 技术设计：多平台架构改造

## 设计总纲

改造分三层推进，每层可独立验证、独立回滚：

```
Layer 0  前置清理        删死代码，把 driver.py 从 6157 行降到约 1000 行
Layer 1  数据层平台化    schema 迁移 + 模型方法加 platform 参数
Layer 2  抽象层          PlatformAdapter / PaymentProvider / IdentityProvider
```

贯穿全局的一条原则：**平台标识 `platform` 是一个字符串 slug，由 adapter 自带，代码里的 adapter 注册表是唯一真值源。数据库里只存这个 slug 字符串，不建 `platforms` 表**——多一张表就多一处需要同步的真值源，而平台数量是个位数且随代码发布变化，不是运行时数据。

---

## 一、身份分层：两张表而非三张

### 决策：`accounts` 保留为「身份层」，新增 `platform_accounts`

PRD 的 R1 描述了邮箱 / GitHub / 平台账号三层。但在当前数据现实里，**邮箱与 GitHub 账号是严格 1:1** 的——每个 hotmail 邮箱注册恰好一个 GitHub 账号（`scripts/run_hotmail_github_signup.py` 的整条链路即如此）。为这个 1:1 关系单独建表是空洞的规范化。

因此落地方案是两张表：

| 表 | 承载 | 说明 |
|---|---|---|
| `accounts`（沿用现表名） | 邮箱身份 + GitHub 身份 | `email` / `email_password` / `email_verify_link` / `login_password`（GitHub 密码）/ `created_at` / `updated_at` |
| `platform_accounts`（新） | 每平台一行 | `platform` + `email` + 该平台的密码、状态、余额、apikey、租户 id |

如果将来出现「一个邮箱开多个 GitHub 账号」的需求，再把 GitHub 层拆出来——那时 `platform_accounts` 已经就位，拆分只影响 `accounts` 一张表。这个决策**写进代码注释**，避免后人误以为是疏漏。

**表名不改**。`accounts` 改名为 `identities` 会牵动 6 个脚本、5 张表的裸字符串引用和全部测试，收益只有语义清晰，不值得。改为在模块 docstring 里写明「本表现在只装身份，不装平台状态」。

### 新表 schema

```sql
CREATE TABLE platform_accounts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    platform    TEXT NOT NULL,
    email       TEXT NOT NULL,
    login_password TEXT,          -- 该平台自己的登录密码；OAuth 平台（opencode）留空
    status      TEXT DEFAULT '',
    tenant_id   TEXT DEFAULT '',  -- opencode 的 wrk_xxx；泛化为「平台侧租户/工作区 id」
    credits_balance    REAL,
    balance_updated_at TEXT,
    apikey             TEXT,
    apikey_updated_at  TEXT,
    created_at  TEXT DEFAULT (datetime('now','localtime')),
    updated_at  TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(platform, email)
);
CREATE INDEX idx_pa_platform_status ON platform_accounts(platform, status);
```

`tenant_id` 是新增能力：现在 `wid` 每次都要重新登录才能拿到（`opencode_login.login_and_open_own_go` 返回值），落库后可以跳过一部分导航。**但本次不改变现有获取逻辑**，只是落库备用，避免引入「缓存失效」这类新故障模式。

### 字段归属对照

| 现 `accounts` 字段 | 去向 | 理由 |
|---|---|---|
| `email` | 留 `accounts` | 邮箱身份 |
| `email_password` | 留 `accounts` | 邮箱身份 |
| `email_verify_link` | 留 `accounts` | 邮箱身份（ruoanzhu 收信链接） |
| `login_password` | 留 `accounts` | **实为 GitHub 密码**，跨平台复用的 OAuth 身份 |
| `status` | 搬 `platform_accounts` | 平台状态 |
| `credits_balance` / `balance_updated_at` | 搬 | 平台余额 |
| `apikey` / `apikey_updated_at` | 搬 | 平台 API key |
| `bound_card_count` / `cards_checked_at` | **删除**（Layer 0） | 零生产调用，Cloudflare 时代遗留 |
| `id` | 留 `accounts` | 被 `src/web/app.py:779,783` 的代理取模兜底消费，语义是「身份序号」，留在身份层正确 |

### status 取值的平台归属

现有 status 取值混杂了三个层次的语义，迁移时按层归位：

| 取值 | 层次 | 迁移去向 |
|---|---|---|
| `imported` | 身份层（仅从 xlsx 导入、未注册 GitHub） | `accounts` 新增 `identity_status` 列 |
| `pending` / `suspended` / `rejected` / `failed` / `flagged` | **GitHub 身份层**（GitHub 注册结果 / 反滥用标记） | `accounts.identity_status` |
| `registered` | 跨层歧义：既表示「GitHub 已注册」也表示「opencode 可用」 | `accounts.identity_status='registered'`，同时**不**在 `platform_accounts` 建行——没有平台行即表示该平台尚未开通 |
| `archived` / `recharged` / `subscribed` | 平台层 | `platform_accounts.status` |
| `bound_N_cards` / `bound` / `billing_page` / `interrupted` / `all_bindings_failed` / `error` / `banned` | 死状态 | Layer 0 删除 |
| `logged_in` / `need_device_verification` | 人工脚本专用 | `accounts.identity_status` |

这个拆分解决了 AdsPower 回收的核心难题（见第四节）：`flagged`/`suspended` 是 GitHub 层的终态，对所有平台一致；`recharged`/`subscribed` 是平台层终态，需要逐平台判断。

**共享常量**：现有两处 status 过滤集合不一致（`src/web/app.py:689-691` 四元组 vs `src/api/routes.py:952` 二元组）。改造时在 `src/utils.py` 抽出 `IDENTITY_TERMINAL_STATUSES` 与 `PLATFORM_TERMINAL_STATUSES` 两个常量，两处都引用它。

---

## 二、卡池占用平台化

### 核心矛盾与解法

「一张卡已被占用」的语义分散在 6 个载体。设计目标是让每个载体各自回答「在**哪个平台**被占用」，同时保留两个**必须维持全局**的例外。

| 载体 | 处理 | 依据 |
|---|---|---|
| `card_pool.status` 的 `bound` / `invalid` / `paid` | **搬到新表 `card_platform_state`** | 一列 TEXT 存不下多平台状态 |
| `card_pool.status` 的 `expired` | **保持全局，留在 card_pool** | 有效期与平台无关（R2.7） |
| `valid_cards` | UNIQUE 加 platform | 「曾在本平台成功过」 |
| `card_payment_state` | 主键改 `(card_number, platform)` | 3DS 由「商户+发卡行」共同决定，换平台即换 Stripe 商户号 |
| `recharge_logs` | 加 platform 列 | 尤其 `last_success_at`——它是「拒付时判冷却还是判废」的判据 |
| `card_bindings` | 加 platform 列 | 领取占位与成功绑定都要按平台 |
| `PaymentCardRegistry._used` | key 改 `(platform, card_number)` | R2.5 |
| `PaymentCardRegistry._in_flight` | **保持全局 key=card_number** | R2.6：同卡同时向发卡行提交会叠加 velocity 风控 |
| `ProxyRegistry` | 保持全局 | 出口 IP 是全局物理资源 |

### 新表 `card_platform_state`

```sql
CREATE TABLE card_platform_state (
    card_number TEXT NOT NULL,
    platform    TEXT NOT NULL,
    status      TEXT DEFAULT '',   -- '' | 'bound' | 'invalid' | 'paid'
    updated_at  TEXT DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (card_number, platform)
);
```

以 `card_number` 而非 `card_pool.id` 为键，与现有全部 `*_by_number` 方法一致（`mark_bound_by_number` / `mark_invalid_by_number` / `mark_status_by_number`），也让卡在分组间移动时状态自动跟随。

`card_pool.status` 列**保留不删**，迁移后只承载 `expired` 与 `''`。保留旧列是回滚保险：代码回退到旧版本时，`refresh_expired_status` 仍能重算出 `expired`，只是丢失 bound/invalid 信息——这比 DROP COLUMN 后无法回退好得多。

### 选卡链路改造

主入口 `AppState._eligible_cards(group_id, exclude_used)`（`src/web/app.py:516-547`）签名变为 `_eligible_cards(platform, group_id, exclude_used)`。下游 12 条排除条件的改法：

| 条件 | 改法 |
|---|---|
| E1 有效期过期 | 不变，全局。但 `refresh_expired_status` 的 skip 列表（`card_pool.py:293`）里的 `BOUND` 要改为查 `card_platform_state` |
| E2 状态不可选 | `get_usable_cards_as_list(group_id)` → `(platform, group_id)`。SQL 变为 `card_pool LEFT JOIN card_platform_state ON 卡号 AND platform=?`，排除 `cps.status IN ('bound','invalid')` 或 `cp.status='expired'` |
| E3 3DS / 速率冷却 | `get_state_map()` → `get_state_map(platform)`，`in_cooldown(num)` → `(num, platform)` |
| E4 新卡优先排序 | `recharge_log.all_success_card_numbers()` → 带 platform。跨平台复用的卡在新平台仍算「新卡」，这是期望行为 |
| E5 本轮已被别账号试过 | `PaymentCardRegistry.used_numbers(platform)` |
| E7 下游冷却安全网 | 带 platform |
| E8 单次最多试 N 张 | 从 env `OPENCODE_RECHARGE_MAX_ATTEMPTS` 改为 `adapter.max_card_attempts`（见第三节平台配置） |
| E9 in-flight 排他 | **不带 platform**（R2.6） |
| E10 绑卡 claim | `claim_batch` 内层 SELECT 加 `AND platform=?` |
| E11/E12 派生集合 | `get_successfully_bound_card_numbers` / `get_declined_card_numbers` 带 platform；`get_stripe_field_error_card_numbers` **保持全局**（卡数据本身脏，与平台无关） |

### 必须反转的守卫

`src/models/card_pool.py:317-321` 现在的守卫是：

```sql
UPDATE card_pool SET status='invalid'
WHERE card_number=? AND card_number NOT IN (SELECT card_number FROM valid_cards)
```

改为按平台判断：

```sql
-- 只有「在本平台成功过」才豁免判废
... AND card_number NOT IN (SELECT card_number FROM valid_cards WHERE platform=?)
```

这是 R2.4 / AC5 的直接落点，也是整个改造里**最容易漏且后果最严重**的一处——漏了会让在 opencode 成功过的坏卡在新平台永远标不成 invalid，无限循环消耗额度。`tests/test_valid_card_invariant.py` 必须新增对照用例钉死。

同样的守卫逻辑出现在 `_bucket_where`（`card_pool.py:80,84`）和 `move_non_invalid_to_group`（`card_pool.py:157`），三处一起改。

### 桶口径的平台归属

`card_pool` 的三个展示桶（valid / unverified / invalid）在多平台下变成「某平台视角下的桶」。`_bucket_where(bucket)` → `_bucket_where(bucket, platform)`。前端卡池页需要带上当前平台上下文，否则「有效卡」这个词失去意义。

---

## 三、抽象层

### 三个协议，各管一件事

```
IdentityProvider   供给可登录的身份     GitHubIdentityProvider
      ↓
PlatformAdapter    平台侧导航与判定     OpencodeAdapter
      ↓
PaymentProvider    支付页面操作         StripeCheckoutProvider
```

这个分层直接对应调研发现的事实：GitHub 注册模块 100% 平台无关（`src/browser/github_signup.py:5` 的 docstring 自己就这么写），而 Stripe 操作已经被充值和订阅两条流程共享（`src/browser/opencode_subscribe.py:16-24` 从 billing 模块 import 了 14 个符号）。抽象不是发明新结构，是把既成事实显式化。

### PlatformAdapter 接口

```python
class PlatformAdapter(Protocol):
    slug: str                      # 'opencode'，数据库里存的就是它
    display_name: str
    capabilities: frozenset[str]   # {'topup', 'subscribe'}，编排层据此跳过不支持的流程

    # 配置（原先散落在 env 变量里的平台参数）
    max_card_attempts: int         # ← OPENCODE_RECHARGE_MAX_ATTEMPTS，默认 8
    recharge_skip_balance: float   # ← OPENCODE_RECHARGE_SKIP_BALANCE，默认 20
    default_topup_amount: float

    # —— 会话 ——
    def extract_tenant_id(self, url: str) -> str | None: ...
    def ensure_session(self, session, creds, monitor=None, timeout=240) -> SessionResult: ...

    # —— 余额 ——
    def read_balance(self, session, tenant_id, monitor=None) -> float | None: ...
    def read_balance_from_current_page(self, session) -> float | None: ...

    # —— 充值 ——
    def top_up(self, session, tenant_id, card, amount, monitor=None, should_stop=None) -> PaymentResult: ...

    # —— 订阅（capabilities 含 'subscribe' 时才实现）——
    def subscribe(self, session, tenant_id, card, monitor=None, should_stop=None, dry=False) -> PaymentResult: ...
```

比调研建议的 12 个方法收窄到 7 个。收窄的依据：`auth_entry_urls` / `click_oauth_entry` / `balance_url` / `start_payment` / `detect_payment_outcome` / `detect_subscription_outcome` 全部只被 `ensure_session`、`top_up`、`subscribe` 这三个编排方法内部调用，**没有外部调用者**。把它们暴露成接口方法，等于强迫第二个平台按 opencode 的内部步骤分解自己的流程——那正是抽象要避免的事。它们留作 `OpencodeAdapter` 的私有方法。

唯一的例外是 `read_balance_from_current_page`：它确实被 `src/api/routes.py:471` 跨模块直接调用（手动开浏览器时轮询余额落库），必须进接口。

### 返回值契约

```python
@dataclass
class SessionResult:
    ok: bool
    tenant_id: str | None
    blocked_by_identity: bool   # ← opencode 的 flagged：GitHub 侧被反滥用标记
    detail: str

@dataclass
class PaymentResult:
    ok: bool
    outcome: str                # success | failed | needs_captcha | unknown | error | dry_ready
    err: str = ''
    last4: str = ''
    mode: str | None = None
    balance_after: float | None = None
    steps: list = field(default_factory=list)
```

`outcome` 的六个取值**语义必须逐字保持不变**，编排层现有的处置规则（`src/services/registration.py:251-296`、`src/web/app.py:1096-1130`）原样保留：

- `success` → 记 `paid` + `valid_cards` + `recharge_logs`，账号置终态
- `failed` → 拒付分支：**在本平台**成功过 → 24h 冷却；从未成功 → 判 `invalid`
- `needs_captcha` → 账号级风控，立即停手，**不消耗卡**
- `error` → 付款前的页面故障，**不消耗卡**
- `unknown` → 未定案，**不消耗卡**
- `dry_ready` → 演练模式填完卡未提交

`error`/`unknown`/`needs_captcha` 三者「不消耗卡」是踩过坑换来的规则，AC13 专门验它。

### adapter 注册表

```python
# src/platforms/__init__.py
_REGISTRY: dict[str, PlatformAdapter] = {}

def register(adapter): _REGISTRY[adapter.slug] = adapter
def get(slug) -> PlatformAdapter: ...
def all_slugs() -> list[str]: ...
```

新目录结构：

```
src/platforms/
    __init__.py          注册表
    base.py              Protocol + SessionResult/PaymentResult dataclass
    opencode/
        __init__.py      OpencodeAdapter（组装下面三个模块）
        login.py         ← src/browser/opencode_login.py
        billing.py       ← src/browser/opencode_billing.py（去掉 Stripe 部分）
        subscribe.py     ← src/browser/opencode_subscribe.py（去掉 Stripe 部分）
src/payments/
    stripe_checkout.py   ← 从 opencode_billing.py 抽出的 20 个 Stripe 函数
src/identity/
    github.py            ← github_signup_service.py 去掉 then_opencode 耦合
```

**文件移动与逻辑修改分开做**（见 implement.md 的步骤划分）：先纯 `git mv` + 改 import，跑测试确认零行为变化，再动逻辑。混在一起做的话，diff 会大到无法 review。

### 编排层改造点（只有 6 处）

生产代码里的 opencode 耦合总共 6 处，逐一对应：

| 位置 | 现状 | 改为 |
|---|---|---|
| `src/web/app.py:1042-1043` | `from src.browser.opencode_login/subscribe import ...` | `adapter = platforms.get(self.platform)` |
| `src/web/app.py:1285-1286` | `_patch_prints` 硬编码模块名列表 | 遍历 `adapter.module_names()` |
| `src/api/routes.py:438` | `from src.browser import opencode_billing as ob` | `platforms.get(req_platform)` |
| `src/services/registration.py:99` | 同上 | `recharge_account(..., adapter=...)` |
| `src/services/github_signup_service.py:26` | `then_opencode` 参数 | 改为 `post_provision: PlatformAdapter | None` |

`registration.recharge_account` 的骨架（余额预检 → 逐卡试付 → outcome 分派 → 卡状态记账）**完全平台无关**，只有 3 个 `ob.*` 调用点换成 `adapter.*`。这是整个 Layer 2 里改动最小、价值最大的一处。

---

## 四、AdsPower 回收判据

`adspower_profiles` 主键保持 `email`（R4.1）。真正要改的是回收候选查询（`src/models/adspower_profile.py:87-93`）：

```sql
-- 现状：单列 status 排优先级
SELECT p.email, p.profile_id, a.status
FROM adspower_profiles p JOIN accounts a ON a.email = p.email
WHERE a.status IN (...) ORDER BY CASE a.status ... 
```

改为两级判定：

1. **身份层终态**（`accounts.identity_status ∈ {failed, pending, rejected, flagged, suspended}`）→ 该邮箱在任何平台都用不了，直接可回收，优先级最高。
2. **身份可用但所有平台均终态**（`identity_status='registered'` 且不存在 `platform_accounts` 中 status 非终态的行）→ 可回收，优先级次之。
3. 其余一律不可回收。

关键是第 2 条用 `NOT EXISTS` 而非 `IN`：

```sql
AND NOT EXISTS (
    SELECT 1 FROM platform_accounts pa
    WHERE pa.email = p.email AND pa.status NOT IN (<平台终态集合>)
)
```

「不存在任何平台处于非终态」——这正是 AC15。用 `IN` 写会在「某平台有行但状态为空」时给出错误答案。

第 1 条替代了现有 `RECLAIM_STATUS_ORDER` 里 `registered` 绝不可回收的特例（`adspower_profile.py:20-21`）：`registered` 现在归 identity 层，且第 2 条会因为「opencode 平台行不存在或非终态」而拒绝回收它，语义等价且更明确。

环境命名 `auto-{email}`（`adspower_driver.py:56-58`）**不变**——环境本来就是 per-email 的，加平台前缀反而误导。

---

## 五、数据迁移

### 迁移版本划分

现有 `PRAGMA user_version` 机制（`src/models/database.py:246-255`）逐版本执行 `executescript`。本次新增：

| 版本 | 内容 | 所属 Layer |
|---|---|---|
| V13 | 删 `invoice_payment_state` 表 | Layer 0 清理 |
| V14 | 建 `platform_accounts` + `accounts.identity_status` 列；数据搬迁 | Layer 1 |
| V15 | 建 `card_platform_state`；从 `card_pool.status` 搬迁 | Layer 1 |
| V16 | 重建 `valid_cards`（UNIQUE 加 platform）、`card_payment_state`（主键加 platform）；`recharge_logs` / `card_bindings` 加 platform 列 | Layer 1 |

SQLite 不支持改主键和改 UNIQUE 约束，V16 里两张表走标准的 create-new → copy → drop → rename。

### 迁移语义

全部既有数据归属 `platform='opencode'`（AC8）。逐表：

- **`platform_accounts`**：为每个 `accounts.status ∈ {archived, recharged, subscribed}` 或有余额/apikey 的账号建一行。纯 `imported`/`failed` 等身份层状态的账号**不建行**——没有平台行即表示该平台未开通，这个语义比建一行空状态更干净。
- **`accounts.identity_status`**：从原 `status` 按第一节的对照表映射。
- **`card_platform_state`**：`card_pool.status ∈ {bound, invalid, paid}` 的行搬过来，platform='opencode'；随后把 `card_pool.status` 置 `''`。`expired` 的行不动——它会被 `refresh_expired_status` 按日期重算，无需迁移。
- **`valid_cards` / `card_payment_state` / `recharge_logs` / `card_bindings`**：全部行填 `platform='opencode'`。

### 幂等性（AC9）

`executescript` 在 `user_version` 机制下天然只跑一次。但迁移脚本本身要能在生产库副本上重复执行以供验证，因此：

- 所有 `CREATE TABLE` 带 `IF NOT EXISTS`
- 所有 `ALTER TABLE ADD COLUMN` 在 Python 侧先查 `PRAGMA table_info` 再决定是否执行（现有 `_migrate` 用 `executescript` 会在列已存在时整体失败，需要小幅加固）
- 数据搬迁用 `INSERT OR IGNORE` + `WHERE NOT EXISTS`

### 回滚形态

三道保险，按代价从低到高：

1. **备份**：迁移前 `cp data/openrouter_auto.db data/openrouter_auto.db.bak-<日期>-preplatform`，沿用 `data/` 下已有的备份命名习惯。
2. **旧列保留**：`card_pool.status` 与 `accounts.status` 列**不删除**，只是不再被新代码写入。代码回退到旧版本后，opencode 流程仍能读到迁移前的状态值（除了迁移中被置空的 bound/invalid），项目可运行。
3. **Layer 0 独立 commit**：清理与改造分离，`git revert` 清理 commit 不影响改造，反之亦然（AC16）。

不提供反向迁移脚本。理由：反向脚本的正确性无法在不真正回滚一次的前提下验证，而备份文件的正确性是自明的。

---

## 六、API 与前端

### 平台参数贯穿链路

流水线启动接口（`POST /api/daily/start`、`POST /api/daily/subscribe/start`）新增必填 `platform` 参数。`AppState.run_daily_pipeline(platform, group_id, ...)` 把它传到底。

账号列表、卡池列表、有效卡导出等读接口新增可选 `platform` 过滤参数；不传时的默认行为**必须显式决定**：

- 账号列表：不传 platform → 返回全部身份，平台字段展开为「该邮箱在各平台的状态」的紧凑表示。
- 卡池列表 / 有效卡：不传 platform → 用当前选中平台（前端始终带上）。「有效卡」在多平台下没有平台无关的定义，服务端不猜。

### 前端

顶栏加一个平台选择器，选中值存 Pinia store（`frontend/src/stores/app.js`），所有请求自动带上。这是最小改动路径——不做多平台并排展示（Out of Scope）。

`frontend/src/views/Accounts.vue` 的 status 下拉（`:15-27`）本就与实际取值脱节（缺 `imported`/`pending`/`suspended`/`rejected`/`recharged`/`subscribed`，多了 5 个死状态）。改造时按第一节的两层 status 重建这个下拉：身份状态与平台状态分成两个筛选器。

注意 `static/assets/*.js` 是 `vite build` 产物，改 `frontend/src/` 后需重新构建，不要直接改 static。

---

## 七、测试策略

### 跨平台对照用例是本次的核心验证手段

每一条隔离需求都要有一对「A 平台做了 X，B 平台不受影响」的对照用例。需要新增的：

| 测试文件 | 新增对照用例 | 对应 AC |
|---|---|---|
| `test_card_pool_bound.py` | 卡在 opencode 标 bound，在 platform B 仍可选 | AC2 |
| `test_card_pool_bound.py` | 卡在 opencode 标 invalid，在 platform B 仍可选 | AC3 |
| `test_valid_card_invariant.py` | **卡在 A 平台是 valid，在 B 平台仍能被标 invalid** | AC5（最关键） |
| `test_card_payment_state`（新建） | A 平台冷却不影响 B 平台 | AC4 |
| `test_registry.py` | A 平台的 `_used` 不影响 B 平台选卡；`_in_flight` 仍全局排他 | AC6 |
| `test_card_claim.py` | A 平台 worker 不会领走 B 平台任务的卡 | — |
| `test_adspower_pool.py` | 任一平台非终态时环境不被回收 | AC15 |
| `test_migration`（新建） | V13-V16 在含数据的库上执行后数据完整、可重复执行 | AC8/AC9 |

### 必须保住的既有不变量

- `test_registry.py:250-259`「全被试过时必须放行」：per-platform 化后这条兜底依然必须成立，不放行会导致卡池被误判耗尽（AC19）。
- `test_card_fault.py` 的错误归因白名单：纯字符串判定，本次**不动**。但要记一笔：新平台若不用 Stripe，`[Stripe字段错误]` 这类前缀不适用，届时需要 per-platform 化。
- `test_card_claim.py` 的总量守恒与原子性：`claim_batch` 加 platform 过滤后，原有 12 个用例的断言不应改变（它们都在单平台上下文里）。

### stub adapter（AC12）

在 `tests/` 下实现一个最小 `StubAdapter`（slug='stub'，`top_up` 直接返回构造好的 `PaymentResult`），用它跑通一遍充值编排流程。这既验证了「新增平台无需改编排层」，也给后续 infron.ai 接入提供了模板。

---

## 八、已知风险

| 风险 | 缓解 |
|---|---|
| **valid_cards 守卫漏改** → 坏卡在新平台无限循环消耗额度 | AC5 专项用例；implement.md 里列为独立 review gate |
| **迁移丢数据** | 迁移前强制备份；旧列保留不删；先在库副本上跑一遍验证 |
| **driver.py 删 5100 行误删仍在用的函数** | 删除前用 `grep -rn '<函数名>' src/ tests/ scripts/` 逐个确认；清理为独立 commit 可整体 revert |
| **AdsPower 回收判据改错** → 误删还在用的环境，账号登录态全丢 | `NOT EXISTS` 写法 + AC15 用例；首次上线时把 `reclaim_batch` 临时降到 1 观察一轮 |
| **Stripe 抽层时破坏 3DS/验证码识别** | 该批函数是踩坑换来的，抽层阶段**只做 `git mv` 和改 import，一行逻辑不动** |
| **前端 platform 参数漏传** → 读到全平台混合数据 | 服务端对卡池类接口的 platform 参数设为必填，缺失直接 400，不做默认值兜底 |
