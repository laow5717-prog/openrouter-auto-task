# 执行计划 — 卡片复用策略与充值金额可配

按依赖顺序分 6 步。每步结束都能单独跑测试，前一步不绿不进下一步。

---

## Step 1 — 数据层：失败计数（R1 的存储）

**改动**
- `src/models/database.py`：新增 `_SCHEMA_V17`，注册 `_MIGRATIONS[17]`。
  ```sql
  ALTER TABLE card_payment_state ADD COLUMN fail_streak INTEGER DEFAULT 0;
  ALTER TABLE card_payment_state ADD COLUMN last_fail_at TEXT;
  ```
- `src/models/card_payment_state.py`：新增 `bump_fail_streak` / `reset_fail_streak` /
  `get_fail_streak`；`get_state_map` 带出 `fail_streak`。模块 docstring 补一段说明
  这张表现在同时承载「冷却」与「连续失败计数」两件事。

**注意**
- `bump_fail_streak` 用 `self.db.transaction()` 包 upsert + 回读。
  事务块内**只能**用 yield 出来的 `conn`，不能调 `self.db.execute/fetchone`
  （`_lock` 不可重入会死锁，见 `database.py:524` 的注释）。
- 不要动 `set_cooldown` 的 `DO UPDATE SET` 列表——它不列 `fail_streak`，正是我们要的。

**验证**
```bash
python3 -m pytest tests/test_card_payment_state.py -q
```
新增用例：计数递增 / 归零 / 按平台隔离 / `set_cooldown` 不清计数。

---

## Step 2 — 配置层：`RechargeConfig`（R1–R4 的参数）

**改动**
- `src/config.py`：新增 `RechargeConfig` dataclass（字段与默认值见 design.md），
  挂到 `AppConfig.recharge`；`_parse_config` 加 `recharge` 分支。
  实现 `pick_amount()` 与 `with_overrides(**kw)`。
- `config.example.yaml` + `config.yaml`：加 `recharge:` 段并注释每项含义。

**注意**
- `with_overrides` 必须返回**新实例**，绝不原地改 `cfg.recharge`——两个平台并发时
  共享同一个全局 `cfg`，原地改会互相覆盖。
- `pick_amount` 内部对区间做一次夹紧（`min<=max`、下界 ≥1），即便调用方漏校验也不会
  抛 `ValueError` 把整条充值打断。

**验证**
```bash
python3 -m pytest tests/test_recharge_policy.py -q     # 新建
```
用例：默认值 / yaml 覆盖 / `with_overrides` 不污染全局 / `pick_amount` 落在区间内且
取值会变化 / 非法区间被夹紧。

---

## Step 3 — 编排层主循环（R1/R2/R3/R4 的行为）

**改动** —— 全部在 `src/services/registration.py::recharge_account`
1. 签名加 `recharge_cfg=None`，函数开头 `recharge_cfg = recharge_cfg or cfg.recharge`。
2. `_log_card_attempt` 加 `amount` 参数，替换写死的 `amount=20`。
3. 循环前初始化 `paid_count` / `session_topped` / `stop_note`。
4. 每张卡取 `amount = recharge_cfg.pick_amount()`，传给 `adapter.top_up(..., amount=amount)`。
5. 成功分支：加 `reset_fail_streak`，把 `_grab_apikey` 移出，末尾判余额上限
   → 达标 `break`、否则 `continue`（**删掉 `return`**）。
6. 失败分支：删掉 `prior_success` 分岔，改为「无条件冷却 + `bump_fail_streak` +
   达阈值才 `mark_invalid_by_number`」。
7. `needs_captcha` 分支：`return` 改 `stop_note` + `break`。
8. 循环后统一出口：`paid_count` 非零则 `_grab_apikey` 并返回 `topup`。
9. 更新函数 docstring —— 它现在描述的是「付成一张即返回」，必须改成新语义，
   否则下一个读代码的人会被误导。

**注意**
- `error` / `unknown` 两个分支**一行都不改**。它们是「不消耗卡」的硬约束，
  `tests/test_platform_adapter.py::test_non_card_failures_do_not_consume_the_card`
  会守着。
- `payment_registry.try_acquire` / `release` 的成对关系保持在 `try/finally` 里不变。
- `attempts` 计数含成功笔数（`max_card_attempts` 是「试卡上限」而非「失败上限」），
  这是已确认的口径。

**要同步更新的既有测试**
- `test_declined_card_never_succeeded_here_is_invalidated`：一次失败不再判废，
  改成连拒 3 次后才 `invalid`，并断言第 1、2 次后仍非 `invalid`。
- 通读 `tests/test_platform_adapter.py` 其余用例，确认 R3 不会让它们的
  `StubAdapter` outcome 序列错位（单卡用例不受影响；多卡用例需核对）。

**验证**
```bash
python3 -m pytest tests/test_platform_adapter.py tests/test_valid_card_invariant.py \
                  tests/test_card_fault.py -q
```

---

## Step 4 — 新增编排层行为测试

新建 `tests/test_recharge_loop.py`，复用 `test_platform_adapter.py` 里的
`StubAdapter` 模式（可抽到 `conftest.py` 或直接 import）。

覆盖：AC1、AC2、AC3、AC4、AC5、AC6、AC7、AC8、AC9、AC10、AC11、AC12。

关键用例草图：
- **AC7**：`outcomes=['success','success','failed']` + 3 张卡 → 2 笔成功，
  `outcome=='topup'`，`top_up` 被调 3 次。
- **AC9**：`StubAdapter` 每次成功返回 `balance_after=150`，`balance_cap=100`
  → 第一笔后即停，`top_up` 只调 1 次。
- **AC9 兜底**：`balance_after=None`，`balance_cap=60`，金额固定 50
  → 第二笔后累计 100 ≥ 60 停手。
- **AC10**：`outcomes=['success','needs_captcha','success']`
  → `outcome=='topup'`（不是 `failed`），`top_up` 调 2 次。
- **AC11/AC12**：把 `amount_min=amount_max=37` → 断言 `recharge_logs.amount == 37`；
  再用宽区间跑多笔，断言取值都落在区间内。

---

## Step 5 — API 与 AppState 透传

**改动**
- `src/web/app.py`
  - `_recharge_one_account(..., recharge_cfg=None)` → 透传给 `recharge_account`。
  - `run_daily_pipeline(..., recharge_cfg=None)` → 透传给 `_recharge_one_account`。
  - 修 `$20` 写死的日志文案（成功笔数/金额从 `responses` 统计；归档阈值取
    `adapter.recharge_skip_balance`）。
- `src/api/routes.py`
  - 新增 `_recharge_cfg_from(data)`：解析 + 校验 `amount_min` / `amount_max` /
    `balance_cap`，非法返回 `(None, "错误说明")`。
  - `/api/daily/start` 与 `/api/accounts/recharge` 都用它；非法 → 400。
  - `/api/daily/start` 的响应里带回实际生效的区间，供前端回显。

**注意**
- `run_daily_pipeline` 是用位置参数 `args=(...)` 起线程的
  （`routes.py:1143`），加参数时**必须同步改那个元组**，否则参数错位且不报错。

**验证**
```bash
python3 -m pytest tests/test_daily_pipeline.py tests/test_api_workers.py \
                  tests/test_platform_concurrency_api.py -q
```
新增 API 用例：合法区间被接受并生效 / `min > max` 返回 400 / 不传时用默认值。

---

## Step 6 — 前端

**改动**
- `frontend/src/stores/settings.js`：加 `amountMin`(20) / `amountMax`(100) /
  `balanceCap`(200) 三个 ref，纳入 `save()` 的 localStorage 持久化。
- `frontend/src/views/Workbench.vue`：侧栏加两行 `.settings-row`
  ——「充值金额 $[min] – $[max]」与「账号余额上限」；`handleStart` 组 body 时带上；
  运行中 `:disabled="appStore.isRunning"`（与分组下拉一致）。
- 前端做一次轻量校验（min ≤ max、正数），错误直接 `alert`，不发请求。

**验证**
```bash
cd frontend && npm run build
```
手工核对：填 30–60 启动任务，后端日志里出现的金额都在 30–60 之间。

---

## 全量验收

```bash
python3 -m pytest tests/ -q
cd frontend && npm run build
```

逐条对照 `prd.md` 的 AC1–AC15 打勾。AC13/AC14 需手工在 UI 上过一遍。

---

## 回滚点

- Step 1–2 独立可回滚（加列 + 加配置，无行为变化）。
- Step 3 是唯一改变线上行为的一步；出问题时配
  `max_fail_streak: 1`、`amount_min/max: 20`、`balance_cap: 1` 可让行为退回接近改造前，
  无需回滚代码。
- Step 5–6 不传新字段即等价于默认值，前后端可分别回滚。
