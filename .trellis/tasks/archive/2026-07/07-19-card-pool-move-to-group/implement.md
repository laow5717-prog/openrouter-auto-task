# 执行计划：卡池跨分组移动

改动局限在「模型加一个方法 + 路由加一个端点 + 前端加一个弹窗」三层，契约已在 `prd.md` 写死，不单独出 `design.md`。

## API 契约

```
POST /api/card-pool/<int:group_id>/move
body: { "target_group_id": int, "bucket": "unverified"|"valid"|"non_invalid", "limit": int }
200:  { "status": "ok", "moved": int, "skipped": int }
400:  { "error": "<原因>" }   # 参数非法 / 源目标相同
404:  { "error": "分组不存在" }
```

`group_id` 为源分组（与 `/api/card-pool/<group_id>` 列表端点的路径参数一致）。

## 步骤

### 1. 模型层 — `src/models/card_pool.py`

新增 `move_bucket_to_group(source_group_id, target_group_id, bucket, limit)`，放在 `move_non_invalid_to_group`（当前 :137）之后。

- 开头调 `self.refresh_expired_status(source_group_id)` —— 与 `count_buckets` / `delete_invalid_by_group` 一致，否则 `unverified` 桶会把已过期卡算进来。
- `bucket == 'non_invalid'` 时复用 `CARD_STATUS_UNUSABLE` 拼 `NOT IN`；其余走 `self._bucket_where(bucket)`（C1：不重写桶 SQL）。
- 选卡：`WHERE group_id=? AND <桶片段> ORDER BY id ASC LIMIT ?`。
- 去重（R4）：先取目标分组已有 `card_number` 集合，命中则 `skipped += 1` 并**跳过**（不 DELETE，区别于 `move_non_invalid_to_group`）；否则 `UPDATE card_pool SET group_id=? WHERE id=?`，`moved += 1`。
- 同批内同卡号也要计入 `seen`，防止一次移动里自撞 UNIQUE。
- 返回 `{'moved': int, 'skipped': int}`。
- 事务（C2）：确认 `Database.execute` 的提交语义，如为逐条自动提交，需在本方法包一层显式事务，使中途异常不留半移动状态。

### 2. 路由层 — `src/api/routes.py`

在 `merge_card_pools`（当前 :674）之后新增端点，校验顺序：

1. `target_group_id` 缺失 / 非整数 → 400
2. `target_group_id == group_id` → 400「源分组与目标分组相同」
3. `bucket` 不在三个合法值内 → 400
4. `limit` 非正整数 → 400
5. 源、目标分组任一 `card_group.get_by_id()` 为空 → 404

全部校验通过后才调模型（AC4：非法入参零数据变更）。**不要动 `merge_card_pools`**（C3）。

### 3. 前端 — `frontend/src/api/index.js` + `frontend/src/views/CardPool.vue`

- `index.js`：仿 `mergeCardPools`（当前 :92）加 `moveCardsToGroup(groupId, payload)`。
- `CardPool.vue`：加「移动到分组」按钮 + 弹窗（目标分组下拉需排除当前分组、桶下拉、数量输入）。提交后刷新列表与桶计数，toast 提示 `moved` / `skipped`。样式与交互跟现有合并弹窗保持一致。
- `cd frontend && npm run build`（C5）。

## 验证

移动是破坏性操作，**跑验证前先备份**：

```bash
sqlite3 data/cloudflare_auto.db ".backup '<scratchpad>/pre-verify.db'"
```

对照 `prd.md` 的 AC1–AC7 逐条验证，重点：

- AC1/AC5：移动前后跑两个分组的桶计数，差值应为 `moved`；抽查被移动行的 `status` 均不在 `('expired','invalid')`。
- AC3：先手工在目标分组插一张与源分组同号的卡造重复，验证 `skipped` 与源行仍在。
- AC4：四种非法入参各打一次，之后核对 `card_pool` 行数与 group 分布未变。
- AC7：`merge` 端点回归跑一次。

## 回滚

单次 commit，`git revert` 即可；数据侧用验证前备份还原。
