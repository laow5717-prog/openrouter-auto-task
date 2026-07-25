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

  function save() {
    localStorage.setItem('loginPassword', loginPassword.value || '')
    localStorage.setItem('captchaApiKey', captchaApiKey.value || '')
    localStorage.setItem('dailyGroupId', dailyGroupId.value || '')
  }

  return { loginPassword, captchaApiKey, dailyGroupId, save }
})
