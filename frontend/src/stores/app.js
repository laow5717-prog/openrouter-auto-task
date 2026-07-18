import { defineStore } from 'pinia'
import { ref } from 'vue'
import { getStatus, getWorkerLogs } from '../api'

export const useAppStore = defineStore('app', () => {
  const isRunning = ref(false)
  const currentAction = ref('Idle')
  const successCount = ref(0)
  const failCount = ref(0)
  const totalInventory = ref(0)
  const logs = ref([])
  const logIndex = ref(0)

  // 并发 worker。串行运行时只有 W1，前端布局退化为单栏。
  // 每个 worker: { id, currentAction, busy, logs, logIndex }
  const workers = ref([])
  const parallelMode = ref(false)

  let pollTimer = null

  function syncWorkers(list) {
    const incoming = list || []
    const known = new Map(workers.value.map((w) => [w.id, w]))
    // 只在 worker 集合真的变了时才换数组引用。轮询每秒一次，无脑重建会让
    // v-for 与依赖 workers 的 watch 每秒全量重算，日志上千行时 UI 明显卡顿。
    const sameSet =
      workers.value.length === incoming.length &&
      incoming.every((info, i) => workers.value[i].id === info.id)

    const next = incoming.map((info) => {
      const existing = known.get(info.id)
      if (existing) {
        existing.currentAction = info.current_action
        existing.busy = info.busy
        existing.serverSeq = info.log_seq
        return existing
      }
      return {
        id: info.id,
        currentAction: info.current_action,
        busy: info.busy,
        serverSeq: info.log_seq,
        logs: [],
        logIndex: 0,
      }
    })

    if (!sameSet) workers.value = next
  }

  async function pollWorkerLogs() {
    // 只拉有新日志的 worker，避免每秒 N 个空请求
    const stale = workers.value.filter((w) => w.serverSeq > w.logIndex)
    await Promise.all(
      stale.map(async (w) => {
        try {
          const data = await getWorkerLogs(w.id, w.logIndex)
          if (data.logs && data.logs.length > 0) {
            w.logs.push(...data.logs)
            if (w.logs.length > 1000) w.logs = w.logs.slice(-500)
          }
          w.logIndex = data.next_index
        } catch (e) {
          console.error('Worker log poll error:', w.id, e)
        }
      })
    )
  }

  async function poll() {
    try {
      const data = await getStatus(logIndex.value)
      isRunning.value = data.is_running
      currentAction.value = data.current_action
      successCount.value = data.success
      failCount.value = data.fail
      totalInventory.value = data.total_inventory
      parallelMode.value = !!data.parallel_mode
      syncWorkers(data.workers)

      if (data.logs && data.logs.length > 0) {
        logs.value.push(...data.logs)
        logIndex.value += data.logs.length
        if (logs.value.length > 2000) {
          logs.value = logs.value.slice(-1000)
        }
      }

      await pollWorkerLogs()
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
    workers.value.forEach((w) => {
      w.logs = []
    })
  }

  return {
    isRunning, currentAction, successCount, failCount, totalInventory,
    logs, logIndex, workers, parallelMode,
    startPolling, stopPolling, clearLogs, poll,
  }
})
