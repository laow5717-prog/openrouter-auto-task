<template>
  <!-- 说明 -->
  <div class="info-banner">
    <Icon name="bolt" size="16" />
    <span>
      <strong>每日一键流水线：</strong>
      按「补绑已有账号 → 注册新号 → 批量充值」串行执行。绑卡分组的可用卡先用于给未绑满的老账号补绑，
      剩余卡注册新号绑定；随后对当日未充值且已绑卡的账号统一 Top-up。
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
          <label class="setting-label">绑卡分组（必选）</label>
          <select v-model="settings.dailyBindGroupId" class="ctrl-input" :disabled="appStore.isRunning">
            <option value="">选择绑定卡分组...</option>
            <option v-for="g in bindGroups" :key="g.id" :value="g.id">{{ g.name }} ({{ g.card_count }}张)</option>
          </select>
        </div>
        <div class="setting-item">
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
        <div class="setting-item">
          <label class="setting-label">每账号最多绑卡数</label>
          <input type="number" min="1" max="5" v-model.number="settings.maxBindableCards"
                 class="ctrl-input" :disabled="appStore.isRunning">
        </div>
        <div class="setting-item">
          <label class="setting-label">2Captcha API Key</label>
          <input type="text" v-model="settings.captchaApiKey" class="ctrl-input"
                 placeholder="用于自动解决人机验证" :disabled="appStore.isRunning">
        </div>
      </div>

      <div style="margin-top:16px;display:flex;gap:10px;align-items:center">
        <button v-if="!appStore.isRunning" class="btn btn-primary" style="width:auto;padding:8px 24px"
                :disabled="!settings.dailyBindGroupId" @click="handleStart">
          <Icon name="play" size="15" /> 开始运行
        </button>
        <button v-else class="btn btn-danger" style="width:auto;padding:8px 24px" @click="handleStop">
          <Icon name="stop" size="15" /> 停止
        </button>
      </div>
    </div>
  </div>

  <!-- 实时监控 -->
  <div class="split-view">
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
</template>

<script setup>
import { ref, watch, nextTick, onMounted } from 'vue'
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

watch(() => appStore.logs.length, () => {
  nextTick(() => {
    if (logContainer.value) logContainer.value.scrollTop = logContainer.value.scrollHeight
  })
})

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
  if (!settings.dailyBindGroupId) { alert('请选择绑卡分组'); return }
  appStore.clearLogs()
  settings.save()

  const body = {
    bind_group_id: settings.dailyBindGroupId,
    max_bindable_cards: settings.maxBindableCards || 2,
  }
  if (settings.dailyPaymentGroupId) body.payment_group_id = settings.dailyPaymentGroupId
  if (settings.cfPassword) body.cf_password = settings.cfPassword
  if (settings.captchaApiKey) body.captcha_api_key = settings.captchaApiKey

  try {
    const result = await startDailyPipeline(body)
    appStore.poll()
    alert(`已启动每日流水线（分组「${result.group_name}」可用卡 ${result.usable_cards} 张）`)
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
</style>
