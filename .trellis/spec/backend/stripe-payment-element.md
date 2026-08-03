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

## 折叠手风琴：字段在展开前不存在于 DOM

**这是「找不到卡号字段」的首要原因，排查时先查这一条。**

账号有多种支付方式（Card / Bank）时，Stripe 把 Payment Element 渲染成**折叠的
手风琴**。此时 payment frame 的 body 只有约 3.7KB，结构是：

```html
<div class="p-PaymentElement ... is-collapsed">
  <div data-is-collapsed="true" data-selected-payment-form="card">
    <div class="p-AccordionButton" role="button" data-value="card" aria-expanded="false">Card</div>
    <div class="p-AccordionPanel" id="card"></div>   <!-- 空 -->
    <div class="p-AccordionButton" data-value="link_card_brand">Bank</div>
```

关键点：**卡号/有效期/CVC 输入框在展开前根本不存在**，不是「还没加载完」。
必须点击 `[role="button"][data-value="card"]` 展开（见 `_expand_stripe_card_accordion`），
面板填充后字段才被创建。

这个现象极具迷惑性，曾连续误判三轮：

- 界面上「Card / Bank」两栏渲染完好、肉眼完全可见 → 看起来已加载完成
- DOM 查询读到零 input → 看起来是选择器失配
- 等再久也不变（实测等满 90s 无变化）→ 又被误判为加载卡住

三条线索单看都指向错误方向，只有 dump payment frame 的 innerHTML 全文才能定死。
诊断「字段找不到」时**第一步就该打这段 HTML**。

只有一种支付方式时 Stripe 直接平铺，无手风琴——本地用测试 key 挂载默认就是这种
形态，**复现折叠态需显式指定** `paymentMethodTypes: ['card','us_bank_account']` +
`layout: {type:'accordion', defaultCollapsed:true}`。

展开操作要**每轮轮询都尝试**，不能只在首轮点一次：Stripe 重建 iframe 后会退回折叠态。

## Link 勾选框必须取消

填完卡号后，Payment Element 底部出现 Link 的
「Save my information for faster checkout」勾选框，**默认勾选**。勾上后 Link 会把
邮箱与手机号变成必填，提交时报 `Please provide a mobile phone number.`，
以 `[Stripe字段错误]` 形式导致绑卡失败。我们只需存卡、不需要 Link 账户，取消即可。

```html
<label for="payment-linkOptInInput" id="Field-linkOptInCheckbox">
  <input id="payment-linkOptInInput" name="linkOptIn" type="checkbox" checked>
  <span class="p-LinkOptIn-labelText">Save my information for faster checkout</span>
```

`input` 被自定义样式遮住，`uncheck()` 过不了可操作性检查，**必须点 `label`**
（见 `_uncheck_link_opt_in`，另有 JS 派发 input/change 事件的兜底——Stripe 靠
React 状态渲染，只改 checked 属性不派发事件不生效）。

时序要求：勾选框在**填完卡号后**才出现，取消操作必须排在填写之后。

注意：该勾选框只在特定条件下出现，本地用测试 key 挂载时不一定复现（曾出现只有
手机号字段、没有勾选框的形态），因此这段逻辑**未经本地验证**，选择器取自真机 DOM。

## 就绪判定

`elements-inner-loader-ui` frame 在场**不能**用来判断界面是否还在加载——折叠态下
它同样挂着，而界面其实已渲染完毕。该 frame 只作日志如实记录，不作推断依据。

判定就绪只能以**卡号字段可定位**为准。「弹窗内 iframe ≥ 2 个」只是容器已渲染的
弱判据，据此返回就绪会把问题掩盖到填写阶段才暴露。

## 跨站点复用时的两处补充（infron.ai 实测，2026-08-04）

**用裸 `name` 选择器，不要依赖 `id`。** 本文开头那张表的 `id` 前缀（`payment-`）
是**该站点的 element 名字**，换个站点就变。infron 的 Payment Element 上
`input[id$="-numberInput"]` 命中不了，`input[name='number']` / `expiry` / `cvc`
稳定命中。地址字段同理：`input[name='postalCode']`。

`name` 是 Stripe 自己的字段契约，跨站点稳定；`id` 由集成方的 element 命名决定。
候选列表把裸 `name` 排在最前。

**支付方式默认不一定是 Card。** infron 的 tab 顺序是
`Alipay | Card | Afterpay | US bank account | Cash App Pay | More`，**默认选中 Alipay**，
不先点 Card 卡号字段根本不渲染。opencode 的 hosted Checkout 用的是 accordion
（`#payment-method-accordion-item-title-card`），两套定位不通用。

**账单地址按卡的国家动态渲染。** 美国卡只渲染邮编，`line1`/`city`/`line2` 根本不存在。
把"未命中"当成失败会误判——地址字段的命中与否不该进 ok 判据。

**卡种未启用时报的是 `incomplete` 而不是"不支持"。** Payment Element 认不出该 BIN，
就按 16 位通用规则要求补齐，于是 14 位 Diners 报 `Your card number is incomplete.`。
这与"我们没填完"的报错**一字不差**，无法从文案区分——两者都必须归 `error`
（不消耗卡），理由见 `error-handling.md` 的「客户端表单校验 ≠ 拒付」。
