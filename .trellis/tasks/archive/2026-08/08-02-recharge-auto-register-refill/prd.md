# 充值任务账号耗尽自动注册补号 + 充值成功去重

## 背景

每日充值任务 `run_daily_pipeline`(app.py)当前对「有登录密码且 status ∉ banned/archived/flagged」
的账号轮转充值。两个缺口:

1. **重复充值**:充值成功后账号标 `status='recharged'`,但筛选**未排除 recharged**——下一轮
   会再次选中它重开浏览器登录(靠登录后「余额≥$20 归档」预检才跳过,白开一次浏览器)。
2. **账号耗尽即停**:50 个 `imported` 账号(hotmail.xlsx 已导入、未注册 GitHub、无登录密码)
   进不了充值轮转。可充账号充完后任务直接结束,不会自动注册新号补充。

订阅任务 `run_daily_subscribe_pipeline` / `_subscribe_one_account` 已有「未注册先注册」能力
(复用 `github_signup_service.signup_one`),充值任务应复用同一套注册流程。

## 需求

R1. **充值成功去重**:`run_daily_pipeline` 账号筛选排除 `recharged`(与 banned/archived/flagged
    并列)。充值成功过的账号从此不再进入充值轮转。

R2. **账号耗尽自动补号**:当某轮「可充值账号集为空」时,从 `imported` 账号(status='imported'
    且 hotmail.xlsx 有该邮箱数据)取一个,自动注册(GitHub 全自动、碰 Arkose 跳过)。
    - 注册成功 → 账号 status='registered' 并存 login_password → 进入充值(登录 opencode 后充值)。
    - 注册未成(无 hotmail 数据/碰 Arkose/挂起/失败)→ 账号状态相应改变(不再是 imported),
      继续取下一个 imported;所有 imported 处理完仍无可充账号 → 任务结束。

R3. **复用而非复制注册逻辑**:把 `_subscribe_one_account` 内的注册段抽成共享帮助函数
    `_register_one_account(acct, worker) -> (result, detail)`,充值补号与订阅注册都调它,
    保证两条流水线注册行为一致(Arkose→pending、挂起→suspended、失败→failed、成功→registered)。

R4. **闭环连贯**:补号注册成功后应能顺畅进入「登录→充值」(充值流程 `ensure_opencode_session`
    本身含登录),无需人工介入。

## 约束

- 不改动订阅任务对外行为(`_register_one_account` 抽取后 `_subscribe_one_account` 结果不变)。
- 注册用 Patchright 栈(signup_one 内部),充值用 vanilla 原生栈,与现状一致;串行不冲突。
- 防死循环:每个 imported 账号经 `_register_one_account` 后 status 必然离开 'imported',
  不会被重复取到;补号阶段不得让 round_num 兜底上限提前耗尽(见 design MAX_ROUNDS 调整)。
- 停止/截图/账号排他(account_registry.claim)集成与现有充值路径一致。

## 验收标准

A1. status='recharged' 的账号不出现在每日充值账号集中(单测:筛选表达式 + 一次实跑日志无重复充值)。
A2. 构造「无可充账号 + 有 imported」场景:任务进入补号,调 `_register_one_account`;
    注册成功的账号随后被充值流程处理(登录→充值),不再直接结束。
A3. `_register_one_account` 抽取后,订阅任务注册行为无回归(reached_captcha/suspended/
    signup_complete/失败 四分支状态落库与抽取前一致)。
A4. 所有 imported 处理完且无其它可充账号 → 任务正常收尾,收尾统计含「注册补号 N 个」。
A5. 语法/导入无回归;既有充值逐卡消耗、3DS、hCaptcha、余额归档逻辑不受影响。

## 范围外

- 卡质量 / hCaptcha 成功率。
- 注册成功率(Arkose 拦截)本身的提升。
- 订阅任务的补号(本任务只做充值侧;订阅已自带注册)。
