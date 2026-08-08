# 后台加「账号归档」与「注册失败重置」

## Goal

后台管理 UI 上加两个批量操作：

1. **归档账号** —— 标记为「以后不再参与任何任务执行」，并同步释放它的 AdsPower 环境。
2. **重置为待注册** —— 把注册失败的账号退回 `imported`，让下一轮重新注册 GitHub。

第 2 条的逻辑已经存在于
[scripts/reset_failed_accounts_to_imported.py](../../../scripts/reset_failed_accounts_to_imported.py)，
本任务是把它搬进 UI，**连同它那两条保护一起搬**（见 R3）。

## 当前状态（2026-08-08）

`accounts.identity_status` 分布：`failed` 38 个、`registered` 14 个，无 `imported`。

重置功能对当前数据的实际效果（只读核对过）：

- 38 个 `failed` **全部**有 `email_verify_link` → 全部可重置。
- 本机 **`hotmail.xlsx` 不存在**。所以「xlsx 读不到按空集处理」不是防御性写法而是
  当前的常态路径——若实现时把 xlsx 缺失当错误或当作「无收码数据」，这个功能对当前
  数据会 100% 失效（点了没反应）。

## Requirements

### R1 新增身份层状态 `retired`

**为什么不叫 `archived`：** 平台层已经有 `archived`（余额已达上限）。而前端用**同一个**
`accStatusLabel` / `accStatusClass` 渲染身份列和平台列
（[Accounts.vue:82](../../../frontend/src/views/Accounts.vue#L82) 与
[:88](../../../frontend/src/views/Accounts.vue#L88)），同名就意味着两列显示完全一样的
徽章，用户分不出「余额充满归档」和「用户主动停用」。代码侧同样受害：
`IDENTITY_TERMINAL_STATUSES` 与 `PLATFORM_TERMINAL_STATUSES` 会各有一个 `archived`，
读代码时得先确认在说哪一层。

所以底层值用 `retired`，**UI 文案仍叫「归档」**（按钮「归档选中」、徽章「已归档（停用）」）
——用户的心智模型不变，只是底层不撞名。

- `accounts.identity_status = 'retired'` 表示「用户主动停用，不再参与任何任务」。
- 加进 `utils.IDENTITY_TERMINAL_STATUSES`。**这一处改动就够了**——所有「这账号还能不能跑」
  的入口都走 `is_identity_terminal()`，已核对全部四处：
  - `app.py::_payable_now()` — 可充值账号
  - `app.py::_reusable_recharged()` — 余额未满的复用池
  - `routes.py::_usable()` — 充值启动门 + 复用池计数
  - `routes.py` 订阅启动门
  `_registerable_imported()` 只认 `identity_status == 'imported'`，天然排除。
- 加进 `adspower_profile.IDENTITY_DEAD_ORDER` 的**最后一位**（排在 `suspended` 之后）：
  归档账号的环境正常情况下在归档那一刻就被释放了，这一档只是兜底（释放失败、或历史
  归档账号）。排最后是因为它与前面几个不同——账号本身是好的，只是用户不要了，
  万一反悔，环境还在更好。

### R2 归档操作

- 端点 `POST /api/accounts/archive`，body `{emails: [...]}`。
- 改 `identity_status = 'retired'`，并**同步释放 AdsPower 环境**（用户已确认）：
  复用现成的 `_release_adspower_for(emails)`，它已经是 best-effort 且会跳过
  `is_busy` 的账号。环境只有 12 格，归档还占着就是白占。
- 释放失败绝不能阻断归档（与删账号同一条红线）。
- 返回 `{retired: N, adspower: {...}}`，前端把环境释放结果一并提示。

### R3 重置为待注册

- 端点 `POST /api/accounts/reset-imported`，body `{emails: [...]}`。
- **必须搬过来的两条保护**（否则功能是负收益）：
  1. **只对 `failed` / `pending` 生效**。`suspended` 刻意排除——那是「注册出来就被
     GitHub 挂起」，同一邮箱重注册大概率还是同样下场，退回 `imported` 只会让它每轮
     白跑一次。其他状态（`registered` / `retired` / …）一律跳过。
  2. **没有收码数据的必须跳过**：`email_verify_link` 为空**且**不在 `hotmail.xlsx` 里
     的账号，重置了也领不走（`_registerable_imported` 要求
     `_hotmail_for_account` 取得到收码数据），只会让列表多几行看着能用其实不能用的账号。
- 返回分类结果 `{reset: [...], skipped_status: [...], skipped_no_mailbox: [...]}`，
  前端要把「跳过了哪些、为什么」显示出来，不能只报一个数字。

### R4 可撤销

- 端点 `POST /api/accounts/unarchive`，把 `retired` 改回 `registered`。
- 理由：归档是不可逆地退出所有任务**且已经删了环境**，误点一次的代价不小。
- 提示里要写明：环境已释放，恢复后首次运行需要重新 GitHub 登录（并会触发一次新设备
  邮箱验证）。

### R5 前端

- 工具栏加「归档选中」「重置为待注册」两个按钮，与现有「删除选中」同一套多选机制
  （`selected` Set），未选中时 disabled。
- 归档要二次确认（它会删环境），文案写明影响。
- 身份状态筛选下拉加「已归档（停用）」选项，`statusMap` 加 `retired` 的文案。
  平台状态那个「已归档」保持不变——底层值已经不同（`retired` vs `archived`），
  两列徽章自然可区分。
- 归档账号在列表里要能一眼看出（徽章配色与正常账号区分）。

## Non-goals

- 不做「归档时保留环境」的选项（用户已选定：一律释放）。
- 不改删除账号的行为。
- 不做归档原因/备注字段。
- 不做定时自动归档规则。

## Acceptance Criteria

- [ ] 归档后的账号不出现在：可充值账号、复用池、待注册 imported、订阅启动门的任何
      一个计数里（四处判据各有测试覆盖）。
- [ ] 归档同步释放 AdsPower 环境；AdsPower 未启用/不可用/删除失败时，账号照样归档成功，
      接口返回里说明环境未释放的原因。
- [ ] 归档正在被 worker 使用的账号时，环境保留（`is_busy` 跳过）、账号状态照改。
- [ ] 重置只动 `failed` / `pending`；传入 `suspended` / `registered` 的邮箱被跳过并在
      返回里分类列出。
- [ ] 无 `email_verify_link` 且不在 `hotmail.xlsx` 的账号被跳过并单独列出。
- [ ] 重置后的账号能被 `_registerable_imported()` 选中（状态 `imported` + 有收码数据）。
- [ ] 取消归档把 `retired` 改回 `registered`。
- [ ] 空 `emails` 数组返回 400，不做全表操作。
- [ ] 既有 567 项测试全绿。
