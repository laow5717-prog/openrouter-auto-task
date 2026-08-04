<template>
  <!-- 说明 -->
  <div class="info-banner">
    <Icon name="bolt" size="16" />
    <span>
      <strong>每日任务：</strong>
      <strong>「开始充值」</strong>逐账号轮转充值（充成一张即换下一个账号，新卡优先、好卡可复用）；可充账号耗尽时，会自动拿列表里「已导入」的邮箱注册 GitHub，注册成功的账号下一轮直接进入登录充值——所以账号列表里只有新导入的邮箱也能直接开跑。
      <strong>「开始订阅」</strong>逐账号轮转 Subscribe to Go：未注册的先注册（碰 Arkose 自动跳过）、已注册的登录后逐卡试付，订阅成功即换下一个账号，直到无可选卡或无待订阅账号。两者互斥、共用卡池分组与 2Captcha Key。
    </span>
  </div>

  <!-- 任务配置 -->
  <div class="panel" style="margin-bottom:16px">
    <div class="panel-header">
      <div class="panel-title"><Icon name="bolt" size="18" /> 跑今日任务</div>
      <span class="status-pill" :class="appStore.isRunning ? 'success' : 'neutral'">
        {{ appStore.isRunning ? '运行中' : '空闲' }}
      </span>
    </div>

    <div style="padding:16px">
      <div class="settings-row">
        <div class="setting-item">
          <label class="setting-label">卡池分组（必选）</label>
          <select v-model="settings.dailyGroupId" class="ctrl-input" :disabled="appStore.isRunning">
            <option value="">选择卡池分组...</option>
            <option v-for="g in groups" :key="g.id" :value="g.id">{{ g.name }} ({{ g.card_count }}张)</option>
          </select>
        </div>
      </div>

      <div class="settings-row">
        <div class="setting-item">
          <label class="setting-label">登录密码（可选，覆盖账号自身密码）</label>
          <input type="text" v-model="settings.loginPassword" class="ctrl-input"
                 placeholder="留空则用各账号已保存的密码" :disabled="appStore.isRunning">
        </div>
        <div class="setting-item">
          <label class="setting-label">2Captcha API Key（可选）</label>
          <input type="text" v-model="settings.captchaApiKey" class="ctrl-input"
                 placeholder="用于自动解决人机验证" :disabled="appStore.isRunning">
        </div>
      </div>

      <div class="settings-row">
        <div class="setting-item">
          <label class="setting-label">单笔充值金额（美元，区间内随机）</label>
          <div class="range-row">
            <input type="number" v-model.number="settings.amountMin" class="ctrl-input"
                   min="1" max="1000" :disabled="appStore.isRunning">
            <span class="range-sep">–</span>
            <input type="number" v-model.number="settings.amountMax" class="ctrl-input"
                   min="1" max="1000" :disabled="appStore.isRunning">
          </div>
        </div>
        <div class="setting-item">
          <label class="setting-label">单账号余额上限（充到此额换下一个账号）</label>
          <input type="number" v-model.number="settings.balanceCap" class="ctrl-input"
                 min="1" :disabled="appStore.isRunning">
        </div>
      </div>

      <div style="margin-top:16px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        <template v-if="!appStore.isRunning">
          <button class="btn btn-primary" style="width:auto;padding:8px 24px"
                  :disabled="!settings.dailyGroupId" @click="handleStart">
            <Icon name="play" size="15" /> 开始充值
          </button>
          <button class="btn btn-primary" style="width:auto;padding:8px 24px"
                  :disabled="!settings.dailyGroupId" @click="handleStartSubscribe">
            <Icon name="bolt" size="15" /> 开始订阅
          </button>
        </template>
        <button v-else class="btn btn-danger" style="width:auto;padding:8px 24px" @click="handleStop">
          <Icon name="stop" size="15" /> 停止
        </button>
      </div>
    </div>
  </div>

  <!-- 实时监控：单 worker 时为原来的「画面 + 日志」双栏；
       多 worker 时每个 worker 一栏，各自画面与日志上下排列 -->
  <div v-if="!isParallel" class="split-view">
    <div class="panel">
      <div class="panel-header">
        <div class="panel-title"><Icon name="monitor" size="18" /> 实时画面</div>
        <span class="status-pill" :class="appStore.isRunning ? 'success' : 'neutral'">
          {{ appStore.isRunning ? 'LIVE' : 'OFFLINE' }}
        </span>
      </div>
      <div class="monitor-body">
        <img v-if="appStore.isRunning" class="monitor-img" :src="videoFeedUrl" alt="Monitor">
        <div v-else style="color:#666;font-size:12px">等待信号...</div>
      </div>
    </div>

    <div class="panel">
      <div class="panel-header">
        <div class="panel-title"><Icon name="terminal" size="18" /> 终端日志</div>
        <button class="action-btn" @click="appStore.clearLogs()">清空</button>
      </div>
      <div class="log-body" ref="logContainer">
        <div v-if="appStore.logs.length === 0" class="log-placeholder">> 准备就绪...</div>
        <div v-for="(log, i) in appStore.logs" :key="i" class="log-entry">{{ log }}</div>
      </div>
    </div>
  </div>

  <div v-else class="worker-grid" :style="{ gridTemplateColumns: `repeat(${appStore.workers.length}, minmax(0, 1fr))` }">
    <div v-for="w in appStore.workers" :key="w.id" class="panel worker-panel">
      <div class="panel-header">
        <div class="panel-title">
          <Icon name="monitor" size="16" /> {{ w.id }}
        </div>
        <span class="status-pill" :class="w.busy ? 'success' : 'neutral'">
          {{ w.busy ? 'LIVE' : 'IDLE' }}
        </span>
      </div>
      <div class="worker-action" :title="w.currentAction">{{ w.currentAction }}</div>
      <div class="monitor-body worker-monitor">
        <img v-if="w.busy" class="monitor-img" :src="`/video_feed?platform=${store.platform}&worker=${w.id}`" :alt="w.id">
        <div v-else style="color:#666;font-size:12px">等待信号...</div>
      </div>
      <div class="log-body worker-log">
        <div v-if="w.logs.length === 0" class="log-placeholder">> 准备就绪...</div>
        <div v-for="(log, i) in w.logs" :key="i" class="log-entry">{{ log }}</div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, nextTick, onMounted } from 'vue'
import { useAppStore } from '../stores/app'
import { useSettingsStore } from '../stores/settings'
import { getCardGroups, startDailyPipeline, startDailySubscribe, stopTask } from '../api'
import Icon from '../components/Icon.vue'

const appStore = useAppStore()
const settings = useSettingsStore()

const groups = ref([])
const logContainer = ref(null)
// 同样要带 platform，理由见 Dashboard.vue 里的注释
const videoFeedUrl = computed(() => `/video_feed?platform=${store.platform}`)

// 只有真正多开 worker 时才切分栏布局，串行时保持原有的双栏视觉
const isParallel = computed(() => appStore.workers.length > 1)

watch(() => appStore.logs.length, () => {
  nextTick(() => {
    if (logContainer.value) logContainer.value.scrollTop = logContainer.value.scrollHeight
  })
})

// 分栏模式下各 worker 日志区各自滚到底
watch(
  () => appStore.workers.map((w) => w.logs.length).join(','),
  () => {
    nextTick(() => {
      document.querySelectorAll('.worker-log').forEach((el) => {
        el.scrollTop = el.scrollHeight
      })
    })
  }
)

async function loadGroups() {
  try {
    groups.value = await getCardGroups()
    // 分组唯一项时默认选中
    if (!settings.dailyGroupId && groups.value.length === 1) {
      settings.dailyGroupId = groups.value[0].id
    }
  } catch (e) { console.error(e) }
}

// 前端先挡一道明显的笔误，省一次往返。真正的权威校验在后端 _recharge_cfg_from，
// 两边口径要一致（1–1000、min ≤ max、余额上限为正）。
function validateRechargePolicy() {
  const { amountMin: lo, amountMax: hi, balanceCap: cap } = settings
  if (!Number.isFinite(lo) || !Number.isFinite(hi) || lo < 1 || hi > 1000) {
    return '单笔充值金额需在 $1–$1000 之间'
  }
  if (lo > hi) return `充值金额区间非法：下界 $${lo} 大于上界 $${hi}`
  if (!Number.isFinite(cap) || cap <= 0) return '单账号余额上限必须大于 0'
  return ''
}

async function handleStart() {
  if (appStore.isRunning) { alert('任务已在运行中'); return }
  if (!settings.dailyGroupId) { alert('请选择卡池分组'); return }
  const policyErr = validateRechargePolicy()
  if (policyErr) { alert(policyErr); return }
  appStore.clearLogs()
  settings.save()

  const body = {
    group_id: settings.dailyGroupId,
    amount_min: settings.amountMin,
    amount_max: settings.amountMax,
    balance_cap: settings.balanceCap,
  }
  if (settings.loginPassword) body.login_password = settings.loginPassword
  if (settings.captchaApiKey) body.captcha_api_key = settings.captchaApiKey

  try {
    const result = await startDailyPipeline(body)
    appStore.poll()
    // 金额区间取后端回显的值，而不是本地输入框——两者不一致时用户应当看见的是
    // 实际生效的那一套。
    const amount = `每笔 $${result.amount_min}–$${result.amount_max} · 余额上限 $${result.balance_cap}`
    // 待注册账号单独列出来。只报 accounts 的话，「刚导入一批新邮箱」这个场景会
    // 显示「可用账号 0 个」——任务其实靠补号跑得好好的，用户却以为启动歪了。
    const registerable = result.registerable_accounts
      ? ` · 待注册补号 ${result.registerable_accounts} 个`
      : ''
    alert(result.group_name
      ? `已启动每日充值任务（分组「${result.group_name}」未消耗卡 ${result.usable_cards} 张 · 可用账号 ${result.accounts} 个${registerable} · ${amount}）`
      : '已启动每日充值任务')
  } catch (e) {
    alert('启动失败: ' + e.message)
  }
}

async function handleStartSubscribe() {
  if (appStore.isRunning) { alert('任务已在运行中'); return }
  if (!settings.dailyGroupId) { alert('请选择卡池分组'); return }
  appStore.clearLogs()
  settings.save()

  const body = { group_id: settings.dailyGroupId }
  if (settings.captchaApiKey) body.captcha_api_key = settings.captchaApiKey

  try {
    const result = await startDailySubscribe(body)
    appStore.poll()
    alert(result.group_name
      ? `已启动每日订阅任务（分组「${result.group_name}」可选卡 ${result.usable_cards} 张 · 待订阅账号 ${result.accounts} 个）`
      : '已启动每日订阅任务')
  } catch (e) {
    alert('启动失败: ' + e.message)
  }
}

async function handleStop() {
  if (!confirm('确定要停止当前任务吗？将在下一个安全检查点退出。')) return
  try { await stopTask() } catch (e) { console.error(e) }
}

onMounted(loadGroups)
</script>

<style scoped>
.info-banner {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 12px 16px;
  margin-bottom: 16px;
  background: #fff7ed;
  border: 1px solid #fed7aa;
  border-radius: 8px;
  font-size: 13px;
  color: #9a3412;
  line-height: 1.6;
}
.info-banner .icon { flex-shrink: 0; margin-top: 2px; }

.status-pill {
  padding: 3px 10px;
  border-radius: 12px;
  font-size: 11px;
  font-weight: 600;
}
.status-pill.success { background: #dcfce7; color: #166534; }
.status-pill.neutral { background: #f3f4f6; color: #6b7280; }

.settings-row {
  display: flex;
  gap: 16px;
  margin-top: 14px;
  flex-wrap: wrap;
}
.setting-item { flex: 1; min-width: 200px; }
.setting-label {
  display: block;
  font-size: 12px;
  font-weight: 500;
  margin-bottom: 4px;
  color: #555;
}
.range-row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.range-row .ctrl-input { flex: 1; min-width: 0; }
.range-sep { color: #9ca3af; flex-shrink: 0; }

.btn-primary .icon, .btn-danger .icon { display: inline-block; vertical-align: -2px; margin-right: 4px; }

.split-view {
  display: grid;
  grid-template-columns: 1.2fr 1fr;
  gap: 24px;
  height: 460px;
}
.monitor-body {
  flex: 1;
  background: #000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 10px;
}
.monitor-img {
  max-width: 100%;
  max-height: 100%;
  border-radius: 4px;
  object-fit: contain;
}
.log-body {
  flex: 1;
  background: var(--terminal-bg);
  padding: 16px;
  overflow-y: auto;
  font-family: 'JetBrains Mono', monospace;
  font-size: 13px;
  line-height: 1.6;
  color: #cbd5e1;
}
.log-entry {
  margin-bottom: 2px;
  word-break: break-all;
  border-bottom: 1px solid rgba(255,255,255,0.05);
  padding: 2px 0;
}
.log-placeholder { color: #666; text-align: center; margin-top: 20px; }

/* 并发分栏：列数由 worker 数驱动（内联 style），每栏内部画面在上、日志在下 */
.worker-grid {
  display: grid;
  gap: 16px;
  height: 460px;
}
.worker-panel {
  display: flex;
  flex-direction: column;
  min-width: 0;          /* 允许栏位收窄，避免长日志把网格撑破 */
}
.worker-action {
  padding: 6px 12px;
  font-size: 11px;
  color: #555;
  background: #f9fafb;
  border-bottom: 1px solid #e5e7eb;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.worker-monitor {
  flex: 0 0 45%;
  min-height: 0;
}
.worker-log {
  flex: 1;
  min-height: 0;
  font-size: 11px;
  padding: 10px;
}
</style>
