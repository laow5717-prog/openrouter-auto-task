import { defineStore } from 'pinia'
import { ref } from 'vue'

export const useSettingsStore = defineStore('settings', () => {
  const loginPassword = ref(localStorage.getItem('loginPassword') || '')
  const captchaApiKey = ref(localStorage.getItem('captchaApiKey') || '')
  // 每日充值任务选定的卡池分组（兼容旧键 dailyPaymentGroupId 的历史值）
  const dailyGroupId = ref(
    localStorage.getItem('dailyGroupId') ||
    localStorage.getItem('dailyPaymentGroupId') || ''
  )

  // 充值策略。每笔充值在 [amountMin, amountMax] 内随机取整数美元；一个账号连续充到
  // balanceCap 就换下一个账号。后端 config.yaml 的 recharge: 段是缺省值，这里是按次覆盖。
  const num = (key, fallback) => {
    const v = Number(localStorage.getItem(key))
    return Number.isFinite(v) && v > 0 ? v : fallback
  }
  const amountMin = ref(num('amountMin', 20))
  const amountMax = ref(num('amountMax', 100))
  const balanceCap = ref(num('balanceCap', 200))

  function save() {
    localStorage.setItem('loginPassword', loginPassword.value || '')
    localStorage.setItem('captchaApiKey', captchaApiKey.value || '')
    localStorage.setItem('dailyGroupId', dailyGroupId.value || '')
    localStorage.setItem('amountMin', String(amountMin.value ?? 20))
    localStorage.setItem('amountMax', String(amountMax.value ?? 100))
    localStorage.setItem('balanceCap', String(balanceCap.value ?? 200))
  }

  return {
    loginPassword, captchaApiKey, dailyGroupId,
    amountMin, amountMax, balanceCap,
    save,
  }
})
