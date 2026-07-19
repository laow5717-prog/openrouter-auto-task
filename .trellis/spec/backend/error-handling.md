# Error Handling

> How errors are handled in this project.

---

## Overview

<!--
Document your project's error handling conventions here.

Questions to answer:
- What error types do you define?
- How are errors propagated?
- How are errors logged?
- How are errors returned to clients?
-->

(To be filled by the team)

---

## Error Types

<!-- Custom error classes/types -->

(To be filled by the team)

---

## Error Handling Patterns

<!-- Try-catch patterns, error propagation -->

(To be filled by the team)

---

## API Error Responses

<!-- Standard error response format -->

(To be filled by the team)

---

## Common Mistakes

<!-- Error handling mistakes your team has made -->

### 按错误前缀归因会误杀好卡

绑卡失败时会把底料卡标为 `invalid`（`card_pool.status`），此后 `get_usable_cards_as_list`
永远不再选中它。**这个操作不可逆**，因此归因必须保守。

错误串带分类前缀（`[外部原因]` / `[表单字段错误]` / `[操作失败]` / `[验证超时]` /
`[浏览器中断]` / `[超时]` / `[Stripe字段错误]` / `[控制台表单错误]`），但**前缀不足以定性**：

- `[Stripe字段错误] Please provide a mobile phone number.` —— 这是 Stripe Link
  勾选框要求填手机号，与卡毫无关系。当时该问题导致**每一张卡**都失败；若按前缀
  归因，一整批完好的卡会被永久标成无效。

因此 `utils.is_card_fault()` 的判定顺序是：

1. 命中否定词（`mobile phone number` / `captcha` / `turnstile` / `人机验证` …）→ 一律不归因
2. 环境类前缀（`[操作失败]` `[验证超时]` `[浏览器中断]` `[超时]`）→ 不归因
3. 卡片类前缀（`[外部原因]` `[表单字段错误]`）→ 归因
4. 其余按文案白名单匹配（declined / incorrect cvc / invalid card number / 被拒 …）
5. 都不匹配 → **不归因**

原则：**宁可漏标**。漏标的代价只是下次再试一次这张卡；误标是永久废掉一张好卡。
新增判定规则时先补 `tests/test_card_fault.py`。
