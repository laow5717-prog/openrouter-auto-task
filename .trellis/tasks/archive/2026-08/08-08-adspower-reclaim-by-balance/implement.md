# 执行计划

## 步骤

### 1. 常量分家（`src/models/adspower_profile.py`）

- [ ] `_PLATFORM_TERMINAL` 拆成 `_PLATFORM_DONE` / `_PLATFORM_REUSABLE` /
      `_PLATFORM_RECLAIMABLE`，加 `RECHARGED_RANK = PLATFORM_DONE_RANK + 1`。
- [ ] **改写第 38-40 行那段注释**：从「与 utils.PLATFORM_TERMINAL_STATUSES 保持一致，
      改动时两处都要改」改成「两者语义已分家，不要同步」，并写清各自回答什么问题。
      这条最容易被下一个人照着改回来。
- [ ] 文件头「回收优先级」那段补第 3 档的说明。

### 2. `reclaim_candidates` SQL 分档

- [ ] WHERE 的 NOT IN 集合换成 `_PLATFORM_RECLAIMABLE`（放 `recharged` 进候选集）。
- [ ] rank 的 CASE 加一档：有任一平台行为 `recharged` → `RECHARGED_RANK`。
- [ ] LEFT JOIN `(SELECT email, MAX(credits_balance) AS bal FROM platform_accounts
      GROUP BY email)` 取余额。
- [ ] ORDER BY 三层：`rank` → NULL 余额推后 → 本档 `bal DESC` → `last_used_at ASC`。
- [ ] SELECT 加 `rank` / `bal` 两列。
- [ ] docstring 同步：三档变四档，写清第 3 档是最后手段及其代价。

⚠️ 占位符顺序：现有 SQL 靠位置传参且已经有 4 组（dead × 3 + terminal × 1），
加了 JOIN 和新 CASE 之后**参数顺序必然变**。改完先跑一次 `reclaim_candidates` 冒烟，
确认没有 `Incorrect number of bindings`。

### 3. `reclaim()` 挑选策略（`src/browser/adspower_driver.py`）

- [ ] 按 design 的循环改写：遇 `RECHARGED_RANK` 时，`picked` 非空就 break；
      否则只挑 1 个然后 break。
- [ ] 日志分两条：常规回收沿用现文案；牺牲活账号单独一条，带 `recharged` 与余额。
- [ ] `reclaim()` docstring 补第 3 档语义。

### 4. 测试（`tests/test_adspower_pool.py`）

- [ ] **重写** `test_reclaim_allowed_once_all_platforms_finished` —— 它现在用
      `recharged` + `subscribed` 断言可回收，正是本任务要反转的语义。改成用
      `archived` + `subscribed`，并把 `recharged` 那条挪进新增的对照测试。
- [ ] `test_archived_reclaimed_before_recharged` — 两个候选一个 archived 一个
      recharged，配额满时只删 archived。**核心回归。**
- [ ] `test_recharged_sacrificed_only_as_last_resort` — 只有 recharged 可选时才删它。
- [ ] `test_recharged_sacrifice_is_capped_at_one` — `reclaim_batch=3` + 三个
      recharged 候选，单次只删 1 个。
- [ ] `test_recharged_sacrifice_picks_highest_balance` — 余额 $20 / $110 / $49，
      删 $110 那个。
- [ ] `test_null_balance_sacrificed_last` — 余额 NULL 的排在所有有余额的之后。
- [ ] `test_unfinished_platform_still_never_reclaimed` — 既有保护不被新档破坏
      （`registered` 平台行存在时永不回收）。
- [ ] `test_no_candidates_at_all_still_raises` — 连 recharged 都没有时仍抛
      `AdsPowerQuotaExceeded`。

### 5. 验证

```bash
.venv/bin/python -m pytest tests/test_adspower_pool.py -q
.venv/bin/python -m pytest tests/ -q          # 全量，当前基线 558 项
```

### 6. 生产库核对（只读，不改数据）

改完后对着真实库跑一次候选查询，确认排序符合预期——当前库里 14 个 recharged、
0 个 archived，第一候选应该是余额 $110 那个：

```bash
sqlite3 "file:data/openrouter_auto.db?mode=ro" "<新 SQL>"
```

## 审查关卡

- 步骤 2 完成后：**必须**用生产库数据核对一次排序，光靠单测的构造数据看不出
  占位符错位（错位往往表现为「查询能跑但排序莫名其妙」）。
- 步骤 3 完成后：确认 `is_busy` 过滤仍在第 3 档之前生效——牺牲一个正在被 worker
  使用的环境会让那个 worker 的浏览器凭空消失。

## 回滚点

- 步骤 1-2 与步骤 3 可分别回滚：SQL 只是多返回两列并调整排序，即使 pool 层没改，
  行为也只是退回「recharged 与 archived 同档」（因为 pool 不看 rank 就不会区分）。
  注意这个中间态**不安全**（recharged 会被批量删），两步要一起提交。
