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
        <button class="action-btn" @click="showMerge">归纳合并</button>
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
        <button class="action-btn" @click="showMove">移动到分组</button>
        <button class="action-btn danger" @click="handleDeleteInvalid">删除无效卡</button>
        <button class="action-btn danger" @click="handleClearPool">清空</button>
      </div>
    </div>

    <!-- 状态筛选 + 桶数量 -->
    <div style="display:flex;gap:8px;align-items:center;padding:10px 16px;flex-wrap:wrap">
      <button class="filter-btn" :class="{ 'filter-btn-primary': poolBucket === '' }" @click="setBucket('')">全部 {{ poolBuckets.total }}</button>
      <button class="filter-btn" :class="{ 'filter-btn-primary': poolBucket === 'valid' }" @click="setBucket('valid')">有效(在库) {{ poolBuckets.valid }}</button>
      <button class="filter-btn" :class="{ 'filter-btn-primary': poolBucket === 'unverified' }" @click="setBucket('unverified')">未验证 {{ poolBuckets.unverified }}</button>
      <button class="filter-btn" :class="{ 'filter-btn-primary': poolBucket === 'invalid' }" @click="setBucket('invalid')">无效 {{ poolBuckets.invalid }}</button>
      <span style="font-size:11px;color:var(--text-sub)">「有效(在库)」= 本分组内已验证且当前可用的卡；全局历史验证卡见上方「查看有效卡」</span>
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
            <th>累计充值</th>
            <th>当日充值</th>
            <th>状态</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="poolLoading">
            <td colspan="10" class="table-loading">加载中...</td>
          </tr>
          <tr v-else-if="poolCards.length === 0">
            <td colspan="10" class="table-empty">暂无卡片数据，请上传 Excel 文件</td>
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
            <td>{{ card.recharge_total || 0 }}</td>
            <td :style="{ color: card.recharge_today ? '#059669' : 'inherit' }">{{ card.recharge_today || 0 }}</td>
            <td>
              <span v-if="card.status === 'expired'" class="status-tag fail">已过期</span>
              <span v-else-if="card.status === 'invalid'" class="status-tag fail">无效</span>
              <!-- 已绑定：一卡一账号，卡已被消耗，不再参与选卡（不同于"无效"） -->
              <span v-else-if="card.status === 'bound'" class="status-tag bound">已绑定</span>
              <span v-else-if="card.is_valid" class="status-tag success">有效</span>
              <span v-else class="status-tag">待验证</span>
              <!-- 选卡规则状态：告知用户为何该卡暂不被选用 -->
              <span v-if="card.tds_cooldown" class="status-tag" style="background:#fef3c7;color:#92400e;margin-left:4px">3DS临时冷却</span>
              <span v-if="card.rate_cooldown" class="status-tag" style="background:#fef3c7;color:#92400e;margin-left:4px">24h达2次冷却</span>
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

  <!-- 归纳合并弹窗 -->
  <Modal :visible="mergeModal.visible" title="归纳合并（移动非无效卡到新分组）" @close="mergeModal.visible = false">
    <div class="form-group">
      <label class="form-label">选择源分组（把这些分组里的有效+未验证卡移动到新分组，无效卡留在原组）</label>
      <div style="max-height:200px;overflow-y:auto;border:1px solid var(--border);border-radius:6px;padding:8px">
        <div v-if="groups.length === 0" style="color:var(--text-sub);font-size:12px">暂无分组</div>
        <label v-for="g in groups" :key="g.id" style="display:flex;align-items:center;gap:8px;padding:4px 0;cursor:pointer">
          <input type="checkbox" :value="g.id" v-model="mergeModal.sourceIds">
          <span>{{ g.name }}</span>
          <span class="group-type-tag small" :class="g.type">{{ g.type === 'bind' ? '绑定卡' : '支付卡' }}</span>
          <span style="color:var(--text-sub);font-size:12px">{{ g.card_count }} 张</span>
        </label>
      </div>
    </div>
    <div class="form-group">
      <label class="form-label">新分组名称</label>
      <input v-model="mergeModal.name" class="ctrl-input" placeholder="如：7-15汇总可用卡">
    </div>
    <div class="form-group">
      <label class="form-label">新分组类型</label>
      <select v-model="mergeModal.type" class="ctrl-input">
        <option value="bind">绑定卡</option>
        <option value="payment">支付卡</option>
      </select>
    </div>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:16px">
      <button class="btn" style="width:auto;padding:8px 20px" @click="mergeModal.visible = false">取消</button>
      <button class="btn btn-primary" style="width:auto;padding:8px 20px" @click="doMerge">开始合并</button>
    </div>
  </Modal>

  <!-- 移动到分组弹窗 -->
  <Modal :visible="moveModal.visible" title="移动到分组" @close="moveModal.visible = false">
    <div class="form-group">
      <label class="form-label">从「{{ selectedGroup?.name }}」移动到</label>
      <select v-model="moveModal.targetId" class="ctrl-input">
        <option :value="null">请选择目标分组</option>
        <option v-for="g in moveTargets" :key="g.id" :value="g.id">
          {{ g.name }}（{{ g.type === 'bind' ? '绑定卡' : '支付卡' }}，{{ g.card_count }} 张）
        </option>
      </select>
    </div>
    <div class="form-group">
      <label class="form-label">卡片范围</label>
      <select v-model="moveModal.bucket" class="ctrl-input">
        <option value="unverified">未验证（{{ poolBuckets.unverified }} 张）</option>
        <option value="valid">有效(在库)（{{ poolBuckets.valid }} 张）</option>
        <option value="non_invalid">有效 + 未验证（{{ poolBuckets.valid + poolBuckets.unverified }} 张）</option>
      </select>
    </div>
    <div class="form-group">
      <label class="form-label">移动数量（按导入顺序从最早的开始取，不足则全部移动）</label>
      <input v-model.number="moveModal.limit" type="number" min="1" class="ctrl-input" placeholder="如：100">
    </div>
    <div style="font-size:12px;color:var(--text-sub);line-height:1.5">
      无效卡（拒付/过期）不会被移动。目标分组已存在相同卡号的卡将被跳过，保留在当前分组。
    </div>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:16px">
      <button class="btn" style="width:auto;padding:8px 20px" @click="moveModal.visible = false">取消</button>
      <button class="btn btn-primary" style="width:auto;padding:8px 20px" :disabled="moveModal.busy" @click="doMove">
        {{ moveModal.busy ? '移动中...' : '开始移动' }}
      </button>
    </div>
  </Modal>

  <!-- 有效卡弹窗 -->
  <Modal :visible="validModal.visible" title="有效卡列表（全局历史验证卡）" @close="validModal.visible = false" wide>
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
      <button class="filter-btn filter-btn-primary" style="background:#059669" @click="exportValidCards">导出 Excel</button>
    </FilterBar>
    <div style="font-size:12px;color:var(--text-sub);margin-bottom:8px;line-height:1.5">
      此处为<strong>全局历史验证卡</strong>（跨所有分组、一经验证成功即长期保留）。它与"某个分组的有效桶"口径不同：
      分组的"有效(在库)"只统计当前在该分组卡池、且未被标记无效的卡。看「池内位置」列可知每张卡当前在哪个分组、什么状态。
    </div>
    <div style="overflow-x:auto">
      <table class="valid-table">
        <thead>
          <tr>
            <th>卡号</th>
            <th>有效期</th>
            <th>持卡人</th>
            <th>来源</th>
            <th>关联账号</th>
            <th>池内位置</th>
            <th>累计充值</th>
            <th>当日充值</th>
            <th>状态</th>
            <th>验证时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="validLoading">
            <td colspan="10" class="table-loading">加载中...</td>
          </tr>
          <tr v-else-if="validCards.length === 0">
            <td colspan="10" class="table-empty">暂无有效卡记录</td>
          </tr>
          <tr v-for="c in validCards" :key="c.id">
            <td style="font-family:monospace">{{ c.card_number }}</td>
            <td>{{ c.expiry_month }}/{{ c.expiry_year }}</td>
            <td>{{ c.first_name }} {{ c.last_name }}</td>
            <td>
              <span class="status-tag" :class="c.source_type === 'bind' ? '' : 'success'">
                {{ c.source_type === 'bind' ? '绑定' : '支付' }}
              </span>
            </td>
            <td>{{ c.source_email || '-' }}</td>
            <td style="font-size:12px">
              <template v-if="c.pool_group">{{ c.pool_group }} · {{ c.pool_status }}</template>
              <span v-else style="color:var(--text-sub)">{{ c.pool_status || '不在卡池' }}</span>
            </td>
            <td>{{ c.recharge_total || 0 }}</td>
            <td :style="{ color: c.recharge_today ? '#059669' : 'inherit' }">{{ c.recharge_today || 0 }}</td>
            <td>
              <span class="status-tag" :class="(c.tds_cooldown || c.rate_cooldown) ? 'fail' : 'success'">
                {{ c.status_text || '可用' }}
              </span>
            </td>
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
import { ref, reactive, computed, onMounted } from 'vue'
import {
  getCardGroups, createCardGroup, updateCardGroup, deleteCardGroup,
  getCardPool, uploadCardPool, deletePoolCard, clearCardPool,
  getValidCards, mergeCardPools, deleteInvalidCards, moveCardsToGroup,
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
const poolBucket = ref('')  // ''=全部 / valid / unverified / invalid
const poolBuckets = reactive({ total: 0, invalid: 0, valid: 0, unverified: 0 })

// 归纳合并弹窗
const mergeModal = reactive({ visible: false, sourceIds: [], name: '', type: 'bind' })

// 移动到分组弹窗
const moveModal = reactive({ visible: false, targetId: null, bucket: 'unverified', limit: 100, busy: false })
const moveTargets = computed(() => groups.value.filter(g => g.id !== selectedGroup.value?.id))

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

async function loadGroups() {
  try {
    groups.value = await getCardGroups()
  } catch (e) { console.error(e) }
}

function selectGroup(g) {
  selectedGroup.value = g
  poolPage.value = 1
  poolBucket.value = ''
  loadPoolCards()
}

function setBucket(b) {
  poolBucket.value = b
  poolPage.value = 1
  loadPoolCards()
}

async function loadPoolCards() {
  if (!selectedGroup.value) return
  poolLoading.value = true
  try {
    const params = { page: poolPage.value, page_size: poolPageSize.value }
    if (poolBucket.value) params.bucket = poolBucket.value
    const data = await getCardPool(selectedGroup.value.id, params)
    poolCards.value = data.data || []
    poolTotal.value = data.total || 0
    if (data.buckets) Object.assign(poolBuckets, data.buckets)
  } catch (e) { console.error(e) } finally { poolLoading.value = false }
}

async function handleDeleteInvalid() {
  if (!selectedGroup.value) return
  if (!confirm(`确定删除分组「${selectedGroup.value.name}」内的所有无效卡（含拒付与过期）吗？此操作不可恢复。`)) return
  try {
    const r = await deleteInvalidCards(selectedGroup.value.id)
    alert(`已删除 ${r.deleted} 张无效卡`)
    poolPage.value = 1
    await loadPoolCards()
    await loadGroups()
  } catch (e) { alert('删除失败: ' + e.message) }
}

function showMerge() {
  mergeModal.sourceIds = []
  mergeModal.name = ''
  mergeModal.type = 'bind'
  mergeModal.visible = true
}

async function doMerge() {
  if (!mergeModal.sourceIds.length) { alert('请至少选择一个源分组'); return }
  if (!mergeModal.name.trim()) { alert('请输入新分组名称'); return }
  try {
    const r = await mergeCardPools({
      source_group_ids: mergeModal.sourceIds,
      name: mergeModal.name.trim(),
      type: mergeModal.type,
    })
    alert(`已合并：移入 ${r.moved} 张，去重 ${r.deduped} 张 → 新分组已创建`)
    mergeModal.visible = false
    await loadGroups()
  } catch (e) { alert('合并失败: ' + e.message) }
}

function showMove() {
  if (!selectedGroup.value) return
  moveModal.targetId = null
  moveModal.bucket = 'unverified'
  moveModal.limit = 100
  moveModal.busy = false
  moveModal.visible = true
}

async function doMove() {
  if (!moveModal.targetId) { alert('请选择目标分组'); return }
  if (!Number.isInteger(moveModal.limit) || moveModal.limit <= 0) { alert('移动数量必须为正整数'); return }
  moveModal.busy = true
  try {
    const r = await moveCardsToGroup(selectedGroup.value.id, {
      target_group_id: moveModal.targetId,
      bucket: moveModal.bucket,
      limit: moveModal.limit,
    })
    const skipped = r.skipped ? `，跳过重复卡 ${r.skipped} 张` : ''
    alert(`已移动 ${r.moved} 张卡${skipped}`)
    moveModal.visible = false
    poolPage.value = 1
    await loadPoolCards()
    await loadGroups()
  } catch (e) { alert('移动失败: ' + e.message) } finally { moveModal.busy = false }
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

function exportValidCards() {
  const q = validFilters.source_type ? `?source_type=${encodeURIComponent(validFilters.source_type)}` : ''
  window.open(`/api/valid-cards/export${q}`, '_blank')
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
/* 有效卡表格：列宽按内容自适应，不换行、不脱敏；超宽时容器横向滚动 */
.valid-table { table-layout: auto; width: auto; min-width: 100%; }
.valid-table th, .valid-table td { white-space: nowrap; }

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
