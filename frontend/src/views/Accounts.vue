<template>
  <div class="panel">
    <div class="panel-header">
      <div class="panel-title"><span>&#128101;</span> 账号列表</div>
      <div style="display:flex;gap:8px;align-items:center">
        <input type="file" ref="importInput" accept=".xlsx,.xls"
               style="font-size:12px;max-width:180px" title="邮箱 / 邮箱密码 / 邮箱认证链接">
        <button class="action-btn" @click="handleImport">导入账号</button>
        <a href="/api/accounts/template" class="action-btn" style="text-decoration:none">下载模版</a>
        <div class="filter-sep"></div>
        <button class="action-btn" @click="handleArchive" :disabled="selected.size === 0">归档选中</button>
        <button class="action-btn" @click="handleResetImported" :disabled="selected.size === 0">重置为待注册</button>
        <button class="action-btn danger" @click="handleDelete" :disabled="selected.size === 0">删除选中</button>
        <button class="action-btn" @click="handleExport('selected')">导出选中</button>
        <button class="action-btn" @click="handleExport('filtered')">导出搜索结果</button>
        <button class="action-btn" @click="loadData">刷新</button>
      </div>
    </div>

    <div v-if="importMsg" style="padding:8px 12px;font-size:12px;line-height:1.7" v-html="importMsg"></div>

    <!-- 归档维度归页签管：它同时决定后端的查询条件与分页总数，
         所以不能只在前端切当前页的数据（那样「已归档」页签只会显示本页碰巧有的那几个） -->
    <div class="scope-tabs">
      <button v-for="t in SCOPE_TABS" :key="t.key" :title="t.hint"
              class="scope-tab" :class="{ active: scope === t.key }"
              @click="switchScope(t.key)">
        {{ t.label }}
        <span class="scope-tab-count">{{ scopeCounts[t.key] }}</span>
      </button>
    </div>

    <FilterBar>
      <input v-model="filters.keyword" class="filter-input" placeholder="搜索邮箱..." style="width:200px">
      <!-- 下拉只列当前页签内的状态：跨页签的选项在这里选中必然得到空列表。
           在用/已归档页签下只有一种状态，下拉整个隐藏。 -->
      <select v-if="identityOptions.length" v-model="filters.identity_status"
              class="filter-select" title="GitHub 注册与封禁结果，跨平台一致">
        <option value="">全部身份状态</option>
        <option v-for="s in identityOptions" :key="s" :value="s">{{ accStatusLabel(s) }}</option>
      </select>
      <select v-model="filters.platform_status" class="filter-select" title="该账号在当前平台的状态">
        <option value="">全部平台状态</option>
        <option value="recharged">已充值</option>
        <option value="subscribed">已订阅</option>
        <option value="archived">已归档</option>
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

    <!-- 只统计当前页：跨页合计需要另一次全量聚合，而 platform_status 是在后端分页之后
         做的前端过滤（见 /api/accounts 注释），两者凑一起会给出自相矛盾的数字 -->
    <div class="page-sum-bar">
      <span class="page-sum-title">当前页合计（{{ accounts.length }} 个账号 · {{ store.platform }}）</span>
      <span class="page-sum-item">
        今日 <b class="green">${{ money(pageSum.today) }}</b> · 累计 <b>${{ money(pageSum.total) }}</b>
      </span>
      <!-- 「全部」页签才拆分：另外两个页签下每一行的归属都由页签本身定死了，
           再拆一次只会得到「一组等于全部、另一组恒为 0」 -->
      <template v-if="scope === 'all' && pageSum.retiredCount > 0">
        <span class="page-sum-sep"></span>
        <span class="page-sum-item">
          已核销 {{ pageSum.retiredCount }} 个：今日 <b>${{ money(pageSum.retiredToday) }}</b> ·
          累计 <b>${{ money(pageSum.retiredTotal) }}</b>
        </span>
        <span class="page-sum-item">
          在用 {{ accounts.length - pageSum.retiredCount }} 个：今日 <b>${{ money(pageSum.activeToday) }}</b> ·
          累计 <b>${{ money(pageSum.activeTotal) }}</b>
        </span>
      </template>
    </div>

    <div style="overflow-x:auto">
      <table class="acc-table">
        <thead>
          <tr>
            <th style="width:40px"><input type="checkbox" :checked="allChecked" @change="toggleAll($event.target.checked)"></th>
            <th>邮箱账号</th>
            <th>GitHub密码</th>
            <th>邮箱密码 <a href="https://mail.tm" target="_blank" style="font-weight:normal;font-size:11px;color:var(--primary)">(mail.tm)</a></th>
            <th style="white-space:nowrap">身份状态</th>
            <th style="white-space:nowrap">平台状态</th>
            <th style="white-space:nowrap">绑定卡片</th>
            <th style="white-space:nowrap" title="该账号今日在当前平台成功充值的金额">今日充值</th>
            <th style="white-space:nowrap" title="该账号在当前平台的累计成功充值金额">累计充值</th>
            <th style="white-space:nowrap">Credits 余额</th>
            <th style="white-space:nowrap">API Key</th>
            <th style="white-space:nowrap">邮箱认证链接</th>
            <th style="white-space:nowrap">时间</th>
            <th style="white-space:nowrap" class="col-actions">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="loading">
            <td colspan="14" class="table-loading">加载中...</td>
          </tr>
          <tr v-else-if="accounts.length === 0">
            <td colspan="14" class="table-empty">暂无数据</td>
          </tr>
          <!-- 已核销的行压暗一档：「全部」页签下它和在用账号混排，光看状态标签不够显眼 -->
          <tr v-for="acc in accounts" :key="acc.email"
              :class="{ 'retired-row': acc.identity_status === 'retired' }">
            <td><input type="checkbox" :checked="selected.has(acc.email)" @change="toggleSelect(acc.email, $event.target.checked)"></td>
            <td>{{ acc.email }}</td>
            <td style="font-family:monospace">{{ acc.password }}</td>
            <td style="font-family:monospace">{{ acc.email_password || '-' }}</td>
            <td>
              <span class="status-tag" :class="accStatusClass(acc.identity_status)">
                {{ accStatusLabel(acc.identity_status) }}
              </span>
            </td>
            <td>
              <span v-if="acc.platform_status" class="status-tag"
                    :class="accStatusClass(acc.platform_status)"
                    :title="`平台 ${acc.platform}`">{{ accStatusLabel(acc.platform_status) }}</span>
              <span v-else style="color:var(--text-sub)" title="尚未在该平台开通">-</span>
            </td>
            <td>
              <span v-if="acc.card_count > 0" class="card-count-badge" @click="showCards(acc.email)">
                {{ acc.card_count }} 张卡
              </span>
              <span v-else class="card-count-badge empty">无</span>
            </td>
            <!-- 0 显示为灰 '-'：绝大多数行今日都是 0，满屏 $0.00 会把真正有金额的行淹掉 -->
            <td style="font-family:monospace">
              <span v-if="acc.recharge_today > 0" class="amount-today">${{ money(acc.recharge_today) }}</span>
              <span v-else style="color:var(--text-sub)">-</span>
            </td>
            <td style="font-family:monospace">
              <span v-if="acc.recharge_total > 0">${{ money(acc.recharge_total) }}</span>
              <span v-else style="color:var(--text-sub)">-</span>
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
              <!-- 取消归档是低频的纠错操作，只对已归档的行显示，不占工具栏位置 -->
              <button v-if="acc.identity_status === 'retired'" class="row-log-btn"
                      @click="handleUnarchive(acc.email)">取消归档</button>
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
      <div style="font-size:13px;color:var(--text-sub);margin-bottom:12px">
        按 config.yaml 的 recharge 策略充值：每笔金额在配置区间内随机（默认 $20–$100，Stripe 美元结算另收手续费），
        同一账号会连充到余额上限或试卡上限为止。
      </div>
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
import { getAccounts, getAccountCards, exportAccounts, deleteAccounts, importAccounts, rechargeAccount, openAccountBrowser, getOpenBrowsers, getRechargeLogsByEmail, getCardGroups, archiveAccounts, unarchiveAccounts, resetAccountsToImported } from '../api'
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
const filters = reactive({ keyword: '', identity_status: '', platform_status: '', date_from: '', date_to: '' })
const selected = reactive(new Set())
const importInput = ref(null)
const importMsg = ref('')

// 归档页签。scope 会随请求发给后端，由它决定 WHERE 条件与分页总数——
// 纯前端切当前页的数据是错的：已归档账号散布在各页，那样「已归档」页签只会
// 显示本页碰巧命中的那几个，而分页器还按全量总数在翻。
// 「在用」只认 registered。它曾经是「除了已归档的全部」，于是注册失败、仅导入、
// 已封禁的账号都被算成在用——那口径回答的是「没被手动归档过」，而不是「还能跑」。
// 五个页签互不重叠且覆盖全部状态，后端 ACCOUNT_SCOPE_STATUSES 是同一份定义。
const SCOPE_TABS = [
  { key: 'all', label: '全部', hint: '不加身份状态过滤' },
  { key: 'active', label: '在用', hint: '已注册且未归档，能参与任务' },
  { key: 'pending', label: '待处理', hint: '仅导入 / 待处理，尚未跑出注册结果' },
  { key: 'abnormal', label: '异常', hint: '注册失败、封禁、挂起、拒绝、GitHub 受限' },
  { key: 'retired', label: '已归档', hint: '已核销，一律跳过所有任务' },
]
// 每个页签下可选的身份状态；空数组表示该页签只有一种状态，下拉没有意义
const SCOPE_IDENTITY_OPTIONS = {
  all: ['imported', 'registered', 'pending', 'failed', 'suspended', 'rejected', 'flagged', 'banned', 'retired'],
  active: [],
  pending: ['imported', 'pending'],
  abnormal: ['failed', 'banned', 'suspended', 'rejected', 'flagged'],
  retired: [],
}
const scope = ref('all')
const scopeCounts = ref({ all: 0, active: 0, pending: 0, abnormal: 0, retired: 0 })
const identityOptions = computed(() => SCOPE_IDENTITY_OPTIONS[scope.value] || [])

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

// 身份层（GitHub 注册与封禁结果，跨平台一致）与平台层（该账号在某平台的业务状态）
// 共用这张映射表——两层的取值集合不重叠，不会撞。
const statusMap = {
  // 身份层
  imported: '仅导入',
  registered: '已注册',
  pending: '待处理',
  failed: '注册失败',
  suspended: '已挂起',
  rejected: '已拒绝',
  flagged: 'GitHub受限',       // GitHub 反滥用 flag，无法授权第三方 OAuth，所有平台通吃
  banned: '已封禁',
  // 用户在后台主动归档：账号本身没坏，是人决定不再用它。底层值刻意不叫 archived——
  // 那个已经是平台层的「余额充满」，而这张表两层共用，同名就没法区分了。
  // 文案带上「已核销」与充值报表的术语对齐，也顺带把它和下面平台层的「已归档」区分开。
  retired: '已核销（已归档）',
  // 平台层
  archived: '已归档',          // 余额≥阈值，该平台的充值跳过
  subscribed: '已订阅',
  recharged: '已充值',
}
function accStatusLabel(s) {
  return statusMap[s] || s || '-'
}

function money(v) {
  return Number(v || 0).toFixed(2)
}

// 当前页的充值金额合计，按「已核销 / 在用」拆两组。
// 只覆盖 accounts 数组里的行——它就是页面上看得见的那些，合计与表格永远对得上。
const pageSum = computed(() => {
  const s = {
    today: 0, total: 0,
    retiredToday: 0, retiredTotal: 0, retiredCount: 0,
    activeToday: 0, activeTotal: 0,
  }
  for (const a of accounts.value) {
    const t = Number(a.recharge_today || 0)
    const all = Number(a.recharge_total || 0)
    s.today += t
    s.total += all
    if (a.identity_status === 'retired') {
      s.retiredCount++
      s.retiredToday += t
      s.retiredTotal += all
    } else {
      s.activeToday += t
      s.activeTotal += all
    }
  }
  return s
})
function accStatusClass(s) {
  if (s === 'banned' || s === 'flagged' || s === 'suspended' || s === 'rejected') return 'fail'
  if (s === 'registered') return 'success'
  if (s === 'subscribed' || s === 'recharged') return 'success'
  if (s === 'failed') return 'fail'
  if (s === 'archived' || s === 'pending' || s === 'imported' || s === 'retired') return 'warn'
  return ''
}

async function loadData() {
  loading.value = true
  try {
    const params = { page: page.value, page_size: pageSize.value, scope: scope.value }
    if (filters.keyword) params.keyword = filters.keyword
    if (filters.identity_status) params.identity_status = filters.identity_status
    if (filters.platform_status) params.platform_status = filters.platform_status
    if (filters.date_from) params.date_from = filters.date_from
    if (filters.date_to) params.date_to = filters.date_to

    const result = await getAccounts(params)
    accounts.value = result.data || []
    total.value = result.total || 0
    if (result.scope_counts) scopeCounts.value = result.scope_counts
  } catch (e) {
    console.error(e)
  } finally {
    loading.value = false
  }
}

// 切页签必须回到第 1 页：在「全部」的第 7 页切到只有 3 个账号的「已归档」，
// 保留页码会落到空白页，看起来就像归档账号全丢了。
//
// 身份状态下拉也要清空：它的取值范围随页签变，留着上一个页签选的值（比如从「异常」
// 带着 banned 切到「待处理」）会和页签条件求交集，得到一个空列表——而那时下拉已经
// 不显示该选项了，用户在界面上找不到任何东西能解释这个空列表。
function switchScope(key) {
  if (scope.value === key) return
  scope.value = key
  filters.identity_status = ''
  page.value = 1
  loadData()
}

function resetFilter() {
  filters.keyword = ''
  filters.identity_status = ''
  filters.platform_status = ''
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

async function handleImport() {
  const file = importInput.value?.files?.[0]
  if (!file) { alert('请先选择 Excel 文件'); return }
  importMsg.value = '<span style="color:#666">导入解析中...</span>'
  try {
    const d = await importAccounts(file)
    let html = `<span style="color:green">已导入 ${d.imported} 个账号（状态「仅导入」，下次任务会自动注册）</span>`
    // 缺认证链接的必须单独提示：这些账号入了库却领不走（注册流程拿不到验证码），
    // 只报「导入成功」的话，用户会困惑于补号流程为什么一个都不碰。
    if (d.no_link_count > 0) {
      html += `<br><span style="color:#dc2626">其中 ${d.no_link_count} 个没有邮箱认证链接，`
        + `无法自动注册（注册要靠它收验证码），请补齐后重新导入</span>`
    }
    if (d.errors?.length) {
      html += `<br><span style="color:orange">${d.errors.length} 行有问题被跳过：`
        + `${d.errors.slice(0, 3).join('；')}${d.errors.length > 3 ? ' …' : ''}</span>`
    }
    importMsg.value = html
    await loadData()
  } catch (e) {
    importMsg.value = `<span style="color:red">导入失败: ${e.message}</span>`
  }
}

// 环境没释放干净必须弹出来说：静默的话用户会以为配额已经腾出来了，
// 直到下一次跑任务报「配额已满」才发现，那时根本联想不到是删除操作没做完。
function reportAdsPower(ads) {
  if (!ads) return
  const parts = []
  if (ads.skipped_busy?.length) {
    parts.push(`以下账号正在运行，其 AdsPower 环境暂未删除（跑完后会自动回收）：\n${ads.skipped_busy.join('、')}`)
  }
  if (ads.failed?.length) {
    parts.push(`以下账号的 AdsPower 环境删除失败，仍占用配额：\n${ads.failed.join('、')}`)
  }
  if (!parts.length) return
  alert(parts.join('\n\n') + (ads.reason ? `\n\n原因：${ads.reason}` : ''))
}

async function handleDelete() {
  if (selected.size === 0) return
  const count = selected.size
  if (!confirm(`确定要删除选中的 ${count} 个账号吗？\n\n此操作将永久删除账号及其关联的卡片绑定记录，并删除其对应的 AdsPower 浏览器环境以释放配额，不可恢复！`)) return
  try {
    const res = await deleteAccounts(Array.from(selected))
    selected.clear()
    await loadData()
    reportAdsPower(res?.adspower)
  } catch (e) {
    alert('删除失败: ' + e.message)
  }
}

async function handleArchive() {
  if (selected.size === 0) return
  const count = selected.size
  if (!confirm(
    `确定要归档选中的 ${count} 个账号吗？\n\n` +
    `归档后该账号不再参与任何任务（充值、订阅、注册补号一律跳过），\n` +
    `并会同步删除它的 AdsPower 浏览器环境以释放配额。\n\n` +
    `可以在列表里点「取消归档」恢复，但环境已删除，恢复后首次运行\n` +
    `需要重新登录 GitHub（会触发一次新设备邮箱验证）。`
  )) return
  try {
    const res = await archiveAccounts(Array.from(selected))
    selected.clear()
    await loadData()
    alert(`已归档 ${res?.retired ?? 0} 个账号`)
    reportAdsPower(res?.adspower)
  } catch (e) {
    alert('归档失败: ' + e.message)
  }
}

async function handleUnarchive(email) {
  if (!confirm(
    `确定要取消归档 ${email} 吗？\n\n` +
    `它会恢复成归档前的身份状态（例如归档前是「已封禁」就仍是已封禁，\n` +
    `不会因为取消归档就变成可用）。归档时环境已删除，若恢复后能参与任务，\n` +
    `首次运行需要重新登录 GitHub（会触发一次新设备邮箱验证）。`
  )) return
  try {
    const res = await unarchiveAccounts([email])
    await loadData()
    if (!res?.restored) alert('没有账号被恢复（它可能不是已归档状态）')
  } catch (e) {
    alert('取消归档失败: ' + e.message)
  }
}

async function handleResetImported() {
  if (selected.size === 0) return
  if (!confirm(
    `确定要把选中的 ${selected.size} 个账号重置为「仅导入」吗？\n\n` +
    `重置后它们会在下一轮任务里重新注册 GitHub。\n` +
    `只有「注册失败」和「待处理」状态的账号会被重置，其余自动跳过。`
  )) return
  try {
    const res = await resetAccountsToImported(Array.from(selected))
    selected.clear()
    await loadData()
    reportReset(res)
  } catch (e) {
    alert('重置失败: ' + e.message)
  }
}

// 分类回显：用户选了 38 个只重置了 12 个时，必须能当场看出另外 26 个为什么没动，
// 否则会以为功能坏了。
function reportReset(res) {
  const parts = [`已重置 ${res?.reset?.length ?? 0} 个账号为「仅导入」`]
  const badStatus = res?.skipped_status || []
  const noMailbox = res?.skipped_no_mailbox || []
  if (badStatus.length) {
    const detail = badStatus.slice(0, 5)
      .map(a => `${a.email}（${statusMap[a.status] || a.status || '未知'}）`).join('\n')
    parts.push(`跳过 ${badStatus.length} 个：状态不是「注册失败/待处理」\n${detail}` +
      (badStatus.length > 5 ? `\n... 另有 ${badStatus.length - 5} 个` : ''))
  }
  if (noMailbox.length) {
    parts.push(
      `跳过 ${noMailbox.length} 个：没有收信链接，重置了也领不走\n` +
      noMailbox.slice(0, 5).join('\n') +
      (noMailbox.length > 5 ? `\n... 另有 ${noMailbox.length - 5} 个` : ''))
  }
  alert(parts.join('\n\n'))
}

async function handleDeleteOne(email) {
  if (!confirm(`确定要删除账号 ${email} 吗？\n\n此操作将永久删除该账号及其关联的卡片绑定记录，并删除其对应的 AdsPower 浏览器环境以释放配额，不可恢复！`)) return
  try {
    const res = await deleteAccounts([email])
    selected.delete(email)
    await loadData()
    reportAdsPower(res?.adspower)
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
    body.identity_status = filters.identity_status
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
.page-sum-bar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 20px;
  padding: 10px 20px;
  background: #fcfcfd;
  border-bottom: 1px solid var(--border);
  font-size: 12px;
  color: var(--text-sub);
}
.page-sum-title { font-weight: 600; color: var(--text-main); }
.page-sum-item b { color: var(--text-main); font-family: monospace; }
.page-sum-item b.green { color: var(--success); }
.page-sum-sep {
  width: 1px;
  height: 14px;
  background: var(--border);
}
.amount-today { color: var(--success); font-weight: 600; }

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

.scope-tabs {
  display: flex;
  gap: 4px;
  padding: 0 20px;
  border-bottom: 1px solid var(--border);
  background: #fff;
}
.scope-tab {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 11px 16px;
  background: none;
  border: none;
  /* 选中态靠这条底边表示，未选中给等宽透明边，避免切换时文字上下跳 1px */
  border-bottom: 2px solid transparent;
  margin-bottom: -1px;
  color: var(--text-sub);
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: color 0.15s, border-color 0.15s;
}
.scope-tab:hover { color: var(--text-main); }
.scope-tab.active {
  color: var(--primary);
  border-bottom-color: var(--primary);
}
.scope-tab-count {
  padding: 1px 7px;
  border-radius: 10px;
  background: #f1f5f9;
  color: var(--text-sub);
  font-size: 11px;
  font-family: 'JetBrains Mono', monospace;
}
.scope-tab.active .scope-tab-count {
  background: #ffedd5;
  color: var(--primary);
}

/* 已核销行压暗，仅在「全部」页签下起区分作用；「已归档」页签下整页都是它，不刺眼 */
.retired-row { opacity: 0.65; }
</style>
