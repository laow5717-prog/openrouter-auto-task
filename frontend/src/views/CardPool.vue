<template>
  <!-- 说明区 -->
  <div class="info-banner">
    <strong>卡片分组说明：</strong>
    <span class="info-tag bind">绑定卡</span> 用于注册账号后绑定信用卡到 Cloudflare 账号，
    <span class="info-tag payment">在线支付卡</span> 用于通过账单记录的 PDF 跳转在线支付页面时使用。
    每天可上传多批卡片数据作为底料，同一分组内按卡号自动去重。
  </div>

  <!-- 统计 -->
  <div class="stats-grid">
    <div class="stat-card">
      <div class="stat-label">分组总数</div>
      <div class="stat-value">{{ groups.length }}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">绑定卡分组</div>
      <div class="stat-value blue">{{ groups.filter(g => g.type === 'bind').length }}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">支付卡分组</div>
      <div class="stat-value green">{{ groups.filter(g => g.type === 'payment').length }}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">有效卡</div>
      <div class="stat-value" style="color:#f59e0b">{{ validSummary.total }}</div>
    </div>
  </div>

  <!-- 分组管理 -->
  <div class="panel" style="margin-bottom:16px">
    <div class="panel-header">
      <div class="panel-title"><span>&#128451;</span> 卡片分组</div>
      <div style="display:flex;gap:8px">
        <button class="action-btn" @click="showCreateGroup">新建分组</button>
        <button class="action-btn" @click="showValidCards">查看有效卡</button>
        <button class="action-btn" @click="loadGroups">刷新</button>
      </div>
    </div>

    <div v-if="groups.length === 0" style="padding:32px;text-align:center;color:var(--text-sub)">
      暂无分组，请点击「新建分组」创建
    </div>

    <div v-else class="group-grid">
      <div v-for="g in groups" :key="g.id" class="group-card" :class="g.type" @click="selectGroup(g)">
        <div class="group-card-header">
          <span class="group-type-tag" :class="g.type">{{ g.type === 'bind' ? '绑定卡' : '支付卡' }}</span>
          <div class="group-actions">
            <button class="mini-btn" @click.stop="editGroup(g)">编辑</button>
            <button class="mini-btn danger" @click.stop="deleteGroup(g)">删除</button>
          </div>
        </div>
        <div class="group-card-name">{{ g.name }}</div>
        <div class="group-card-count">{{ g.card_count }} 张卡</div>
        <div v-if="g.description" class="group-card-desc">{{ g.description }}</div>
      </div>
    </div>
  </div>

  <!-- 选中分组的卡片列表 -->
  <div v-if="selectedGroup" class="panel">
    <div class="panel-header">
      <div class="panel-title">
        <span>&#128179;</span>
        {{ selectedGroup.name }}
        <span class="group-type-tag small" :class="selectedGroup.type">{{ selectedGroup.type === 'bind' ? '绑定卡' : '支付卡' }}</span>
      </div>
      <div style="display:flex;gap:8px;align-items:center">
        <input type="file" ref="fileInput" accept=".xlsx,.xls" style="font-size:12px;max-width:200px">
        <button class="action-btn" @click="handleUpload">上传卡片</button>
        <a href="/api/card/template" class="action-btn" style="text-decoration:none">下载模版</a>
        <button class="action-btn danger" @click="handleClearPool">清空</button>
      </div>
    </div>

    <div v-if="uploadMsg" style="padding:8px 16px;font-size:12px" v-html="uploadMsg"></div>

    <div style="overflow-x:auto">
      <table>
        <thead>
          <tr>
            <th>#</th>
            <th>卡号</th>
            <th>有效期</th>
            <th>CVC</th>
            <th>持卡人</th>
            <th>国家</th>
            <th>状态</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="poolLoading">
            <td colspan="8" class="table-loading">加载中...</td>
          </tr>
          <tr v-else-if="poolCards.length === 0">
            <td colspan="8" class="table-empty">暂无卡片数据，请上传 Excel 文件</td>
          </tr>
          <tr v-for="card in poolCards" :key="card.id">
            <td>{{ card.id }}</td>
            <td style="font-family:monospace">{{ card.card_number }}</td>
            <td :class="{ 'expired-cell': card.status === 'expired' }">
              {{ card.expiry_month }}/{{ card.expiry_year }}
            </td>
            <td>{{ card.cvc }}</td>
            <td>{{ card.first_name }} {{ card.last_name }}</td>
            <td>{{ card.country }}</td>
            <td>
              <span v-if="card.status === 'expired'" class="status-tag fail">已过期</span>
              <span v-else-if="card.status === 'invalid'" class="status-tag fail">无效</span>
              <span v-else-if="card.is_valid" class="status-tag success">有效</span>
              <span v-else class="status-tag">待验证</span>
            </td>
            <td>
              <button class="mini-btn danger" @click="deleteCard(card.id)">删除</button>
            </td>
          </tr>
        </tbody>
      </table>
        <div v-if="!poolCards || !poolCards.length" class="empty-state">暂无数据</div>
    </div>

    <Pagination :total="poolTotal" :page="poolPage" :page-size="poolPageSize"
      @change="p => { poolPage = p; loadPoolCards() }"
      @update:page-size="s => { poolPageSize = s }" />
  </div>

  <!-- 新建/编辑分组弹窗 -->
  <Modal :visible="groupModal.visible" :title="groupModal.editing ? '编辑分组' : '新建分组'" @close="groupModal.visible = false">
    <div class="form-group">
      <label class="form-label">分组名称</label>
      <input v-model="groupModal.name" class="ctrl-input" placeholder="如：日常绑定卡、支付专用卡">
    </div>
    <div v-if="!groupModal.editing" class="form-group">
      <label class="form-label">分组类型</label>
      <select v-model="groupModal.type" class="ctrl-input">
        <option value="bind">绑定卡 - 用于注册后绑卡到 CF 账号</option>
        <option value="payment">在线支付卡 - 用于 PDF 在线支付</option>
      </select>
    </div>
    <div class="form-group">
      <label class="form-label">备注说明（可选）</label>
      <input v-model="groupModal.description" class="ctrl-input" placeholder="备注信息">
    </div>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:16px">
      <button class="btn" style="width:auto;padding:8px 20px" @click="groupModal.visible = false">取消</button>
      <button class="btn btn-primary" style="width:auto;padding:8px 20px" @click="saveGroup">保存</button>
    </div>
  </Modal>

  <!-- 有效卡弹窗 -->
  <Modal :visible="validModal.visible" title="有效卡列表" @close="validModal.visible = false" wide>
    <div class="stats-grid" style="margin-bottom:12px">
      <div class="stat-card small">
        <div class="stat-label">总计</div>
        <div class="stat-value">{{ validSummary.total }}</div>
      </div>
      <div class="stat-card small">
        <div class="stat-label">绑定验证</div>
        <div class="stat-value blue">{{ validSummary.bind_count }}</div>
      </div>
      <div class="stat-card small">
        <div class="stat-label">支付验证</div>
        <div class="stat-value green">{{ validSummary.payment_count }}</div>
      </div>
    </div>
    <FilterBar>
      <input v-model="validFilters.keyword" class="filter-input" placeholder="搜索卡号/邮箱..." style="width:180px">
      <select v-model="validFilters.source_type" class="filter-select">
        <option value="">全部来源</option>
        <option value="bind">绑定验证</option>
        <option value="payment">支付验证</option>
      </select>
      <button class="filter-btn filter-btn-primary" @click="validPage = 1; loadValidCards()">查询</button>
      <button class="filter-btn filter-btn-reset" @click="validFilters.keyword = ''; validFilters.source_type = ''; validPage = 1; loadValidCards()">重置</button>
    </FilterBar>
    <div style="overflow-x:auto">
      <table>
        <thead>
          <tr>
            <th>卡号</th>
            <th>有效期</th>
            <th>持卡人</th>
            <th>来源</th>
            <th>关联账号</th>
            <th>验证时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="validLoading">
            <td colspan="6" class="table-loading">加载中...</td>
          </tr>
          <tr v-else-if="validCards.length === 0">
            <td colspan="6" class="table-empty">暂无有效卡记录</td>
          </tr>
          <tr v-for="c in validCards" :key="c.id">
            <td style="font-family:monospace">{{ maskCard(c.card_number) }}</td>
            <td>{{ c.expiry_month }}/{{ c.expiry_year }}</td>
            <td>{{ c.first_name }} {{ c.last_name }}</td>
            <td>
              <span class="status-tag" :class="c.source_type === 'bind' ? '' : 'success'">
                {{ c.source_type === 'bind' ? '绑定' : '支付' }}
              </span>
            </td>
            <td>{{ c.source_email || '-' }}</td>
            <td style="font-size:11px">{{ c.validated_at }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <Pagination :total="validTotal" :page="validPage" :page-size="20"
      @change="p => { validPage = p; loadValidCards() }" />
  </Modal>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import {
  getCardGroups, createCardGroup, updateCardGroup, deleteCardGroup,
  getCardPool, uploadCardPool, deletePoolCard, clearCardPool,
  getValidCards,
} from '../api'
import FilterBar from '../components/FilterBar.vue'
import Pagination from '../components/Pagination.vue'
import Modal from '../components/Modal.vue'

const groups = ref([])
const selectedGroup = ref(null)

// Pool cards
const poolCards = ref([])
const poolTotal = ref(0)
const poolPage = ref(1)
const poolPageSize = ref(20)
const poolLoading = ref(false)
const uploadMsg = ref('')
const fileInput = ref(null)

// Group modal
const groupModal = reactive({ visible: false, editing: false, id: null, name: '', type: 'bind', description: '' })

// Valid cards
const validModal = reactive({ visible: false })
const validCards = ref([])
const validTotal = ref(0)
const validPage = ref(1)
const validLoading = ref(false)
const validSummary = reactive({ total: 0, bind_count: 0, payment_count: 0 })
const validFilters = reactive({ keyword: '', source_type: '' })

function maskCard(num) {
  if (!num || num.length < 8) return num
  return num.slice(0, 4) + ' **** **** ' + num.slice(-4)
}

async function loadGroups() {
  try {
    groups.value = await getCardGroups()
  } catch (e) { console.error(e) }
}

function selectGroup(g) {
  selectedGroup.value = g
  poolPage.value = 1
  loadPoolCards()
}

async function loadPoolCards() {
  if (!selectedGroup.value) return
  poolLoading.value = true
  try {
    const data = await getCardPool(selectedGroup.value.id, { page: poolPage.value, page_size: poolPageSize.value })
    poolCards.value = data.data || []
    poolTotal.value = data.total || 0
  } catch (e) { console.error(e) } finally { poolLoading.value = false }
}

function showCreateGroup() {
  groupModal.editing = false
  groupModal.id = null
  groupModal.name = ''
  groupModal.type = 'bind'
  groupModal.description = ''
  groupModal.visible = true
}

function editGroup(g) {
  groupModal.editing = true
  groupModal.id = g.id
  groupModal.name = g.name
  groupModal.type = g.type
  groupModal.description = g.description || ''
  groupModal.visible = true
}

async function saveGroup() {
  if (!groupModal.name.trim()) { alert('请输入分组名称'); return }
  try {
    if (groupModal.editing) {
      await updateCardGroup(groupModal.id, { name: groupModal.name, description: groupModal.description })
    } else {
      await createCardGroup({ name: groupModal.name, type: groupModal.type, description: groupModal.description })
    }
    groupModal.visible = false
    await loadGroups()
  } catch (e) { alert('保存失败: ' + e.message) }
}

async function deleteGroup(g) {
  if (!confirm(`确定删除分组「${g.name}」吗？\n分组内的所有卡片数据也会被删除！`)) return
  try {
    await deleteCardGroup(g.id)
    if (selectedGroup.value?.id === g.id) selectedGroup.value = null
    await loadGroups()
  } catch (e) { alert('删除失败: ' + e.message) }
}

async function handleUpload() {
  const file = fileInput.value?.files?.[0]
  if (!file) { alert('请先选择 Excel 文件'); return }
  uploadMsg.value = '<span style="color:#666">上传解析中...</span>'
  try {
    const data = await uploadCardPool(selectedGroup.value.id, file)
    let html = `<span style="color:green">导入成功: ${data.added} 张新卡</span>`
    if (data.skipped > 0) html += `<span style="color:orange;margin-left:8px">${data.skipped} 张同分组重复已跳过</span>`
    if (data.conflicts?.length) {
      const grouped = {}
      data.conflicts.forEach(c => { grouped[c.group_name] = (grouped[c.group_name] || 0) + 1 })
      const detail = Object.entries(grouped).map(([name, cnt]) => `「${name}」${cnt}张`).join('、')
      html += `<br><span style="color:#dc2626">${data.conflicts.length} 张卡已在其他分组中（${detail}），已跳过</span>`
    }
    if (data.expired > 0) html += `<br><span style="color:#dc2626">${data.expired} 张卡有效期已过期，已标记为无效（不参与任务）</span>`
    if (data.errors?.length) html += `<br><span style="color:orange">${data.errors.length} 条数据有问题被跳过</span>`
    uploadMsg.value = html
    await loadGroups()
    await loadPoolCards()
  } catch (e) {
    uploadMsg.value = `<span style="color:red">失败: ${e.message}</span>`
  }
}

async function deleteCard(cardId) {
  if (!confirm('确定删除这张卡片？')) return
  try {
    await deletePoolCard(cardId)
    await loadPoolCards()
    await loadGroups()
  } catch (e) { alert('删除失败: ' + e.message) }
}

async function handleClearPool() {
  if (!confirm(`确定清空分组「${selectedGroup.value.name}」的所有卡片？`)) return
  try {
    await clearCardPool(selectedGroup.value.id)
    await loadPoolCards()
    await loadGroups()
  } catch (e) { alert('清空失败: ' + e.message) }
}

async function showValidCards() {
  validModal.visible = true
  validPage.value = 1
  await loadValidCards()
}

async function loadValidCards() {
  validLoading.value = true
  try {
    const params = { page: validPage.value, page_size: 20 }
    if (validFilters.keyword) params.keyword = validFilters.keyword
    if (validFilters.source_type) params.source_type = validFilters.source_type
    const data = await getValidCards(params)
    validCards.value = data.data || []
    validTotal.value = data.total || 0
    if (data.summary) Object.assign(validSummary, data.summary)
  } catch (e) { console.error(e) } finally { validLoading.value = false }
}

onMounted(() => {
  loadGroups()
  // 预加载有效卡统计
  getValidCards({ page: 1, page_size: 1 }).then(data => {
    if (data.summary) Object.assign(validSummary, data.summary)
  }).catch(() => {})
})
</script>

<style scoped>
.info-banner {
  padding: 12px 16px;
  margin-bottom: 16px;
  background: #eff6ff;
  border: 1px solid #bfdbfe;
  border-radius: 8px;
  font-size: 13px;
  color: #1e40af;
  line-height: 1.6;
}
.info-tag {
  display: inline-block;
  padding: 1px 8px;
  border-radius: 4px;
  font-size: 12px;
  font-weight: 600;
}
.info-tag.bind { background: #dbeafe; color: #1e40af; }
.info-tag.payment { background: #dcfce7; color: #166534; }

.group-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 12px;
  padding: 16px;
}
.group-card {
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 14px 16px;
  cursor: pointer;
  transition: all 0.15s;
}
.group-card:hover { border-color: var(--primary); box-shadow: 0 2px 8px rgba(0,0,0,0.06); }
.group-card.bind { border-left: 3px solid #3b82f6; }
.group-card.payment { border-left: 3px solid #22c55e; }
.group-card-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.group-card-name { font-size: 15px; font-weight: 600; margin-bottom: 4px; }
.group-card-count { font-size: 13px; color: var(--text-sub); }
.group-card-desc { font-size: 12px; color: #999; margin-top: 6px; }

.group-type-tag {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 11px;
  font-weight: 600;
}
.group-type-tag.bind { background: #dbeafe; color: #1e40af; }
.group-type-tag.payment { background: #dcfce7; color: #166534; }
.group-type-tag.small { font-size: 10px; padding: 1px 6px; margin-left: 6px; }

.group-actions { display: flex; gap: 4px; }
.mini-btn {
  padding: 2px 8px;
  font-size: 11px;
  border: 1px solid #e5e7eb;
  border-radius: 4px;
  background: #fff;
  cursor: pointer;
  transition: all 0.15s;
}
.mini-btn:hover { background: #f9fafb; }
.mini-btn.danger { border-color: #fecaca; color: #dc2626; }
.mini-btn.danger:hover { background: #fef2f2; }

.form-group { margin-bottom: 12px; }
.form-label { display: block; font-size: 13px; font-weight: 500; margin-bottom: 4px; color: #555; }

.action-btn.danger { background: #fef2f2; color: #dc2626; border-color: #fecaca; }
.action-btn.danger:hover { background: #fee2e2; }

.stat-card.small { padding: 10px 14px; }
.stat-card.small .stat-label { font-size: 11px; }
.stat-card.small .stat-value { font-size: 18px; }
.expired-cell { color: #991b1b; text-decoration: line-through; }
</style>
