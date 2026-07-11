<template>
  <div class="stats-grid">
    <div class="stat-card">
      <div class="stat-label">当前步骤</div>
      <div class="stat-value" style="font-size:18px">{{ store.currentAction }}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">本次成功</div>
      <div class="stat-value green">{{ store.successCount }}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">本次失败</div>
      <div class="stat-value red">{{ store.failCount }}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">库存总数</div>
      <div class="stat-value blue">{{ store.totalInventory }}</div>
    </div>
  </div>

  <div class="split-view">
    <div class="panel">
      <div class="panel-header">
        <div class="panel-title"><span>&#128250;</span> 实时画面</div>
        <span class="status-pill" :class="store.isRunning ? 'success' : 'neutral'">
          {{ store.isRunning ? 'LIVE' : 'OFFLINE' }}
        </span>
      </div>
      <div class="monitor-body">
        <img v-if="store.isRunning" class="monitor-img" :src="videoFeedUrl" alt="Monitor">
        <div v-else style="color:#666;font-size:12px">等待信号...</div>
      </div>
    </div>

    <div class="panel">
      <div class="panel-header">
        <div class="panel-title"><span>&#128220;</span> 终端日志</div>
        <button class="action-btn" @click="store.clearLogs()">清空</button>
      </div>
      <div class="log-body" ref="logContainer">
        <div v-if="store.logs.length === 0" class="log-placeholder">> 准备就绪...</div>
        <div v-for="(log, i) in store.logs" :key="i" class="log-entry">{{ log }}</div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, watch, nextTick } from 'vue'
import { useAppStore } from '../stores/app'

const store = useAppStore()
const logContainer = ref(null)
const videoFeedUrl = '/video_feed'

watch(() => store.logs.length, () => {
  nextTick(() => {
    if (logContainer.value) logContainer.value.scrollTop = logContainer.value.scrollHeight
  })
})
</script>

<style scoped>
.split-view {
  display: grid;
  grid-template-columns: 1.2fr 1fr;
  gap: 24px;
  height: 500px;
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
