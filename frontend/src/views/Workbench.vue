<template>
  <!-- 说明 -->
  <div class="info-banner">
    <Icon name="bolt" size="16" />
    <span>
      <strong>每日一键流水线：</strong>
      {{ modeHint }}
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
          <label class="setting-label">运行模式</label>
          <div class="mode-tabs">
            <button v-for="m in MODES" :key="m.value" type="button"
                    class="mode-tab" :class="{ active: settings.dailyMode === m.value }"
                    :disabled="appStore.isRunning" :title="m.hint"
                    @click="settings.dailyMode = m.value">
              {{ m.label }}
            </button>
          </div>
        </div>
      </div>

      <div class="settings-row">
        <div class="setting-item" v-if="needsBindGroup">
          <label class="setting-label">绑卡分组（必选）</label>
          <select v-model="settings.dailyBindGroupId" class="ctrl-input" :disabled="appStore.isRunning">
            <option value="">选择绑定卡分组...</option>
            <option v-for="g in bindGroups" :key="g.id" :value="g.id">{{ g.name }} ({{ g.card_count }}张)</option>
          </select>
        </div>
        <div class="setting-item" v-if="needsRecharge">
          <label class="setting-label">支付卡分组（可选，用于处理欠费发票）</label>
          <select v-model="settings.dailyPaymentGroupId" class="ctrl-input" :disabled="appStore.isRunning">
            <option value="">不使用（仅 Top-up Credits）</option>
            <option v-for="g in paymentGroups" :key="g.id" :value="g.id">{{ g.name }} ({{ g.card_count }}张)</option>
          </select>
        </div>
      </div>

      <div class="settings-row">
        <div class="setting-item">
          <label class="setting-label">Cloudflare 统一密码</label>
          <input type="text" v-model="settings.cfPassword" class="ctrl-input"
                 placeholder="留空则新号随机生成" :disabled="appStore.isRunning">
        </div>
        <div class="setting-item" v-if="needsBindGroup">
          <label class="setting-label">每账号最多绑卡数</label>
          <input type="number" min="1" max="5" v-model.number="settings.maxBindableCards"
                 class="ctrl-input" :disabled="appStore.isRunning">
        </div>
        <div class="setting-item" v-if="needsBindGroup">
          <label class="setting-label">2Captcha API Key</label>
          <input type="text" v-model="settings.captchaApiKey" class="ctrl-input"
                 placeholder="用于自动解决人机验证" :disabled="appStore.isRunning">
        </div>
      </div>

      <div style="margin-top:16px;display:flex;gap:10px;align-items:center">
        <button v-if="!appStore.isRunning" class="btn btn-primary" style="width:auto;padding:8px 24px"
                :disabled="needsBindGroup && !settings.dailyBindGroupId" @click="handleStart">
          <Icon name="play" size="15" /> 开始运行
        </button>
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
        <img v-if="w.busy" class="monitor-img" :src="`/video_feed?worker=${w.id}`" :alt="w.id">
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
import { getCardGroups, startDailyPipeline, stopTask } from '../api'
import Icon from '../components/Icon.vue'

const appStore = useAppStore()
const settings = useSettingsStore()

const bindGroups = ref([])
const paymentGroups = ref([])
const logContainer = ref(null)
const videoFeedUrl = '/video_feed'

// 与后端 /api/daily/start 的 mode 取值一一对应
const MODES = [
  { value: 'full', label: '绑卡 + 充值',
    hint: '按「补绑已有账号 → 注册新号 → 批量充值」串行执行。绑卡分组的可用卡先用于给未绑满的老账号补绑，剩余卡注册新号绑定；随后对已绑卡的账号统一充值。' },
  { value: 'bind_only', label: '仅绑卡',
    hint: '只跑补绑与注册新号两段，跑完即结束，不执行任何充值。适合先囤号、之后再统一充。' },
  { value: 'recharge_only', label: '仅充值',
    hint: '跳过卡池准备与全部绑卡动作，直接对已有账号执行充值。候选口径：已设置 Cloudflare 密码且已成功绑卡 ≥1 张的账号。' },
]

const needsBindGroup = computed(() => settings.dailyMode !== 'recharge_only')
const needsRecharge = computed(() => settings.dailyMode !== 'bind_only')
const modeHint = computed(
  () => MODES.find((m) => m.value === settings.dailyMode)?.hint || ''
)

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
    bindGroups.value = await getCardGroups({ type: 'bind' })
    paymentGroups.value = await getCardGroups({ type: 'payment' })
    // 分组唯一项时默认选中
    if (!settings.dailyBindGroupId && bindGroups.value.length === 1) {
      settings.dailyBindGroupId = bindGroups.value[0].id
    }
    if (!settings.dailyPaymentGroupId && paymentGroups.value.length === 1) {
      settings.dailyPaymentGroupId = paymentGroups.value[0].id
    }
  } catch (e) { console.error(e) }
}

async function handleStart() {
  if (appStore.isRunning) { alert('任务已在运行中'); return }
  if (needsBindGroup.value && !settings.dailyBindGroupId) { alert('请选择绑卡分组'); return }
  appStore.clearLogs()
  settings.save()

  const body = { mode: settings.dailyMode }
  if (needsBindGroup.value) {
    body.bind_group_id = settings.dailyBindGroupId
    body.max_bindable_cards = settings.maxBindableCards || 2
    if (settings.captchaApiKey) body.captcha_api_key = settings.captchaApiKey
  }
  if (needsRecharge.value && settings.dailyPaymentGroupId) {
    body.payment_group_id = settings.dailyPaymentGroupId
  }
  if (settings.cfPassword) body.cf_password = settings.cfPassword

  try {
    const result = await startDailyPipeline(body)
    appStore.poll()
    const label = MODES.find((m) => m.value === settings.dailyMode)?.label || settings.dailyMode
    alert(result.group_name
      ? `已启动每日流水线 · ${label}（分组「${result.group_name}」可用卡 ${result.usable_cards} 张）`
      : `已启动每日流水线 · ${label}`)
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

.mode-tabs {
  display: inline-flex;
  border: 1px solid #d1d5db;
  border-radius: 8px;
  overflow: hidden;
}
.mode-tab {
  padding: 7px 18px;
  font-size: 13px;
  border: none;
  border-left: 1px solid #e5e7eb;
  background: #fff;
  color: #4b5563;
  cursor: pointer;
}
.mode-tab:first-child { border-left: none; }
.mode-tab:hover:not(:disabled) { background: #f3f4f6; }
.mode-tab.active { background: #2563eb; color: #fff; font-weight: 600; }
.mode-tab:disabled { cursor: not-allowed; opacity: .6; }

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
