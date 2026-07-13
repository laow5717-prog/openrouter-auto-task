<template>
  <div class="stats-grid">
    <div class="stat-card">
      <div class="stat-label">总卡数</div>
      <div class="stat-value">{{ summary.total }}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">绑定成功</div>
      <div class="stat-value green">{{ summary.success }}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">绑定失败</div>
      <div class="stat-value red">{{ summary.failed }}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">待处理</div>
      <div class="stat-value blue">{{ summary.pending }}</div>
    </div>
  </div>

  <!-- 未完成提示 -->
  <div v-if="unfinishedInfo.show && !appStore.isRunning" class="unfinished-banner">
    <span>检测到上次导入的 {{ unfinishedInfo.total }} 张卡中，已绑定 {{ unfinishedInfo.bound }} 张，还剩 <strong>{{ unfinishedInfo.remaining }}</strong> 张未处理。可直接点击「启动信用卡驱动任务」继续处理（无需重新上传）。</span>
    <button class="banner-close" @click="unfinishedInfo.show = false">&times;</button>
  </div>

  <!-- 上传区 -->
  <div class="panel" style="margin-bottom:16px">
    <div class="panel-header">
      <div class="panel-title"><span>&#128193;</span> 上传信用卡 Excel</div>
      <a href="/api/card/template" class="action-btn" style="text-decoration:none">下载模版</a>
    </div>
    <div style="padding:16px">
      <div style="font-size:12px;color:#666;margin-bottom:12px">
        上传包含信用卡数据的 Excel 文件，系统将根据卡片数量自动注册账号并逐张绑定。
      </div>
      <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        <input type="file" ref="fileInput" accept=".xlsx,.xls" style="font-size:12px">
        <button class="action-btn" @click="handleUpload">上传并解析</button>
      </div>
      <div v-if="uploadMsg" style="margin-top:10px;font-size:12px" v-html="uploadMsg"></div>

      <div class="settings-row">
        <div class="setting-item">
          <label class="setting-label">Cloudflare 统一密码</label>
          <input type="text" v-model="settings.cfPassword" class="ctrl-input" placeholder="留空则每个账号随机生成">
        </div>
        <div class="setting-item">
          <label class="setting-label">2Captcha API Key</label>
          <input type="text" v-model="settings.captchaApiKey" class="ctrl-input" placeholder="用于自动解决人机验证">
        </div>
      </div>

      <!-- 从分组启动 -->
      <div v-if="bindGroups.length > 0" style="margin-top:14px;padding-top:14px;border-top:1px dashed var(--border)">
        <div style="font-size:12px;color:#666;margin-bottom:8px">
          或从卡片分组启动（使用「卡片管理」中配置的绑定卡分组）：
        </div>
        <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
          <select v-model="selectedGroupId" class="ctrl-input" style="width:200px">
            <option value="">选择绑定卡分组...</option>
            <option v-for="g in bindGroups" :key="g.id" :value="g.id">{{ g.name }} ({{ g.card_count }}张)</option>
          </select>
          <button v-if="!appStore.isRunning" class="btn btn-primary" style="width:auto;padding:8px 20px" :disabled="!selectedGroupId" @click="handleStartFromGroup">
            从分组启动
          </button>
        </div>
      </div>

      <div style="margin-top:16px;display:flex;gap:10px;align-items:center">
        <button v-if="!appStore.isRunning" class="btn btn-primary" style="width:auto;padding:8px 20px" @click="handleStartCardTask">
          从上传文件启动
        </button>
        <button v-else class="btn btn-danger" style="width:auto;padding:8px 20px" @click="handleStop">
          停止
        </button>
        <a v-if="summary.total > 0 && summary.pending === 0" href="/api/card/report" class="action-btn" style="text-decoration:none">下载报告</a>
      </div>
    </div>
  </div>

  <!-- 绑卡记录 -->
  <div class="panel">
    <div class="panel-header">
      <div class="panel-title"><span>&#128203;</span> 绑卡记录</div>
      <button class="action-btn" @click="loadData">刷新</button>
    </div>

    <FilterBar>
      <input v-model="filters.keyword" class="filter-input" placeholder="搜索卡号/绑定账号..." style="width:200px">
      <select v-model="filters.status" class="filter-select">
        <option value="">全部状态</option>
        <option value="success">成功</option>
        <option value="failed">失败</option>
        <option value="pending">待处理</option>
      </select>
      <button class="filter-btn filter-btn-primary" @click="page = 1; loadData()">查询</button>
      <button class="filter-btn filter-btn-reset" @click="resetFilter">重置</button>
    </FilterBar>

    <div style="overflow:hidden">
      <table>
        <thead>
          <tr>
            <th>#</th>
            <th>卡号(后4位)</th>
            <th>状态</th>
            <th>绑定账号</th>
            <th>错误信息</th>
            <th>处理时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="loading">
            <td colspan="6" class="table-loading">加载中...</td>
          </tr>
          <tr v-else-if="records.length === 0">
            <td colspan="6" class="table-empty">暂无数据</td>
          </tr>
          <tr v-for="r in records" :key="r.index">
            <td>{{ r.index }}</td>
            <td style="font-family:monospace">{{ r.card_display }}</td>
            <td>
              <span class="status-tag" :class="statusClass(r.status)">{{ statusText(r.status) }}</span>
            </td>
            <td>{{ r.bound_to_email }}</td>
            <td style="font-size:11px">{{ r.error }}</td>
            <td style="font-size:11px">{{ r.attempt_time }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <Pagination :total="total" :page="page" :page-size="pageSize"
      @change="p => { page = p; loadData() }"
      @update:page-size="s => { pageSize = s }" />
  </div>
</template>

<script setup>
import { ref, reactive, onMounted, onUnmounted } from 'vue'
import { useAppStore } from '../stores/app'
import { useSettingsStore } from '../stores/settings'
import { getCardStatus, uploadCardExcel, startCardTask, stopTask, checkUnfinishedCards, getCardGroups, startCardTaskFromGroup } from '../api'
import FilterBar from '../components/FilterBar.vue'
import Pagination from '../components/Pagination.vue'

const appStore = useAppStore()
const settings = useSettingsStore()

const records = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const loading = ref(false)
const summary = reactive({ total: 0, success: 0, failed: 0, pending: 0 })
const filters = reactive({ keyword: '', status: '' })
const uploadMsg = ref('')
const fileInput = ref(null)
const unfinishedInfo = reactive({ show: false, remaining: 0, total: 0, bound: 0 })
const bindGroups = ref([])
const selectedGroupId = ref('')

let refreshTimer = null

function statusClass(s) { return s === 'success' ? 'success' : s === 'failed' ? 'fail' : '' }
function statusText(s) { return s === 'success' ? 'SUCCESS' : s === 'failed' ? 'FAILED' : 'PENDING' }

async function loadData() {
  loading.value = true
  try {
    const params = { page: page.value, page_size: pageSize.value }
    if (filters.keyword) params.keyword = filters.keyword
    if (filters.status) params.status = filters.status

    const data = await getCardStatus(params)
    records.value = data.data || []
    total.value = data.total || 0
    if (data.summary) Object.assign(summary, data.summary)
  } catch (e) {
    console.error(e)
  } finally {
    loading.value = false
  }
}

function resetFilter() {
  filters.keyword = ''
  filters.status = ''
  page.value = 1
  loadData()
}

async function handleUpload() {
  const file = fileInput.value?.files?.[0]
  if (!file) { alert('请先选择 Excel 文件'); return }
  uploadMsg.value = '<span style="color:#666">上传解析中...</span>'
  try {
    const data = await uploadCardExcel(file)
    let html = `<span style="color:green">解析成功: ${data.total} 张信用卡</span>`
    if (data.errors?.length) html += `<br><span style="color:orange">${data.errors.length} 条数据有问题被跳过</span>`
    uploadMsg.value = html
    summary.total = data.total
    summary.pending = data.total
  } catch (e) {
    uploadMsg.value = `<span style="color:red">失败: ${e.message}</span>`
  }
}

async function handleStartCardTask() {
  if (appStore.isRunning) { alert('任务已在运行中'); return }
  appStore.clearLogs()
  settings.save()

  const body = { max_bindable_cards: 2 }
  if (settings.cfPassword) body.cf_password = settings.cfPassword
  if (settings.captchaApiKey) body.captcha_api_key = settings.captchaApiKey

  try {
    await startCardTask(body)
  } catch (e) {
    alert('启动失败: ' + e.message)
  }
}

async function handleStop() {
  if (!confirm('确定要停止当前任务吗？')) return
  try { await stopTask() } catch (e) { console.error(e) }
}

async function checkUnfinished() {
  try {
    const data = await checkUnfinishedCards()
    if (data.has_unfinished) {
      unfinishedInfo.show = true
      unfinishedInfo.remaining = data.remaining
      unfinishedInfo.total = data.total
      unfinishedInfo.bound = data.bound
    }
  } catch (e) {
    console.error(e)
  }
}

async function loadBindGroups() {
  try {
    bindGroups.value = await getCardGroups({ type: 'bind' })
  } catch (e) { console.error(e) }
}

async function handleStartFromGroup() {
  if (appStore.isRunning) { alert('任务已在运行中'); return }
  if (!selectedGroupId.value) { alert('请选择卡片分组'); return }
  appStore.clearLogs()
  settings.save()

  const body = { group_id: selectedGroupId.value, max_bindable_cards: 2 }
  if (settings.cfPassword) body.cf_password = settings.cfPassword
  if (settings.captchaApiKey) body.captcha_api_key = settings.captchaApiKey

  try {
    const result = await startCardTaskFromGroup(body)
    alert(`已启动，分组共 ${result.total_cards} 张卡`)
  } catch (e) {
    alert('启动失败: ' + e.message)
  }
}

onMounted(() => {
  loadData()
  checkUnfinished()
  loadBindGroups()
  refreshTimer = setInterval(() => { if (appStore.isRunning) loadData() }, 2000)
})

onUnmounted(() => { if (refreshTimer) clearInterval(refreshTimer) })
</script>

<style scoped>
.unfinished-banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 16px;
  margin-bottom: 16px;
  background: #fff8e1;
  border: 1px solid #ffe082;
  border-radius: 8px;
  font-size: 13px;
  color: #6d4c00;
  line-height: 1.5;
}
.banner-close {
  background: none;
  border: none;
  font-size: 18px;
  cursor: pointer;
  color: #999;
  flex-shrink: 0;
}
.banner-close:hover { color: #333; }
.settings-row {
  display: flex;
  gap: 16px;
  margin-top: 14px;
}
.setting-item { flex: 1; }
.setting-label {
  display: block;
  font-size: 12px;
  font-weight: 500;
  margin-bottom: 4px;
  color: #555;
}
</style>
