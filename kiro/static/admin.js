// ═══════════════════════════════════════════════════════════
// State
// ═══════════════════════════════════════════════════════════
let apiKey = localStorage.getItem('kiro_admin_key') || '';
let sseAbortController = null;
let paused = false;
let autoScroll = true;
let activeLevels = new Set(['DEBUG','INFO','SUCCESS','WARNING','ERROR','CRITICAL']);
let configDraft = {};   // section → key → value (only modified values)
let logCount = 0;
let selectedAccounts = new Set();
let currentAccounts = [];

const BASE = window.location.origin;

// ═══════════════════════════════════════════════════════════
// Auth
// ═══════════════════════════════════════════════════════════
async function doLogin() {
  const key = document.getElementById('key-input').value.trim();
  if (!key) { showLoginError('请输入 API Key'); return; }
  const btn = document.querySelector('.login-btn');
  btn.textContent = '验证中…'; btn.disabled = true;
  try {
    const r = await apiFetch('/admin/status', {}, key);
    if (r.ok) {
      apiKey = key;
      localStorage.setItem('kiro_admin_key', key);
      showApp();
    } else {
      showLoginError('❌ API Key 不正确');
    }
  } catch (e) {
    showLoginError('❌ 无法连接到服务器');
  } finally {
    btn.textContent = '登录管理控制台'; btn.disabled = false;
  }
}

function showLoginError(msg) {
  document.getElementById('login-error').textContent = msg;
}

function doLogout() {
  localStorage.removeItem('kiro_admin_key');
  stopSSE();
  apiKey = '';
  document.getElementById('app').classList.remove('visible');
  document.getElementById('login-screen').style.display = 'flex';
  document.getElementById('key-input').value = '';
}

async function showApp() {
  document.getElementById('login-screen').style.display = 'none';
  document.getElementById('app').classList.add('visible');
  await loadDashboard();
  loadConfig();
  startSSE();
}

// Auto-login
if (apiKey) showApp().catch(doLogout);

document.getElementById('key-input').addEventListener('keydown', e => {
  if (e.key === 'Enter') doLogin();
});

// ═══════════════════════════════════════════════════════════
// API helper
// ═══════════════════════════════════════════════════════════
async function apiFetch(path, opts = {}, key = apiKey) {
  const headers = { 'X-Admin-Key': key, ...(opts.headers || {}) };
  // Only set Content-Type for body requests
  if (opts.body && typeof opts.body === 'string') {
    headers['Content-Type'] = 'application/json';
  }
  return fetch(BASE + path, { ...opts, headers });
}

// ═══════════════════════════════════════════════════════════
// Tab switching
// ═══════════════════════════════════════════════════════════
function switchTab(name) {
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('pane-' + name).classList.add('active');
  document.getElementById('tab-' + name).classList.add('active');
  document.getElementById('add-fab').style.display = name === 'dashboard' ? '' : 'none';
  if (name === 'config') loadConfig();
  if (name === 'models') refreshModelControls();
  if (name === 'logs' && !sseAbortController) startSSE();
}

// ═══════════════════════════════════════════════════════════
// Dashboard
// ═══════════════════════════════════════════════════════════
async function loadDashboard() {
  try {
    const r = await apiFetch('/admin/status');
    if (r.status === 401) { doLogout(); return; }
    if (!r.ok) return;
    renderDashboard(await r.json());
  } catch (e) { /* silent */ }
}

function getAccountStatus(a) {
  if (a.is_disabled) return 'disabled';
  if (a.is_exhausted) return 'exhausted';
  if ((a.cooldown_remaining_seconds || 0) > 0) return 'cooling';
  return 'active';
}

/** True when portal reports a Kiro Pro subscription (subscriptionTitle → quota.plan). */
function isKiroProPlan(plan) {
  if (!plan || typeof plan !== 'string') return false;
  return /KIRO\s*PRO\b/i.test(plan.trim());
}

function renderDashboard(data) {
  document.getElementById('s-total').textContent     = data.total_accounts ?? '—';
  document.getElementById('s-active').textContent    = data.available ?? '—';
  document.getElementById('s-cooling').textContent   = data.cooling_down ?? '—';
  document.getElementById('s-exhausted').textContent = data.exhausted ?? '—';
  document.getElementById('s-disabled').textContent  = data.disabled ?? '—';
  document.getElementById('s-quota').textContent     = data.total_remaining_quota ?? '—';

  const badge = document.getElementById('mode-badge');
  const isMulti = data.mode === 'multi';
  badge.textContent = isMulti ? 'Multi' : 'Single';
  badge.className = 'badge-mode ' + (isMulti ? 'multi' : 'single');

  document.getElementById('last-refresh').textContent = '刷新于 ' + new Date().toLocaleTimeString();

  const accounts = data.accounts || [];
  currentAccounts = accounts;
  selectedAccounts = new Set([...selectedAccounts].filter(name => accounts.some(a => a.name === name)));
  document.getElementById('account-count').textContent = `(共 ${accounts.length} 个)`;

  const grid = document.getElementById('account-grid');
  if (!accounts.length) {
    grid.innerHTML = `<div class="empty-state" style="grid-column:1/-1">
      <div class="empty-icon">📭</div>
      <div class="empty-title">暂无账号</div>
      <div class="empty-sub">点击右下角 + 添加第一个账号</div>
    </div>`;
    return;
  }
  grid.innerHTML = accounts.map(renderAccountCard).join('');
}

function updateDashboardFromStatus(data) {
  // Update statistics cards
  document.getElementById('s-total').textContent     = data.total_accounts ?? '—';
  document.getElementById('s-active').textContent    = data.available ?? '—';
  document.getElementById('s-cooling').textContent   = data.cooling_down ?? '—';
  document.getElementById('s-exhausted').textContent = data.exhausted ?? '—';
  document.getElementById('s-disabled').textContent  = data.disabled ?? '—';
  document.getElementById('s-quota').textContent     = data.total_remaining_quota ?? '—';

  const badge = document.getElementById('mode-badge');
  const isMulti = data.mode === 'multi';
  badge.textContent = isMulti ? 'Multi' : 'Single';
  badge.className = 'badge-mode ' + (isMulti ? 'multi' : 'single');

  document.getElementById('last-refresh').textContent = '刷新于 ' + new Date().toLocaleTimeString();

  const accounts = data.accounts || [];
  currentAccounts = accounts;
  selectedAccounts = new Set([...selectedAccounts].filter(name => accounts.some(a => a.name === name)));
  document.getElementById('account-count').textContent = `(共 ${accounts.length} 个)`;

  const grid = document.getElementById('account-grid');
  if (!accounts.length) {
    grid.innerHTML = `<div class="empty-state" style="grid-column:1/-1">
      <div class="empty-icon">📭</div>
      <div class="empty-title">暂无账号</div>
      <div class="empty-sub">点击右下角 + 添加第一个账号</div>
    </div>`;
    return;
  }
  grid.innerHTML = accounts.map(renderAccountCard).join('');
}

function renderAccountCard(a) {
  const status = getAccountStatus(a);
  const isPro = isKiroProPlan(a.quota?.plan);
  const badges = {
    active   : ['badge-active',    'Active'],
    cooling  : ['badge-cooling',   'Cooling'],
    exhausted: ['badge-exhausted', 'Exhausted'],
    disabled : ['badge-disabled',  'Disabled'],
  };
  const [badgeCls, badgeTxt] = badges[status];

  // Quota bar
  let quotaHtml = '';
  if (a.quota) {
    const { used = 0, limit = 0, remaining = 0, plan = '', next_reset = '—', trial_expiry = '' } = a.quota;
    const pct = limit > 0 ? (used / limit * 100) : 0;
    const fillCls = pct >= 95 ? 'danger' : pct >= 80 ? 'warning' : '';
    const formatNextReset = (str, trialStr) => {
      if (!str || str === '—') return '—';
      try {
        const d = new Date(str);
        if (isNaN(d.getTime())) return str;
        const fmt = d => `${d.getFullYear()}/${d.getMonth()+1}/${d.getDate()}`;
        const reset = fmt(d);

        // Use trial_expiry from backend if available
        if (trialStr && trialStr !== '—' && trialStr !== '') {
          const trialD = new Date(trialStr);
          if (!isNaN(trialD.getTime())) {
            const diffDays = Math.ceil((trialD - new Date()) / (24 * 3600 * 1000));
            let remColor = 'var(--green)';
            let remText = ` (剩${diffDays}天)`;
            if (diffDays <= 0) { remColor = 'var(--red)'; remText = ' (已过期)'; }
            else if (diffDays <= 2) { remColor = 'var(--yellow)'; }
            const remSpan = `<span style="color:${remColor};font-size:10px">${remText}</span>`;
            return `<span style="color:var(--accent);font-weight:600">重置 ${reset}</span> · <span style="color:var(--blue);font-weight:600">试用 ${fmt(trialD)}</span>${remSpan}`;
          }
        }

        // Fallback: only show reset date if no trial info
        return `<span style="color:var(--accent);font-weight:600">重置 ${reset}</span>`;
      } catch(e) { return str; }
    };

    quotaHtml = `
      <div class="quota-section">
        <div class="quota-labels">
          <span>配额使用</span>
          <span class="quota-val">${used.toFixed(1)} / ${limit.toFixed(0)}</span>
        </div>
        <div class="progress-bar"><div class="progress-fill ${fillCls}" style="width:${Math.min(pct,100).toFixed(1)}%"></div></div>
        <div class="quota-meta">
          <span>剩余 ${remaining.toFixed(1)}</span>
          <span>${formatNextReset(next_reset, trial_expiry)}</span>
        </div>
      </div>`;
  } else {
    quotaHtml = `<div style="font-size:12px;color:var(--text3);margin-bottom:14px">⏳ 配额信息未获取</div>`;
  }

  // Cooldown banner
  let cooldownHtml = '';
  const rem = a.cooldown_remaining_seconds || 0;
  if (status === 'cooling' && rem > 0) {
    const m = Math.floor(rem / 60), s = rem % 60;
    cooldownHtml = `<div class="cooldown-info">⏳ 冷却剩余 ${m > 0 ? m + 'm ' : ''}${s}s</div>`;
  }

  const proxyDisplay = a.proxy_url
    ? `<div style="color:var(--text3);font-size:11px;margin-bottom:4px;">当前代理: ${escHtml(a.proxy_url)}</div>`
    : '';
  const proxyHtml = `
      <div class="proxy-row" style="margin:10px 0;font-size:12px">
        ${proxyDisplay}
        <div style="display:flex;gap:6px;align-items:center">
          <input type="text" class="input-proxy" id="proxy-input-${escAttr(a.name)}"
            placeholder="http://127.0.0.1:7890（留空并保存可清除）" value="" style="flex:1;min-width:0;padding:6px 8px;border-radius:6px;border:1px solid var(--border);background:var(--bg2);color:var(--text)">
          <button type="button" class="btn btn-ghost" onclick="saveAccountProxy('${escAttr(a.name)}')">保存代理</button>
        </div>
      </div>`;

  // Action buttons
  const toggleDisabledBtn = a.is_disabled
    ? `<button class="btn btn-success" onclick="accountAction('${a.name}','enable')">✓ 启用</button>`
    : `<button class="btn btn-warning" onclick="accountAction('${a.name}','disable')">⊘ 禁用</button>`;
  const cooldownBtn = status === 'cooling'
    ? `<button class="btn btn-ghost" onclick="accountAction('${a.name}','cooldown-clear')">⚡ 解除冷却</button>` : '';

  // Host Switch button
  const switchHostBtn = `<button class="btn btn-ghost ${a.is_active_on_host ? 'disabled' : 'primary'}" 
                                 onclick="switchHostAccount('${escAttr(a.name)}')" 
                                 ${a.is_active_on_host ? 'disabled title="此账号已是主机活动账号"' : 'title="在主机上启用此账号"'}>
                            🔀 切换
                        </button>`;

  // Files section
  let filesHtml = '';
  /*
  if (a.files && a.files.length > 0) {
    const fileItems = a.files.map(f => `
      <div class="file-item">
        <span class="file-name" title="${escAttr(f)}">${escHtml(f)}</span>
        <button class="file-dl-btn" onclick="downloadAccountFile('${escAttr(a.name)}','${escAttr(f)}')" title="下载此文件">💾</button>
      </div>
    `).join('');

    filesHtml = `
      <div class="files-section">
        <div class="files-title">🔑 凭证文件 (用于本地切换账号)</div>
        <div class="files-list">${fileItems}</div>
        <div class="help-tip">
          💡 点击 <b>"切换账号"</b> 可同步到本地 IDE（需设置目录映射），或手动下载文件放入：<br>
          • <b>Mac</b>: <code>~/.aws/sso/cache/</code><br>
          • <b>Win</b>: <code>%USERPROFILE%\\.aws\\sso\\cache\\</code>
        </div>
      </div>`;
  }
  */

  // Active status badge for host (放在状态徽章前面)
  const activeHostBadge = a.is_active_on_host 
    ? `<span class="host-active-badge" title="主机当前正在使用此账号">🏠 当前选用</span>` 
    : '';

  const checkboxHtml = `
    <label style="display:flex;align-items:center;gap:6px;font-size:12px;color:var(--text2)">
      <input type="checkbox" ${selectedAccounts.has(a.name) ? 'checked' : ''} onchange="toggleAccountSelect('${escAttr(a.name)}', this.checked)">
      选择
    </label>
  `;

  return `
    <div class="account-card status-${status} ${a.is_active_on_host ? 'active-host' : ''} ${isPro ? 'account-kiro-pro' : ''}" id="card-${escAttr(a.name)}">
      <div class="card-top" style="flex-direction: column; align-items: stretch; gap: 6px; margin-bottom: 14px;">
        <div style="display: flex; justify-content: space-between; align-items: center;">
          <div style="display:flex; align-items:center; gap:10px;">
            ${checkboxHtml}
            <div class="card-name" style="margin-bottom: 0">📁 ${escHtml(a.name)}</div>
          </div>
          <div style="display: flex; align-items: center; gap: 6px; flex-wrap: wrap; justify-content: flex-end;">
            ${activeHostBadge}
            <span class="status-badge ${badgeCls}">${badgeTxt}</span>
          </div>
        </div>
        <div class="card-email" style="white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">
          ${escHtml(a.email || '邮箱未知')}
          ${a.quota?.plan ? ` · <span class="plan-inline${isPro ? ' plan-kiro-pro' : ''}">${escHtml(a.quota.plan)}</span>` : ''}
          ${a.quota?.overage_enabled ? ` · <span class="overage-badge" title="已开启超支模式，配额用尽后仍可继续使用">💳 超支开启</span>` : ''}
        </div>
      </div>
      ${quotaHtml}
      ${cooldownHtml}
      ${proxyHtml}
      <div class="card-stats">
        <div class="cs-item">
          <div class="cs-label">活跃请求</div>
          <div class="cs-val text-active">${a.active_requests ?? 0}</div>
        </div>
        <div class="cs-item">
          <div class="cs-label">累计请求</div>
          <div class="cs-val">${a.total_requests ?? 0}</div>
        </div>
      </div>
      <div class="card-actions-grid">
        <button class="btn btn-ghost" onclick="accountAction('${escAttr(a.name)}','quota-refresh')">🔄 刷新</button>
        ${toggleDisabledBtn}
        <button class="btn btn-ghost danger" onclick="confirmDelete('${escAttr(a.name)}')">🗑 删除</button>
        ${switchHostBtn}
      </div>
      ${filesHtml}
    </div>
  `;
}

function toggleAccountSelect(name, checked) {
  if (checked) selectedAccounts.add(name);
  else selectedAccounts.delete(name);
}

function toggleSelectAllAccounts() {
  const allNames = (currentAccounts || []).map(a => a.name);
  if (!allNames.length) {
    showToast('暂无可选择账号', 'warning');
    return;
  }
  const allSelected = allNames.every(name => selectedAccounts.has(name));
  if (allSelected) {
    selectedAccounts.clear();
    showToast('已取消全选', 'info');
  } else {
    allNames.forEach(name => selectedAccounts.add(name));
    showToast(`已全选 ${allNames.length} 个账号`, 'success');
  }
  loadDashboard();
}

async function exportSelectedAccountsZip() {
  const accountNames = [...selectedAccounts];
  if (!accountNames.length) {
    showToast('请先勾选要导出的账号', 'warning');
    return;
  }

  const ok = await showConfirm({
    title: '确认批量导出？',
    message: `将导出 ${accountNames.length} 个账号到 ZIP 文件。`,
    okText: '开始导出',
    type: 'primary'
  });
  if (!ok) return;

  const shouldDelete = window.confirm(
    '导出成功后，是否删除这些已导出的账号？\n\n确定=删除账号；取消=保留账号。'
  );

  try {
    const r = await apiFetch('/admin/accounts/export-zip', {
      method: 'POST',
      body: JSON.stringify({
        account_names: accountNames,
        delete_exported_accounts: shouldDelete
      })
    });

    if (!r.ok) {
      const data = await r.json().catch(() => ({}));
      showToast(data.detail || '批量导出失败', 'error');
      return;
    }

    const blob = await r.blob();
    const url = window.URL.createObjectURL(blob);
    const contentDisposition = r.headers.get('Content-Disposition') || '';
    const match = contentDisposition.match(/filename="([^"]+)"/);
    const filename = (match && match[1]) ? match[1] : `kiro-accounts-${Date.now()}.zip`;
    const a = document.createElement('a');
    a.style.display = 'none';
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    window.URL.revokeObjectURL(url);
    a.remove();

    if (shouldDelete) {
      selectedAccounts.clear();
      showToast(`导出成功并已删除 ${accountNames.length} 个账号`, 'success');
    } else {
      showToast(`导出成功，共 ${accountNames.length} 个账号`, 'success');
    }
    loadDashboard();
  } catch (e) {
    showToast('网络错误', 'error');
  }
}

async function saveAccountProxy(name) {
  const inp = document.getElementById(`proxy-input-${name}`);
  if (!inp) return;
  const proxy_url = inp.value.trim();
  try {
    const r = await apiFetch(`/admin/accounts/${encodeURIComponent(name)}/proxy`, {
      method: 'PATCH',
      body: JSON.stringify({ proxy_url: proxy_url || '' }),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      showToast(data.detail || `保存失败 (${r.status})`, 'error');
      return;
    }
    inp.value = '';
    showToast('代理已更新', 'success');
    await loadDashboard();
  } catch (e) {
    showToast(String(e), 'error');
  }
}

async function downloadAccountFile(accountName, fileName) {
  try {
    const res = await fetch(`${BASE}/admin/accounts/${accountName}/files/${fileName}`, {
      headers: { 'X-Admin-Key': apiKey }
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      showToast('下载失败: ' + (data.detail || res.statusText), 'error');
      return;
    }
    const blob = await res.blob();
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.style.display = 'none';
    a.href = url;
    a.download = fileName;
    document.body.appendChild(a);
    a.click();
    window.URL.revokeObjectURL(url);
    a.remove();
  } catch (e) {
    showToast('下载失败: 网络错误', 'error');
  }
}

async function accountAction(name, action) {
  try {
    const r = await apiFetch(`/admin/accounts/${encodeURIComponent(name)}/${action}`, { method: 'POST' });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) { showToast(data.detail || `操作失败`, 'error'); return; }
    showToast(`${name}: ${action} ✓`, 'success');
    loadDashboard();
  } catch (e) { showToast('网络错误', 'error'); }
}

async function refreshAllQuotas() {
  const btn = event.target;
  const originalText = btn.textContent;
  btn.disabled = true;
  btn.textContent = '🔄 刷新中…';

  try {
    const r = await apiFetch('/admin/accounts/quota-refresh-all', { method: 'POST' });
    const data = await r.json().catch(() => ({}));

    if (!r.ok) {
      showToast(data.detail || '批量刷新失败', 'error');
      return;
    }

    const { total, refreshed, failed, new_accounts = 0 } = data;
    let message = '';

    if (new_accounts > 0) {
      message = `发现 ${new_accounts} 个新账号！`;
    }

    if (failed > 0) {
      message += (message ? ' ' : '') + `刷新完成: ${refreshed}/${total} 成功，${failed} 失败`;
      showToast(message, 'warning');
    } else {
      message += (message ? ' ' : '') + `全部刷新成功 (${refreshed}/${total})`;
      showToast(message, 'success');
    }

    // Reload dashboard to show updated quotas and new accounts
    loadDashboard();
  } catch (e) {
    showToast('网络错误', 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = originalText;
  }
}

async function autoImportHostCacheAccount() {
  const shouldImport = await showConfirm({
    title: '自动获取 Kiro 账号？',
    message: '将从当前主机 SSO cache 目录读取凭证并自动添加到账号列表。',
    okText: '开始获取',
    type: 'primary'
  });
  if (!shouldImport) return;

  const shouldDeleteSource = window.confirm(
    '导入成功后，是否自动删除主机 SSO cache 中的这两个 JSON 文件？\n\n选择“确定”会删除，方便你重新注册 Kiro 账号。'
  );

  try {
    const r = await apiFetch('/admin/accounts/import-host-cache', {
      method: 'POST',
      body: JSON.stringify({ delete_source_files: shouldDeleteSource })
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      showToast(data.detail || '自动获取失败', 'error');
      return;
    }

    const deleted = Array.isArray(data.deleted_source_files) ? data.deleted_source_files.length : 0;
    const suffix = deleted > 0 ? `，并已删除源文件 ${deleted} 个` : '';
    showToast(`已自动添加账号 ${data.account_name}${suffix}`, 'success');
    loadDashboard();
  } catch (e) {
    showToast('网络错误', 'error');
  }
}

async function switchHostAccount(name) {
  const ok = await showConfirm({
    title: '确认切换本地账号？',
    message: `这会将账号 "${name}" 的凭证同步到主机 AWS 缓存目录。如果您正在使用 IDE，完成后点击插件刷新按钮即可生效。`,
    okText: '确认切换',
    type: 'primary'
  });
  if (!ok) return;

  try {
    const r = await apiFetch(`/admin/accounts/${encodeURIComponent(name)}/switch`, { method: 'POST' });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) { 
      showToast(data.detail || `切换失败`, 'error'); 
      return; 
    }
    showToast(`成功切换主机账号为: ${name}`, 'success');
    // 重要：切换后延迟一小段时间再刷新，确保后端文件写入完成且系统缓存同步
    setTimeout(() => loadDashboard(), 300);
  } catch (e) { showToast('网络错误', 'error'); }
}

async function confirmDelete(name) {
  const ok = await showConfirm({
    title: '确认删除账号？',
    message: `您确定要永久删除账号 "${name}" 吗？此操作将彻底删除凭证文件目录且不可恢复。`,
    okText: '确认删除',
    type: 'danger'
  });
  if (!ok) return;

  try {
    const r = await apiFetch(`/admin/accounts/${encodeURIComponent(name)}`, { method: 'DELETE' });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) { showToast(data.detail || '删除失败', 'error'); return; }
    showToast(`账号 ${name} 已删除`, 'success');
    loadDashboard();
  } catch (e) { showToast('网络错误', 'error'); }
}

// ═══════════════════════════════════════════════════════════
// Add Account Modal
// ═══════════════════════════════════════════════════════════
function openAddModal()  { document.getElementById('add-modal').classList.add('open'); }
function closeAddModal() {
  document.getElementById('add-modal').classList.remove('open');
  document.getElementById('add-error').textContent = '';
  resetDrop('auth-drop', 'auth-file');
  resetDrop('dev-drop',  'dev-file');
  resetDrop('zip-drop',  'zip-file');
}

function onDragOver(e, id)  { e.preventDefault(); document.getElementById(id).classList.add('drag'); }
function onDragLeave(id)    { document.getElementById(id).classList.remove('drag'); }
function onDrop(e, dropId, inputId) {
  e.preventDefault();
  document.getElementById(dropId).classList.remove('drag');
  const file = e.dataTransfer.files[0];
  if (!file) return;
  const dt = new DataTransfer(); dt.items.add(file);
  document.getElementById(inputId).files = dt.files;
  updateDropLabel(dropId, file.name);
}
function onFileSelect(input, dropId) {
  if (input.files[0]) updateDropLabel(dropId, input.files[0].name);
}
function updateDropLabel(id, name) {
  const el = document.getElementById(id);
  el.textContent = '✅ ' + name;
  el.classList.add('has-file');
}
function resetDrop(dropId, inputId) {
  const el = document.getElementById(dropId);
  el.textContent = '📄 点击选择或拖放文件';
  el.className = 'field-drop';
  document.getElementById(inputId).value = '';
}

async function submitAddAccount() {
  const authFile = document.getElementById('auth-file').files[0];
  const devFile  = document.getElementById('dev-file').files[0];
  const errEl = document.getElementById('add-error');
  const btn   = document.getElementById('add-submit-btn');
  if (!authFile) { errEl.textContent = '请先选择 kiro-auth-token.json'; return; }
  if (!devFile) { errEl.textContent = '请先选择 {hash}.json (SSO 凭证)'; return; }
  const fd = new FormData();
  fd.append('auth_token_file', authFile, authFile.name);
  if (devFile) fd.append('device_reg_file', devFile, devFile.name);
  btn.disabled = true; btn.textContent = '添加中…'; errEl.textContent = '';
  try {
    const r = await fetch(BASE + '/admin/accounts', { method: 'POST', headers: { 'X-Admin-Key': apiKey }, body: fd });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) { errEl.textContent = '❌ ' + (data.detail || '添加失败'); return; }
    showToast(`账号 ${data.account_name} 添加成功！`, 'success');
    closeAddModal(); loadDashboard();
  } catch (e) { errEl.textContent = '❌ 网络错误'; }
  finally { btn.disabled = false; btn.textContent = '添加账号'; }
}

async function submitImportZipAccounts() {
  const zipFile = document.getElementById('zip-file').files[0];
  const errEl = document.getElementById('add-error');
  const btn = document.getElementById('import-zip-btn');
  if (!zipFile) {
    errEl.textContent = '请先选择账号 ZIP 包';
    return;
  }

  const fd = new FormData();
  fd.append('zip_file', zipFile, zipFile.name);
  btn.disabled = true;
  btn.textContent = '导入中…';
  errEl.textContent = '';

  try {
    const r = await fetch(BASE + '/admin/accounts/import-zip', {
      method: 'POST',
      headers: { 'X-Admin-Key': apiKey },
      body: fd
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      errEl.textContent = '❌ ' + (data.detail || 'ZIP 导入失败');
      return;
    }

    const imported = data.imported_count ?? 0;
    const skipped = data.skipped_count ?? 0;
    if (skipped > 0) {
      const firstReason = data.skipped?.[0]?.reason ? `，示例：${data.skipped[0].reason}` : '';
      showToast(`ZIP导入完成：成功 ${imported}，跳过 ${skipped}${firstReason}`, 'warning');
    } else {
      showToast(`ZIP导入成功：共 ${imported} 个账号`, 'success');
    }

    closeAddModal();
    loadDashboard();
  } catch (e) {
    errEl.textContent = '❌ 网络错误';
  } finally {
    btn.disabled = false;
    btn.textContent = '导入ZIP';
  }
}

// ═══════════════════════════════════════════════════════════
// Config Tab
// ═══════════════════════════════════════════════════════════
const CONFIG_META = {
  pool: {
    label: '账号池',
    fields: {
      cooldown_seconds:     { label: 'COOLDOWN_SECONDS', unit: '秒', hint: '冷却时间，429 错误后等待多久', type: 'number' },
      queue_timeout:        { label: 'QUEUE_TIMEOUT', unit: '秒', hint: '队列超时，等待可用账号的最长时间', type: 'number' },
      quota_check_interval: { label: 'QUOTA_CHECK_INTERVAL', unit: '秒', hint: '配额检查间隔，自动检查配额的频率', type: 'number' },
    }
  },
  timeout: {
    label: '超时设置',
    fields: {
      first_token_timeout:     { label: 'FIRST_TOKEN_TIMEOUT', unit: '秒', hint: '首 Token 超时', type: 'number' },
      first_token_max_retries: { label: 'FIRST_TOKEN_MAX_RETRIES', unit: '次', hint: '首 Token 最大重试', type: 'number' },
      streaming_read_timeout:  { label: 'STREAMING_READ_TIMEOUT', unit: '秒', hint: '流式读取超时', type: 'number' },
    }
  },
  reasoning: {
    label: '推理 / Thinking',
    fields: {
      fake_reasoning:            { label: 'FAKE_REASONING_ENABLED', hint: '模拟推理，是否启用 fake thinking', type: 'select', 
                                   options: { 'true': '开启', 'false': '关闭' } },
      fake_reasoning_max_tokens: { label: 'FAKE_REASONING_MAX_TOKENS', hint: '推理最大 Tokens', type: 'number' },
      fake_reasoning_handling:   { label: 'FAKE_REASONING_HANDLING', hint: '推理内容处理方式', type: 'select', 
                                   options: { 
                                     'as_reasoning_content': 'as_reasoning_content (作为推理字段 - 推荐 3.7)', 
                                     'as_text_prefix': 'as_text_prefix (作为正文前缀)', 
                                     'strip': 'strip (直接丢弃)' 
                                   } },
    }
  },
  logging: {
    label: '日志 / 调试',
    fields: {
      log_level:        { label: 'LOG_LEVEL', hint: '日志级别', type: 'select', 
                           options: { 'DEBUG': 'DEBUG (详细)', 'INFO': 'INFO (常规)', 'WARNING': 'WARNING (警告)', 'ERROR': 'ERROR (错误)' } },
      debug_mode:       { label: 'DEBUG_MODE', hint: '请求调试模式 (off/errors/all)', type: 'select', 
                           options: { 'off': 'off (禁用)', 'errors': 'errors (仅错误 - 推荐)', 'all': 'all (全量请求)' } },
      log_history_size: { label: 'LOG_HISTORY_SIZE', hint: '日志历史条数，内存中保留的最近条数', type: 'number' },
    }
  },
  accounts: {
    label: '账号目录 / 模式',
    fields: {
      multi_creds_dir: { label: 'KIRO_MULTI_CREDS_DIR', type: 'text', hint: '设置后切换多账号模式，置空用 .env' },
      refresh_token: { label: 'REFRESH_TOKEN', type: 'text', hint: '单账号 Refresh Token，置空用 .env' },
      kiro_creds_file: { label: 'KIRO_CREDS_FILE', type: 'text', hint: 'Kiro IDE 凭证文件路径，置空用 .env' },
      kiro_cli_db_file: { label: 'KIRO_CLI_DB_FILE', type: 'text', hint: 'Kiro CLI 数据库路径，置空用 .env' },
      profile_arn: { label: 'PROFILE_ARN', type: 'text', hint: 'AWS Profile ARN (可选)，置空用 .env' },
      region: { label: 'KIRO_REGION', type: 'text', hint: 'AWS Region (默认 us-east-1)，置空用 .env' },
    }
  },
};

const ENV_HINTS = {
  PROXY_API_KEY:        "连接网关所需的鉴权密钥 (Header: Authorization: Bearer ...)",
  KIRO_MULTI_CREDS_DIR: "多账号模式专用的凭证目录 (扫描子文件夹中的凭证)",
  KIRO_REGION:          "连接 AWS 的区域代码 (示例: us-east-1)",
  KIRO_HOST_CACHE_DIR:  "主机凭证映射路径 (用于一键切换本地 IDE 账号)",
  VPN_PROXY_URL:        "外部网络代理，支持 http:// 或 socks5://",
  SERVER_PORT:          "网关监听端口 (如需修改请编辑 .env 并重启 Docker)",
  SERVER_HOST:          "网关监听地址 (通常为 0.0.0.1)",
  DEBUG_MODE:           "请求调试记录等级 (off / errors / all)",
  REFRESH_TOKEN:        "单账号模式下的离线刷新令牌",
  KIRO_CREDS_FILE:      "单账号模式下的凭证文件路径",
  KIRO_CLI_DB_FILE:     "Kiro CLI 数据库文件路径 (SQLite)",
  PROFILE_ARN:          "AWS IAMIdentityCenter Profile ARN (Enterprise 模式)",
};

async function loadConfig() {
  const grid = document.getElementById('config-grid');
  const envGrid = document.getElementById('env-grid');
  try {
    const r = await apiFetch('/admin/config');
    if (!r.ok) return;
    const data = await r.json();
    renderConfigGrid(data);
    renderEnvGrid(data.env_config || {});
  } catch (e) {
    grid.innerHTML = `<div class="empty-state" style="grid-column:1/-1"><div class="empty-icon">⚠️</div><div class="empty-title">加载失败</div><div class="empty-sub">${e.message}</div></div>`;
  }
}

function renderConfigGrid(data) {
  const effective = data.effective || {};
  const ymlCfg    = data.gateway_yml || {};
  const grid = document.getElementById('config-grid');
  grid.innerHTML = '';

  for (const [section, meta] of Object.entries(CONFIG_META)) {
    const effSec = effective[section] || {};
    const ymlSec = (ymlCfg[section] || {});
    const div = document.createElement('div');
    div.className = 'config-group';
    let inner = `<div class="config-group-title">${meta.label}</div>`;

    for (const [key, f] of Object.entries(meta.fields)) {
      // Use effective value (actual running), or fallback to yml override
      const effVal = effSec[key];
      const ymlVal = ymlSec[key];
      const displayVal = (effVal !== undefined && effVal !== null) ? effVal : (ymlVal !== undefined && ymlVal !== null ? ymlVal : '');
      const isOverridden = ymlVal !== undefined && ymlVal !== null;

      let inputHtml = '';
      if (f.type === 'select') {
        let opts = '';
        if (Array.isArray(f.options)) {
          opts = f.options.map(o => `<option value="${o}" ${String(displayVal) === o ? 'selected' : ''}>${o}</option>`).join('');
        } else {
          opts = Object.entries(f.options).map(([val, label]) => 
            `<option value="${val}" ${String(displayVal) === val ? 'selected' : ''}>${label}</option>`
          ).join('');
        }
        inputHtml = `<select class="config-val-select" data-section="${section}" data-key="${key}" onchange="onConfigChange(this,'${section}','${key}')">
          ${opts}
        </select>`;
      } else {
        inputHtml = `<input class="config-val-input" type="${f.type === 'number' ? 'number' : 'text'}"
          value="${escHtml(String(displayVal))}" step="any"
          data-section="${section}" data-key="${key}"
          oninput="onConfigChange(this,'${section}','${key}')">`;
      }

      const overrideBadge = isOverridden ? `<div class="config-override-badge">yml覆盖</div>` : '';
      const unitSpan = f.unit ? `<span style="font-size:10px;color:var(--text3);margin-left:4px">${f.unit}</span>` : '';
      inner += `
        <div class="config-row">
          <div class="config-key-wrap">
            <div class="config-key">${f.label}${unitSpan}</div>
            ${f.hint ? `<div class="config-hint">${f.hint}</div>` : ''}
            ${overrideBadge}
          </div>
          ${inputHtml}
        </div>`;
    }
    div.innerHTML = inner;
    grid.appendChild(div);
  }
}

function renderEnvGrid(envCfg) {
  const grid = document.getElementById('env-grid');
  grid.innerHTML = Object.entries(envCfg).map(([section, fields]) => {
    const rows = Object.entries(fields).map(([k, v]) => {
      const hint = ENV_HINTS[k] ? `<div class="config-hint">${ENV_HINTS[k]}</div>` : '';
      return `<div class="config-row">
        <div class="config-key-wrap">
          <div class="config-key">${k}</div>
          ${hint}
        </div>
        <input class="config-val-input readonly" value="${escHtml(String(v))}" readonly>
      </div>`;
    }).join('');
    return `<div class="config-group"><div class="config-group-title">${section}</div>${rows}</div>`;
  }).join('');
}

function onConfigChange(el, section, key) {
  if (!configDraft[section]) configDraft[section] = {};
  let v = el.value;
  if (v === 'true') v = true;
  else if (v === 'false') v = false;
  configDraft[section][key] = v === '' ? null : (el.type === 'number' ? Number(v) : v);
  const el2 = document.getElementById('save-status');
  el2.textContent = '● 有未保存的修改';
  el2.className = 'save-status saving';
}

async function saveConfig() {
  const statusEl = document.getElementById('save-status');
  statusEl.textContent = '保存中…'; statusEl.className = 'save-status saving';

  // Build payload from draft. We include nulls to allow unsetting overrides.
  const payload = {};
  for (const [section, fields] of Object.entries(configDraft)) {
    if (Object.keys(fields).length) {
      payload[section] = fields;
    }
  }

  if (Object.keys(payload).length === 0) {
    statusEl.textContent = '无任何更改'; statusEl.className = 'save-status';
    return;
  }

  try {
    const r = await apiFetch('/admin/config', { method: 'PATCH', body: JSON.stringify(payload) });
    const res = await r.json().catch(() => ({}));
    if (!r.ok) {
      statusEl.textContent = '❌ ' + (res.detail || '保存失败'); statusEl.className = 'save-status error';
      return;
    }
    statusEl.textContent = res.pool_reinitialized ? '✅ 已保存（账号池已重新初始化）' : '✅ 已保存并生效';
    statusEl.className = 'save-status saved';
    configDraft = {};
    showToast('配置已保存并生效 ✓', 'success');
    loadConfig(); // reload to get updated effective values
  } catch (e) {
    statusEl.textContent = '❌ 网络错误'; statusEl.className = 'save-status error';
  }
}

// ═══════════════════════════════════════════════════════════
// Logs — SSE via fetch ReadableStream (supports custom headers)
// ═══════════════════════════════════════════════════════════
function stopSSE() {
  if (sseAbortController) { sseAbortController.abort(); sseAbortController = null; }
}

function startSSE() {
  stopSSE();
  updateSSEStatus('connecting');
  sseAbortController = new AbortController();
  _runSSE(sseAbortController);
}

async function _runSSE(ctrl) {
  try {
    const resp = await fetch(BASE + '/admin/logs/stream', {
      headers: { 'X-Admin-Key': apiKey },
      signal: ctrl.signal,
    });
    if (!resp.ok) {
      updateSSEStatus('error');
      if (!ctrl.signal.aborted) setTimeout(() => { if (!ctrl.signal.aborted) _runSSE(ctrl); }, 3000);
      return;
    }
    updateSSEStatus('connected');

    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = '';

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl;
      // Parse SSE: each event ends with \n\n
      while ((nl = buf.indexOf('\n\n')) !== -1) {
        const block = buf.slice(0, nl);
        buf = buf.slice(nl + 2);
        for (const line of block.split('\n')) {
          if (line.startsWith('data: ')) {
            try {
              const entry = JSON.parse(line.slice(6));
              // Handle different event types
              if (entry.type === 'log') {
                appendLog(entry);
              } else if (entry.type === 'status') {
                updateDashboardFromStatus(entry.data);
              }
            } catch (e) { /* skip malformed */ }
          }
        }
      }
    }
  } catch (e) {
    if (ctrl.signal.aborted) return; // intentional stop
  }
  updateSSEStatus('error');
  // Reconnect after 3s unless aborted
  setTimeout(() => { if (!ctrl.signal.aborted) _runSSE(ctrl); }, 3000);
}

function updateSSEStatus(state) {
  const el = document.getElementById('sse-status');
  const map = {
    connecting: ['● 连接中', 'log-sse-status sse-connecting'],
    connected:  ['● 已连接', 'log-sse-status sse-connected'],
    error:      ['● 已断开', 'log-sse-status sse-error'],
  };
  const [txt, cls] = map[state] || map.error;
  el.textContent = txt; el.className = cls;
}

function appendLog(entry) {
  if (paused) return;
  const level = (entry.level || 'INFO').toUpperCase();
  const normLevel = level === 'WARN' ? 'WARNING' : level;

  const div = document.createElement('div');
  div.className = 'log-line';
  div.dataset.level = normLevel;
  div.dataset.msg   = (entry.message || '').toLowerCase();

  const levelCls = { DEBUG:'log-debug', INFO:'log-info', SUCCESS:'log-success',
    WARNING:'log-warning', ERROR:'log-error', CRITICAL:'log-critical' }[normLevel] || 'log-info';

  div.innerHTML =
    `<span class="log-time">${escHtml(entry.time || '')}</span>` +
    `<span class="log-level ${levelCls}">${normLevel.slice(0,5)}</span>` +
    `<span class="log-msg">${escHtml(entry.message || '')}</span>` +
    `<span class="log-module">${escHtml(entry.module || '')}</span>`;

  // Apply current filters
  if (!activeLevels.has(normLevel)) div.classList.add('log-hidden');
  const kw = (document.getElementById('log-search').value || '').toLowerCase();
  if (kw && !div.dataset.msg.includes(kw)) div.classList.add('log-hidden');

  const stream = document.getElementById('log-stream');
  stream.appendChild(div);

  // Keep max 200 DOM entries
  while (stream.children.length > 200) stream.removeChild(stream.firstChild);

  logCount++;
  document.getElementById('log-count').textContent = stream.children.length + ' 条';

  if (autoScroll) stream.scrollTop = stream.scrollHeight;
}

function clearLogs() {
  document.getElementById('log-stream').innerHTML = '';
  logCount = 0;
  document.getElementById('log-count').textContent = '0 条';
}

function togglePause() {
  paused = !paused;
  const btn = document.getElementById('pause-btn');
  btn.textContent = paused ? '▶ 继续' : '⏸ 暂停';
  btn.className = 'log-ctrl-btn ' + (paused ? 'pause-active' : '');
}

function toggleAutoScroll() {
  autoScroll = !autoScroll;
  const btn = document.getElementById('scroll-btn');
  btn.className = 'log-ctrl-btn ' + (autoScroll ? 'autoscroll-on' : '');
  btn.textContent = autoScroll ? '⬇ 自动滚动' : '📌 固定';
  if (autoScroll) {
    const s = document.getElementById('log-stream');
    s.scrollTop = s.scrollHeight;
  }
}

function toggleLevel(btn) {
  const level = btn.dataset.level;
  if (activeLevels.has(level)) {
    activeLevels.delete(level);
    btn.className = 'level-btn inactive';
  } else {
    activeLevels.add(level);
    btn.className = `level-btn active-${level}`;
  }
  applyLogFilter();
}

function applyLogFilter() {
  const kw = (document.getElementById('log-search').value || '').toLowerCase();
  document.querySelectorAll('.log-line').forEach(el => {
    const lvl = el.dataset.level || 'INFO';
    const msg = el.dataset.msg || '';
    el.classList.toggle('log-hidden', !activeLevels.has(lvl) || (kw && !msg.includes(kw)));
  });
}

// ═══════════════════════════════════════════════════════════
// Toast
// ═══════════════════════════════════════════════════════════
function showToast(msg, type = 'info') {
  const c = document.getElementById('toast-container');
  const d = document.createElement('div');
  d.className = `toast ${type}`;
  d.textContent = msg;
  c.appendChild(d);
  setTimeout(() => d.remove(), 2900);
}

// ═══════════════════════════════════════════════════════════
// Utils
// ═══════════════════════════════════════════════════════════
function escHtml(s) {
  return String(s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;');
}
function escAttr(s) { return String(s).replace(/"/g,'&quot;'); }

/** ─── Custom Confirmation Modal ────────────────────────── */
function showConfirm({ title, message, icon = '⚠️', okText = '确认执行', cancelText = '取消', type = 'danger' }) {
  return new Promise((resolve) => {
    const modal = document.getElementById('confirm-modal');
    const titleEl = document.getElementById('confirm-title');
    const msgEl = document.getElementById('confirm-msg');
    const iconEl = document.getElementById('confirm-icon');
    const okBtn = document.getElementById('confirm-ok-btn');
    const cancelBtn = document.getElementById('confirm-cancel-btn');

    titleEl.textContent = title;
    msgEl.textContent = message;
    iconEl.textContent = icon;
    okBtn.textContent = okText;
    cancelBtn.textContent = cancelText;

    // Reset and apply button classes
    okBtn.className = 'btn';
    okBtn.classList.add(type === 'danger' ? 'btn-danger' : 'btn-primary');

    modal.classList.add('open');

    const cleanup = (val) => {
      modal.classList.remove('open');
      okBtn.onclick = null;
      cancelBtn.onclick = null;
      resolve(val);
    };

    okBtn.onclick = () => cleanup(true);
    cancelBtn.onclick = () => cleanup(false);
    
    // Also close on background click
    modal.onclick = (e) => {
      if (e.target === modal) cleanup(false);
    };
  });
}

// ═══════════════════════════════════════════════════════════
// Mobile Drawer
// ═══════════════════════════════════════════════════════════
function toggleDrawer() {
  const drawer = document.getElementById('drawer');
  const overlay = document.getElementById('drawer-overlay');
  const toggle = document.getElementById('menu-toggle');
  
  const isOpen = drawer.classList.contains('open');
  
  if (isOpen) {
    closeDrawer();
  } else {
    drawer.classList.add('open');
    overlay.classList.add('open');
    toggle.classList.add('active');
    document.body.style.overflow = 'hidden';
  }
}

function closeDrawer() {
  const drawer = document.getElementById('drawer');
  const overlay = document.getElementById('drawer-overlay');
  const toggle = document.getElementById('menu-toggle');
  
  drawer.classList.remove('open');
  overlay.classList.remove('open');
  toggle.classList.remove('active');
  document.body.style.overflow = '';
}

function switchTabMobile(name) {
  // Switch tab
  switchTab(name);
  
  // Update drawer nav buttons
  document.querySelectorAll('.drawer-nav-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('drawer-tab-' + name).classList.add('active');
  
  // Close drawer
  closeDrawer();
}

// Update drawer info when dashboard updates
const originalRenderDashboard = renderDashboard;
renderDashboard = function(data) {
  originalRenderDashboard(data);
  
  // Sync drawer badge and refresh time
  const badge = document.getElementById('drawer-mode-badge');
  const isMulti = data.mode === 'multi';
  badge.textContent = isMulti ? 'Multi' : 'Single';
  badge.className = 'badge-mode ' + (isMulti ? 'multi' : 'single');
  
  document.getElementById('drawer-last-refresh').textContent = '刷新于 ' + new Date().toLocaleTimeString();
};

// ═══════════════════════════════════════════════════════════
// Models Control
// ═══════════════════════════════════════════════════════════
let modelControls = {};

async function refreshModelControls() {
  const loading = document.getElementById('models-loading');
  const content = document.getElementById('models-content');
  if (!loading || !content) return;

  loading.style.display = '';
  content.style.display = 'none';

  try {
    const r = await apiFetch('/admin/models/control');
    if (r.status === 401) { doLogout(); return; }
    if (!r.ok) {
      showToast('获取模型控制数据失败', 'error');
      loading.innerHTML = '<div class="empty-icon">❌</div><div class="empty-title">加载失败</div><div class="empty-sub">无法获取模型列表</div>';
      return;
    }

    const data = await r.json();
    modelControls = data.controls || {};

    const planTypes = data.plan_types || [];
    const modelsByPlan = data.models_by_plan || {};

    if (planTypes.length === 0) {
      loading.innerHTML = '<div class="empty-icon">🤖</div><div class="empty-title">暂无账号</div><div class="empty-sub">请先添加账号后再管理模型</div>';
      return;
    }

    let html = '';
    planTypes.forEach(planType => {
      const models = modelsByPlan[planType] || [];
      html += `
        <div class="model-section">
          <div class="model-section-header">
            <div class="model-section-title">
              <span class="plan-badge" data-plan="${escAttr(planType)}">${escHtml(planType)}</span>
              <span style="font-size:14px;font-weight:600;color:var(--text)">可用模型</span>
            </div>
            <span class="model-section-count">${models.length} 个模型</span>
          </div>
          <div class="model-list" data-plan="${escAttr(planType)}">
            ${models.map(modelId => `
              <div class="model-item">
                <span class="model-name" title="${escAttr(modelId)}">${escHtml(modelId)}</span>
                <button class="model-toggle ${modelControls[planType]?.[modelId] ? 'active' : ''}"
                  data-plan="${escAttr(planType)}" data-model="${escAttr(modelId)}"
                  onclick="toggleModelControl(this)"></button>
              </div>
            `).join('')}
          </div>
        </div>
      `;
    });

    content.innerHTML = html;
    loading.style.display = 'none';
    content.style.display = '';
  } catch (e) {
    console.error('Error loading model controls:', e);
    showToast('加载模型控制失败', 'error');
    loading.innerHTML = '<div class="empty-icon">❌</div><div class="empty-title">加载失败</div><div class="empty-sub">网络错误</div>';
  }
}

function toggleModelControl(btn) {
  const planType = btn.dataset.plan;
  const modelId = btn.dataset.model;

  if (!modelControls[planType]) modelControls[planType] = {};

  modelControls[planType][modelId] = !modelControls[planType][modelId];
  btn.classList.toggle('active', modelControls[planType][modelId]);
}

async function saveModelControls() {
  const statusEl = document.getElementById('models-save-status');
  if (statusEl) statusEl.textContent = '保存中…';

  try {
    const r = await apiFetch('/admin/models/control', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ controls: modelControls })
    });

    if (r.status === 401) { doLogout(); return; }
    if (r.ok) {
      showToast('模型配置已保存', 'success');
      if (statusEl) statusEl.textContent = '已保存';
      setTimeout(() => { if (statusEl) statusEl.textContent = '准备就绪'; }, 1200);
    } else {
      showToast('保存失败', 'error');
      if (statusEl) statusEl.textContent = '保存失败';
    }
  } catch (e) {
    console.error('Error saving model controls:', e);
    showToast('保存失败', 'error');
    if (statusEl) statusEl.textContent = '保存失败';
  }
}
