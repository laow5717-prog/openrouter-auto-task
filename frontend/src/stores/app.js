import { defineStore } from 'pinia'
import { ref, onUnmounted } from 'vue'
import { getStatus } from '../api'

export const useAppStore = defineStore('app', () => {
  const isRunning = ref(false)
  const currentAction = ref('Idle')
  const successCount = ref(0)
  const failCount = ref(0)
  const totalInventory = ref(0)
  const logs = ref([])
  const logIndex = ref(0)

  let pollTimer = null

  async function poll() {
    try {
      const data = await getStatus(logIndex.value)
      isRunning.value = data.is_running
      currentAction.value = data.current_action
      successCount.value = data.success
      failCount.value = data.fail
      totalInventory.value = data.total_inventory

      if (data.logs && data.logs.length > 0) {
        logs.value.push(...data.logs)
        logIndex.value += data.logs.length
        if (logs.value.length > 2000) {
          logs.value = logs.value.slice(-1000)
        }
      }
    } catch (e) {
      console.error('Poll error:', e)
    }
  }

  function startPolling() {
    poll()
    pollTimer = setInterval(poll, 1000)
  }

  function stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer)
      pollTimer = null
    }
  }

  function clearLogs() {
    logs.value = []
    logIndex.value = 0
  }

  return {
    isRunning, currentAction, successCount, failCount, totalInventory,
    logs, logIndex,
    startPolling, stopPolling, clearLogs, poll,
  }
})
