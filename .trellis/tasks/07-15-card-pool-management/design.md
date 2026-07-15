# 技术设计 — 卡池分组管理

## 涉及边界

- `src/models/card_pool.py`：新增 桶筛选查询、桶计数、移动合并、批量删无效。
- `src/api/routes.py`：扩展 `GET /api/card-pool/<gid>`（bucket 参数）+ 桶计数；新增合并、删无效两个端点。
- `frontend/src/views/CardPool.vue`：分组卡片列表加状态筛选 + 桶计数；加"归纳合并"弹窗与"删除无效卡"按钮。`npm run build`。
- 无 DB schema 变更（复用 card_pool.status + valid_cards）。

## 桶定义（SQL 口径）

| 桶 | 条件 |
|----|------|
| 无效 invalid | `status IN ('invalid','expired')` |
| 有效 valid | `status NOT IN ('invalid','expired')` AND `card_number IN (SELECT card_number FROM valid_cards)` |
| 未验证 unverified | `status NOT IN ('invalid','expired')` AND `card_number NOT IN (SELECT card_number FROM valid_cards)` |
| 合并可移动（非无效） | `status NOT IN ('invalid','expired')` （= 有效 + 未验证） |

## 模型方法（card_pool.py）

- `get_by_group(group_id, page, page_size, bucket='')`：给现有方法加 `bucket` 过滤（'valid'/'unverified'/'invalid'/''）。用上表条件拼 WHERE，COUNT 与 SELECT 同条件。
- `count_buckets(group_id)`：返回 `{total, invalid, valid, unverified}`，一条聚合查询（valid/unverified 用 `card_number IN (SELECT ... valid_cards)` 的 CASE 统计）。
- `delete_invalid_by_group(group_id)`：`DELETE FROM card_pool WHERE group_id=? AND status IN ('invalid','expired')`，返回 rowcount。删前先 `refresh_expired_status(group_id)` 以确保过期卡已被标记。
- `move_non_invalid_to_group(source_group_ids, target_group_id)`：移动语义 + 去重，返回 `{moved, deduped}`。实现：
  1. 取源组所有"非无效"卡行（id, card_number, group_id），按 `card_number` 分组；
  2. 每个 distinct card_number：选一行 `UPDATE card_pool SET group_id=target WHERE id=?` 移入新组；该号在源组的其余"非无效"行 `DELETE`（去重，避免 `UNIQUE(card_number,target)` 冲突）；
  3. 若某卡号在目标组已存在（理论上新组为空，可忽略；稳妥起见移动前判重，已存在则该源行直接删除）；
  4. 统计 moved（移入数=distinct 非无效卡号数）与 deduped（删除的重复行数）。
  - 全程在一个事务里（借助 db 的 execute；如无显式事务 API，逐条 execute 亦可，失败不致命但需日志）。

## 路由（routes.py）

- `GET /api/card-pool/<gid>?bucket=valid|unverified|invalid`：透传 bucket 给 get_by_group；响应追加 `buckets`=count_buckets 结果。
- `POST /api/card-pool/merge`：body `{source_group_ids:[..], name, type}`。校验：≥1 源组、name 非空、type∈{bind,payment}。流程：`card_group.create(name,type)` → `card_pool.move_non_invalid_to_group(source_ids, new_id)` → 返回 `{group_id, moved, deduped}`。
- `POST /api/card-pool/<gid>/delete-invalid`：调 `delete_invalid_by_group`，返回 `{deleted}`。

## 前端（CardPool.vue）

- **分组卡片列表**：顶部加状态筛选（全部/有效/未验证/无效）按钮组 + 桶数量徽标；切换时带 `bucket` 重新拉取。
- **归纳合并**：分组面板加"归纳合并"按钮 → 弹窗：多选源分组（复选框列表）、输入新分组名、选类型 → 调 merge 接口 → 成功后刷新分组列表并提示 moved/deduped。
- **删除无效卡**：选中分组的卡片面板加"删除无效卡"按钮 → 二次确认 → 调 delete-invalid → 刷新列表与桶计数。
- 复用现有 `api/index.js` 封装风格新增三个调用。构建 `npm run build`。

## 数据流

```
合并: 选源组[A,B] + 新组名/类型
  → create group → move_non_invalid_to_group([A,B], new)
       每个 distinct 非无效卡号: 移一行入 new, 删源组其余同号非无效行
  → 新组=有效+未验证去重并集; A/B 只剩无效卡
删无效: 选组G → refresh_expired_status(G) → DELETE status IN(invalid,expired)
筛选: GET ?bucket=valid → WHERE 非无效 AND in valid_cards
```

## 兼容性 / 回滚

- 无 schema 变更；新增方法/端点/前端为增量。
- 移动是破坏性数据操作（改 group_id / 删重复行）——执行前**用户二次确认**，实现前**备份 DB**。回滚靠备份。
- 删无效为硬删除，删前二次确认 + 返回数量。

## 风险

- 移动合并的去重：同卡号跨组多行，只保留一行入新组、其余删除——需确认删除的是"非无效"重复行，不误删无效卡（无效卡不参与移动、留在源组）。
- `UNIQUE(card_number, group_id)`：移动入新组前若新组已存在同号（新组通常为空，风险低），需判重跳过，避免约束报错。
- `is_valid`/valid_cards 交叉子查询在大表上的性能：卡池规模为百级，可接受。
- 过期状态需先 `refresh_expired_status` 再统计/删除，否则"到期但未标记"的卡会被误判为非无效。
