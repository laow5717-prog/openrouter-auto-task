# 执行计划

## 步骤

1. **冷却到期时刻** — `src/models/card_payment_state.py::set_cooldown`
   - SQL 改为 `MAX(次日00:00, now + N hours)`（见 design.md 第一节）。
   - docstring 改写：说明为什么取 max（23:59 失败一分钟后就能刷，当日限制会形同虚设），
     以及 `hours` 现在是**下限**而非时长。
   - 3DS 路径共用本方法，无需单独处理（R6）。

2. **config 默认值与注释** — `config.example.yaml` + `config.yaml`
   - `fail_cooldown_hours` 注释改写为「冷却时长下限；实际到期 = max(次日00:00, now+本值)」。
   - **默认值 24 → 12**，否则自然日规则被下限完全覆盖、改动等于没做
     （见 design.md 第一节的表）。效果：中午前失败的卡次日零点回来，
     中午后失败的按 12h 滑动。

3. **并发实时复查** — `src/services/registration.py` 试卡循环
   - `try_acquire` 成功后、`attempts += 1` 之前插入实时 `in_cooldown` 复查，
     命中则 `continue`。
   - **必须写在 `try:` 之内**，否则 `continue` 会跳过 `finally` 的
     `payment_registry.release(num)`，卡的 in-flight 占用泄漏。
   - 异常吞掉按「不在冷却」放行（R5）。

4. **测试**
   - `tests/test_card_payment_state.py`（或新建）：
     - 15:00 失败 → 到期次日 00:00（AC1）
     - 23:59 失败 → 到期 now + N 小时（AC2）
     - 成功不设冷却（AC3）
     - `hours` 从 config 生效（AC6）
     - ⚠️ 时间相关断言不能依赖「跑测试的真实时刻」。`set_cooldown` 用的是 SQLite 的
       `datetime('now','localtime')`，无法 monkeypatch Python 的 clock——
       用例要么注入固定 now，要么只断言「到期 ≥ 次日零点」这类与当前时刻无关的不变式。
       这是本次测试最容易写歪的地方。
   - `tests/test_card_concurrency.py`：会话快照之后卡被别的 worker 设冷却 →
     本会话跳过（AC4）；复查抛异常 → 卡仍被尝试、会话不中断（AC5）。

## 验证命令

```bash
.venv/bin/python -m pytest tests/test_card_payment_state.py tests/test_card_concurrency.py -q
.venv/bin/python -m pytest tests/test_recharge_loop.py tests/test_recharge_policy.py \
    tests/test_card_fault.py tests/test_daily_pipeline.py -q
.venv/bin/python -m pytest tests/ -q        # 全量（AC7）
```

## review 门

- 全量 pytest 绿。
- 人工核对：实时复查在 `try:` 内，`finally` 的 release 仍会执行。
- 人工核对：`fail_cooldown_hours` 的默认值确实让自然日分支能生效
  （即默认值 < 从典型失败时刻到次日零点的小时数）。

## 回滚点

2 个生产文件 + config + 测试，`git checkout --` 即可回退。

## 注意事项

- ⚠️ 本机服务已重启（pid 4980）且空闲；改完需再重启一次才生效。
- 冷却变宽松会**提高烧卡速度**：失败卡更早回到可选集、更快被再拒一次，
  `fail_streak` 达阈值判废也会加快。当前卡池成功率本就低，上线后要观察。
