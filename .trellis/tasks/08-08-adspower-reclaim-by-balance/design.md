# 技术设计

## 改动边界

- `src/models/adspower_profile.py` — 常量分家 + `reclaim_candidates` SQL 分档。
- `src/browser/adspower_driver.py` — `reclaim()` 的挑选策略（最后手段 + 单次 1 个）与日志。
- `tests/test_adspower_pool.py` — 重写 1 项、新增若干项。

`src/utils.py` 的 `PLATFORM_TERMINAL_STATUSES` **不动**（R4）。

## 常量分家

```python
# 平台层「真做完了」——余额已达 balance_cap 被归档，或订阅完成。环境里的登录态没人要了。
_PLATFORM_DONE = ('archived', 'subscribed')

# 平台层「还要再跑」——recharged 现在的含义是「有一些余额、还没到 balance_cap」，
# 下一轮还会被 _reusable_recharged() 领走继续充。环境里的 GitHub 登录态正是它下次要用的。
_PLATFORM_REUSABLE = ('recharged',)

# 能进候选集的全集：非这两类（如 registered 等着首充）一律不可回收。
_PLATFORM_RECLAIMABLE = _PLATFORM_DONE + _PLATFORM_REUSABLE

PLATFORM_DONE_RANK = len(IDENTITY_DEAD_ORDER)      # 第 2 档
RECHARGED_RANK = PLATFORM_DONE_RANK + 1            # 第 3 档：最后手段
```

`adspower_profile.py:38` 那句「与 utils.PLATFORM_TERMINAL_STATUSES 保持一致，改动时两处
都要改」必须改写。两者语义已分家：utils 那份回答「这账号还能不能充值」，这份回答
「这环境里还有没有值得留的登录态」。08-05 之后 `recharged` 对前者是终态、对后者不是。

## `reclaim_candidates` 的 SQL

### WHERE：放宽到 `_PLATFORM_RECLAIMABLE`

```sql
OR (
    EXISTS (SELECT 1 FROM platform_accounts pa WHERE pa.email = p.email)
    AND NOT EXISTS (
        SELECT 1 FROM platform_accounts pa
        WHERE pa.email = p.email
          AND COALESCE(pa.status,'') NOT IN (archived, subscribed, recharged)
    )
)
```

「至少开通过一个平台」的 EXISTS 与「不存在非终态平台行」的 NOT EXISTS 都原样保留，理由
见原 docstring（少了 EXISTS 会把刚注册好、还没开通任何平台的账号全判成可回收）。变的只是
NOT IN 的集合从 `_PLATFORM_TERMINAL` 换成 `_PLATFORM_RECLAIMABLE`——`recharged` 现在能进
候选集，但由 rank 决定它排最后。

### rank：多一档

```sql
CASE
    WHEN a.email IS NULL THEN -1
    WHEN identity_status IN (dead) THEN <dead order>
    WHEN EXISTS (SELECT 1 FROM platform_accounts pa
                 WHERE pa.email = p.email AND COALESCE(pa.status,'') = 'recharged')
         THEN {RECHARGED_RANK}
    ELSE {PLATFORM_DONE_RANK}
END AS rank
```

「有任一平台行是 recharged」即落第 3 档。混合情况（opencode `recharged` + other
`subscribed`）算第 3 档——只要还有一个平台要接着跑，这个环境就有价值。

### 余额：LEFT JOIN 聚合

```sql
LEFT JOIN (SELECT email, MAX(credits_balance) AS bal
           FROM platform_accounts GROUP BY email) b ON b.email = p.email
```

`MAX` 而非当前平台的值：环境按 email 分配、不按平台拆（见文件头），所以余额也要按 email
汇总。任一平台余额已高 → 该账号整体接近完成 → 牺牲它的期望损失更小。

### ORDER BY

```sql
ORDER BY rank,
         CASE WHEN rank = {RECHARGED_RANK} AND b.bal IS NULL THEN 1 ELSE 0 END,
         CASE WHEN rank = {RECHARGED_RANK} THEN b.bal END DESC,
         p.last_used_at ASC
```

三层：

1. `rank` — 档位优先。
2. NULL 余额推到本档最后。`credits_balance` 为 NULL 意味着余额读不到
   （`update_balance` 在 `balance_after` 读不到时直接 return，infron 常态、opencode 偶发），
   不知道就保守保留。**不能只靠 SQLite「DESC 时 NULL 排最后」的默认行为**——那是实现
   细节，要显式写出来并用测试锁住。
3. 本档内按余额 DESC（非第 3 档时该 CASE 恒为 NULL，不影响相对顺序），最后回落既有的
   `last_used_at ASC`（LRU）。

返回值加 `rank` 和 `bal` 两列：前者给 pool 层做「最后手段」判断，后者给日志。

## `reclaim()` 的挑选策略

候选已按 rank 排好，所以遇到第一个 `RECHARGED_RANK` 就意味着前面的高优先级档已经走完了。

```python
picked, sacrificed = [], None
for row in candidates:
    if len(picked) >= limit:
        break
    email = row["email"]
    if email in exclude or self._is_busy(email):
        continue
    if row["rank"] == RECHARGED_RANK:
        # 最后手段：已经挑到真终态环境就不必牺牲活账号；要牺牲也只牺牲一个。
        if picked:
            break
        sacrificed = row
        picked.append(row)
        break
    picked.append(row)
```

`if picked: break` 而不是 `continue`——后面全是同档或更低优先级的，没有再看的必要。

### 为什么第 3 档单次只回收 1 个

第 0/1/2 档删的是垃圾，批量删（`reclaim_batch` = 3）纯赚。第 3 档删的是活账号的登录态，
每删一个的代价是下次跑它时一次 GitHub 完整重登 + 一次新设备邮箱验证（数分钟 + 一封验证
码，见 `billing._auto_verify_device`）。批量删三个等于一次赔三份，而配额只要腾出一格就能
继续跑。

## 日志

第 3 档单独一条，写明是牺牲而非常规回收，并带余额：

```
[AdsPower] 无真终态环境可回收，牺牲 1 个活账号环境腾配额: a@b.c(recharged, 余额 $110)
```

现在的「已回收 N 个环境释放配额」看不出删的是垃圾还是活账号。这条日志出现的频率就是
「配额压力有多大」的直接指标。

## 兼容性

- `reclaim_candidates` 返回的 dict 多两个键（`rank` / `bal`），既有调用方只读
  `email` / `profile_id` / `status`，不受影响。
- 无 schema 变更（`credits_balance` 已在 `platform_accounts` 表里）。
- 无配置项新增。

## 回滚

两个文件的改动都是自包含的，`git revert` 即可。回滚后退回「recharged 环境被当垃圾删」
——不会更坏，只是继续频繁换环境。

## 风险

**配额压力不会消失。** 环境上限 12 < 活跃账号数（当前 14 个 recharged）是物理事实。
改完之后，配额满时仍然会牺牲活账号环境，只是：牺牲的是余额最高（最接近归档）的那个，
一次只牺牲一个，且只在真的没有垃圾可清时才动。如果日志里那条「牺牲活账号」高频出现，
说明该调的是 `balance_cap`（调低让账号更快真正归档）或轮转里的账号数——那是策略问题，
不在本任务范围。
