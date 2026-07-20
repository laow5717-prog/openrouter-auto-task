const BASE = ''

async function request(url, options = {}) {
  const res = await fetch(BASE + url, options)
  if (!res.ok) {
    const text = await res.text()
    throw new Error(text || `HTTP ${res.status}`)
  }
  return res
}

async function get(url, params = {}) {
  const qs = new URLSearchParams(params).toString()
  const fullUrl = qs ? `${url}?${qs}` : url
  const res = await request(fullUrl)
  return res.json()
}

async function post(url, body = {}) {
  const res = await request(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
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

// Status
export const getStatus = (logIndex = 0) => get('/api/status', { log_index: logIndex })
export const getWorkerLogs = (workerId, index = 0) =>
  get(`/api/workers/${workerId}/logs`, { index })

// Task control
export const startTask = (data) => post('/api/start', data)
export const stopTask = () => post('/api/stop')

// Accounts
export const getAccounts = (params) => get('/api/accounts', params)
export const getAccountCards = (email) => get(`/api/accounts/${encodeURIComponent(email)}/cards`)
export const exportAccounts = (body) => postBlob('/api/accounts/export', body)
export const deleteAccounts = (emails) => post('/api/accounts/delete', { emails })
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

// Valid cards
export const getValidCards = (params) => get('/api/valid-cards', params)

// Daily one-click pipeline
export const startDailyPipeline = (data) => post('/api/daily/start', data)
