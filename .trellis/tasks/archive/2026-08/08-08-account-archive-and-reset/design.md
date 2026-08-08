# 技术设计

## 改动边界

| 文件 | 改什么 |
|---|---|
| `src/utils.py` | `IDENTITY_TERMINAL_STATUSES` 加 `retired` |
| `src/models/adspower_profile.py` | `IDENTITY_DEAD_ORDER` 末尾加 `retired` |
| `src/services/account_reset.py`（新增） | 重置资格判定，供 API 与既有脚本共用 |
| `src/api/routes.py` | 三个端点：archive / unarchive / reset-imported |
| `frontend/src/api/index.js` | 三个调用函数 |
| `frontend/src/views/Accounts.vue` | 两个按钮 + 筛选项 + 状态文案 |
| `scripts/reset_failed_accounts_to_imported.py` | 改为调用共用模块，不再自带判定逻辑 |
| `tests/test_account_archive_reset.py`（新增） | 覆盖 AC |

## 为什么 `retired` 只需改一个常量

所有「这账号还能不能跑」的入口都走 `is_identity_terminal()`，已逐一核对：

```
app.py::_payable_now()          :1038   可充值账号
app.py::_reusable_recharged()   :1118   余额未满的复用池
routes.py::_usable()            :1400   充值启动门 + 复用池计数
routes.py 订阅启动门             :1499
```

`_registerable_imported()` 只认 `identity_status == 'imported'`，`retired` 天然不在内。

这是既有设计的红利——四处判据共用一个谓词，所以「加一个身份终态」是一处改动。测试要
把这四处**逐一**钉住，否则将来有人新增一个绕过 `is_identity_terminal` 的入口时不会报警。

## 重置资格判定抽成共用模块

`scripts/reset_failed_accounts_to_imported.py` 里那两条保护（只动 `failed`/`pending`、
必须有收码数据）是真实踩坑换来的领域知识。API 若另写一份，两份必然漂移。

新增 `src/services/account_reset.py`：

```python
RESETTABLE = ('failed', 'pending')   # suspended 刻意不在内，见 docstring

def classify_for_reset(accounts, hotmail_emails):
    """把账号分成 (可重置, 状态不符, 无收码数据) 三组。纯函数，不碰 DB。"""
```

- 纯函数、不碰 DB、不碰 Flask —— 单测直接喂 dict 列表。
- 脚本与 API 都调它，两条保护只有一份实现。
- 收码数据判据与 `app.py::_hotmail_for_account` 同源：`email_verify_link` 非空
  **或** 邮箱在 `hotmail.xlsx` 里。xlsx 读取失败按空集处理（多数账号的链接在 DB 里，
  xlsx 只是第二来源），不能因为读不到 xlsx 就把所有账号判成不可重置。

## 端点

三个都是 `POST`，body `{emails: [...]}`，空数组一律 400（不做全表操作——这三个都是
批量写操作，漏传参数就是全表事故）。

### `POST /api/accounts/archive`

```
1. 空数组 → 400
2. UPDATE accounts SET identity_status='retired' WHERE email IN (...)
3. _release_adspower_for(emails)      ← 复用现成的，best-effort
4. → {retired: N, adspower: {...}}
```

**顺序与删账号一致：先改状态再释放环境。** 理由同 `delete_accounts` 那段注释——
AdsPower 挂了绝不能挡住状态变更。区别是删账号先释放后删行（那里释放要读 DB 映射），
这里状态变更不影响映射，先后都行，取「先落库、再尽力清环境」更稳。

### `POST /api/accounts/unarchive`

`retired` → `registered`。**只改 `retired` 的行**，WHERE 里带
`identity_status='retired'`，避免误改别的状态。

### `POST /api/accounts/reset-imported`

```
1. 空数组 → 400
2. 读这些 email 的账号行 + hotmail.xlsx 邮箱集
3. classify_for_reset(...) → (ready, bad_status, no_mailbox)
4. 只 UPDATE ready 那批 → identity_status='imported'
5. → {reset: [...], skipped_status: [...], skipped_no_mailbox: [...]}
```

返回分类明细而非单个数字：用户选了 38 个 failed，结果只重置了 12 个，必须能当场看出
另外 26 个为什么没动。只报数字会让人以为功能坏了。

## 前端

工具栏（[Accounts.vue:8-14](../../../frontend/src/views/Accounts.vue#L8-L14)）在
「删除选中」旁边加两个按钮，沿用同一套 `selected` Set 与 disabled 规则：

- **归档选中** — 二次确认，文案写明「不再参与任何任务 + 同步删除 AdsPower 环境
  （登录态丢失，恢复后需重新登录）」。
- **重置为待注册** — 二次确认，结果弹窗按三类展示。

`retired` 的展示：`statusMap` 加 `retired: '已归档（停用）'`，`accStatusClass` 里归到
`warn`（与平台层 `archived` 同色即可——底层值不同，文案已能区分）。身份状态筛选下拉
加对应 `<option value="retired">`。

「取消归档」放在行内操作区（与「删除」同排），只在 `identity_status === 'retired'` 时
显示——它是低频的纠错操作，不值得占工具栏位置。

## 兼容性

- `retired` 是新增枚举值，历史数据里不存在，无迁移。
- 无 schema 变更。
- `IDENTITY_TERMINAL_STATUSES` 加一项会让 `is_identity_terminal` 对 `retired` 返回 True
  ——这正是目的，且现有数据里没有 `retired`，不影响任何既有账号。
- `IDENTITY_DEAD_ORDER` 加一项会让 `PLATFORM_DONE_RANK`（= `len(IDENTITY_DEAD_ORDER)`）
  和 `RECHARGED_RANK` 各自 +1。两者都是派生值，SQL 里用的是 f-string 插值，无硬编码
  数字，安全。**但 `tests/test_adspower_pool.py` 里若有硬编码 rank 数字会挂**——
  实施时确认（当前测试用的是相对比较 `<`，应该安全）。

## 回滚

各文件改动自包含。回滚后 `retired` 状态的账号会变成「不认识的状态」——
`is_identity_terminal` 返回 False，它们会重新进入轮转。所以回滚前要先把这些账号
改成别的状态，否则归档失效。这一点写进 implement.md 的回滚点。
