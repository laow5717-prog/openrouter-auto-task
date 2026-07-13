<template>
  <div class="panel">
    <div class="panel-header">
      <div class="panel-title"><span>&#128176;</span> 充值记录</div>
      <div style="display:flex;gap:8px;align-items:center">
        <button class="action-btn" @click="loadData">刷新</button>
      </div>
    </div>

    <FilterBar>
      <input v-model="filters.email" class="filter-input" placeholder="搜索邮箱..." style="width:200px">
      <select v-model="filters.status" class="filter-select">
        <option value="">全部状态</option>
        <option value="success">成功</option>
        <option value="failed">失败</option>
        <option value="pending">进行中</option>
      </select>
      <div class="filter-sep"></div>
      <input type="date" v-model="filters.date_from" class="filter-date" title="开始日期">
      <span style="font-size:12px;color:var(--text-sub)">至</span>
      <input type="date" v-model="filters.date_to" class="filter-date" title="结束日期">
      <button class="filter-btn filter-btn-primary" @click="page = 1; loadData()">查询</button>
      <button class="filter-btn filter-btn-reset" @click="resetFilter">重置</button>
    </FilterBar>

    <div style="overflow-x:auto">
      <table class="log-table">
        <thead>
          <tr>
            <th>账号邮箱</th>
            <th>使用卡片</th>
            <th>金额</th>
            <th style="white-space:nowrap">状态</th>
            <th>错误信息</th>
            <th style="white-space:nowrap">时间</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="loading">
            <td colspan="7" class="table-loading">加载中...</td>
          </tr>
          <tr v-else-if="logs.length === 0">
            <td colspan="7" class="table-empty">暂无数据</td>
          </tr>
          <tr v-for="log in logs" :key="log.id">
            <td>{{ log.email }}</td>
            <td style="font-family:monospace">{{ log.card_display || '-' }}</td>
            <td style="font-family:monospace">${{ log.amount }}</td>
            <td>
              <span class="status-tag" :class="statusClass(log.status)">{{ statusText(log.status) }}</span>
            </td>
            <td class="error-cell">{{ log.error || '-' }}</td>
            <td style="white-space:nowrap;font-size:12px">{{ log.created_at }}</td>
            <td>
              <button v-if="log.api_response" class="row-detail-btn" @click="showDetail(log)">详情</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <Pagination :total="total" :page="page" :page-size="pageSize"
      @change="p => { page = p; loadData() }"
      @update:page-size="s => { pageSize = s }" />
  </div>

  <Modal :visible="detailVisible" title="API 响应详情" @close="detailVisible = false" wide>
    <pre class="detail-pre">{{ detailContent }}</pre>
  </Modal>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { getRechargeLogs } from '../api'
import FilterBar from '../components/FilterBar.vue'
import Pagination from '../components/Pagination.vue'
import Modal from '../components/Modal.vue'

const logs = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const loading = ref(false)
const filters = reactive({ email: '', status: '', date_from: '', date_to: '' })

const detailVisible = ref(false)
const detailContent = ref('')

function statusClass(s) {
  return s === 'success' ? 'success' : s === 'failed' ? 'fail' : 'warn'
}
function statusText(s) {
  return s === 'success' ? '成功' : s === 'failed' ? '失败' : '进行中'
}

async function loadData() {
  loading.value = true
  try {
    const params = { page: page.value, page_size: pageSize.value }
    if (filters.email) params.email = filters.email
    if (filters.status) params.status = filters.status
    if (filters.date_from) params.date_from = filters.date_from
    if (filters.date_to) params.date_to = filters.date_to

    const data = await getRechargeLogs(params)
    logs.value = data.data || []
    total.value = data.total || 0
  } catch (e) {
    console.error(e)
  } finally {
    loading.value = false
  }
}

function resetFilter() {
  filters.email = ''
  filters.status = ''
  filters.date_from = ''
  filters.date_to = ''
  page.value = 1
  loadData()
}

function showDetail(log) {
  try {
    const parsed = JSON.parse(log.api_response)
    detailContent.value = JSON.stringify(parsed, null, 2)
  } catch {
    detailContent.value = log.api_response
  }
  detailVisible.value = true
}

onMounted(loadData)
</script>

<style scoped>
.log-table th,
.log-table td {
  padding: 10px 14px;
  font-size: 13px;
}
.error-cell {
  max-width: 300px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: #dc2626;
  font-size: 12px;
}
.row-detail-btn {
  padding: 3px 10px;
  font-size: 12px;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: #fff;
  color: var(--primary);
  cursor: pointer;
  transition: all 0.15s;
}
.row-detail-btn:hover { background: #fff7ed; }
.detail-pre {
  background: #f8f9fa;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 16px;
  font-size: 12px;
  font-family: 'JetBrains Mono', monospace;
  overflow-x: auto;
  max-height: 400px;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
