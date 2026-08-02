<template>
  <div class="info-banner">
    <strong>代理 IP 说明：</strong>
    每个账号处理时会领用一个空闲代理出口 IP（反关联），2 worker 并发各用不同代理、互不冲突，用完释放回池。
    粘贴格式 <code>用户:密码@主机:端口</code>（每行一个，兼容纯冒号 <code>用户:密码:主机:端口</code>）。凭据在库中，列表打码显示。
  </div>

  <div class="stats-grid">
    <div class="stat-card">
      <div class="stat-label">代理总数</div>
      <div class="stat-value">{{ total }}</div>
    </div>
  </div>

  <!-- 导入 -->
  <div class="panel" style="margin-bottom:16px">
    <div class="panel-header">
      <div class="panel-title"><Icon name="monitor" size="18" /> 粘贴导入</div>
      <div style="display:flex;gap:8px">
        <button class="action-btn" @click="doImport" :disabled="importing || !importText.trim()">
          {{ importing ? '导入中…' : '导入' }}
        </button>
        <button class="action-btn" @click="loadProxies">刷新</button>
        <button class="action-btn" style="color:#ef4444" @click="doClear" :disabled="!total">清空</button>
      </div>
    </div>
    <div style="padding:12px 16px">
      <textarea v-model="importText" class="ctrl-input"
                style="width:100%;min-height:120px;font-family:monospace;resize:vertical"
                placeholder="ff76b46bcb5e076c:92dsTEyCpnbIOxt0@gateway.i-proxy.com:10000&#10;每行一个…"></textarea>
      <div v-if="importMsg" style="margin-top:8px;font-size:13px;color:var(--text-sub)">{{ importMsg }}</div>
    </div>
  </div>

  <!-- 列表 -->
  <div class="panel">
    <div class="panel-header">
      <div class="panel-title"><Icon name="cards" size="18" /> 代理列表（{{ total }}）</div>
    </div>
    <div v-if="rows.length === 0" style="padding:32px;text-align:center;color:var(--text-sub)">
      暂无代理，粘贴导入
    </div>
    <table v-else class="data-table">
      <thead>
        <tr><th>#</th><th>主机</th><th>端口</th><th>用户名</th><th>密码</th><th>状态</th><th>操作</th></tr>
      </thead>
      <tbody>
        <tr v-for="r in rows" :key="r.id">
          <td>{{ r.id }}</td>
          <td>{{ r.host }}</td>
          <td>{{ r.port }}</td>
          <td style="font-family:monospace">{{ r.username }}</td>
          <td style="font-family:monospace">{{ r.password }}</td>
          <td>{{ r.status || '可用' }}</td>
          <td><button class="mini-btn" style="color:#ef4444" @click="doDelete(r.id)">删除</button></td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import Icon from '../components/Icon.vue'
import { getProxies, importProxies, deleteProxy, clearProxies } from '../api/index.js'

const rows = ref([])
const total = ref(0)
const importText = ref('')
const importing = ref(false)
const importMsg = ref('')

async function loadProxies() {
  const res = await getProxies({ page: 1, page_size: 500 })
  rows.value = res.data || []
  total.value = res.total || 0
}

async function doImport() {
  importing.value = true
  importMsg.value = ''
  try {
    const res = await importProxies(importText.value)
    importMsg.value = `导入成功 ${res.added} 个，跳过 ${res.skipped} 个（重复/非法），当前共 ${res.total} 个`
    importText.value = ''
    await loadProxies()
  } catch (e) {
    importMsg.value = '导入失败：' + (e.message || e)
  } finally {
    importing.value = false
  }
}

async function doDelete(id) {
  await deleteProxy(id)
  await loadProxies()
}

async function doClear() {
  if (!confirm('确定清空全部代理？')) return
  await clearProxies()
  await loadProxies()
}

onMounted(loadProxies)
</script>
