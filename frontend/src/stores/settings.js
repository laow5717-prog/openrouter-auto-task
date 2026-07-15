import { defineStore } from 'pinia'
import { ref } from 'vue'

export const useSettingsStore = defineStore('settings', () => {
  const cfPassword = ref(localStorage.getItem('cfPassword') || '')
  const captchaApiKey = ref(localStorage.getItem('captchaApiKey') || '')
  const dailyBindGroupId = ref(localStorage.getItem('dailyBindGroupId') || '')
  const dailyPaymentGroupId = ref(localStorage.getItem('dailyPaymentGroupId') || '')
  const maxBindableCards = ref(Number(localStorage.getItem('maxBindableCards')) || 2)

  function save() {
    localStorage.setItem('cfPassword', cfPassword.value || '')
    localStorage.setItem('captchaApiKey', captchaApiKey.value || '')
    localStorage.setItem('dailyBindGroupId', dailyBindGroupId.value || '')
    localStorage.setItem('dailyPaymentGroupId', dailyPaymentGroupId.value || '')
    localStorage.setItem('maxBindableCards', String(maxBindableCards.value || 2))
  }

  return { cfPassword, captchaApiKey, dailyBindGroupId, dailyPaymentGroupId, maxBindableCards, save }
})
