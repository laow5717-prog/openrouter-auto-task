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
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { getAdspowerSettings, saveAdspowerSettings, testAdspowerSettings } from '../api'
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

onMounted(load)
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
</style>
