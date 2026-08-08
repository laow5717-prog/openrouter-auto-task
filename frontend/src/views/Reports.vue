<template>
  <!-- 今日 KPI：固定看「今天」，不随下方区间筛选变化（后端 today 段独立于区间） -->
  <div class="stats-grid">
    <div class="stat-card">
      <div class="stat-label">今日充值金额</div>
      <div class="stat-value green">${{ fmt(report.today.amount) }}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">今日成功笔数</div>
      <div class="stat-value blue">{{ report.today.success_count }}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">今日使用卡片</div>
      <div class="stat-value">{{ report.today.card_count }}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">今日充值账号</div>
      <div class="stat-value">{{ report.today.account_count }}</div>
    </div>
  </div>

  <div class="panel">
    <div class="panel-header">
      <div class="panel-title"><span>&#128202;</span> 充值报表</div>
      <div style="display:flex;gap:8px;align-items:center">
        <span class="range-hint">{{ report.date_from }} ~ {{ report.date_to }} · {{ platformName }}</span>
        <button class="action-btn" @click="loadData">刷新</button>
      </div>
    </div>

    <FilterBar>
      <input type="date" v-model="filters.date_from" class="filter-date" title="开始日期">
      <span style="font-size:12px;color:var(--text-sub)">至</span>
      <input type="date" v-model="filters.date_to" class="filter-date" title="结束日期">
      <button class="filter-btn filter-btn-primary" @click="loadData">查询</button>
      <button class="filter-btn filter-btn-reset" @click="resetFilter">重置</button>
      <span style="font-size:12px;color:var(--text-sub)">默认展示最近 30 天</span>
    </FilterBar>

    <!-- 区间汇总 -->
    <div class="summary-bar">
      <div class="sum-item">
        <span class="sum-label">区间总金额</span>
        <span class="sum-value green">${{ fmt(report.summary.total_amount) }}</span>
      </div>
      <div class="sum-item">
        <span class="sum-label">成功 / 失败笔数</span>
        <span class="sum-value">
          <b class="green">{{ report.summary.success_count }}</b> /
          <b class="red">{{ report.summary.failed_count }}</b>
        </span>
      </div>
      <div class="sum-item">
        <span class="sum-label">成功率</span>
        <span class="sum-value">{{ report.summary.success_rate }}%</span>
      </div>
      <div class="sum-item">
        <span class="sum-label" :title="CARD_COUNT_HINT">使用卡片数</span>
        <span class="sum-value">{{ report.summary.card_count }}</span>
      </div>
      <div class="sum-item">
        <span class="sum-label">涉及账号数</span>
        <span class="sum-value">{{ report.summary.account_count }}</span>
      </div>
      <div class="sum-sep"></div>
      <div class="sum-item">
        <span class="sum-label">已核销账号贡献</span>
        <span class="sum-value">
          ${{ fmt(report.verified.amount) }}
          <em class="sum-sub">（{{ report.verified.account_count }} 个）</em>
        </span>
      </div>
      <div class="sum-item">
        <span class="sum-label">在用账号贡献</span>
        <span class="sum-value">
          ${{ fmt(report.active.amount) }}
          <em class="sum-sub">（{{ report.active.account_count }} 个）</em>
        </span>
      </div>
    </div>

    <!-- 每日趋势 -->
    <div style="overflow-x:auto">
      <table class="log-table">
        <thead>
          <tr>
            <th style="white-space:nowrap">日期</th>
            <th style="width:26%">充值金额</th>
            <th style="white-space:nowrap">成功</th>
            <th style="white-space:nowrap">失败</th>
            <th style="white-space:nowrap">成功率</th>
            <th style="white-space:nowrap" :title="CARD_COUNT_HINT">使用卡片</th>
            <th style="white-space:nowrap">充值账号</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="loading">
            <td colspan="7" class="table-loading">加载中...</td>
          </tr>
          <tr v-else-if="report.daily.length === 0">
            <td colspan="7" class="table-empty">该区间内没有充值记录</td>
          </tr>
          <tr v-for="row in report.daily" :key="row.date">
            <td style="white-space:nowrap;font-family:monospace">{{ row.date }}</td>
            <td>
              <div class="bar-cell">
                <div class="bar-track">
                  <div class="bar-fill" :style="{ width: barWidth(row.amount) }"></div>
                </div>
                <span class="bar-amount">${{ fmt(row.amount) }}</span>
              </div>
            </td>
            <td class="green">{{ row.success_count }}</td>
            <td class="red">{{ row.failed_count }}</td>
            <td>{{ dayRate(row) }}%</td>
            <td>{{ row.card_count }}</td>
            <td>{{ row.account_count }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- 账号榜单 -->
  <div class="panel" style="margin-top:24px">
    <div class="panel-header">
      <div class="panel-title"><span>&#128100;</span> 账号充值排行</div>
      <span class="range-hint">
        共 {{ report.accounts_total }} 个账号<span v-if="truncated">，仅展示金额前 {{ report.accounts.length }} 名</span>
      </span>
    </div>
    <div style="overflow-x:auto">
      <table class="log-table">
        <thead>
          <tr>
            <th>账号邮箱</th>
            <th style="white-space:nowrap">核销状态</th>
            <th style="white-space:nowrap">充值金额</th>
            <th style="white-space:nowrap">成功笔数</th>
            <th style="white-space:nowrap" :title="CARD_COUNT_HINT">使用卡片</th>
            <th style="white-space:nowrap">最后充值</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="loading">
            <td colspan="6" class="table-loading">加载中...</td>
          </tr>
          <tr v-else-if="report.accounts.length === 0">
            <td colspan="6" class="table-empty">该区间内没有充值记录</td>
          </tr>
          <tr v-for="row in report.accounts" :key="row.email">
            <td>{{ row.email }}</td>
            <td>
              <span v-if="row.is_verified" class="status-tag bound">已核销</span>
              <span v-else class="status-tag success">在用</span>
            </td>
            <td class="green" style="font-family:monospace">${{ fmt(row.amount) }}</td>
            <td>{{ row.success_count }}</td>
            <td>{{ row.card_count }}</td>
            <td style="white-space:nowrap;font-size:12px">{{ row.last_at || '-' }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, watch } from 'vue'
import { getRechargeReport } from '../api'
import { useAppStore } from '../stores/app'
import FilterBar from '../components/FilterBar.vue'

// 历史上 card_display 的写入格式不统一（完整卡号 / 脱敏 '•••• 1234'），报表按完整串去重，
// 所以早期日期里同一张卡的两种写法会被算成两张。这句话挂在三处「使用卡片」的 title 上。
const CARD_COUNT_HINT =
  '按完整卡号去重。早期记录中存在脱敏卡号（•••• 1234），无法与完整卡号还原为同一张，可能使早期日期的卡片数偏大'

const store = useAppStore()
const loading = ref(false)
const filters = reactive({ date_from: '', date_to: '' })

const EMPTY = () => ({
  date_from: '', date_to: '',
  today: { amount: 0, success_count: 0, card_count: 0, account_count: 0 },
  summary: { total_amount: 0, success_count: 0, failed_count: 0, success_rate: 0, card_count: 0, account_count: 0 },
  verified: { amount: 0, account_count: 0 },
  active: { amount: 0, account_count: 0 },
  daily: [],
  accounts: [],
  accounts_total: 0,
})
// 用一个完整形状的初始值，而不是 ref(null) + 满模板的可选链：
// 首帧和请求失败时模板读的都是这份零值，不会有一屏 undefined
const report = ref(EMPTY())

const platformName = computed(() => {
  const p = store.platforms.find((x) => x.slug === store.platform)
  return p ? p.display_name : store.platform
})

const truncated = computed(() => report.value.accounts_total > report.value.accounts.length)

// 柱长以区间内最大日金额为满格。maxAmount 为 0 时直接给 0 宽——
// 除零会得到 NaN%，那个值会让整条 style 失效、柱子反而铺满整格
const maxAmount = computed(() =>
  report.value.daily.reduce((m, r) => Math.max(m, r.amount || 0), 0))

function barWidth(amount) {
  if (!maxAmount.value) return '0%'
  return `${Math.max((amount / maxAmount.value) * 100, 1.5)}%`
}

function dayRate(row) {
  const attempts = row.success_count + row.failed_count
  return attempts ? Math.round((row.success_count / attempts) * 1000) / 10 : 0
}

function fmt(v) {
  return Number(v || 0).toFixed(2)
}

async function loadData() {
  loading.value = true
  try {
    const params = {}
    if (filters.date_from) params.date_from = filters.date_from
    if (filters.date_to) params.date_to = filters.date_to
    const data = await getRechargeReport(params)
    report.value = { ...EMPTY(), ...data }
    // 回填服务端补齐的默认区间，让日期框显示的就是当前实际口径
    filters.date_from = data.date_from || ''
    filters.date_to = data.date_to || ''
  } catch (e) {
    console.error(e)
  } finally {
    loading.value = false
  }
}

function resetFilter() {
  // 清空后重新请求，由服务端补回最近 30 天——默认区间只在后端定义一处
  filters.date_from = ''
  filters.date_to = ''
  loadData()
}

// 平台切换必须重新拉取：整份报表都按 platform 过滤，不重拉会把上一个平台的
// 数字一直挂在屏幕上，而页面上没有任何迹象表明它已经过期
watch(() => store.platform, loadData)

onMounted(loadData)
</script>

<style scoped>
/* 与 RechargeLogs 的表格密度保持一致（全局 th/td 是 16px 24px，报表列多会撑爆） */
.log-table th,
.log-table td {
  padding: 10px 14px;
  font-size: 13px;
}

.range-hint {
  font-size: 12px;
  color: var(--text-sub);
}

.summary-bar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 28px;
  padding: 16px 24px;
  border-bottom: 1px solid var(--border);
  background: #fcfcfd;
}
.sum-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.sum-label {
  font-size: 12px;
  color: var(--text-sub);
}
.sum-value {
  font-size: 16px;
  font-weight: 600;
  color: var(--text-main);
}
.sum-sub {
  font-style: normal;
  font-size: 12px;
  font-weight: 400;
  color: var(--text-sub);
}
.sum-sep {
  width: 1px;
  align-self: stretch;
  background: var(--border);
}

.bar-cell {
  display: flex;
  align-items: center;
  gap: 10px;
}
.bar-track {
  flex: 1;
  min-width: 60px;
  height: 8px;
  background: #f1f5f9;
  border-radius: 4px;
  overflow: hidden;
}
.bar-fill {
  height: 100%;
  background: var(--primary);
  border-radius: 4px;
  transition: width 0.3s;
}
.bar-amount {
  font-family: monospace;
  font-size: 13px;
  white-space: nowrap;
  min-width: 72px;
  text-align: right;
}

.green { color: var(--success); }
.red { color: var(--danger); }
</style>
