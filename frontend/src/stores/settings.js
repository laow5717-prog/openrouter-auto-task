import { defineStore } from 'pinia'
import { ref } from 'vue'

export const useSettingsStore = defineStore('settings', () => {
  const loginPassword = ref(localStorage.getItem('loginPassword') || '')
  const captchaApiKey = ref(localStorage.getItem('captchaApiKey') || '')
  const dailyBindGroupId = ref(localStorage.getItem('dailyBindGroupId') || '')
  const dailyPaymentGroupId = ref(localStorage.getItem('dailyPaymentGroupId') || '')
  const maxBindableCards = ref(Number(localStorage.getItem('maxBindableCards')) || 2)
  // 每日任务运行模式：full=绑卡+充值 / bind_only=仅绑卡 / recharge_only=仅充值
  const dailyMode = ref(localStorage.getItem('dailyMode') || 'full')

  function save() {
    localStorage.setItem('dailyMode', dailyMode.value || 'full')
    localStorage.setItem('loginPassword', loginPassword.value || '')
    localStorage.setItem('captchaApiKey', captchaApiKey.value || '')
    localStorage.setItem('dailyBindGroupId', dailyBindGroupId.value || '')
    localStorage.setItem('dailyPaymentGroupId', dailyPaymentGroupId.value || '')
    localStorage.setItem('maxBindableCards', String(maxBindableCards.value || 2))
  }

  return { loginPassword, captchaApiKey, dailyBindGroupId, dailyPaymentGroupId, maxBindableCards, dailyMode, save }
})
