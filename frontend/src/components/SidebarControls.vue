<template>
  <div class="control-box">
    <div class="control-header">任务控制</div>

    <div class="ctrl-row">
      <label class="ctrl-label">注册数量</label>
      <input type="number" v-model.number="targetCount" class="ctrl-input" min="1" max="50">
    </div>

    <div class="ctrl-row">
      <label class="ctrl-label">Cloudflare 统一密码</label>
      <input type="text" v-model="settings.cfPassword" class="ctrl-input" placeholder="留空则每个账号随机生成">
      <div class="hint">设置后所有账号使用同一密码</div>
    </div>

    <div class="ctrl-row">
      <label class="ctrl-label">2Captcha API Key</label>
      <input type="text" v-model="settings.captchaApiKey" class="ctrl-input" placeholder="用于自动解决人机验证">
      <div class="hint">遇到 Turnstile 验证时自动调用</div>
    </div>

    <div class="ctrl-row">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
        <label class="ctrl-label" style="margin-bottom:0">信用卡列表</label>
        <button class="action-btn" @click="addCard" style="font-size:12px;">+ 添加</button>
      </div>
      <div class="hint" style="margin-bottom:8px">每个账号最多绑定 2 张，每张卡需填写独立账单地址</div>
      <CardEntry
        v-for="(card, idx) in cards"
        :key="card.id"
        :index="idx"
        :card="card"
        @remove="removeCard(idx)"
      />
    </div>

    <button v-if="!appStore.isRunning" class="btn btn-primary" style="width:100%;margin-top:8px" @click="handleStart">
      启动任务
    </button>
    <button v-else class="btn btn-danger" style="width:100%;margin-top:8px" @click="handleStop">
      停止运行
    </button>
  </div>
</template>

<script setup>
import { ref, reactive } from 'vue'
import { useAppStore } from '../stores/app'
import { useSettingsStore } from '../stores/settings'
import { startTask, stopTask } from '../api'
import CardEntry from './CardEntry.vue'

const appStore = useAppStore()
const settings = useSettingsStore()
const targetCount = ref(1)

let cardIdCounter = 0
const cards = reactive([])

function addCard() {
  cards.push({ id: cardIdCounter++, number: '', cvc: '', expiry_month: '', expiry_year: '',
    first_name: '', last_name: '', country: 'United States', address: '', address2: '',
    city: '', state: '', zip: '', company: '' })
}

function removeCard(idx) { cards.splice(idx, 1) }

function collectCards() {
  const result = []
  const missing = []
  cards.forEach((c, idx) => {
    if (!c.number) return
    const required = { 'First name': c.first_name, 'Last name': c.last_name, 'Country': c.country,
      'Address': c.address, 'City': c.city, 'State': c.state, 'ZIP': c.zip }
    for (const [label, val] of Object.entries(required)) {
      if (!val.trim()) missing.push(`卡 #${idx + 1}: ${label}`)
    }
    result.push({ ...c })
  })
  if (missing.length > 0) {
    alert('以下必填字段未填写:\n' + missing.join('\n'))
    return null
  }
  return result.length > 0 ? result : undefined
}

async function handleStart() {
  const cardInfoList = collectCards()
  if (cardInfoList === null) return

  appStore.clearLogs()
  settings.save()

  const body = { count: targetCount.value }
  if (settings.cfPassword) body.cf_password = settings.cfPassword
  if (settings.captchaApiKey) body.captcha_api_key = settings.captchaApiKey
  if (cardInfoList) body.card_info_list = cardInfoList

  try {
    await startTask(body)
  } catch (e) {
    alert('启动失败: ' + e.message)
  }
}

async function handleStop() {
  if (!confirm('确定要停止当前任务吗？')) return
  try { await stopTask() } catch (e) { console.error(e) }
}
</script>

<style scoped>
.control-box {
  background: #f8fafc;
  padding: 20px;
  border-radius: var(--radius);
  border: 1px solid var(--border);
}
.control-header {
  font-size: 12px;
  font-weight: 600;
  text-transform: uppercase;
  color: var(--text-sub);
  margin-bottom: 16px;
  letter-spacing: 0.5px;
}
.ctrl-row { margin-bottom: 12px; }
.ctrl-label {
  display: block;
  font-size: 13px;
  margin-bottom: 4px;
  font-weight: 500;
}
.hint { font-size: 11px; color: #999; margin-top: 2px; }
</style>
