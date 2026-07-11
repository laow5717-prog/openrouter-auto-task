<template>
  <div class="stats-grid">
    <div class="stat-card">
      <div class="stat-label">总记录</div>
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

  <div class="panel">
    <div class="panel-header">
      <div class="panel-title"><span>&#128203;</span> 历史绑卡记录</div>
      <div style="display:flex;gap:8px;align-items:center">
        <button class="action-btn" @click="handleExport">导出</button>
        <button class="action-btn" @click="loadData">刷新</button>
        <button class="action-btn action-btn-danger" @click="handleCleanup">清理无效数据</button>
      </div>
    </div>

    <FilterBar>
      <input v-model="filters.keyword" class="filter-input" placeholder="搜索卡号/绑定账号..." style="width:200px">
      <select v-model="filters.status" class="filter-select">
        <option value="">全部状态</option>
        <option value="success">成功</option>
        <option value="failed">失败</option>
        <option value="pending">待处理</option>
      </select>
      <div class="filter-sep"></div>
      <input type="date" v-model="filters.date_from" class="filter-date" title="开始日期">
      <span style="font-size:12px;color:var(--text-sub)">至</span>
      <input type="date" v-model="filters.date_to" class="filter-date" title="结束日期">
      <button class="filter-btn filter-btn-primary" @click="page = 1; loadData()">查询</button>
      <button class="filter-btn filter-btn-reset" @click="resetFilter">重置</button>
    </FilterBar>

    <div style="overflow-x:auto">
      <table class="history-table">
        <thead>
          <tr>
            <th>卡号</th>
            <th>有效期</th>
            <th>CVC</th>
            <th>持卡人</th>
            <th style="white-space:nowrap">状态</th>
            <th>绑定账号</th>
            <th>国家</th>
            <th>地址</th>
            <th>城市</th>
            <th>州/省</th>
            <th>邮编</th>
            <th>错误信息</th>
            <th style="white-space:nowrap">处理时间</th>
            <th style="white-space:nowrap">任务批次</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="loading">
            <td colspan="14" class="table-loading">加载中...</td>
          </tr>
          <tr v-else-if="records.length === 0">
            <td colspan="14" class="table-empty">暂无数据</td>
          </tr>
          <tr v-for="(r, i) in records" :key="i">
            <td style="font-family:monospace;white-space:nowrap">{{ r.card_number || '-' }}</td>
            <td style="white-space:nowrap">{{ r.expiry_month && r.expiry_year ? `${r.expiry_month}/${r.expiry_year}` : '-' }}</td>
            <td style="font-family:monospace">{{ r.cvc || '-' }}</td>
            <td style="white-space:nowrap">{{ r.card_holder || '-' }}</td>
            <td>
              <span class="status-tag" :class="statusClass(r.status)">{{ statusText(r.status) }}</span>
            </td>
            <td>{{ r.bound_to_email || '-' }}</td>
            <td style="white-space:nowrap">{{ r.country || '-' }}</td>
            <td>{{ [r.address, r.address2].filter(Boolean).join(', ') || '-' }}</td>
            <td style="white-space:nowrap">{{ r.city || '-' }}</td>
            <td style="white-space:nowrap">{{ r.state || '-' }}</td>
            <td style="white-space:nowrap">{{ r.zip || '-' }}</td>
            <td>{{ r.error || '-' }}</td>
            <td style="white-space:nowrap;font-size:12px">{{ r.attempted_at || '-' }}</td>
            <td style="font-family:monospace;font-size:12px">{{ r.task_id || '-' }}</td>
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
import { ref, reactive, onMounted } from 'vue'
import { getCardHistory, exportCardHistory, cleanupCardHistory } from '../api'
import FilterBar from '../components/FilterBar.vue'
import Pagination from '../components/Pagination.vue'

const records = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const loading = ref(false)
const summary = reactive({ total: 0, success: 0, failed: 0, pending: 0 })
const filters = reactive({ keyword: '', status: '', date_from: '', date_to: '' })

function statusClass(s) { return s === 'success' ? 'success' : s === 'failed' ? 'fail' : '' }
function statusText(s) { return s === 'success' ? '成功' : s === 'failed' ? '失败' : '待处理' }

async function loadData() {
  loading.value = true
  try {
    const params = { page: page.value, page_size: pageSize.value }
    if (filters.keyword) params.keyword = filters.keyword
    if (filters.status) params.status = filters.status
    if (filters.date_from) params.date_from = filters.date_from
    if (filters.date_to) params.date_to = filters.date_to

    const data = await getCardHistory(params)
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
  filters.date_from = ''
  filters.date_to = ''
  page.value = 1
  loadData()
}

async function handleExport() {
  try {
    const body = {}
    if (filters.keyword) body.keyword = filters.keyword
    if (filters.status) body.status = filters.status
    if (filters.date_from) body.date_from = filters.date_from
    if (filters.date_to) body.date_to = filters.date_to

    const blob = await exportCardHistory(body)
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = 'card_history_export.xlsx'
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  } catch (e) {
    alert('导出失败: ' + e.message)
  }
}

async function handleCleanup() {
  if (!confirm('将删除所有属于已停止/已完成任务的"待处理"记录，此操作不可撤销，确认继续？')) return
  try {
    const res = await cleanupCardHistory()
    alert(`已清理 ${res.deleted} 条无效 pending 记录`)
    loadData()
  } catch (e) {
    alert('清理失败: ' + e.message)
  }
}

onMounted(loadData)
</script>

<style scoped>
.action-btn-danger {
  background: #c0392b;
  color: #fff;
}
.action-btn-danger:hover {
  background: #a93226;
}
.history-table {
  white-space: nowrap;
}
.history-table th,
.history-table td {
  padding: 10px 12px;
  font-size: 13px;
  white-space: nowrap;
}
</style>
