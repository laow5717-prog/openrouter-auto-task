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

// Task control
export const startTask = (data) => post('/api/start', data)
export const stopTask = () => post('/api/stop')

// Accounts
export const getAccounts = (params) => get('/api/accounts', params)
export const getAccountCards = (email) => get(`/api/accounts/${encodeURIComponent(email)}/cards`)
export const exportAccounts = (body) => postBlob('/api/accounts/export', body)

// Card mode
export const uploadCardExcel = (file) => {
  const fd = new FormData()
  fd.append('file', file)
  return postFile('/api/card/upload', fd)
}
export const startCardTask = (data) => post('/api/card/start', data)
export const getCardStatus = (params) => get('/api/card/status', params)

// Card history
export const getCardHistory = (params) => get('/api/card/history', params)
export const exportCardHistory = (body) => postBlob('/api/card/history/export', body)
