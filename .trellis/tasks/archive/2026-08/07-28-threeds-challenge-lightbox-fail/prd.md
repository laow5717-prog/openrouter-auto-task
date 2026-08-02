# 3DS challengeFrame 挑战弹窗判失败:关闭并换卡

## Goal

点 Pay 后等待 3DS 结果期间,Stripe 可能弹出交互挑战 Lightbox
(`.LightboxModal` 内嵌 `iframe#challengeFrame` / `name=stripe-challenge-frame`,
实机 2026-07-28 capitalone ACS)。它要求持卡人在发卡行侧完成验证,自动化场景无人可点,
干等只会耗满 120s 超时。应视为校验不通过:关闭弹窗并换下一张卡。

## Requirements

- R1 新增检测 `_threeds_challenge_lightbox`:任意 frame 内
  `.LightboxModal iframe#challengeFrame / iframe[name=stripe-challenge-frame] / .ThreeDS2-challenge`。
- R2 宽限期 `_THREEDS_CHALLENGE_GRACE_SEC`(30s):首见后先等自动放行
  (弹窗消失→复位;余额到账→success 优先判)。超期仍在 → 点 Cancel
  (`.LightboxModalClose`)关闭,返回 outcome "failed"(上层按曾成功与否冷却/判无效)。
- R3 充值(`detect_payment_result`)与订阅(`detect_subscribe_result`)两条判定循环都接入;
  首见即置 saw_3ds(禁止回头解 hCaptcha)并初始化 overlay 基线,与既有 3DS 语义一致。
- R4 既有信号(failure modal、拒付文案、新弹窗超基线、URL 版挑战检测)行为不变。

## Acceptance Criteria

- [x] 真实 DOM 冒烟(用户提供的弹窗 markup):检测命中、点 Cancel 关闭成功、
      关闭后不再命中;普通 LightboxModal(无 challengeFrame)不误报。
- [x] 两条流程(billing/subscribe)导入与语法无回归。
- [x] failed 文案含「3DS 挑战弹窗 …未自动通过」,可在 recharge_logs 追溯。
