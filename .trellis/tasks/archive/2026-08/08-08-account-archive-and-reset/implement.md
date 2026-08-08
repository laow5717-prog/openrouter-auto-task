# 执行计划

## 步骤

### 1. 身份终态加 `retired`

- [ ] `src/utils.py`：`IDENTITY_TERMINAL_STATUSES` 加 `'retired'`，并在旁边注释写清
      它与其它四个的区别——那四个是「账号坏了」，`retired` 是**用户主动停用**，
      账号本身是好的。
- [ ] `src/models/adspower_profile.py`：`IDENTITY_DEAD_ORDER` **末尾**加 `'retired'`，
      注释说明为什么排最后（环境通常在归档那一刻就释放了，这一档只是兜底；且账号是好的，
      万一反悔环境还在更好）。
- [ ] 跑 `tests/test_adspower_pool.py`，确认没有硬编码 rank 数字被这一项挤歪
      （`PLATFORM_DONE_RANK` / `RECHARGED_RANK` 都会 +1）。

### 2. 重置资格判定抽成共用模块

- [ ] 新增 `src/services/account_reset.py`：`RESETTABLE` 常量 + `classify_for_reset`
      纯函数，返回 `(ready, bad_status, no_mailbox)`。
- [ ] docstring 搬运两条保护的**理由**，不只是规则：`suspended` 为什么排除、
      没收码数据为什么必须跳过。这些是踩坑换来的，丢了理由下次就会有人「优化」掉。
- [ ] 改 `scripts/reset_failed_accounts_to_imported.py` 调用它，删掉脚本里重复的判定。
      脚本的 CLI 行为（预览/`--apply`/`--include-suspended`）保持不变。

### 3. 三个 API 端点（`src/api/routes.py`）

- [ ] `POST /api/accounts/archive` — 改状态 + `_release_adspower_for`，返回
      `{retired, adspower}`。
- [ ] `POST /api/accounts/unarchive` — `retired` → `registered`，WHERE 带
      `identity_status='retired'` 防误改。
- [ ] `POST /api/accounts/reset-imported` — 调 `classify_for_reset`，只改 ready 那批，
      返回三类明细。
- [ ] 三个都：空 `emails` → 400。**不做全表操作**，漏传参数就是全表事故。

### 4. 前端

- [ ] `frontend/src/api/index.js` 加 `archiveAccounts` / `unarchiveAccounts` /
      `resetAccountsToImported`。
- [ ] `Accounts.vue` 工具栏加两个按钮（沿用 `selected` Set + disabled 规则）。
- [ ] 归档二次确认，文案写明「不再参与任何任务 + 同步删除 AdsPower 环境」。
- [ ] 重置结果按三类展示，跳过的要说明原因。
- [ ] `statusMap` 加 `retired: '已归档（停用）'`；`accStatusClass` 归 `warn`。
- [ ] 身份状态筛选下拉加 `<option value="retired">已归档（停用）</option>`。
- [ ] 行内操作区加「取消归档」，仅 `identity_status === 'retired'` 时显示。

### 5. 测试（`tests/test_account_archive_reset.py` 新增）

判据类（钉住 R1 的四个入口，防将来有人新增绕过 `is_identity_terminal` 的路径）：

- [ ] `test_retired_is_identity_terminal`
- [ ] `test_retired_excluded_from_payable` — `_payable_now` 语义
- [ ] `test_retired_excluded_from_reusable` — `_reusable_recharged` 语义
- [ ] `test_retired_excluded_from_registerable` — 只认 `imported`
- [ ] `test_retired_excluded_from_start_gates` — 两个启动门的计数

重置类（纯函数，直接喂 dict）：

- [ ] `test_only_failed_and_pending_are_resettable`
- [ ] `test_suspended_is_not_resettable` — 刻意排除，不是遗漏
- [ ] `test_account_without_mailbox_is_skipped`
- [ ] `test_verify_link_alone_is_enough` / `test_xlsx_membership_alone_is_enough`
- [ ] `test_missing_xlsx_does_not_block_db_linked_accounts` — xlsx 读不到时，
      有 `email_verify_link` 的账号仍可重置

端点类：

- [ ] `test_archive_sets_retired_and_releases_env`
- [ ] `test_archive_survives_adspower_failure` — 环境释放失败，账号照样归档
- [ ] `test_unarchive_only_touches_retired`
- [ ] `test_empty_emails_returns_400`（三个端点各一）

### 6. 验证

```bash
.venv/bin/python -m pytest tests/ -q          # 基线 567 项
cd frontend && npm run build                  # 前端要重新构建到 static/
```

⚠️ 前端改动**必须重新 build**——服务读的是 `static/assets/` 里的产物，不 build 的话
UI 上什么都不会变，而测试全绿会让人以为做完了。

## 审查关卡

- 步骤 1 完成后：确认 `PLATFORM_DONE_RANK` / `RECHARGED_RANK` 位移没有打破
  `test_adspower_pool.py`（上一个任务刚加的那 9 项尤其相关）。
- 步骤 3 完成后：用生产库只读核对一次——38 个 `failed` 里有多少真的有收码数据。
  如果绝大多数都没有，说明这个功能对当前数据几乎无效，要如实告诉用户而不是交付一个
  「点了没反应」的按钮。

## 回滚点

步骤 1-2、3、4 可分别回滚。

⚠️ **回滚前必须先处理已归档的账号**：`retired` 是新增枚举值，回滚后
`is_identity_terminal('retired')` 返回 False，那些账号会**重新进入轮转**——归档静默失效。
所以回滚顺序是：先把 `retired` 的账号改成别的终态（或删掉），再回滚代码。
