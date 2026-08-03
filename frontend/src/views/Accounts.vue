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
        <option value="billing_page">账单页面</option>
        <option value="interrupted">已中断</option>
        <option value="all_bindings_failed">绑卡全部失败</option>
        <option value="banned">已封禁</option>
        <option value="flagged">GitHub受限</option>
        <option value="archived">已归档</option>
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
            <th>GitHub密码</th>
            <th>邮箱密码 <a href="https://mail.tm" target="_blank" style="font-weight:normal;font-size:11px;color:var(--primary)">(mail.tm)</a></th>
            <th style="white-space:nowrap">状态</th>
            <th style="white-space:nowrap">绑定卡片</th>
            <th style="white-space:nowrap">Credits 余额</th>
            <th style="white-space:nowrap">API Key</th>
            <th style="white-space:nowrap">邮箱认证链接</th>
            <th style="white-space:nowrap">时间</th>
            <th style="white-space:nowrap" class="col-actions">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="loading">
            <td colspan="11" class="table-loading">加载中...</td>
          </tr>
          <tr v-else-if="accounts.length === 0">
            <td colspan="11" class="table-empty">暂无数据</td>
          </tr>
          <tr v-for="acc in accounts" :key="acc.email">
            <td><input type="checkbox" :checked="selected.has(acc.email)" @change="toggleSelect(acc.email, $event.target.checked)"></td>
            <td>{{ acc.email }}</td>
            <td style="font-family:monospace">{{ acc.password }}</td>
            <td style="font-family:monospace">{{ acc.email_password || '-' }}</td>
            <td>
              <span class="status-tag" :class="accStatusClass(acc.status)">{{ accStatusLabel(acc.status) }}</span>
            </td>
            <td>
              <span v-if="acc.card_count > 0" class="card-count-badge" @click="showCards(acc.email)">
                {{ acc.card_count }} 张卡
              </span>
              <span v-else class="card-count-badge empty">无</span>
            </td>
            <td>
              <span v-if="acc.credits_balance !== null && acc.credits_balance !== undefined"
                    class="balance-badge" :class="{ zero: acc.credits_balance <= 0 }"
                    :title="acc.balance_updated_at ? `更新于 ${acc.balance_updated_at}` : ''">
                ${{ Number(acc.credits_balance).toFixed(2) }}
              </span>
              <span v-else style="color:var(--text-sub)">-</span>
            </td>
            <td style="font-family:monospace"
                :title="acc.apikey_updated_at ? `抓取于 ${acc.apikey_updated_at}` : ''">
              {{ acc.apikey || '-' }}
            </td>
            <td>
              <a v-if="acc.email_verify_link" :href="acc.email_verify_link" target="_blank"
                 style="color:var(--primary)">{{ acc.email_verify_link }}</a>
              <span v-else style="color:var(--text-sub)">-</span>
            </td>
            <td>{{ acc.time }}</td>
            <td class="col-actions" style="display:flex;gap:6px;align-items:center">
              <button
                class="row-browse-btn"
                :disabled="openBrowserEmails.has(acc.email)"
                @click="handleOpenBrowser(acc.email)"
              >{{ openBrowserEmails.has(acc.email) ? '查看中' : '查看' }}</button>
              <button
                class="row-recharge-btn"
                :disabled="rechargingEmail === acc.email || store.isRunning"
                @click="handleRecharge(acc.email)"
              >{{ rechargingEmail === acc.email ? '充值中...' : '充值' }}</button>
              <button class="row-log-btn" @click="showRechargeLogs(acc.email)">充值记录</button>
              <button class="row-delete-btn" @click="handleDeleteOne(acc.email)">删除</button>
            </td>
          </tr>
        </tbody>
      </table>
        <div v-if="!accounts || !accounts.length" class="empty-state">暂无数据</div>
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

  <!-- 充值确认弹窗 -->
  <Modal :visible="rechargeConfirmVisible" title="充值确认" @close="rechargeConfirmVisible = false">
    <div style="margin-bottom:16px">
      <div style="font-size:14px;margin-bottom:8px">账号: <strong>{{ rechargeTargetEmail }}</strong></div>
      <div style="font-size:13px;color:var(--text-sub);margin-bottom:12px">在 opencode Zen 充值 $20 credits（Stripe 美元结算，含手续费约 $21.23）</div>
    </div>
    <div style="margin-bottom:16px">
      <label style="display:block;font-size:13px;font-weight:500;margin-bottom:6px;color:#555">
        支付卡分组
      </label>
      <select v-model="rechargeGroupId" class="ctrl-input" style="width:100%">
        <option value="">请选择支付卡分组</option>
        <option v-for="g in paymentGroups" :key="g.id" :value="g.id">{{ g.name }} ({{ g.card_count }}张)</option>
      </select>
      <div style="font-size:11px;color:#999;margin-top:4px">
        将用该分组的卡在 Stripe 结算页自动填卡支付；遇 3DS 验证会记该卡冷却并自动换下一张卡。
      </div>
    </div>
    <div style="display:flex;gap:8px;justify-content:flex-end">
      <button class="btn" style="width:auto;padding:8px 20px" @click="rechargeConfirmVisible = false">取消</button>
      <button class="btn btn-primary" style="width:auto;padding:8px 20px" @click="confirmRecharge">确认充值</button>
    </div>
  </Modal>

  <!-- 充值记录弹窗 -->
  <Modal :visible="rechargeModalVisible" :title="rechargeModalTitle" @close="rechargeModalVisible = false" wide>
    <div v-if="rechargeLogsLoading" class="table-loading">加载中...</div>
    <div v-else-if="rechargeLogs.length === 0" style="text-align:center;color:var(--text-sub);padding:24px">
      该账号暂无充值记录
    </div>
    <div v-else>
      <div v-for="log in rechargeLogs" :key="log.id" class="recharge-log-item">
        <div class="recharge-log-header">
          <span class="recharge-log-amount">${{ log.amount }}</span>
          <span class="status-tag" :class="log.status === 'success' ? 'success' : log.status === 'failed' ? 'fail' : 'warn'">
            {{ log.status === 'success' ? '成功' : log.status === 'failed' ? '失败' : '进行中' }}
          </span>
        </div>
        <div class="recharge-log-meta">
          <span v-if="log.card_display">卡片: {{ log.card_display }}</span>
          <span>{{ log.created_at }}</span>
        </div>
        <div v-if="log.error" class="recharge-log-error">{{ log.error }}</div>
      </div>
    </div>
  </Modal>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onUnmounted } from 'vue'
import { getAccounts, getAccountCards, exportAccounts, deleteAccounts, rechargeAccount, openAccountBrowser, getOpenBrowsers, getRechargeLogsByEmail, getCardGroups } from '../api'
import { useAppStore } from '../stores/app'
const store = useAppStore()

// 已打开浏览器的账号集合
const openBrowserEmails = ref(new Set())
let browserPollTimer = null

async function pollOpenBrowsers() {
  try {
    const data = await getOpenBrowsers()
    openBrowserEmails.value = new Set(data.emails || [])
  } catch { /* ignore */ }
}

function startBrowserPoll() {
  pollOpenBrowsers()
  browserPollTimer = setInterval(pollOpenBrowsers, 3000)
}

function stopBrowserPoll() {
  if (browserPollTimer) { clearInterval(browserPollTimer); browserPollTimer = null }
}
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

// 充值状态
const rechargingEmail = ref('')
const rechargeConfirmVisible = ref(false)
const rechargeTargetEmail = ref('')
const rechargeGroupId = ref('')
const paymentGroups = ref([])

// 卡片弹窗
const modalVisible = ref(false)
const modalTitle = ref('')
const cardList = ref([])
const cardsLoading = ref(false)

// 充值记录弹窗
const rechargeModalVisible = ref(false)
const rechargeModalTitle = ref('')
const rechargeLogs = ref([])
const rechargeLogsLoading = ref(false)

const allChecked = computed(() => {
  if (accounts.value.length === 0) return false
  return accounts.value.every(a => selected.has(a.email))
})

const statusMap = {
  registered: '已注册',
  bound: '已绑卡',
  failed: '失败',
  error: '错误',
  interrupted: '已中断',
  billing_page: '账单页面',
  all_bindings_failed: '绑卡全部失败',
  banned: '已封禁',
  flagged: 'GitHub受限',       // GitHub 反滥用 flag，无法授权第三方 OAuth，每日任务不再轮转
  archived: '已归档',          // 余额≥阈值，每日充值跳过
  suspended: '已挂起',
  subscribed: '已订阅',
  recharged: '已充值',
}
function accStatusLabel(s) {
  return statusMap[s] || s
}
function accStatusClass(s) {
  if (s === 'banned' || s === 'flagged' || s === 'suspended') return 'fail'
  if (s.includes('bound') || s === 'registered') return 'success'
  if (s === 'subscribed' || s === 'recharged') return 'success'
  if (s.includes('failed') || s === 'error') return 'fail'
  if (s === 'interrupted' || s === 'billing_page' || s === 'archived') return 'warn'
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

async function handleRecharge(email) {
  rechargeTargetEmail.value = email
  rechargeGroupId.value = ''
  // 加载全部卡分组（已不区分类型，均为支付卡）
  try {
    paymentGroups.value = await getCardGroups()
  } catch { paymentGroups.value = [] }
  rechargeConfirmVisible.value = true
}

async function confirmRecharge() {
  const email = rechargeTargetEmail.value
  // opencode 充值必须用卡在 Stripe 填卡付款，故分组必选
  if (!rechargeGroupId.value) {
    alert('请选择一个支付卡分组（充值需用卡在 Stripe 填卡支付）')
    return
  }
  rechargeConfirmVisible.value = false
  rechargingEmail.value = email
  try {
    await rechargeAccount(email, rechargeGroupId.value)
    alert(`已启动充值任务，请在 Dashboard 查看进度`)
  } catch (e) {
    alert('充值请求失败: ' + e.message)
  } finally {
    rechargingEmail.value = ''
  }
}

async function handleOpenBrowser(email) {
  try {
    await openAccountBrowser(email)
    alert(`已打开 ${email} 的浏览器，关闭浏览器窗口后自动结束`)
  } catch (e) {
    alert('打开浏览器失败: ' + e.message)
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

async function showRechargeLogs(email) {
  rechargeModalTitle.value = `${email} - 充值记录`
  rechargeModalVisible.value = true
  rechargeLogsLoading.value = true
  rechargeLogs.value = []

  try {
    rechargeLogs.value = await getRechargeLogsByEmail(email)
  } catch (e) {
    console.error(e)
  } finally {
    rechargeLogsLoading.value = false
  }
}

onMounted(() => { loadData(); startBrowserPoll() })
onUnmounted(stopBrowserPoll)
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

/* 操作列固定在表格右侧，横向滚动时保持可见 */
.acc-table th.col-actions,
.acc-table td.col-actions {
  position: sticky;
  right: 0;
  background: #fff;
  box-shadow: -6px 0 8px -6px rgba(0, 0, 0, 0.12);
}
.acc-table th.col-actions { z-index: 3; }
.acc-table td.col-actions { z-index: 2; }

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

.balance-badge {
  display: inline-block;
  padding: 3px 10px;
  border-radius: 20px;
  font-family: 'JetBrains Mono', monospace;
  font-size: 12px;
  font-weight: 600;
  background: #dcfce7;
  color: #15803d;
  cursor: default;
}
.balance-badge.zero { background: #f3f4f6; color: #9ca3af; }

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

.row-recharge-btn {
  padding: 3px 10px;
  font-size: 12px;
  border: 1px solid #bbf7d0;
  border-radius: 4px;
  background: #fff;
  color: #16a34a;
  cursor: pointer;
  transition: all 0.15s;
}
.row-recharge-btn:hover:not(:disabled) { background: #f0fdf4; }
.row-recharge-btn:disabled { opacity: 0.5; cursor: not-allowed; }

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

.row-browse-btn {
  padding: 3px 10px;
  font-size: 12px;
  border: 1px solid #bfdbfe;
  border-radius: 4px;
  background: #fff;
  color: #2563eb;
  cursor: pointer;
  transition: all 0.15s;
}
.row-browse-btn:hover:not(:disabled) { background: #eff6ff; }
.row-browse-btn:disabled { opacity: 0.4; cursor: not-allowed; }

.row-log-btn {
  padding: 3px 10px;
  font-size: 12px;
  border: 1px solid #ddd6fe;
  border-radius: 4px;
  background: #fff;
  color: #7c3aed;
  cursor: pointer;
  transition: all 0.15s;
}
.row-log-btn:hover { background: #f5f3ff; }

.recharge-log-item {
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 14px 18px;
  margin-bottom: 10px;
}
.recharge-log-item:last-child { margin-bottom: 0; }
.recharge-log-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}
.recharge-log-amount { font-size: 18px; font-weight: 600; font-family: 'JetBrains Mono', monospace; }
.recharge-log-meta {
  display: flex;
  gap: 16px;
  font-size: 12px;
  color: var(--text-sub);
}
.recharge-log-error {
  margin-top: 8px;
  padding: 6px 10px;
  background: #fef2f2;
  color: #dc2626;
  border-radius: 6px;
  font-size: 12px;
}
</style>
