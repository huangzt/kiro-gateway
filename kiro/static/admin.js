// ═══════════════════════════════════════════════════════════
// State
// ═══════════════════════════════════════════════════════════
let apiKey = localStorage.getItem('kiro_admin_key') || '';
let autoRefreshTimer = null;
let sseAbortController = null;
let paused = false;
let autoScroll = true;
let activeLevels = new Set(['DEBUG','INFO','SUCCESS','WARNING','ERROR','CRITICAL']);
let configDraft = {};   // section → key → value (only modified values)
let logCount = 0;

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
  clearInterval(autoRefreshTimer);
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
  autoRefreshTimer = setInterval(() => {
    if (document.getElementById('pane-dashboard').classList.contains('active')) loadDashboard();
  }, 10000);
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
  if (name === 'dashboard') loadDashboard();
  if (name === 'config') loadConfig();
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

function renderDashboard(data) {
  document.getElementById('s-total').textContent     = data.total_accounts ?? '—';
  document.getElementById('s-active').textContent    = data.available ?? '—';
  document.getElementById('s-cooling').textContent   = data.cooling_down ?? '—';
  document.getElementById('s-exhausted').textContent = data.exhausted ?? '—';
  document.getElementById('s-disabled').textContent  = data.disabled ?? '—';

  const badge = document.getElementById('mode-badge');
  const isMulti = data.mode === 'multi';
  badge.textContent = isMulti ? 'Multi' : 'Single';
  badge.className = 'badge-mode ' + (isMulti ? 'multi' : 'single');

  document.getElementById('last-refresh').textContent = '刷新于 ' + new Date().toLocaleTimeString();

  const accounts = data.accounts || [];
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
    const { used = 0, limit = 0, remaining = 0, plan = '', next_reset = '—' } = a.quota;
    const pct = limit > 0 ? (used / limit * 100) : 0;
    const fillCls = pct >= 95 ? 'danger' : pct >= 80 ? 'warning' : '';
    const formatNextReset = (str) => {
      if (!str || str === '—') return '—';
      try {
        const d = new Date(str);
        if (isNaN(d.getTime())) return str;
        const fmt = d => `${d.getFullYear()}/${d.getMonth()+1}/${d.getDate()}`;
        const reset = fmt(d);
        const trialD = new Date(d.getTime() + 7 * 24 * 3600 * 1000);
        const diffDays = Math.ceil((trialD - new Date()) / (24 * 3600 * 1000));
        let remColor = 'var(--green)';
        let remText = ` (剩${diffDays}天)`;
        if (diffDays <= 0) { remColor = 'var(--red)'; remText = ' (已过期)'; }
        else if (diffDays <= 2) { remColor = 'var(--yellow)'; }
        const remSpan = `<span style="color:${remColor};font-size:10px">${remText}</span>`;
        return `<span style="color:var(--accent);font-weight:600">重置 ${reset}</span> · <span style="color:var(--blue);font-weight:600">试用 ${fmt(trialD)}</span>${remSpan}`;
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
          <span>${formatNextReset(next_reset)}</span>
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

  // Active status badge for host (放在状态徽章前面)
  const activeHostBadge = a.is_active_on_host 
    ? `<span class="host-active-badge" title="主机当前正在使用此账号">🏠 当前选用</span>` 
    : '';

  return `
    <div class="account-card status-${status} ${a.is_active_on_host ? 'active-host' : ''}" id="card-${escAttr(a.name)}">
      <div class="card-top" style="flex-direction: column; align-items: stretch; gap: 6px; margin-bottom: 14px;">
        <div style="display: flex; justify-content: space-between; align-items: center;">
          <div class="card-name" style="margin-bottom: 0">📁 ${escHtml(a.name)}</div>
          <div style="display: flex; align-items: center; gap: 6px;">
            ${activeHostBadge}
            <span class="status-badge ${badgeCls}">${badgeTxt}</span>
          </div>
        </div>
        <div class="card-email" style="white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">
          ${escHtml(a.email || '邮箱未知')}
          ${a.quota?.plan ? ` · <span style="color:var(--text3); font-weight:500; font-size:11px">${escHtml(a.quota.plan)}</span>` : ''}
        </div>
      </div>
      ${quotaHtml}
      ${cooldownHtml}
      <div class="card-stats">
        <div class="cs-item"><div class="cs-label">活跃</div><div class="cs-val">${a.active_requests ?? 0}</div></div>
        <div class="cs-item"><div class="cs-label">累计</div><div class="cs-val">${a.total_requests ?? 0}</div></div>
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
            try { appendLog(JSON.parse(line.slice(6))); } catch (e) { /* skip malformed */ }
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

  // Keep max 2000 DOM entries
  while (stream.children.length > 2000) stream.removeChild(stream.firstChild);

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
