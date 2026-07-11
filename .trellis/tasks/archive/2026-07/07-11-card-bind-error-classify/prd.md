# 绑卡失败错误信息分类细化

## Goal

绑卡失败时，将具体失败原因写入 `card_bindings.error` 字段，并按来源/性质分类，取代固定写入的 `"bind failed"`。

## 错误分类

| 分类前缀 | 含义 |
|---|---|
| `[控制台表单错误]` | 浏览器控制台拦截的 Stripe Setup intent / Form handler 错误 |
| `[外部原因]` | 银行/发卡机构拒绝（declined、do not honor、余额不足等） |
| `[表单字段错误]` | 表单字段验证失败（CVC/卡号/有效期/地址格式错误） |
| `[Stripe字段错误]` | Stripe iframe DOM 内检测到的字段错误 |
| `[支付处理错误]` | 通用支付处理错误（processing error、something went wrong 等） |
| `[页面错误]` | dialog 内 role="alert" 等错误元素文本 |
| `[浏览器中断]` | Selenium 异常 |
| `[操作失败]` | 找不到按钮/弹窗/iframe 等 UI 操作失败 |
| `[验证超时]` | Turnstile 人机验证超时 |
| `[提交超时]` | 提交按钮持续 loading 超过阈值 |
| `[超时]` | 整体等待结果超时 |

## Requirements

1. `_check_dialog_card_error`：返回带分类前缀的错误字符串
2. `add_credit_card`：返回类型改为 `(bool, str)`，失败时附带分类错误字符串
3. `_wait_for_payment_submit_result`：返回类型改为 `(bool, str)`
4. `registration.py` 两处调用解包元组；有 `mark_failed` 的地方将 error_reason 写入

## Acceptance Criteria

- [ ] 绑卡失败后 `card_bindings.error` 包含分类前缀和具体原因
- [ ] 不改变成功路径行为
- [ ] 不改动 `_handle_dialog_turnstile` 函数签名
