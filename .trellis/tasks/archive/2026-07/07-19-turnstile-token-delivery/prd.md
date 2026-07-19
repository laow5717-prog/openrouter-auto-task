# 修复 Turnstile token 未交付导致注册静默失败

> ⚠️ 本 PRD 的成因分析尚未实证，取证脚本待跑（见文末「待取证」）。
> 取证结果可能推翻主假设，届时需要回来改本文档而不是硬套现有方案。

## 现象

用户报告：注册页面的人机校验「一直过不去」，浏览器上 Turnstile 组件始终没有勾选。

日志（`server.log`）显示的实际序列与用户观感不同：

```
🤖 尝试使用 2Captcha 解决内嵌 Turnstile...
  2Captcha returned token (length: 816)
  Turnstile token injected
  ✅ 2Captcha 解决成功！
🔘 正在点击注册按钮...
✅ 注册表单已提交
等待验证邮件 (最长 120 秒)...
等待验证邮件超时
未收到验证数据
```

`server.log` 中 4 次注册尝试，**4 次全是这个结局**，成功 0 次。
（日志每条出现两次是 `max_workers: 2` 两个 worker 并发所致，非重复打印。）

## 主假设：token 从未真正交付给页面

`_inject_turnstile_token`（`src/services/captcha.py:488-523`）只把 token 写进
hidden input 的 `.value`。同一文件里的 hCaptcha 版本（`:414-485`）做了四件事，
Turnstile 版只做了第一件：

| 步骤 | hCaptcha | Turnstile |
|------|----------|-----------|
| 填 response 字段的值 | ✅ | ✅ |
| 调官方 API 交付 token（`setResponse`） | ✅ | ❌ 空壳（`:509-516` 取出 widgetId 后什么都没做） |
| 触发 `input` / `change` 事件 | ✅ | ❌ 缺失 |
| 调 `data-callback` 回调 | ✅ | ❌ 缺失 |

Cloudflare 注册页是 React 应用。只设 `.value` 而不派发 `input`/`change` 事件，
React 的受控组件状态不会更新，提交时 payload 里的 token 仍是空的 → CF 后端拒绝
注册 → 不发验证邮件。

### 佐证：判定函数本身在绕过这个问题

`_is_turnstile_truly_solved`（`driver.py:1442-1494`）在 2Captcha 注入后取代
`_is_turnstile_solved` 使用，理由是「token 是自己注入的」。它改看
「submit 按钮 enabled」「widget 消失」等**间接信号**——而这些在 token 无效时同样
成立，所以「✅ 2Captcha 解决成功」是误判。

### 放大问题：失败被吞掉

`fill_signup_form`（`driver.py:3261`）忽略 `_handle_inline_turnstile` 的返回值。
Turnstile 超时返回 `False`，代码照样点提交按钮，且 `fill_signup_form` 仍返回
`True`。上层 `registration.py:72` 只看到「表单填写成功」。

## 竞争假设（未排除）

**邮箱域名被 CF 拉黑**。临时邮箱来自 mail.tm 的 `web-library.net`。若 CF 已把该
域名列入一次性邮箱黑名单，则会静默拒绝注册且不发邮件——**症状与主假设完全一致**。
若属实，修 token 注入完全无效。

取证的核心目的就是区分这两者。

## 待取证

取证脚本：`scratchpad/probe_turnstile.py`（被动监听，不改产品代码）。
需要 2Captcha key（存在浏览器 localStorage，非 config.yaml）：

```bash
CAPTCHA_API_KEY=<key> .venv/bin/python3 <scratchpad>/probe_turnstile.py
```

| | 问题 | 手段 | 判读 |
|---|------|------|------|
| Q1 | 注入后 widget 真 solved 吗 | 抓 `data-status` / `data-callback` / input value 长度，对比两个判定函数 | 分歧即证明判定误报 |
| Q2 | **提交时 payload 里 token 是什么** | `page.on('request')` 抓注册 POST body | **决定性证据** |
| Q3 | CF 报错了吗 | 响应 body + 提交后页面可见文本 | 出现邮箱相关错误 → 竞争假设成立 |

判读规则：

- payload 中 `cf_challenge_response` 为空或字段缺失 → **主假设成立**，按下方需求修
- payload 中 token 完整（816 字符）但 CF 仍拒绝 → **主假设被推翻**，重写本 PRD，
  优先排查邮箱域名与 2Captcha token 的 IP/指纹绑定问题

## 需求（以主假设成立为前提）

### R1 补齐 token 交付

`_inject_turnstile_token` 对齐 hCaptcha 的实现：

- 用 native value setter 设值后派发 `input` / `change` 事件（绕过 React 受控组件
  对 `.value` 直接赋值的忽略）
- 调用 `window.turnstile` 的官方交付 API（把 `:509-516` 的空壳补完）
- 调用 `data-callback` 指定的全局回调函数

### R2 不再吞掉 Turnstile 失败

`fill_signup_form` 必须尊重 `_handle_inline_turnstile` 的返回值：Turnstile 未通过
时不点提交按钮，直接返回失败。

### R3 失败可诊断

注册提交后未收到邮件时，把页面可见的错误文本打进日志。当前完全看不到 CF 说了什么，
这正是本次排查耗时的原因。

## 验收标准

1. 取证脚本重跑，提交 payload 中 `cf_challenge_response` 带完整 token（非空）
2. 走通一次真实注册：收到验证邮件并完成账号创建
3. 人为让 Turnstile 失败（如填错 2Captcha key），`fill_signup_form` 返回 `False`
   且日志明确写出失败原因，不再出现「表单已提交」后静默等邮件
4. 现有绑卡 / 充值路径的 hCaptcha 与 dialog Turnstile 行为不变（无回归）

## 约束

- **不引入 CDP 穿透 closed shadow DOM**。`driver.py:932-934` 有明确记录：创建 CDP
  会话/发送 DOM 命令会破坏 Patchright 隐蔽性，触发 Cloudflare
  "There was a problem with verification"。这是踩过坑的决定，不要推翻。
  同理不注册 `page.on("console")`（强制 `Runtime.enable`）、不用 Playwright
  `locale` 选项（`Emulation.setLocaleOverride`）。
- 本任务只改注册路径的内嵌 Turnstile。`_handle_dialog_turnstile`（绑卡弹窗）与
  hCaptcha 路径若要一并改，需单独验证。
