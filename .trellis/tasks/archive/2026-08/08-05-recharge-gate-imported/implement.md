# 执行计划

## 步骤

1. **改启动门** — `src/api/routes.py::start_daily_pipeline`
   - 在 `reusable_count` 之后新增 `registerable_count`，判据：
     `identity_status == 'imported'` 且 `state._hotmail_for_account(a)` 非 None。
   - 注释写清「判据必须与 `run_daily_pipeline._registerable_imported()` 对齐，改一处要改两处」，
     沿用该文件既有的注释风格（现有 `reusable_count` 上方就是这么写的）。
   - 400 判定改为三者全 0，文案补上待注册那一类。
   - 200 响应体加 `registerable_accounts`。

2. **补测试** — 新建 `tests/test_daily_start_gate.py`
   - `test_imported_only_can_start`：库里只有一个 `imported` + `email_verify_link` 的账号 → 200，
     且 `registerable_accounts == 1`，流水线桩被调用。（AC1 / AC2）
   - `test_imported_without_link_is_not_counted`：只有 `imported` 无 link 的账号 → 400。（AC3）
   - `test_no_account_at_all_still_rejected`：一个账号都没有 → 400 且文案含「待注册」。（AC4）
   - fixture 结构照搬 `tests/test_recharge_cfg_api.py`（`create_app(db_path=tempfile.mktemp())`
     + 替换 `state.run_daily_pipeline` 为记参数的桩）。
   - 注意：`hotmail.xlsx` 在测试环境可能存在也可能不存在，用例必须靠
     `email_verify_link` 这条路径成立，不依赖 xlsx。无 link 的用例要确保邮箱不会
     恰好命中 xlsx——用明显的测试域名（如 `nolink@test.invalid`）。

## 验证命令

```bash
.venv/bin/python -m pytest tests/test_daily_start_gate.py -q          # 新用例
.venv/bin/python -m pytest tests/test_recharge_cfg_api.py \
    tests/test_api_platform_scoping.py tests/test_platform_concurrency_api.py -q   # 相邻端点回归
.venv/bin/python -m pytest tests/ -q                                  # 全量（AC6）
```

## review 门

- 全量 pytest 绿。
- 人工比对启动门判据与 `app.py::_registerable_imported()` 两处逐条一致。

## 回滚点

改动集中在一个函数 + 一个新测试文件，`git checkout -- src/api/routes.py` 即可回退，
新测试文件独立删除即可。

## 注意事项

- ⚠️ 当前 opencode 每日充值任务**正在运行中**（进程 15918，W1/W2 在跑）。
  改代码不会影响已运行的进程（Flask 未开 reload），但**改完不要重启服务**，
  否则会打断正在进行的支付会话。重启时机交给用户决定。
- 本任务不改任何账号数据（用户明确要求）。
