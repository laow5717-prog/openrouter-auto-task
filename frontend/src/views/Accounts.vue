<template>
  <div class="panel">
    <div class="panel-header">
      <div class="panel-title"><span>&#128101;</span> 账号列表</div>
      <div style="display:flex;gap:8px;align-items:center">
        <button class="action-btn danger" @click="handleDelete" :disabled="selected.size === 0">删除选中</button>
        <button class="action-btn" @click="handleExport('selected')">导出选中</button>
        <button class="action-btn" @click="handleExport('filtered')">导出搜索结果</button>
        <button class="action-btn" @click="loadData">刷新</button>
      </div>
    </div>

    <FilterBar>
      <input v-model="filters.keyword" class="filter-input" placeholder="搜索邮箱..." style="width:200px">
      <select v-model="filters.status" class="filter-select">
        <option value="">全部状态</option>
        <option value="registered">已注册</option>
        <option value="bound">已绑卡</option>
        <option value="failed">失败</option>
        <option value="error">错误</option>
      </select>
      <div class="filter-sep"></div>
      <input type="date" v-model="filters.date_from" class="filter-date" title="开始日期">
      <span style="font-size:12px;color:var(--text-sub)">至</span>
      <input type="date" v-model="filters.date_to" class="filter-date" title="结束日期">
      <button class="filter-btn filter-btn-primary" @click="page = 1; loadData()">查询</button>
      <button class="filter-btn filter-btn-reset" @click="resetFilter">重置</button>
      <span v-if="selected.size > 0" style="font-size:12px;color:var(--text-sub);margin-left:8px">
        已选 {{ selected.size }} 项
      </span>
    </FilterBar>

    <div style="overflow-x:auto">
      <table class="acc-table">
        <thead>
          <tr>
            <th style="width:40px"><input type="checkbox" :checked="allChecked" @change="toggleAll($event.target.checked)"></th>
            <th>邮箱账号</th>
            <th>CF密码</th>
            <th>邮箱密码 <a href="https://mail.tm" target="_blank" style="font-weight:normal;font-size:11px;color:var(--primary)">(mail.tm)</a></th>
            <th style="white-space:nowrap">状态</th>
            <th style="white-space:nowrap">绑定卡片</th>
            <th style="white-space:nowrap">时间</th>
            <th style="white-space:nowrap">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="loading">
            <td colspan="8" class="table-loading">加载中...</td>
          </tr>
          <tr v-else-if="accounts.length === 0">
            <td colspan="8" class="table-empty">暂无数据</td>
          </tr>
          <tr v-for="acc in accounts" :key="acc.email">
            <td><input type="checkbox" :checked="selected.has(acc.email)" @change="toggleSelect(acc.email, $event.target.checked)"></td>
            <td>{{ acc.email }}</td>
            <td style="font-family:monospace">{{ acc.password }}</td>
            <td style="font-family:monospace">{{ acc.email_password || '-' }}</td>
            <td>
              <span class="status-tag" :class="accStatusClass(acc.status)">{{ acc.status }}</span>
            </td>
            <td>
              <span v-if="acc.card_count > 0" class="card-count-badge" @click="showCards(acc.email)">
                {{ acc.card_count }} 张卡
              </span>
              <span v-else class="card-count-badge empty">无</span>
            </td>
            <td>{{ acc.time }}</td>
            <td>
              <button class="row-delete-btn" @click="handleDeleteOne(acc.email)">删除</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <Pagination :total="total" :page="page" :page-size="pageSize"
      @change="p => { page = p; loadData() }"
      @update:page-size="s => { pageSize = s }" />
  </div>

  <!-- 信用卡详情弹窗 -->
  <Modal :visible="modalVisible" :title="modalTitle" @close="modalVisible = false" wide>
    <div v-if="cardsLoading" class="table-loading">加载中...</div>
    <div v-else-if="cardList.length === 0" style="text-align:center;color:var(--text-sub);padding:24px">
      该账号暂无绑定的信用卡
    </div>
    <div v-else>
      <div v-for="card in cardList" :key="card.id" class="card-detail-card">
        <div class="card-detail-header">
          <span class="card-detail-number">{{ card.card_number }}</span>
          <span class="status-tag" :class="card.status === 'success' ? 'success' : card.status === 'failed' ? 'fail' : ''">
            {{ card.status === 'success' ? '成功' : card.status === 'failed' ? '失败' : '待处理' }}
          </span>
        </div>
        <div class="card-detail-grid">
          <div class="card-field"><span class="card-field-label">有效期</span><span>{{ card.expiry_month }}/{{ card.expiry_year }}</span></div>
          <div class="card-field"><span class="card-field-label">CVC</span><span>{{ card.cvc }}</span></div>
          <div class="card-field"><span class="card-field-label">持卡人</span><span>{{ card.card_holder || '-' }}</span></div>
          <div class="card-field"><span class="card-field-label">绑定时间</span><span>{{ card.attempted_at || '-' }}</span></div>
        </div>
        <div class="card-detail-section">账单地址</div>
        <div class="card-detail-grid">
          <div class="card-field"><span class="card-field-label">国家</span><span>{{ card.country || '-' }}</span></div>
          <div class="card-field"><span class="card-field-label">州/省</span><span>{{ card.state || '-' }}</span></div>
          <div class="card-field"><span class="card-field-label">城市</span><span>{{ card.city || '-' }}</span></div>
          <div class="card-field"><span class="card-field-label">邮编</span><span>{{ card.zip || '-' }}</span></div>
          <div class="card-field full"><span class="card-field-label">地址1</span><span>{{ card.address || '-' }}</span></div>
          <div class="card-field full"><span class="card-field-label">地址2</span><span>{{ card.address2 || '-' }}</span></div>
          <div class="card-field"><span class="card-field-label">公司</span><span>{{ card.company || '-' }}</span></div>
        </div>
        <div v-if="card.error && card.error !== '-'" class="card-detail-error">错误: {{ card.error }}</div>
      </div>
    </div>
  </Modal>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { getAccounts, getAccountCards, exportAccounts, deleteAccounts } from '../api'
import FilterBar from '../components/FilterBar.vue'
import Pagination from '../components/Pagination.vue'
import Modal from '../components/Modal.vue'

const accounts = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const loading = ref(false)
const filters = reactive({ keyword: '', status: '', date_from: '', date_to: '' })
const selected = reactive(new Set())

// 弹窗
const modalVisible = ref(false)
const modalTitle = ref('')
const cardList = ref([])
const cardsLoading = ref(false)

const allChecked = computed(() => {
  if (accounts.value.length === 0) return false
  return accounts.value.every(a => selected.has(a.email))
})

function accStatusClass(s) {
  if (s.includes('bound') || s.includes('registered')) return 'success'
  if (s.includes('failed') || s.includes('error')) return 'fail'
  return ''
}

async function loadData() {
  loading.value = true
  try {
    const params = { page: page.value, page_size: pageSize.value }
    if (filters.keyword) params.keyword = filters.keyword
    if (filters.status) params.status = filters.status
    if (filters.date_from) params.date_from = filters.date_from
    if (filters.date_to) params.date_to = filters.date_to

    const result = await getAccounts(params)
    accounts.value = result.data || []
    total.value = result.total || 0
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

function toggleSelect(email, checked) {
  if (checked) selected.add(email)
  else selected.delete(email)
}

function toggleAll(checked) {
  accounts.value.forEach(a => {
    if (checked) selected.add(a.email)
    else selected.delete(a.email)
  })
}

async function handleDelete() {
  if (selected.size === 0) return
  const count = selected.size
  if (!confirm(`确定要删除选中的 ${count} 个账号吗？\n\n此操作将永久删除账号及其关联的卡片绑定记录，不可恢复！`)) return
  try {
    await deleteAccounts(Array.from(selected))
    selected.clear()
    await loadData()
  } catch (e) {
    alert('删除失败: ' + e.message)
  }
}

async function handleDeleteOne(email) {
  if (!confirm(`确定要删除账号 ${email} 吗？\n\n此操作将永久删除该账号及其关联的卡片绑定记录，不可恢复！`)) return
  try {
    await deleteAccounts([email])
    selected.delete(email)
    await loadData()
  } catch (e) {
    alert('删除失败: ' + e.message)
  }
}

async function handleExport(mode) {
  if (mode === 'selected' && selected.size === 0) {
    alert('请先勾选要导出的账号')
    return
  }

  const body = { mode }
  if (mode === 'selected') {
    body.emails = Array.from(selected)
  } else {
    body.keyword = filters.keyword
    body.status = filters.status
    body.date_from = filters.date_from
    body.date_to = filters.date_to
  }

  try {
    const blob = await exportAccounts(body)
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = 'accounts_export.xlsx'
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  } catch (e) {
    alert('导出失败: ' + e.message)
  }
}

async function showCards(email) {
  modalTitle.value = `${email} - 绑定的信用卡`
  modalVisible.value = true
  cardsLoading.value = true
  cardList.value = []

  try {
    cardList.value = await getAccountCards(email)
  } catch (e) {
    console.error(e)
  } finally {
    cardsLoading.value = false
  }
}

onMounted(loadData)
</script>

<style scoped>
.acc-table th,
.acc-table td {
  padding: 12px 14px;
  white-space: nowrap;
}
.acc-table td:nth-child(2) {
  white-space: normal;
  word-break: break-all;
  min-width: 180px;
}
.acc-table td:nth-child(3),
.acc-table td:nth-child(4) {
  white-space: normal;
  word-break: break-all;
  min-width: 100px;
}

.card-count-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 10px;
  border-radius: 20px;
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.15s;
  background: #dbeafe;
  color: #1e40af;
}
.card-count-badge:hover { background: #bfdbfe; }
.card-count-badge.empty { background: #f3f4f6; color: #9ca3af; cursor: default; }

.card-detail-card {
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 16px 20px;
  margin-bottom: 12px;
}
.card-detail-card:last-child { margin-bottom: 0; }
.card-detail-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}
.card-detail-number { font-family: 'JetBrains Mono', monospace; font-size: 15px; font-weight: 600; letter-spacing: 1px; }
.card-detail-section {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-sub);
  margin: 12px 0 8px;
  padding-top: 8px;
  border-top: 1px dashed var(--border);
}
.card-detail-grid {
  display: grid;
  grid-template-columns: 1fr 1fr 1fr;
  gap: 8px 16px;
}
.card-field {
  font-size: 13px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.card-field.full { grid-column: span 3; }
.card-field-label { font-size: 11px; color: var(--text-sub); }
.action-btn.danger { background: #fef2f2; color: #dc2626; border-color: #fecaca; }
.action-btn.danger:hover:not(:disabled) { background: #fee2e2; }
.action-btn.danger:disabled { opacity: 0.4; cursor: not-allowed; }

.row-delete-btn {
  padding: 3px 10px;
  font-size: 12px;
  border: 1px solid #fecaca;
  border-radius: 4px;
  background: #fff;
  color: #dc2626;
  cursor: pointer;
  transition: all 0.15s;
}
.row-delete-btn:hover { background: #fef2f2; }

.card-detail-error {
  margin-top: 10px;
  padding: 8px 12px;
  background: #fef2f2;
  color: #dc2626;
  border-radius: 6px;
  font-size: 12px;
}
</style>
