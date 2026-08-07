<template>
  <aside class="sidebar">
    <div class="brand">
      <div class="brand-icon">CF</div>
      <div class="brand-text">CF Auto</div>
    </div>

    <nav class="nav-menu">
      <router-link to="/" class="nav-item" active-class="active" exact-active-class="active">
        <Icon name="bolt" /> 每日任务
      </router-link>
      <router-link to="/monitor" class="nav-item" active-class="active">
        <Icon name="dashboard" /> 运行监控
      </router-link>
      <router-link to="/card-pool" class="nav-item" active-class="active">
        <Icon name="cards" /> 卡片管理
      </router-link>
      <router-link to="/accounts" class="nav-item" active-class="active">
        <Icon name="accounts" /> 账号管理
      </router-link>
      <router-link to="/card-history" class="nav-item" active-class="active">
        <Icon name="history" /> 绑卡记录
      </router-link>
      <router-link to="/recharge-logs" class="nav-item" active-class="active">
        <Icon name="wallet" /> 充值记录
      </router-link>
      <router-link to="/proxies" class="nav-item" active-class="active">
        <Icon name="monitor" /> 代理管理
      </router-link>
      <router-link to="/settings" class="nav-item" active-class="active">
        <Icon name="monitor" /> 系统设置
      </router-link>
    </nav>

  </aside>

  <main class="main-view">
    <header class="page-header">
      <h2 class="page-title">{{ pageTitle }}</h2>
      <div class="header-right">
        <!-- 平台切换：账号状态与卡的占用全部按平台隔离，切了之后各列表看到的是
             那个平台的视角。**运行中也能切**——两个平台可以同时跑，这个下拉框
             表示的是「当前在看哪个」，不是「当前在跑哪个」。 -->
        <label class="platform-picker" title="切换查看的平台（两个平台可同时运行）">
          <span class="platform-label">平台</span>
          <select
            class="platform-select"
            :value="store.platform"
            @change="store.switchPlatform($event.target.value)"
          >
            <option v-for="p in store.platforms" :key="p.slug" :value="p.slug">
              {{ p.display_name }}{{ isPlatformRunning(p.slug) ? ' ●' : '' }}
            </option>
          </select>
        </label>
        <!-- 别的平台在跑时必须可见：否则它出问题了用户完全看不到。 -->
        <button
          v-for="slug in otherRunning"
          :key="slug"
          class="other-running"
          :title="`${displayName(slug)} 正在运行，点击切过去查看`"
          @click="store.switchPlatform(slug)"
        >
          <span class="status-dot running"></span>
          {{ displayName(slug) }} 运行中
        </button>
        <div class="status-badge" :title="quotaTitle">
          <span class="status-dot" :class="{ running: store.isRunning }"></span>
          <span>{{ store.isRunning ? '运行中' : '系统空闲' }}</span>
          <span v-if="quotaText" class="quota-chip">{{ quotaText }}</span>
        </div>
        <!-- 全局停止。任务跑起来之后用户会在各页面之间来回切，而停止按钮只存在于
             每日任务页和侧边栏里——真要中断时得先找回那一页。这里挂一个常驻入口，
             并且停的是**所有在跑的平台**，不只当前在看的那个。 -->
        <button
          v-if="runningPlatforms.length"
          class="global-stop"
          :disabled="stopping"
          :title="stopTitle"
          @click="handleGlobalStop"
        >
          <Icon name="stop" size="13" />
          {{ stopping ? '停止中…' : stopLabel }}
        </button>
      </div>
    </header>

    <router-view />
  </main>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useAppStore } from './stores/app'
import { stopTask } from './api'
import Icon from './components/Icon.vue'
const store = useAppStore()
const route = useRoute()

function isPlatformRunning(slug) {
  return !!store.platformStates?.[slug]?.is_running
}

function displayName(slug) {
  return store.platforms.find((p) => p.slug === slug)?.display_name || slug
}

// 正在跑、但**不是当前正在看**的平台。这是并发下最容易被忽略的一块：
// 用户只盯着一个面板，另一个平台卡住或报错时毫无提示。
const otherRunning = computed(() =>
  Object.entries(store.platformStates || {})
    .filter(([slug, st]) => st.is_running && slug !== store.platform)
    .map(([slug]) => slug)
)

// 正在跑的**全部**平台。全局停止针对的是它们，而不是 store.platform——
// 用户切到卡片管理页看了半天之后，脑子里的「当前平台」和 store 里的未必是同一个，
// 让一个常驻的红色按钮去停一个看不见的目标，比没有这个按钮更糟。
const runningPlatforms = computed(() =>
  Object.entries(store.platformStates || {})
    .filter(([, st]) => st.is_running)
    .map(([slug]) => slug)
)

const stopLabel = computed(() =>
  runningPlatforms.value.length > 1 ? `停止全部 (${runningPlatforms.value.length})` : '停止'
)

const stopTitle = computed(() =>
  `停止 ${runningPlatforms.value.map(displayName).join('、')} 的任务`
)

const stopping = ref(false)

async function handleGlobalStop() {
  const slugs = runningPlatforms.value.slice()
  if (!slugs.length) return

  const names = slugs.map(displayName)
  const msg = slugs.length > 1
    ? `确定要停止全部 ${slugs.length} 个平台的任务吗？\n\n${names.map((n) => `· ${n}`).join('\n')}\n\n将在下一个安全检查点退出。`
    : `确定要停止「${names[0]}」的任务吗？将在下一个安全检查点退出。`
  if (!confirm(msg)) return

  stopping.value = true
  // /api/stop 一次只停一个 ctx，多平台就逐个发。用 allSettled 而不是 all：
  // 某个平台可能刚好自己跑完了（后端对没在跑的平台返回 400），那一个 reject
  // 不该让另一个平台的停止请求一起丢掉。
  const results = await Promise.allSettled(slugs.map((slug) => stopTask(slug)))
  results.forEach((r, i) => {
    if (r.status === 'rejected') console.error('停止失败:', slugs[i], r.reason)
  })
  store.poll()
}

// 全都停下来之后复位。不复位的话，下一次任务启动时按钮一冒出来就是禁用态。
watch(runningPlatforms, (list) => {
  if (!list.length) stopping.value = false
})

// AdsPower 环境配额。两个平台抢同一批环境，占用情况值得一直挂在眼前。
const quotaText = computed(() => {
  const q = store.quota
  if (!q || !q.total) return ''
  return `环境 ${q.total_held}/${q.total}`
})

const quotaTitle = computed(() => {
  const q = store.quota
  if (!q || !q.total) return ''
  const per = Object.entries(q.held || {})
    .map(([slug, n]) => `${displayName(slug)} ${n}/${q.reserved?.[slug] ?? '-'}`)
    .join('，')
  return per ? `AdsPower 环境占用：${per}（总 ${q.total_held}/${q.total}）`
             : `AdsPower 环境占用 ${q.total_held}/${q.total}`
})

const titleMap = { workbench: '每日任务', monitor: '运行监控', cardPool: '卡片管理', accounts: '账号管理', cardHistory: '绑卡记录', rechargeLogs: '充值记录' }
const pageTitle = computed(() => titleMap[route.name] || '系统概览')

onMounted(() => {
  store.loadPlatforms()
  store.startPolling()
})
onUnmounted(() => store.stopPolling())
</script>

<style scoped>
.sidebar {
  width: 300px;
  background: var(--bg-card);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  padding: 24px;
  z-index: 10;
  overflow-y: auto;
}
.brand {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 32px;
}
.brand-icon {
  width: 32px; height: 32px;
  background: var(--primary);
  color: white;
  border-radius: 8px;
  display: flex; align-items: center; justify-content: center;
  font-weight: bold; font-size: 14px;
}
.brand-text { font-size: 20px; font-weight: 700; letter-spacing: -0.5px; }

.nav-menu {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 24px;
}
.nav-item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 16px;
  border-radius: var(--radius);
  color: var(--text-sub);
  cursor: pointer;
  transition: all 0.2s;
  font-weight: 500;
  font-size: 14px;
  text-decoration: none;
}
.nav-item:hover { background: #f9fafb; color: var(--text-main); }
.nav-item.active { background: #fff7ed; color: var(--primary); }

.main-view { flex: 1; overflow-y: auto; padding: 32px 40px; }

.page-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 32px;
}
.page-title { font-size: 24px; font-weight: 700; }
.header-right {
  display: flex;
  align-items: center;
  gap: 10px;
}
.platform-picker {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 5px 10px 5px 12px;
  background: white;
  border: 1px solid var(--border);
  border-radius: 20px;
  font-size: 13px;
}
.platform-label { color: var(--text-sub); font-weight: 500; }
.platform-select {
  border: none;
  background: transparent;
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
  cursor: pointer;
  outline: none;
}
.platform-select:disabled { cursor: not-allowed; opacity: 0.55; }

/* 另一个平台在跑时的提示。做成按钮是因为它同时是「切过去看」的入口——
   看见异常却要再去下拉框里找一次，多一步就少一次点击。 */
.other-running {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border: 1px solid var(--border, #d0d5dd);
  border-radius: 999px;
  background: transparent;
  color: var(--text-sub, #667085);
  font-size: 12px;
  cursor: pointer;
}
.other-running:hover { background: rgba(0, 0, 0, 0.04); }

.quota-chip {
  margin-left: 8px;
  padding: 1px 7px;
  border-radius: 999px;
  background: rgba(0, 0, 0, 0.06);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}
.status-badge {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  background: white;
  border: 1px solid var(--border);
  border-radius: 20px;
  font-size: 13px;
  font-weight: 500;
}
.status-dot {
  width: 8px; height: 8px;
  border-radius: 50%;
  background: #d1d5db;
}
.status-dot.running {
  background: var(--success);
  box-shadow: 0 0 0 2px rgba(16, 185, 129, 0.2);
  animation: pulse 2s infinite;
}

/* 实心红而不是页面里那个浅底的 .btn-danger：顶栏这一排全是胶囊状的
   状态展示件，停止是这里唯一一个有破坏性的动作，得让它一眼区分于旁边的信息。 */
.global-stop {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 14px;
  border: 1px solid var(--danger);
  border-radius: 20px;
  background: var(--danger);
  color: #fff;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
}
.global-stop:hover:not(:disabled) { filter: brightness(0.92); }
.global-stop:disabled { opacity: 0.6; cursor: not-allowed; }
</style>
