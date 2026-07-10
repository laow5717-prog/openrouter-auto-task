let isRunning = false;
let logIndex = 0;
let pollInterval = null;

// 初始化
document.addEventListener('DOMContentLoaded', () => {
    switchTab('dashboard');
    startPolling();
    loadSavedSettings();
});

// 切换视图
function switchTab(tabName) {
    document.querySelectorAll('.view-section').forEach(el => el.classList.remove('active'));
    document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));

    document.getElementById(`view-${tabName}`).classList.add('active');

    const navMap = { 'dashboard': 0, 'cardmode': 1, 'accounts': 2 };
    document.querySelectorAll('.nav-item')[navMap[tabName]].classList.add('active');

    if (tabName === 'accounts') {
        loadAccounts();
    }
    if (tabName === 'cardmode') {
        loadCardStatus();
    }
}

// 轮询状态
function startPolling() {
    pollStatus();
    pollInterval = setInterval(pollStatus, 1000);
}

async function pollStatus() {
    try {
        const res = await fetch(`/api/status?log_index=${logIndex}`);
        const data = await res.json();
        updateUI(data);
    } catch (e) {
        console.error("轮询错误:", e);
    }
}

function updateUI(data) {
    // 更新基本指标
    document.getElementById('valAction').textContent = data.current_action;
    document.getElementById('valSuccess').textContent = data.success;
    document.getElementById('valFail').textContent = data.fail;
    document.getElementById('valInventory').textContent = data.total_inventory;

    // 更新运行状态
    isRunning = data.is_running;
    const btnStart = document.getElementById('btnStart');
    const btnStop = document.getElementById('btnStop');
    const statusDot = document.getElementById('statusDot');
    const statusText = document.getElementById('statusText');

    if (isRunning) {
        btnStart.classList.add('hidden');
        btnStop.classList.remove('hidden');
        statusDot.classList.add('running');
        statusText.textContent = "运行中";
    } else {
        btnStart.classList.remove('hidden');
        btnStop.classList.add('hidden');
        statusDot.classList.remove('running');
        statusText.textContent = "系统空闲";
    }

    // 更新监控画面
    const monitorImg = document.getElementById('liveMonitor');
    const noSignal = document.getElementById('noSignal');
    const monitorStatus = document.getElementById('monitorStatus');

    if (isRunning) {
        monitorImg.classList.remove('hidden');
        noSignal.classList.add('hidden');

        if (!monitorImg.src || monitorImg.src.indexOf('/video_feed') === -1) {
            monitorImg.src = "/video_feed";
        }

        monitorStatus.textContent = "LIVE";
        monitorStatus.classList.remove('neutral');
        monitorStatus.classList.add('success');
    } else {
        monitorStatus.textContent = "OFFLINE";
        monitorStatus.classList.remove('success');
        monitorStatus.classList.add('neutral');
    }

    // 追加日志
    if (data.logs && data.logs.length > 0) {
        const container = document.getElementById('logContainer');

        // 移除占位符
        const placeholder = container.querySelector('.log-placeholder');
        if (placeholder) placeholder.remove();

        data.logs.forEach(logLine => {
            const div = document.createElement('div');
            div.className = 'log-entry';
            div.textContent = logLine;
            container.appendChild(div);
        });

        // 自动滚动到底部
        container.scrollTop = container.scrollHeight;

        // 更新索引
        logIndex += data.logs.length;
    }
}

// 信用卡条目计数器
let cardEntryCounter = 0;

// 添加一条信用卡输入（含独立账单地址）
function addCardEntry(data) {
    const id = cardEntryCounter++;
    const container = document.getElementById('cardList');
    const entry = document.createElement('div');
    entry.className = 'card-entry';
    entry.id = `cardEntry_${id}`;
    const d = data || {};
    entry.innerHTML = `
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
            <span class="card-entry-label" style="font-size:12px; font-weight:600; color:var(--primary);">#${container.children.length + 1}</span>
            <button type="button" class="action-btn" onclick="removeCardEntry('cardEntry_${id}')" style="font-size:11px; color:var(--danger); border-color:#fecaca;">删除</button>
        </div>
        <div style="font-size:11px; color:var(--text-sub); font-weight:500; margin-bottom:4px;">卡片信息</div>
        <div style="display:flex; gap:6px; margin-bottom:6px;">
            <input type="text" class="ctrl-input card-number" placeholder="卡号 *" value="${d.number || ''}" style="flex:3">
            <input type="text" class="ctrl-input card-cvc" placeholder="CVC *" maxlength="4" value="${d.cvc || ''}" style="flex:1">
        </div>
        <div style="display:flex; gap:6px; margin-bottom:8px;">
            <input type="text" class="ctrl-input card-exp-month" placeholder="月(MM) *" maxlength="2" value="${d.expiry_month || ''}" style="flex:1">
            <input type="text" class="ctrl-input card-exp-year" placeholder="年(YYYY) *" maxlength="4" value="${d.expiry_year || ''}" style="flex:1">
        </div>
        <div style="font-size:11px; color:var(--text-sub); font-weight:500; margin-bottom:4px;">账单地址</div>
        <div style="display:flex; gap:6px; margin-bottom:6px;">
            <input type="text" class="ctrl-input card-first-name" placeholder="First name *" value="${d.first_name || ''}" style="flex:1">
            <input type="text" class="ctrl-input card-last-name" placeholder="Last name *" value="${d.last_name || ''}" style="flex:1">
        </div>
        <div class="ctrl-row" style="margin-bottom:6px;">
            <input type="text" class="ctrl-input card-country" placeholder="Country *" value="${d.country || 'United States'}" style="width:100%">
        </div>
        <div class="ctrl-row" style="margin-bottom:6px;">
            <input type="text" class="ctrl-input card-address" placeholder="Address line 1 *" value="${d.address || ''}" style="width:100%">
        </div>
        <div class="ctrl-row" style="margin-bottom:6px;">
            <input type="text" class="ctrl-input card-address2" placeholder="Address line 2 (optional)" value="${d.address2 || ''}" style="width:100%">
        </div>
        <div style="display:flex; gap:6px; margin-bottom:6px;">
            <input type="text" class="ctrl-input card-city" placeholder="City *" value="${d.city || ''}" style="flex:1">
            <input type="text" class="ctrl-input card-state" placeholder="State *" value="${d.state || ''}" style="flex:1">
        </div>
        <div style="display:flex; gap:6px;">
            <input type="text" class="ctrl-input card-zip" placeholder="ZIP code *" value="${d.zip || ''}" style="flex:1">
            <input type="text" class="ctrl-input card-company" placeholder="Organization (optional)" value="${d.company || ''}" style="flex:1">
        </div>
    `;
    container.appendChild(entry);
    renumberCards();
}

// 删除一条信用卡
function removeCardEntry(entryId) {
    const entry = document.getElementById(entryId);
    if (entry) {
        entry.remove();
        renumberCards();
    }
}

// 重新编号
function renumberCards() {
    const entries = document.querySelectorAll('#cardList .card-entry');
    entries.forEach((entry, idx) => {
        const label = entry.querySelector('span');
        if (label) label.textContent = `#${idx + 1}`;
    });
}

// 收集所有信用卡信息（每张卡含独立账单地址）
function collectCardInfoList() {
    const entries = document.querySelectorAll('#cardList .card-entry');
    if (entries.length === 0) return null;

    const cards = [];
    const missing = [];

    entries.forEach((entry, idx) => {
        const number = entry.querySelector('.card-number').value.trim();
        if (!number) return;

        const firstName = entry.querySelector('.card-first-name').value.trim();
        const lastName = entry.querySelector('.card-last-name').value.trim();
        const country = entry.querySelector('.card-country').value.trim();
        const address = entry.querySelector('.card-address').value.trim();
        const city = entry.querySelector('.card-city').value.trim();
        const state = entry.querySelector('.card-state').value.trim();
        const zip = entry.querySelector('.card-zip').value.trim();

        // 校验必填字段
        const required = { 'First name': firstName, 'Last name': lastName, 'Country': country, 'Address': address, 'City': city, 'State': state, 'ZIP': zip };
        for (const [label, val] of Object.entries(required)) {
            if (!val) missing.push(`卡 #${idx + 1}: ${label}`);
        }

        cards.push({
            number: number,
            expiry_month: entry.querySelector('.card-exp-month').value.trim(),
            expiry_year: entry.querySelector('.card-exp-year').value.trim(),
            cvc: entry.querySelector('.card-cvc').value.trim(),
            first_name: firstName,
            last_name: lastName,
            country: country,
            address: address,
            address2: entry.querySelector('.card-address2').value.trim(),
            city: city,
            state: state,
            zip: zip,
            company: entry.querySelector('.card-company').value.trim(),
        });
    });

    if (missing.length > 0) {
        alert("以下必填字段未填写:\n" + missing.join("\n"));
        return 'invalid';
    }

    return cards.length > 0 ? cards : null;
}

// 启动任务
async function startTask() {
    const count = parseInt(document.getElementById('targetCount').value) || 1;
    const cardInfoList = collectCardInfoList();
    if (cardInfoList === 'invalid') return;

    // 清空旧日志
    clearLogs();

    const cfPassword = document.getElementById('cfPassword').value.trim();

    const captchaApiKey = document.getElementById('captchaApiKey').value.trim();

    // 保存设置到 localStorage
    saveSettings(cfPassword, captchaApiKey);

    try {
        const body = { count: count };
        if (cfPassword) {
            body.cf_password = cfPassword;
        }
        if (captchaApiKey) {
            body.captcha_api_key = captchaApiKey;
        }
        if (cardInfoList) {
            body.card_info_list = cardInfoList;
        }

        const res = await fetch('/api/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });

        if (!res.ok) {
            alert("启动失败: " + await res.text());
        }
    } catch (e) {
        alert("请求失败: " + e);
    }
}

// 停止任务
async function stopTask() {
    if (!confirm("确定要停止当前任务吗？")) return;

    try {
        await fetch('/api/stop', { method: 'POST' });
    } catch (e) {
        console.error(e);
    }
}

// 清空日志
function clearLogs() {
    document.getElementById('logContainer').innerHTML = '<div class="log-placeholder">等待任务启动...</div>';
    logIndex = 0;
}

// 加载账号列表
async function loadAccounts() {
    const tbody = document.getElementById('accountTableBody');
    tbody.innerHTML = '<tr><td colspan="5" style="text-align:center">加载中...</td></tr>';

    try {
        const res = await fetch('/api/accounts');
        const accounts = await res.json();
        renderAccounts(accounts);
    } catch (e) {
        tbody.innerHTML = `<tr><td colspan="5" style="text-align:center;color:red">加载失败: ${e}</td></tr>`;
    }
}

function renderAccounts(accounts) {
    const tbody = document.getElementById('accountTableBody');
    tbody.innerHTML = '';

    if (accounts.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:#666">暂无数据</td></tr>';
        return;
    }

    accounts.forEach(acc => {
        let statusClass = '';
        if (acc.status.includes('成功') || acc.status.includes('已注册') || acc.status.includes('已添加')) statusClass = 'success';
        if (acc.status.includes('失败')) statusClass = 'fail';

        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td>${acc.email}</td>
            <td style="font-family:monospace">${acc.password}</td>
            <td style="font-family:monospace">${acc.email_password || '-'}</td>
            <td><span class="status-tag ${statusClass}">${acc.status}</span></td>
            <td>${acc.time}</td>
        `;
        tbody.appendChild(tr);
    });

    window.allAccounts = accounts;
}

// 保存/加载设置
function saveSettings(cfPassword, captchaApiKey) {
    if (captchaApiKey) localStorage.setItem('captchaApiKey', captchaApiKey);
    if (cfPassword) localStorage.setItem('cfPassword', cfPassword);
}

function loadSavedSettings() {
    const savedCaptchaKey = localStorage.getItem('captchaApiKey');
    const savedCfPassword = localStorage.getItem('cfPassword');
    if (savedCaptchaKey) document.getElementById('captchaApiKey').value = savedCaptchaKey;
    if (savedCfPassword) document.getElementById('cfPassword').value = savedCfPassword;
}

// 搜索账号
function filterAccounts() {
    const term = document.getElementById('searchInput').value.toLowerCase();
    if (!window.allAccounts) return;

    const filtered = window.allAccounts.filter(acc =>
        acc.email.toLowerCase().includes(term)
    );
    renderAccounts(filtered);
}

// ==========================================
// 信用卡驱动模式
// ==========================================

// 上传 Excel
async function uploadCardExcel() {
    const fileInput = document.getElementById('cardExcelFile');
    if (!fileInput.files.length) {
        alert("请先选择 Excel 文件");
        return;
    }

    const formData = new FormData();
    formData.append('file', fileInput.files[0]);

    const resultDiv = document.getElementById('uploadResult');
    resultDiv.innerHTML = '<span style="color:#666">上传解析中...</span>';

    try {
        const res = await fetch('/api/card/upload', { method: 'POST', body: formData });
        const data = await res.json();

        if (!res.ok) {
            resultDiv.innerHTML = `<span style="color:red">❌ ${data.error}</span>` +
                (data.details ? `<br><small>${data.details.join('<br>')}</small>` : '');
            return;
        }

        let html = `<span style="color:green">✅ 解析成功: ${data.total} 张信用卡</span>`;
        if (data.errors && data.errors.length > 0) {
            html += `<br><span style="color:orange">⚠️ ${data.errors.length} 条数据有问题被跳过</span>`;
        }
        html += '<div style="margin-top:8px; max-height:150px; overflow-y:auto; font-size:11px;">';
        data.preview.forEach(p => {
            html += `<div style="padding:2px 0;">#${p.index + 1} ${p.card_display} - ${p.name}</div>`;
        });
        html += '</div>';
        resultDiv.innerHTML = html;

        // 更新卡片统计
        document.getElementById('cardTotal').textContent = data.total;
        document.getElementById('cardPending').textContent = data.total;

    } catch (e) {
        resultDiv.innerHTML = `<span style="color:red">❌ 请求失败: ${e}</span>`;
    }
}

// 启动信用卡驱动任务
async function startCardDrivenTask() {
    if (isRunning) {
        alert("任务已在运行中");
        return;
    }

    clearLogs();

    const cfPassword = document.getElementById('cfPassword').value.trim();
    const captchaApiKey = document.getElementById('captchaApiKey').value.trim();

    saveSettings(cfPassword, captchaApiKey);

    const body = { max_bindable_cards: 2 };
    if (cfPassword) body.cf_password = cfPassword;
    if (captchaApiKey) body.captcha_api_key = captchaApiKey;

    try {
        const res = await fetch('/api/card/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await res.json();

        if (!res.ok) {
            alert("启动失败: " + (data.error || "未知错误"));
            return;
        }

        // 切换按钮状态
        document.getElementById('btnStartCardMode').classList.add('hidden');
        document.getElementById('btnStopCardMode').classList.remove('hidden');

    } catch (e) {
        alert("请求失败: " + e);
    }
}

// 加载信用卡绑定状态
async function loadCardStatus() {
    try {
        const res = await fetch('/api/card/status');
        const data = await res.json();

        // 更新统计
        if (data.summary) {
            document.getElementById('cardTotal').textContent = data.summary.total;
            document.getElementById('cardSuccess').textContent = data.summary.success;
            document.getElementById('cardFailed').textContent = data.summary.failed;
            document.getElementById('cardPending').textContent = data.summary.pending;

            // 任务完成时显示下载报告按钮
            if (data.summary.total > 0 && data.summary.pending === 0) {
                document.getElementById('btnDownloadReport').style.display = 'inline-block';
            }
        }

        // 更新表格
        const tbody = document.getElementById('cardTableBody');
        if (!data.records || data.records.length === 0) {
            tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:#666">暂无数据</td></tr>';
            return;
        }

        tbody.innerHTML = '';
        data.records.forEach(r => {
            let statusClass = '';
            let statusText = r.status;
            if (r.status === 'success') { statusClass = 'success'; statusText = '✅ 成功'; }
            else if (r.status === 'failed') { statusClass = 'fail'; statusText = '❌ 失败'; }
            else { statusClass = ''; statusText = '⏳ 待处理'; }

            const tr = document.createElement('tr');
            tr.innerHTML = `
                <td>${r.index + 1}</td>
                <td style="font-family:monospace">${r.card_display}</td>
                <td><span class="status-tag ${statusClass}">${statusText}</span></td>
                <td>${r.bound_to_email}</td>
                <td style="font-size:11px">${r.error}</td>
                <td style="font-size:11px">${r.attempt_time}</td>
            `;
            tbody.appendChild(tr);
        });

    } catch (e) {
        console.error("加载卡片状态失败:", e);
    }
}

// 定期刷新卡片状态（运行中时）
setInterval(() => {
    if (isRunning && document.getElementById('view-cardmode').classList.contains('active')) {
        loadCardStatus();
    }
    // 更新卡模式按钮状态
    const btnStart = document.getElementById('btnStartCardMode');
    const btnStop = document.getElementById('btnStopCardMode');
    if (btnStart && btnStop) {
        if (isRunning) {
            btnStart.classList.add('hidden');
            btnStop.classList.remove('hidden');
        } else {
            btnStart.classList.remove('hidden');
            btnStop.classList.add('hidden');
        }
    }
}, 2000);
