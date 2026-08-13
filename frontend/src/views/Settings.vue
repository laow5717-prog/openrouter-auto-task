<template>
  <div class="info-banner">
    <strong>AdsPower 指纹浏览器：</strong>
    这里改的配置**覆盖** <code>config.yaml</code> 的同名项，保存即生效、无需重启。
    留空则回落到 <code>config.yaml</code> 的默认值。
    关掉总开关后，任务会走本地 Chrome 持久 profile（<code>data/profiles/&lt;邮箱&gt;</code>），
    与接入 AdsPower 之前完全一致——这是唯一的回退手段。
  </div>

  <div class="panel">
    <div class="panel-header">
      <div class="panel-title"><Icon name="monitor" size="18" /> AdsPower 配置</div>
      <div style="display:flex;gap:8px">
        <button class="action-btn" @click="doTest" :disabled="testing">
          {{ testing ? '检测中…' : '连通性检测' }}
        </button>
        <button class="action-btn" @click="load">重新加载</button>
      </div>
    </div>

    <div style="padding:16px;max-width:720px">
      <div class="set-row">
        <label class="set-label">启用 AdsPower</label>
        <div>
          <label style="display:flex;align-items:center;gap:8px;cursor:pointer">
            <input type="checkbox" v-model="form.enabled">
            <span style="font-size:13px;color:var(--text-sub)">
              {{ form.enabled ? '任务使用 AdsPower 指纹环境' : '任务使用本地 Chrome profile' }}
            </span>
          </label>
          <div v-if="from_db.enabled" class="set-hint">已在此处设置（覆盖 config.yaml）</div>
        </div>
      </div>

      <div class="set-row">
        <label class="set-label">API Key</label>
        <div>
          <input v-model="form.api_key" class="ctrl-input" style="width:100%;font-family:monospace"
                 placeholder="尚未配置">
          <div class="set-hint">
            AdsPower 客户端「自动化 - API - API Key」里复制。清空并保存则回落 config.yaml。
          </div>
        </div>
      </div>

      <div class="set-row">
        <label class="set-label">本地 API 地址</label>
        <div>
          <input v-model="form.base_url" class="ctrl-input" style="width:100%"
                 placeholder="http://local.adspower.net:50325">
          <div class="set-hint">AdsPower 客户端默认监听 50325 端口。留空回落 config.yaml。</div>
        </div>
      </div>

      <div style="display:flex;gap:10px;align-items:center;margin-top:18px">
        <button class="action-btn" @click="save" :disabled="saving">
          {{ saving ? '保存中…' : '保存' }}
        </button>
        <span v-if="msg" :style="{ fontSize: '13px', color: msgColor }">{{ msg }}</span>
      </div>
    </div>
  </div>

  <div class="panel" style="margin-top:24px">
    <div class="panel-header">
      <div class="panel-title"><Icon name="monitor" size="18" /> 并行 Worker 数</div>
      <button class="action-btn" @click="loadConc">重新加载</button>
    </div>

    <div style="padding:16px;max-width:720px">
      <div class="set-hint" style="margin:0 0 14px">
        每个平台同时驱动的浏览器数，范围 {{ conc.min_workers }}-{{ conc.max_workers }}。
        <b>改动在下一轮任务开始时生效</b>，不会打断正在跑的任务。留空并保存则回落
        <code>config.yaml</code>。
      </div>

      <div v-for="p in conc.data" :key="p.slug" class="set-row">
        <label class="set-label">{{ p.display_name }}</label>
        <div>
          <div style="display:flex;align-items:center;gap:10px">
            <input type="number" v-model="workerForm[p.slug]" class="ctrl-input"
                   style="width:90px" :min="conc.min_workers" :max="conc.max_workers"
                   :placeholder="String(p.yaml_default)">
            <span class="set-hint" style="margin:0">
              config.yaml 默认 {{ p.yaml_default }}
              <template v-if="p.platform_quota != null"> · 自有环境额度 {{ p.platform_quota }}</template>
            </span>
          </div>
          <div v-if="p.from_db" class="set-hint">已在此处设置（覆盖 config.yaml）</div>
          <!-- 超过自有额度不是错误，是会悄悄变慢：超出的 worker 每轮都要向别的平台
               借环境，对方在跑时就退化成排队等，名义并发度和实际吞吐对不上 -->
          <div v-if="overQuota(p)" class="set-warn">
            超过该平台自有环境额度（{{ p.platform_quota }}），超出的 worker 需向其他平台借用，
            对方运行时会排队等待
          </div>
        </div>
      </div>

      <!-- 总量约束：AdsPower 的环境是各平台共用的，加起来超了谁都跑不满 -->
      <div v-if="conc.adspower_enabled && totalWorkers > conc.total_quota" class="set-warn"
           style="margin-top:12px">
        各平台合计 {{ totalWorkers }} 个 worker，超过 AdsPower 环境总上限
        {{ conc.total_quota }}。同时运行多个平台时会有 worker 一直等不到环境。
      </div>

      <div style="display:flex;gap:10px;align-items:center;margin-top:18px">
        <button class="action-btn" @click="saveConc" :disabled="savingConc">
          {{ savingConc ? '保存中…' : '保存' }}
        </button>
        <span v-if="concMsg" :style="{ fontSize: '13px', color: concMsgColor }">{{ concMsg }}</span>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import {
  getAdspowerSettings, saveAdspowerSettings, testAdspowerSettings,
  getConcurrencySettings, saveConcurrencySettings,
} from '../api'
import Icon from '../components/Icon.vue'

const form = reactive({ enabled: false, api_key: '', base_url: '' })
const from_db = ref({})
const saving = ref(false)
const testing = ref(false)
const msg = ref('')
const msgColor = ref('var(--text-sub)')

function say(text, ok = true) {
  msg.value = text
  msgColor.value = ok ? 'green' : '#dc2626'
}

async function load() {
  try {
    const d = await getAdspowerSettings()
    from_db.value = d.from_db || {}
    form.enabled = !!d.enabled
    form.base_url = d.base_url || ''
    form.api_key = d.api_key || ''
  } catch (e) {
    say(`加载失败: ${e.message}`, false)
  }
}

async function save() {
  saving.value = true
  msg.value = ''
  try {
    await saveAdspowerSettings({
      enabled: form.enabled,
      api_key: form.api_key,
      base_url: form.base_url,
    })
    await load()
    say('已保存，立即生效（无需重启）')
  } catch (e) {
    say(`保存失败: ${e.message}`, false)
  } finally {
    saving.value = false
  }
}

async function doTest() {
  testing.value = true
  msg.value = ''
  try {
    const r = await testAdspowerSettings()
    say(r.detail, r.ok)
  } catch (e) {
    say(`检测失败: ${e.message}`, false)
  } finally {
    testing.value = false
  }
}

// ---------- 并行 Worker 数 ----------

const conc = ref({ data: [], min_workers: 1, max_workers: 10, total_quota: 0, adspower_enabled: false })
// 只放「用户设过的」值：占位符显示 yaml 默认值，输入框留空就表示回落默认，
// 与 AdsPower 那几项「清空即回落」的语义保持一致。
const workerForm = reactive({})
const savingConc = ref(false)
const concMsg = ref('')
const concMsgColor = ref('var(--text-sub)')

const totalWorkers = computed(() =>
  conc.value.data.reduce((sum, p) => {
    const v = parseInt(workerForm[p.slug], 10)
    return sum + (Number.isNaN(v) ? p.yaml_default : v)
  }, 0))

function overQuota(p) {
  if (p.platform_quota == null) return false
  const v = parseInt(workerForm[p.slug], 10)
  return (Number.isNaN(v) ? p.yaml_default : v) > p.platform_quota
}

function sayConc(text, ok = true) {
  concMsg.value = text
  concMsgColor.value = ok ? 'green' : '#dc2626'
}

async function loadConc() {
  try {
    const d = await getConcurrencySettings()
    conc.value = d
    for (const p of d.data || []) {
      // 没设过的留空，让占位符去显示 yaml 默认值——预填成默认值的话，
      // 用户一保存就把默认值固化成了覆盖值，此后改 config.yaml 再也不生效
      workerForm[p.slug] = p.from_db ? String(p.workers) : ''
    }
  } catch (e) {
    sayConc(`加载失败: ${e.message}`, false)
  }
}

async function saveConc() {
  savingConc.value = true
  concMsg.value = ''
  try {
    const workers = {}
    for (const p of conc.value.data) {
      const raw = String(workerForm[p.slug] ?? '').trim()
      workers[p.slug] = raw === '' ? null : raw
    }
    await saveConcurrencySettings({ workers })
    await loadConc()
    sayConc('已保存，下一轮任务开始时生效')
  } catch (e) {
    sayConc(`保存失败: ${e.message}`, false)
  } finally {
    savingConc.value = false
  }
}

onMounted(() => { load(); loadConc() })
</script>

<style scoped>
.set-row {
  display: grid;
  grid-template-columns: 130px 1fr;
  gap: 14px;
  align-items: start;
  padding: 12px 0;
  border-bottom: 1px solid var(--border);
}
.set-row:last-of-type { border-bottom: none; }
.set-label {
  font-size: 13px;
  font-weight: 600;
  padding-top: 7px;
}
.set-hint {
  font-size: 12px;
  color: var(--text-sub);
  margin-top: 5px;
  line-height: 1.6;
}
.set-warn {
  margin-top: 6px;
  padding: 6px 10px;
  border-radius: 6px;
  background: #fffbeb;
  color: #b45309;
  font-size: 12px;
  line-height: 1.6;
}
</style>
