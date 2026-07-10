let isRunning = false;
let logIndex = 0;
let pollInterval = null;

// 初始化
document.addEventListener('DOMContentLoaded', () => {
    switchTab('dashboard');
    startPolling();
});

// 切换视图
function switchTab(tabName) {
    document.querySelectorAll('.view-section').forEach(el => el.classList.remove('active'));
    document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));

    document.getElementById(`view-${tabName}`).classList.add('active');

    const navIndex = tabName === 'dashboard' ? 0 : 1;
    document.querySelectorAll('.nav-item')[navIndex].classList.add('active');

    if (tabName === 'accounts') {
        loadAccounts();
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

// 收集信用卡信息
function collectCardInfo() {
    const number = document.getElementById('cardNumber').value.trim();
    if (!number) return null;

    return {
        number: number,
        expiry_month: document.getElementById('cardExpMonth').value.trim(),
        expiry_year: document.getElementById('cardExpYear').value.trim(),
        cvc: document.getElementById('cardCvc').value.trim(),
        name: document.getElementById('billingName').value.trim(),
        address: document.getElementById('billingAddress').value.trim(),
        city: document.getElementById('billingCity').value.trim(),
        state: document.getElementById('billingState').value.trim(),
        zip: document.getElementById('billingZip').value.trim(),
        country: document.getElementById('billingCountry').value.trim() || 'US',
    };
}

// 启动任务
async function startTask() {
    const count = parseInt(document.getElementById('targetCount').value) || 1;
    const cardInfo = collectCardInfo();

    // 清空旧日志
    clearLogs();

    try {
        const body = { count: count };
        if (cardInfo) {
            body.card_info = cardInfo;
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

// 搜索账号
function filterAccounts() {
    const term = document.getElementById('searchInput').value.toLowerCase();
    if (!window.allAccounts) return;

    const filtered = window.allAccounts.filter(acc =>
        acc.email.toLowerCase().includes(term)
    );
    renderAccounts(filtered);
}
