# Stripe Payment Element 自动化契约

以下事实由本地挂载真实 Stripe Payment Element 实测得出（非推测），
探针方式：本地静态页 + `stripe.elements({mode:'setup', currency:'usd'})`，
用项目自身的 patchright 打开后 dump `page.frames` 结构。

## 字段命名

Cloudflare 绑卡弹窗用的是 **Payment Element**（不是旧的 Card Element）。
字段全部位于 `componentName=payment` 的那一个 frame 内（URL 形如
`js.stripe.com/v3/elements-inner-accessory-target-*.html`），同层平铺，不分帧：

| 字段 | name | id |
|------|------|-----|
| 卡号 | `number` | `payment-numberInput` |
| 有效期 | `expiry` | `payment-expiryInput` |
| CVC | `cvc` | `payment-cvcInput` |

要点：

- `id` 前缀是 **element 类型**（`payment-`），不是固定串。用后缀匹配
  `input[id$="-numberInput"]`，不要写死 `#Field-numberInput`（该命名不存在）。
- 三个字段在**同一个 frame** 内。遍历 frame 填字段时不能用 `if/elif` 链，
  否则一次循环只填一个，单 frame 场景下有效期和 CVC 永远填不上。
- `page.locator(...)` 不穿透 iframe，必须先拿到 Frame 再 `frame.locator(...)`。
  Playwright 的 CSS 选择器会穿透 open shadow DOM，所以字段找不到时可排除该因素。

## 输入必须回读校验

Stripe 字段在输入过程中重格式化并重建 DOM，与逐字输入构成竞态，**随机吞字符**。
实测同一份代码连续跑，有效期 `'1230'` 分别得到 `'12 / 30'`（对）、`'1'`、
`'02 / 30'`（月份都错）。固定延迟治不好——`delay=150` 比 `delay=50` 更差。

必须「填完回读、不符重填」（见 `_type_and_verify_stripe_field`），两个要点也是实测所得：

- **清空要逐次 Backspace**。`Control+a` + `Delete` 清不干净，残留会与重填字符
  混合成 `'10 / 23'` 这种四次重试全败的结果。
- **重试要递增延迟**（60ms 起，每次 +120ms）。失败几乎都是输入过快被吞。

比对时只取数字：Stripe 会插入空格/斜杠（`'4242 4242 4242 4242'`、`'12 / 30'`）。

改进后连跑 5 轮，三个字段全部通过（卡号偶尔需要第 2 次）。

## 渲染耗时

frame 列表里出现 `elements-inner-loader-ui` 即表示 Stripe 仍在显示**加载骨架屏**，
字段尚未渲染。曾因 `_wait_for_stripe_fields_ready` 超时仅 15s 而在骨架屏阶段就放弃，
退化到 Tab 盲打。弹窗内同时加载 PayPal / hCaptcha / GooglePay，慢网络下远超 15s
属正常，现为 90s。

判定就绪只能以**卡号字段可定位**为准。「弹窗内 iframe ≥ 2 个」只是容器已渲染的
弱判据，据此返回就绪会把问题掩盖到填写阶段才暴露。
