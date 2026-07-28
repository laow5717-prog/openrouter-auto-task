# 每日充值:opencode 会话重建走完整 OAuth 链 + 关闭浏览器假死修复

## 背景(2026-07-28 实测)

每日充值任务跑 fernandezr701@hotmail.com 时出现两个问题(web 日志 03:16:51–03:22:12):

1. **任务层失败**:GitHub 密码登录成功后,`opencode_billing.ensure_opencode_session`
   只重访 `opencode.ai/auth` 睡 3 秒取 wid,从不点「Continue with GitHub」/「Authorize」。
   凡 profile 里 opencode 会话 cookie 过期(GitHub 能重登)的账号,该路径必然失败,
   报「GitHub 已登录但未能建立 opencode 会话」。完整 OAuth 点击链已存在于
   `opencode_login.login_and_open_own_go`(点 Continue → Authorize → flag 检测 → provision 重试),
   但充值路径未复用。
2. **体验层假死 ~5 分钟**:失败返回后 `driver.quit()` 里 `context.close()` 阻塞;
   30s 看门狗按 profile 强杀 Chrome,但当时 Chrome 进程已退(日志无「回收 N 个」),
   真正卡住的是 Playwright node driver(`cli.js run-driver`),看门狗不管 node,
   于是静默干等约 300s 才自解。期间无任何日志,现象如同任务挂死。

## 需求

R1. `ensure_opencode_session` 在 GitHub 登录成功后,建立 opencode 会话须复用
    `opencode_login` 的完整 OAuth 链(Continue with GitHub、Authorize、flagged 检测、
    provision 重试),不再裸等 3 秒。
    - 已登录快路径(重访 /auth 直接拿到 wid)保持不变。
    - flagged 账号返回明确原因,供上层记失败原因。
    - 返回契约 `(wid, detail)` 不变,调用方(registration/routes/scripts)无需改动。

R2. `quit()` 看门狗强杀 Chrome 后若 close 仍未解除阻塞,须在短时间(≈10s)内回收
    Playwright node driver 进程,让失败路径几秒内收尾,不再静默 5 分钟。
    - node pid 在 driver 创建时捕获保存;看门狗线程只做 os.kill,不碰 Playwright 对象。
    - close 正常完成时不得误杀(用完成标志位判定)。
    - create_driver 与 create_driver_vanilla 两条栈都覆盖。

## 验收标准

A1. 模拟「opencode cookie 失效 + GitHub 可登录」的账号跑每日充值:能点到
    Continue with GitHub / Authorize 并拿到 wid,或明确报 flagged 原因;不再出现
    「GitHub 已登录但未能建立 opencode 会话」的裸失败(除非 OAuth 链真实失败且 detail 说明原因)。
A2. close 阻塞场景:看门狗 30s 杀 Chrome → ≤10s 后回收 node,任务日志在 1 分钟内
    出现下一步输出,不再有 5 分钟静默。
A3. 正常关闭路径(close 秒级完成)无回归:无误杀 node、无多余告警日志。
A4. 现有调用方契约不变:`ensure_opencode_session(session, monitor, login_password, email)`
    返回 `(wid, detail)`;`close_driver` 幂等不抛。

## 范围外

- 卡质量 / hCaptcha 求解成功率。
- close 阻塞 300s 的 node 侧根因(只做兜底回收)。
