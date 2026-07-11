import { defineStore } from 'pinia'
import { ref } from 'vue'

export const useSettingsStore = defineStore('settings', () => {
  const cfPassword = ref(localStorage.getItem('cfPassword') || '')
  const captchaApiKey = ref(localStorage.getItem('captchaApiKey') || '')

  function save() {
    if (cfPassword.value) localStorage.setItem('cfPassword', cfPassword.value)
    if (captchaApiKey.value) localStorage.setItem('captchaApiKey', captchaApiKey.value)
  }

  return { cfPassword, captchaApiKey, save }
})
