# 执行计划 — 卡池分组管理

> 前置：合并/删无效是破坏性 DB 操作，实现与实测前 **备份 `data/cloudflare_auto.db`**。落笔改每处前真实读取该行确认。

## 前置校验

- [ ] P1：真实读取 `card_pool.get_by_group`/`refresh_expired_status`/`add_cards`、`card_group.create`、`valid_card.is_valid`、`routes.py` 卡池端点、`CardPool.vue` 分组与卡片列表 + `api/index.js` 封装。
- [ ] P2：确认 `card_group.create(name, type)` 的签名与返回（新组 id）。

## 实现步骤（按依赖顺序）

- [ ] S1 — `card_pool.py` `count_buckets(group_id)`：一条聚合返回 total/invalid/valid/unverified（valid/unverified 用 `card_number IN (SELECT card_number FROM valid_cards)`）。
- [ ] S2 — `card_pool.py` `get_by_group(..., bucket='')`：加 bucket 过滤（valid/unverified/invalid/全部），COUNT 与 SELECT 同 WHERE。
- [ ] S3 — `card_pool.py` `delete_invalid_by_group(group_id)`：先 `refresh_expired_status`，再 `DELETE ... status IN ('invalid','expired')`，返回 rowcount。
- [ ] S4 — `card_pool.py` `move_non_invalid_to_group(source_group_ids, target_group_id)`：按 design 去重移动，返回 {moved, deduped}；含目标组已存在同号的判重跳过。
- [ ] S5 — `routes.py`：
  - `GET /api/card-pool/<gid>` 加 `bucket` 参数透传 + 响应加 `buckets`（count_buckets）。
  - `POST /api/card-pool/merge`（校验源组/name/type → create group → move → 返回 group_id/moved/deduped）。
  - `POST /api/card-pool/<gid>/delete-invalid`。
- [ ] S6 — 前端 `api/index.js`：新增 `mergeCardPools`、`deleteInvalidCards`、给 `getCardPool` 传 bucket。
- [ ] S7 — 前端 `CardPool.vue`：
  - 卡片列表加状态筛选按钮组 + 桶数量徽标（读 buckets）。
  - 分组面板加"归纳合并"按钮 + 弹窗（多选源组/新组名/类型）。
  - 卡片面板加"删除无效卡"按钮 + 二次确认。
  - `cd frontend && npm run build`。

## 验证 / 门槛

- [ ] V1 — 导入/语法：`.venv/bin/python3 -c "import src.api.routes, src.models.card_pool"`。
- [ ] V2 — 临时 DB 造数据单测（不跑浏览器）：
  - 组A: 2未验证/1有效/2无效；组B: 1未验证(与A同号1张)/1无效。
  - count_buckets 正确；get_by_group bucket 过滤正确。
  - merge([A,B],new)：new 内 = A/B 非无效去重并集（同号只1张，AC4）；A/B 只剩无效（AC3）；moved/deduped 数正确。
  - delete_invalid_by_group(A)：A 内无效=0，其余不动（AC5）。
- [ ] V3 — 接口冒烟：`curl` merge / delete-invalid / bucket 查询，返回结构正确。
- [ ] V4 — 前端构建通过；筛选/合并弹窗/删无效按钮可见可用（后端联调）。

## 审查门 / 回滚点

- 审查门：S1-S7 完成后回读全 diff，仅触及 card_pool/routes/CardPool.vue/api。
- 回滚点：破坏性 DB 操作靠备份回滚；接口/前端各自独立可回退。

## 注意事项

- 前端改动 `npm run build`（memory 约定）。
- 删除/移动均为硬操作，前端二次确认；返回受影响数量给用户。
- 统计/删除前先 `refresh_expired_status`，保证过期卡口径一致。
