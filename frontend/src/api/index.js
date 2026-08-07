const BASE = ''

// 当前平台。账号状态、卡的占用与冷却全部按它隔离，服务端对卡池类接口要求必填，
// 所以这里统一注入，而不是让每个调用点自己记得传——漏传一处就会读到错平台的数据。
// 由 stores/app.js 在平台切换时调用 setPlatform 同步。
let currentPlatform = 'opencode'

export function setPlatform(p) {
  if (p) currentPlatform = p
}

export function getPlatform() {
  return currentPlatform
}

async function request(url, options = {}) {
  const res = await fetch(BASE + url, options)
  if (!res.ok) {
    const text = await res.text()
    throw new Error(text || `HTTP ${res.status}`)
  }
  return res
}

async function get(url, params = {}) {
  const qs = new URLSearchParams({ platform: currentPlatform, ...params }).toString()
  const fullUrl = qs ? `${url}?${qs}` : url
  const res = await request(fullUrl)
  return res.json()
}

async function post(url, body = {}) {
  const res = await request(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ platform: currentPlatform, ...body }),
  })
  return res.json()
}

async function postFile(url, formData) {
  const res = await request(url, { method: 'POST', body: formData })
  return res.json()
}

async function postBlob(url, body = {}) {
  const res = await request(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  return res.blob()
}

// Platforms
export const getPlatforms = () => get('/api/platforms')

// Status
export const getStatus = (logIndex = 0) => get('/api/status', { log_index: logIndex })
export const getWorkerLogs = (workerId, index = 0) =>
  get(`/api/workers/${workerId}/logs`, { index })

// Task control
export const startTask = (data) => post('/api/start', data)
// platform 可选：不传就停「当前在看的平台」（post 会自动注入 currentPlatform），
// 传了则停指定平台。顶栏的全局停止要能停掉**没在看的那个平台**，靠的就是这个参数。
export const stopTask = (platform) => post('/api/stop', platform ? { platform } : {})

// Accounts
export const getAccounts = (params) => get('/api/accounts', params)
export const getAccountCards = (email) => get(`/api/accounts/${encodeURIComponent(email)}/cards`)
export const exportAccounts = (body) => postBlob('/api/accounts/export', body)
export const deleteAccounts = (emails) => post('/api/accounts/delete', { emails })
export const importAccounts = (file) => {
  const fd = new FormData()
  fd.append('file', file)
  return postFile('/api/accounts/import', fd)
}
export const rechargeAccount = (email, paymentGroupId) => {
  const body = { email }
  if (paymentGroupId) body.payment_group_id = paymentGroupId
  return post('/api/accounts/recharge', body)
}
export const openAccountBrowser = (email) => post('/api/accounts/open-browser', { email })
export const getOpenBrowsers = () => get('/api/accounts/open-browsers')

// Recharge logs
export const getRechargeLogs = (params) => get('/api/recharge-logs', params)
export const getRechargeLogsByEmail = (email) => get(`/api/recharge-logs/${encodeURIComponent(email)}`)
export const getRechargeLogsByCard = (cardNumber) => get('/api/card-recharge-logs', { card_number: cardNumber })

// Card history
export const getCardHistory = (params) => get('/api/card/history', params)
export const exportCardHistory = (body) => postBlob('/api/card/history/export', body)
export const cleanupCardHistory = () => post('/api/card/history/cleanup')

// Card groups
export const getCardGroups = (params) => get('/api/card-groups', params)
export const createCardGroup = (data) => post('/api/card-groups', data)
export const updateCardGroup = (id, data) => request(`/api/card-groups/${id}`, {
  method: 'PUT',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(data),
}).then(r => r.json())
export const deleteCardGroup = (id) => request(`/api/card-groups/${id}`, { method: 'DELETE' }).then(r => r.json())

// Card pool
export const getCardPool = (groupId, params) => get(`/api/card-pool/${groupId}`, params)
export const uploadCardPool = (groupId, file) => {
  const fd = new FormData()
  fd.append('file', file)
  return postFile(`/api/card-pool/${groupId}/upload`, fd)
}
export const deletePoolCard = (cardId) => request(`/api/card-pool/card/${cardId}`, { method: 'DELETE' }).then(r => r.json())
export const clearCardPool = (groupId) => post(`/api/card-pool/${groupId}/clear`)
export const mergeCardPools = (body) => post('/api/card-pool/merge', body)
export const moveCardsToGroup = (groupId, body) => post(`/api/card-pool/${groupId}/move`, body)
export const deleteInvalidCards = (groupId) => post(`/api/card-pool/${groupId}/delete-invalid`)

// Proxies
export const getProxies = (params) => get('/api/proxies', params)
export const importProxies = (text) => post('/api/proxies/import', { text })
export const deleteProxy = (id) => request(`/api/proxies/${id}`, { method: 'DELETE' }).then(r => r.json())
export const clearProxies = () => post('/api/proxies/clear')

// Valid cards
export const getValidCards = (params) => get('/api/valid-cards', params)

// Daily one-click pipeline
export const startDailyPipeline = (data) => post('/api/daily/start', data)
// Daily subscribe pipeline（账号轮转：注册/登录 + Stripe 订阅）
export const startDailySubscribe = (data) => post('/api/daily/subscribe/start', data)

// Settings
export const getAdspowerSettings = () => get('/api/settings/adspower')
export const saveAdspowerSettings = (body) => request('/api/settings/adspower', {
  method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
}).then(r => r.json())
export const testAdspowerSettings = () => post('/api/settings/adspower/test')
