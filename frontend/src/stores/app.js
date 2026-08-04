import { defineStore } from 'pinia'
import { ref } from 'vue'
import { getStatus, getWorkerLogs, getPlatforms, setPlatform } from '../api'

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

  // 当前平台。账号状态、卡的占用与冷却全部按它隔离，服务端对卡池类接口要求必填。
  // 这里只管展示与持久化，实际注入请求的是 api/index.js 的 setPlatform——
  // 让每个调用点自己记得传参数迟早会漏，漏一处就读到错平台的数据。
  const platforms = ref([])
  const platform = ref(localStorage.getItem('platform') || 'opencode')
  setPlatform(platform.value)

  async function loadPlatforms() {
    try {
      const data = await getPlatforms()
      platforms.value = data.data || []
      // 本地存的平台可能已被删掉（改了代码里的注册表），回落到服务端当前值
      if (!platforms.value.some((p) => p.slug === platform.value)) {
        switchPlatform(data.current || platforms.value[0]?.slug || 'opencode')
      }
    } catch (e) {
      console.error('平台列表加载失败:', e)
    }
  }

  // 各平台的运行状态概览（来自 /api/status 的 platforms 字段）。
  // 存在的意义是让用户看见**没在看的那个平台**——它出问题时否则完全不可见。
  const platformStates = ref({})
  // AdsPower 环境配额快照 { total, total_held, reserved, held, recall }
  const quota = ref(null)

  function switchPlatform(slug) {
    if (!slug || slug === platform.value) return
    platform.value = slug
    localStorage.setItem('platform', slug)
    setPlatform(slug)

    // 切平台必须把「属于上一个平台」的运行态清掉：日志、worker、计数全是
    // 那个平台的。不清的话新平台的日志会**追加**在旧平台的后面，两个平台的
    // 输出混成一片——而且因为 logIndex 还停在旧位置，新平台开头那段还会丢。
    logs.value = []
    logIndex.value = 0
    workers.value = []
    isRunning.value = false
    currentAction.value = 'Idle'
    successCount.value = 0
    failCount.value = 0
    poll()          // 立刻拉一次，别让界面空着等到下一个轮询周期
  }

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
      platformStates.value = data.platforms || {}
      quota.value = data.quota || null
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
    platforms, platform, loadPlatforms, switchPlatform, platformStates, quota,
    startPolling, stopPolling, clearLogs, poll,
  }
})
