# 执行计划 — 充值任务账号耗尽自动注册补号 + 充值成功去重

## 顺序清单

- [ ] S1. 抽取 `_register_one_account(self, acct, worker=None) -> (result, detail)`
  - 从 `_subscribe_one_account` 707–733 剪切注册段为独立方法(纯搬迁,逻辑不变)。
  - 四分支:无 hotmail→skipped;reached_captcha→pending/skipped;account_suspended→
    suspended/skipped;非 signup_complete→failed;signup_complete→registered。
  - **验证**:`_subscribe_one_account` 改为调用它,`uv run python -c "import src.web.app"` 通过;
    人工比对分支状态落库与原逻辑逐条一致。

- [ ] S2. 充值成功去重(R1)
  - `run_daily_pipeline` 账号筛选排除集加 `'recharged'`;docstring 同步。
  - **验证**:构造账号列表单测,recharged 不入选。

- [ ] S3. 补号帮助函数
  - `_payable_accounts()`:实时 `account_model.get_all(order_desc=False)` 过滤
    (有密码 且 status ∉ banned/archived/flagged/recharged)。
  - `_next_registerable_imported()`:返回首个 status=='imported' 且
    `_hotmail_by_email(email)` 非空的账号;无则 None。
  - **验证**:两函数对当前 DB 返回符合预期(payable 数、下一个 imported)。

- [ ] S4. `run_daily_pipeline` 主循环接入补号(R2/R4)
  - 每轮实时算 payable;为空则取 imported 注册(claim/release 排他),`continue`;
    无 imported 可注册则 break。
  - 补号迭代不递增 round_num;`MAX_ROUNDS` 基数纳入 imported_count(或补号单列上限)。
  - 新增 `registered_total`;收尾统计追加「注册补号 N 个」。
  - **验证**:语法/导入通过;停止标志在补号阶段生效(补号前后检查 stop_requested)。

- [ ] S5. 集成冒烟(不真扣款)
  - Mock signup_one + recharge_account,跑一遍 run_daily_pipeline 的补号分支:
    无可充账号 → 触发注册(mock 成功)→ 下轮该账号进入充值 mock → 收尾统计正确。
  - 断言:`update_status` 调用序列、无死循环、imported 耗尽正常 break。

## 校验命令

```bash
uv run python -c "import src.web.app; print('import OK')"
# 单测(scratchpad 临时脚本):筛选去重 + 补号 mock 编排
uv run python /private/tmp/.../scratchpad/test_refill.py
```

## 评审门 / 回滚点

- S1 抽取后立即验证订阅无回归(高风险:搬迁易漏分支)——不过则回滚 S1 重做。
- S4 是核心编排改动;若补号逻辑异常,可临时用「排除集加 recharged」(S2)单独交付,
  补号(S3/S4)回滚,先解决重复充值这一半。
- 全程不真跑扣款;真实验证由用户在 UI 点「开始充值」观察日志(有 imported 时应见补号→充值)。

## 风险

- signup_one 全自动碰 Arkose 成功率低 → 补号多为 skipped(pending)。属预期,不影响正确性
  (账号离开 imported,不死循环);真正转正账号数取决于 Arkose。
- Patchright 注册栈与 vanilla 充值栈同 profile 串行切换,注册后 session 已关,充值重开——
  与订阅任务同款,已验证可行。
