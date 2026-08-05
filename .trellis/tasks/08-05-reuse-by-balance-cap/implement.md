# 执行计划

## 步骤

1. **模型层** — `src/models/recharge_log.py` 新增 `success_amount_by_email(platform, since)`
   - 一次聚合返回 `{email: float}`，SQL 见 design.md。
   - `since` 为空/None 时返回空 dict（防止误当成「全时段」把历史充值算进本次运行）。
   - 注释写清 N+1 的规避理由，风格对齐同文件的 `count_success_by_last4`。

2. **复用判据** — `src/web/app.py` `run_daily_pipeline`
   - 入口取 `run_started_at`（格式必须与 `datetime('now','localtime')` 一致）。
   - 删除 `reused_this_run`，**不设次数上限**。
   - `_reusable_recharged` 判据改为有效余额（见 design.md 第二节）。
   - 原 `reused_this_run` 那段长注释要**改写而不是删除**：理由 2（防死循环）依然成立，
     只是挡它的机制从「次数」换成了「金额累加」。删掉的话下一个人会重蹈覆辙。

3. **领取顺序** — 同文件 `_try_claim`
   - 可充值与可复用合并为一档（`_payable_now() + _reusable_recharged()`），
     待注册 imported 排其后。
   - 改写 `reuse_logged` 的日志文案（不再是「无新账号可领」才触发）。
   - 顺序注释要重写：原注释论证的是「回退池排最后是刻意的」，与新行为相反。

4. **收敛兜底** — 同文件轮边界判定
   - `progressed` 改为 `paid 增加 or 可选卡有新增`，取差集而非 `!=`（见 design.md 第三节）。
   - 注释写明 113 轮现场，说明为什么「卡变少」不算进展。

5. **测试** — `tests/test_daily_pipeline.py`
   - ⚠️ 前置：现有 `_Tracker.recharge` 只写 `platform_accounts.status='recharged'`，
     **不写 recharge_logs**。新判据依赖那张表，tracker 必须补写一条 success 日志
     （带 amount），否则 `topped` 恒为 0，金额闸测不出来。这是本次测试改动的关键点。
   - 改写 `test_recharged_accounts_are_reused_at_most_once` → 新语义：
     余额未达 cap 可多次复用，累计到 cap 后停（AC1/AC2）。
   - 改写 `test_reuse_only_kicks_in_after_new_accounts_are_exhausted` → 新顺序语义：
     可复用与可充值同档，imported 排其后（AC5）。
   - 新增 `test_reuse_converges_when_balance_never_updates`：
     tracker 不写 `credits_balance`（模拟 `balance_after` 读不到），
     断言任务正常结束、复用次数有限（AC3/AC4）。
   - 新增 `test_all_failures_converge_in_bounded_rounds`：充值全失败且卡逐张被标废，
     断言轮数有界、任务收敛（AC6，复现 113 轮场景）。
   - 新增 `test_new_cards_still_count_as_progress`：可选卡集合有新增时不被判零进展（AC7）。

## 验证命令

```bash
.venv/bin/python -m pytest tests/test_daily_pipeline.py -q          # 主战场
.venv/bin/python -m pytest tests/test_recharge_loop.py tests/test_recharge_policy.py \
    tests/test_card_concurrency.py -q                                # 相邻回归
.venv/bin/python -m pytest tests/ -q                                 # 全量（AC9）
```

## review 门

- 全量 pytest 绿。
- 人工核对收敛性论证：成功路径靠金额累加到 cap；失败路径靠修好的零进展兜底。
  两条都要成立——去掉次数闸后没有第三道保险。
- 人工核对 `_payable_now` 与 `_reusable_recharged` 无交集（recharged 是平台终态）。

## 回滚点

改动集中在 2 个生产文件 + 1 个测试文件，`git checkout --` 即可回退。

## 注意事项

- ⚠️ 本机服务当前**空闲**（上一轮任务已停），改代码不影响运行中的会话；
  改完要重启服务才生效。
- 卡池质量问题（`success=2 / fail=147`）与本任务无关，不在此处理。
  放宽复用 + 合并同档后单账号充值笔数会上升，**烧卡速度也会同比上升**——
  上线前应先确认卡池可用，否则修好的收敛兜底只会让它更快地把卡烧完然后停下。
