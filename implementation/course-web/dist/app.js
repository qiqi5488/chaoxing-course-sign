import { store } from './state.js';
import { createScanPage } from './scan-page.js';
import { createAccountDetail } from './account-detail.js';
import { createMonitorPage } from './monitor-page.js';
import { createSignHistoryPage } from './sign-history-page.js';

const paths = {
  users: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2m20 0v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/><circle cx="9" cy="7" r="4"/>',
  shield: '<path d="M12 22s8-4 8-11V5l-8-3-8 3v6c0 7 8 11 8 11Z"/><path d="m9 12 2 2 4-4"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  arrow: '<path d="M5 12h14m-6-6 6 6-6 6"/>',
  chevron: '<path d="m9 5 7 7-7 7"/>',
  close: '<path d="m6 6 12 12M6 18 18 6"/>',
  search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
  help: '<circle cx="12" cy="12" r="9"/><path d="M9.3 9a2.8 2.8 0 0 1 5.4 1c0 2-2.7 2-2.7 4m0 3h.01"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5m0-9h.01"/>',
  lock: '<rect x="5" y="10" width="14" height="11" rx="3"/><path d="M8 10V7a4 4 0 0 1 8 0v3m-4 5v2"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  school: '<path d="m3 9 9-5 9 5-9 5-9-5Zm4 3v5c3 2 7 2 10 0v-5m4-3v7"/>',
  more: '<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
  star: '<path d="m12 3 2.8 5.8 6.4.9-4.6 4.5 1.1 6.3-5.7-3-5.7 3 1.1-6.3-4.6-4.5 6.4-.9L12 3Z"/>',
  list: '<rect x="3" y="3" width="18" height="18" rx="4"/><path d="m7 8 1 1 2-2m3 1h4M7 15h3m3 0h4"/>',
  eye: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/>',
  eyeOff: '<path d="m3 3 18 18M10.6 5.1 12 5c6.5 0 10 7 10 7a20 20 0 0 1-3.1 3.8M6.5 6.5A23 23 0 0 0 2 12s3.5 7 10 7a11 11 0 0 0 5.5-1.5m-7.6-7.6a3 3 0 0 0 4.2 4.2"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M5 21v-2a7 7 0 0 1 14 0v2"/>',
  refresh: '<path d="M20 7v5h-5M4 17v-5h5m-4-4a7 7 0 0 1 12-3l3 3M4 16l3 3a7 7 0 0 0 12-3"/>',
  trash: '<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7"/>',
  edit: '<path d="m15 5 4 4M4 20l4-1L20 7a2.8 2.8 0 0 0-4-4L4 15v5Z"/>',
  logout: '<path d="M9 21H4V3h5m4 5 4 4-4 4m-5-4h13"/>',
  qr: '<path d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5"/><rect x="7" y="7" width="3" height="3"/><rect x="14" y="7" width="3" height="3"/><rect x="7" y="14" width="3" height="3"/><path d="M14 14h3v3m-3-1v1"/>',
  image: '<rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8" cy="8" r="1.5"/><path d="m21 15-5-5L5 21"/>',
  back: '<path d="m10 5-7 7 7 7M3 12h18"/>',
  book: '<path d="M12 6v15m0-15C9 3 5 3 2 4v15c3-1 7-1 10 2m0-15c3-3 7-3 10-2v15c-3-1-7-1-10 2"/>',
};
const icon = (name, size = 20) => `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.user}</svg>`;
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const main = document.querySelector('#main');
const sheet = document.querySelector('#sheet');
const toastEl = document.querySelector('#toast');
let query = '', filter = 'all', grantFilter = 'all', bulkMode = false, selected = new Set();
let busy = false, toastTimer, returnFocus;
const detailId = () => { try { return decodeURIComponent(location.hash.match(/^#\/accounts\/([^/?]+)(?:\?.*)?$/)?.[1] || ''); } catch { return ''; } };
const historyAccountId = () => { try { return decodeURIComponent(location.hash.match(/^#\/monitor\/accounts\/([^/?]+)\/records(?:\?.*)?$/)?.[1] || ''); } catch { return ''; } };
const page = () => historyAccountId() ? 'sign-history' : detailId() ? 'detail' : ({ '#/authorizations': 'authorizations', '#/scan': 'scan', '#/monitor': 'monitor' }[location.hash.split('?')[0]] || 'accounts');
const accountById = id => store.getState().accounts.find(account => account.id === id);
const time = value => value ? new Date(value).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false }) : '—';
const hasGrant = a => a.grant && (!a.grant.expiresAt || Date.parse(a.grant.expiresAt) > Date.now());
const avatar = (a, extra = '') => `<span class="avatar ${esc(a.color)} ${extra}">${esc(Array.from(a.name)[0] || '同')}</span>`;
const status = a => `<span class="status ${esc(a.status)}">${icon(a.status === 'active' ? 'check' : a.status === 'expired' ? 'clock' : 'lock', 11)}${{ active: '已登录', expired: '已失效', unauthorized: '待授权' }[a.status]}</span>`;
const defaultBadge = a => a.id === store.getState().defaultAccountId ? `<span class="default-badge">${icon('star', 9)}默认</span>` : '';

document.querySelector('#help-button').innerHTML = icon('help', 20);
document.querySelector('#service-strip-icon').innerHTML = icon('info', 13);
document.querySelector('#nav-accounts-icon').innerHTML = icon('users', 21);
document.querySelector('#nav-grants-icon').innerHTML = icon('shield', 21);
document.querySelector('#nav-scan-icon').innerHTML = icon('qr', 23);
document.querySelector('#nav-monitor-icon').innerHTML = icon('eye', 21);
const scanPage = createScanPage({ store, esc, icon, avatar, toast });
const accountDetail = createAccountDetail({ store, esc, icon, avatar, status, time, hasGrant });
const monitorPage = createMonitorPage({ store, esc, icon, avatar, time, toast });
const signHistoryPage = createSignHistoryPage({ store, esc, icon, avatar });

function toast(message) {
  clearTimeout(toastTimer);
  toastEl.textContent = message;
  toastEl.classList.add('visible');
  toastTimer = setTimeout(() => toastEl.classList.remove('visible'), 3200);
}

function showError(error) {
  const message = error?.message || '操作未完成，请稍后重试。';
  if (sheet.open) {
    let target = sheet.querySelector('.form-error');
    if (!target) {
      target = document.createElement('div');
      target.className = 'form-error';
      target.setAttribute('role', 'alert');
      sheet.querySelector('.sheet-body').prepend(target);
    }
    target.textContent = message;
    target.scrollIntoView({ block: 'nearest' });
  } else toast(message);
}

async function perform(operation, onSuccess, button) {
  if (busy) return;
  busy = true;
  sheet.querySelectorAll('.form-error').forEach(el => { el.textContent = ''; });
  const controls = [...sheet.querySelectorAll('input, button')].map(el => [el, el.disabled]);
  controls.forEach(([el]) => { el.disabled = true; });
  const label = button?.innerHTML;
  if (button) { button.disabled = true; button.innerHTML = '<span class="spinner" aria-hidden="true"></span>正在处理…'; }
  let result;
  try { result = await operation(); }
  catch (error) { showError(error); return; }
  finally {
    busy = false;
    controls.forEach(([el, disabled]) => { if (el.isConnected) el.disabled = disabled; });
    if (button?.isConnected) { button.disabled = false; button.innerHTML = label; }
  }
  onSuccess?.(result);
}

function filteredAccounts() {
  return store.getState().accounts.filter(a => (filter === 'all' || a.status === filter) &&
    `${a.name} ${a.maskedIdentifier} ${a.school}`.toLowerCase().includes(query.trim().toLowerCase()));
}

function accountCard(a) {
  const note = a.status === 'expired' ? '登录会话需要更新' : a.status === 'unauthorized' ? '尚未授权保存会话' : '会话持续保留';
  return `<article class="account-card ${selected.has(a.id) && bulkMode ? 'selected' : ''}">
    <div class="account-top">${bulkMode ? `<input class="selection-box" type="checkbox" data-select="${esc(a.id)}" aria-label="选择${esc(a.name)}" ${selected.has(a.id) ? 'checked' : ''}>` : ''}
      <button class="account-open" data-action="detail" data-id="${esc(a.id)}" aria-label="查看${esc(a.name)}的账号详情">${avatar(a)}<span><span class="account-name">${esc(a.name)}${defaultBadge(a)}</span><span class="account-identifier">${esc(a.maskedIdentifier)}</span></span></button>${status(a)}
    </div>
    <div class="account-info">${icon('school', 13)}<span>${esc(a.school)} · 学习通账号</span><button class="more-button" data-action="account-actions" data-id="${esc(a.id)}" aria-label="${esc(a.name)}的更多操作">${icon('more', 17)}</button></div>
    <div class="account-bottom"><span class="${a.status === 'expired' ? 'expired-note' : ''}">${note}</span><button class="text-button" data-action="${a.status === 'expired' ? 'relogin' : 'grant'}" data-id="${esc(a.id)}">${a.status === 'expired' ? '重新登录' : a.status === 'unauthorized' ? '去授权' : '管理授权'}${icon('chevron', 12)}</button></div>
  </article>`;
}

function renderList() {
  const target = document.querySelector('#account-list');
  if (!target) return;
  const rows = filteredAccounts();
  target.innerHTML = rows.length ? rows.map(accountCard).join('') : `<div class="empty"><span>${icon('search', 25)}</span><h3>${query ? '没有找到这个账号' : '这里还没有账号'}</h3><p>${query ? '试试姓名，或账号的前后几位。' : '添加账号后，可以统一管理登录与授权。'}</p><button class="text-button" data-action="${query || filter !== 'all' ? 'clear-search' : 'add'}">${query || filter !== 'all' ? '查看全部账号' : '添加第一个账号'}${icon('arrow', 14)}</button></div>`;
  renderBulkBar();
}

function renderBulkBar() {
  document.querySelector('.bulk-bar')?.remove();
  if (!bulkMode || page() !== 'accounts') return;
  const visible = filteredAccounts();
  const all = visible.length > 0 && visible.every(a => selected.has(a.id));
  const bar = document.createElement('div');
  bar.className = 'bulk-bar';
  const scanCount = store.getState().accounts.filter(a => selected.has(a.id) && scanPage.canSelect(a)).length;
  bar.innerHTML = `<label><input class="selection-box" type="checkbox" data-action="select-all" ${all ? 'checked' : ''} ${visible.length ? '' : 'disabled'}>全选 <span>· 已选 ${selected.size} 个</span></label><button class="bulk-remove" data-action="remove-selected" ${selected.size ? '' : 'disabled'}>移除</button><button class="bulk-scan" data-action="scan-selected" ${scanCount ? '' : 'disabled'}>扫码签到</button>`;
  document.querySelector('.app-shell').append(bar);
}

function renderAccounts() {
  const accounts = store.getState().accounts;
  const count = key => accounts.filter(a => a.status === key).length;
  main.innerHTML = `<div class="page-top"><div><p class="eyebrow">YOUR ACCOUNT SPACE</p><h1>账号管理</h1><p>登录状态与会话授权，一处管理。</p></div><span class="page-mark">${icon('users', 22)}</span></div>
    <div class="stats"><div class="stat"><strong>${accounts.length}<span>个</span></strong><p>全部账号</p></div><div class="stat"><strong>${count('active')}<span>个</span></strong><p><i class="status-dot"></i>正常使用</p></div><div class="stat"><strong>${accounts.length - count('active')}<span>个</span></strong><p><i class="status-dot orange"></i>需要处理</p></div></div>
    <button class="add-hero" data-action="add"><span class="hero-text"><span class="hero-kicker">连接你的学习通</span><strong>添加一个签到账号</strong><span class="hero-caption">登录与会话授权，一次完成</span></span><span class="hero-plus">${icon('plus', 26)}</span></button>
    <div class="section-heading"><h2>我的账号<small>${accounts.length}</small></h2><button class="text-button" data-action="bulk">${icon(bulkMode ? 'check' : 'list', 14)}${bulkMode ? '完成管理' : '批量管理'}</button></div>
    <label class="search-box">${icon('search', 17)}<span class="visually-hidden">搜索账号</span><input id="account-search" type="search" placeholder="搜索姓名或账号" value="${esc(query)}" autocomplete="off"></label>
    <div class="filters" aria-label="按登录状态筛选">${[['all', '全部', accounts.length], ['active', '已登录', count('active')], ['unauthorized', '待授权', count('unauthorized')], ['expired', '已失效', count('expired')]].map(([key, label, total]) => `<button class="filter" data-filter="${key}" aria-pressed="${filter === key}">${label}<small>${total}</small></button>`).join('')}</div>
    <div class="account-list" id="account-list"></div>
    <p class="privacy-foot">${icon('lock', 12)}登录密码不会保存在本地</p>`;
  renderList();
}

function grantCard(a) {
  const active = hasGrant(a);
  return `<article class="grant-card"><div class="grant-head">${avatar(a)}<div class="name-block"><strong>${esc(a.name)}</strong><small>${esc(a.maskedIdentifier)}</small></div><span class="status ${active ? 'active' : 'unauthorized'}">${icon(active ? 'shield' : 'lock', 11)}${active ? '已授权' : '待授权'}</span></div>
    <div class="grant-scopes"><span class="scope-chip">${icon('lock', 11)}保存登录会话</span><span class="scope-chip">${icon('user', 11)}读取基本资料</span></div>
    ${active ? '<div class="grant-expiry"><span>持续保留</span><span>可随时撤销</span></div>' : '<div class="grant-expiry"><span>尚未保留该账号的登录会话</span></div>'}
    ${a.status === 'expired' && active ? '<p class="sheet-sub" style="margin:0 0 7px;color:#a78252">授权仍有效，登录会话需要重新验证。</p>' : ''}
    <div class="grant-actions">${active ? `<button class="text-button danger" data-action="revoke" data-id="${esc(a.id)}">撤销授权</button>` : '<span></span>'}<button class="text-button" data-action="grant" data-id="${esc(a.id)}">${active ? '查看授权' : '登录并授权'}${icon('chevron', 12)}</button></div></article>`;
}

function renderAuthorizations() {
  const { accounts, events } = store.getState();
  const authorized = accounts.filter(hasGrant).length;
  const rows = accounts.filter(a => grantFilter === 'all' || (grantFilter === 'active' ? hasGrant(a) : !hasGrant(a)));
  main.innerHTML = `<div class="page-top"><div><p class="eyebrow">PERMISSION, WITH YOU</p><h1>会话授权</h1><p>登录状态，由你决定如何保留。</p></div><span class="page-mark">${icon('shield', 23)}</span></div>
    <div class="grant-intro"><span>${icon('shield', 25)}</span><div><strong>每个账号，都由你掌控</strong><p>会话持续保存，学习通会话失效或主动撤销时结束。</p></div></div>
    <div class="section-heading"><h2>授权中的账号<small>${authorized}</small></h2></div>
    <div class="filters" aria-label="按授权状态筛选">${[['all', '全部', accounts.length], ['active', '已授权', authorized], ['pending', '待授权', accounts.length - authorized]].map(([key, label, total]) => `<button class="filter" data-grant-filter="${key}" aria-pressed="${grantFilter === key}">${label}<small>${total}</small></button>`).join('')}</div>
    ${rows.length ? rows.map(grantCard).join('') : `<div class="empty"><span>${icon('shield', 25)}</span><h3>暂无${grantFilter === 'pending' ? '待授权' : '已授权'}账号</h3><p>添加账号后，可以在这里管理会话授权。</p><button class="text-button" data-action="add">添加账号${icon('arrow', 14)}</button></div>`}
    ${events.length ? `<section class="events"><div class="section-heading"><h2>最近操作</h2></div>${events.slice(0, 5).map(event => `<div class="event"><span>${icon('clock', 14)}</span><div><p>${esc(event.accountName)} · ${esc(event.action)}</p><time>${time(event.at)}</time></div></div>`).join('')}</section>` : ''}
    <p class="privacy-foot">${icon('lock', 12)}会话授权不代表自动提交签到</p>`;
}

function render() {
  const connection = store.getConnection();
  main.setAttribute('aria-busy', String(connection.loading));
  document.querySelector('#service-status').textContent = connection.loading ? '连接服务中' : connection.connected ? '账号中心' : '连接失败';
  document.querySelector('#service-message').textContent = connection.loading ? '正在同步账号状态…' : connection.connected ? '登录成功后自动完成会话授权' : connection.error;
  document.querySelectorAll('[data-action="add"], [data-action="refresh"]').forEach(button => {
    button.disabled = connection.loading || (button.dataset.action === 'add' && !connection.connected);
  });
  const ids = new Set(store.getState().accounts.map(a => a.id));
  selected = new Set([...selected].filter(id => ids.has(id)));
  document.querySelectorAll('[data-nav]').forEach(link => {
    if (link.dataset.nav === (page() === 'detail' ? 'accounts' : page() === 'sign-history' ? 'monitor' : page())) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  });
  document.querySelector('.bulk-bar')?.remove();
  if (!connection.connected) {
    scanPage.leave();
    accountDetail.leave();
    monitorPage.leave();
    signHistoryPage.leave();
    main.innerHTML = `<div class="page-top"><div><p class="eyebrow">YOUR ACCOUNT SPACE</p><h1>账号管理</h1></div></div><div class="empty"><span>${icon(connection.loading ? 'clock' : 'info', 25)}</span><h3>${connection.loading ? '正在连接账号服务' : '暂时无法获取账号'}</h3><p>${connection.loading ? '请稍候，正在读取服务端状态。' : esc(connection.error)}</p>${connection.loading ? '' : '<button class="text-button" data-action="refresh">重新连接</button>'}</div>`;
    return;
  }
  if (page() !== 'sign-history') signHistoryPage.leave();
  if (page() === 'sign-history') { accountDetail.leave(); scanPage.leave(); monitorPage.leave(); signHistoryPage.render(main, historyAccountId()); }
  else if (page() === 'monitor') { accountDetail.leave(); scanPage.leave(); monitorPage.render(main); }
  else {
    monitorPage.leave();
    if (page() === 'scan') { accountDetail.leave(); scanPage.render(main); }
    else {
      scanPage.leave();
      if (page() === 'detail') accountDetail.render(main, detailId());
      else { accountDetail.leave(); if (page() === 'authorizations') renderAuthorizations(); else renderAccounts(); }
    }
  }
}

function openSheet(title, content) {
  if (!sheet.open) returnFocus = document.activeElement;
  sheet.innerHTML = `<div class="sheet-head"><h2 id="sheet-title" tabindex="-1">${title}</h2><button class="icon-button" data-action="close-sheet" aria-label="关闭弹窗">${icon('close', 18)}</button></div><div class="sheet-body">${content}</div>`;
  if (!sheet.open) { sheet.showModal(); document.body.style.overflow = 'hidden'; }
  sheet.scrollTop = 0;
  requestAnimationFrame(() => (sheet.querySelector('[autofocus]') || sheet.querySelector('#sheet-title'))?.focus({ preventScroll: true }));
}

function closeSheet() { if (!busy) sheet.close(); }
sheet.addEventListener('close', () => {
  document.body.style.overflow = '';
  sheet.innerHTML = '';
  (returnFocus?.isConnected ? returnFocus : document.querySelector('[aria-current="page"]'))?.focus({ preventScroll: true });
});
sheet.addEventListener('cancel', e => { if (busy) e.preventDefault(); });
sheet.addEventListener('click', e => {
  if (e.target !== sheet) return;
  const r = sheet.getBoundingClientRect();
  if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) closeSheet();
});

function showLogin(id) {
  const a = id ? accountById(id) : null;
  openSheet(a ? '重新登录账号' : '添加学习通账号', `
    <p class="sheet-sub">${a ? '重新验证登录状态，并保存新的账号会话。' : '登录成功后，直接完成该账号的会话授权。'}</p>
    <form id="login-form" data-account-id="${esc(a?.id || '')}" novalidate>
      <label class="field"><span>学习通账号</span><input id="login-identifier" name="identifier" inputmode="text" autocomplete="off" placeholder="手机号或学号" maxlength="64" value="${esc(a?.maskedIdentifier || '')}" ${a ? 'readonly' : 'autofocus'} aria-describedby="identifier-error"><span class="field-error" id="identifier-error"></span></label>
      <label class="field"><span>登录密码</span><span class="password-wrap"><input id="login-password" name="password" type="password" autocomplete="off" maxlength="128" placeholder="请输入学习通密码" ${a ? 'autofocus' : ''} aria-describedby="password-error"><button type="button" data-action="toggle-password" aria-label="显示密码">${icon('eye', 18)}</button></span><span class="field-error" id="password-error"></span></label>
      ${a ? '' : '<label class="field"><span>账号备注<span class="optional">选填</span></span><input name="nickname" placeholder="例如：我的主账号" maxlength="20" autocomplete="off"></label>'}
      <div class="form-error" role="alert" id="login-error"></div>
      <button class="primary-button" type="submit">登录并授权${icon('arrow', 16)}</button>
      <p class="sheet-note">会话持续保存，可随时撤销</p>
    </form>`);
}

function openGrant(id) {
  const a = accountById(id);
  if (!a) return;
  if (hasGrant(a)) {
    showAuthorization(id);
  } else showLogin(id);
}

function showAuthorization(id) {
  const a = accountById(id);
  if (!a?.grant) return openGrant(id);
  openSheet('会话授权详情', `
    <div class="consent-account">${avatar(a)}<div><strong>${esc(a.name)}</strong><small>${esc(a.maskedIdentifier)}</small></div></div>
    <div class="detail-rows"><div class="detail-row"><span>授权状态</span><span>${hasGrant(a) ? '持续授权' : '尚未授权'}</span></div><div class="detail-row"><span>登录状态</span>${status(a)}</div><div class="detail-row"><span>授权时间</span><span>${time(a.grant.grantedAt)}</span></div><div class="detail-row"><span>会话保留</span><span>学习通会话失效或主动撤销时结束</span></div></div>
    <div class="permission-row"><span class="permission-icon">${icon('lock', 17)}</span><div><strong>保存登录会话</strong><p>不包含保存账号密码的授权</p></div></div><div class="permission-row"><span class="permission-icon">${icon('user', 17)}</span><div><strong>读取基本资料</strong><p>姓名、头像及学校信息</p></div></div>
    <div class="form-notice">${icon('info', 14)}<span>撤销授权将清除保留的登录会话。账号仍会保留在列表中，方便下次重新登录。</span></div>
    <button class="primary-button" data-action="${a.status === 'expired' ? 'relogin' : 'verify-session'}" data-id="${esc(id)}">${a.status === 'expired' ? '重新登录账号' : '核验登录会话'}</button><button class="secondary-button" data-action="revoke" data-id="${esc(id)}">撤销会话授权</button>`);
}

function showDone(id, title = '账号已就绪') {
  const a = accountById(id);
  openSheet('操作完成', `<div class="done-view"><div class="done-icon">${icon('check', 34)}</div><h3>${title}</h3><p>${esc(a?.name || '账号')}的登录会话已保存<br>持续保留，直到学习通会话失效或主动撤销</p><button class="primary-button" data-action="done">返回账号列表${icon('arrow', 17)}</button><p class="sheet-note">账号密码不会保存在浏览器或服务端</p></div>`);
}

function showDetail(id) {
  if (!accountById(id)) return;
  closeSheet();
  const route = '/accounts/' + encodeURIComponent(id);
  if (location.hash === '#' + route) render(); else location.hash = route;
}

function showAccountActions(id) {
  const a = accountById(id);
  if (!a) return;
  const isDefault = a.id === store.getState().defaultAccountId;
  openSheet('账号操作', `<div class="detail-summary">${avatar(a)}<div><h3>${esc(a.name)}</h3><p>${esc(a.maskedIdentifier)} · ${esc(a.school)}</p></div></div>
    <div class="detail-rows"><div class="detail-row"><span>登录状态</span>${status(a)}</div><div class="detail-row"><span>最近登录</span><span>${time(a.lastLogin)}</span></div><div class="detail-row"><span>会话授权</span><span>${hasGrant(a) ? '持续保留' : '尚未授权'}</span></div><div class="detail-row"><span>默认账号</span><span>${isDefault ? '是' : '否'}</span></div></div>
    <button class="primary-button" data-action="${a.status === 'active' ? 'grant' : 'relogin'}" data-id="${esc(id)}">${a.status === 'active' ? '管理会话授权' : '重新登录并授权'}</button>
    <div style="margin-top:13px"><button class="detail-action" data-action="rename" data-id="${esc(id)}">${icon('edit', 16)}修改账号备注${icon('chevron', 14)}</button>
    ${!isDefault && a.status === 'active' ? `<button class="detail-action" data-action="default" data-id="${esc(id)}">${icon('star', 16)}设为默认账号${icon('chevron', 14)}</button>` : ''}
    ${hasGrant(a) ? `<button class="detail-action" data-action="revoke" data-id="${esc(id)}">${icon('logout', 16)}撤销会话授权${icon('chevron', 14)}</button>` : ''}
    <button class="detail-action danger" data-action="remove" data-id="${esc(id)}">${icon('trash', 16)}移除这个账号${icon('chevron', 14)}</button></div>`);
}

function confirmAction(title, description, action, id = '') {
  openSheet(title, `<div class="confirm-icon">${icon(action === 'confirm-remove' || action === 'confirm-bulk-remove' ? 'trash' : 'shield', 27)}</div><p class="confirm-copy">${description}</p><button class="primary-button danger-button" data-action="${action}" data-id="${esc(id)}">确认${title.replace('？', '')}</button><button class="secondary-button" data-action="close-sheet">取消</button>`);
}

function showAbout() {
  openSheet('关于账号服务', `<div class="about-emblem"><img src="./icon.svg" alt="课签"></div><h3 class="about-title">你的账号，你来管理。</h3><div class="about-copy"><p>课签通过本站后端连接学习通，登录成功后直接保存该账号的授权会话。</p><p>密码只用于当次登录，不会保存在浏览器或服务端。登录会话由服务端保存，浏览器只显示脱敏账号信息。</p><p>会话持续保留，直到学习通会话失效或你主动撤销。会话失效后需要重新登录。</p></div><button class="primary-button" data-action="close-sheet">知道了</button>`);
}

document.addEventListener('click', async event => {
  const button = event.target.closest('[data-action]');
  if (!button || busy || button.disabled) return;
  const { action, id } = button.dataset;
  if (store.getConnection().loading && !['about', 'close-sheet', 'toggle-password', 'done'].includes(action)) return;
  if (action === 'refresh') { await perform(() => store.refresh()); return; }
  if (action === 'add') { if (store.getConnection().connected) showLogin(); }
  else if (action === 'about') showAbout();
  else if (action === 'close-sheet') closeSheet();
  else if (action === 'detail') showDetail(id);
  else if (action === 'account-actions') showAccountActions(id);
  else if (action === 'relogin') showLogin(id);
  else if (action === 'grant') openGrant(id);
  else if (action === 'verify-session') await perform(() => store.grantSession(id), () => { showAuthorization(id); toast('学习通登录会话有效'); }, button);
  else if (action === 'done') { closeSheet(); location.hash = '/accounts'; }
  else if (action === 'default') await perform(() => store.setDefault(id), () => { showAccountActions(id); toast('已设为默认账号'); }, button);
  else if (action === 'bulk') { bulkMode = !bulkMode; selected.clear(); render(); }
  else if (action === 'scan-selected') { scanPage.select([...selected]); location.hash = '/scan'; }
  else if (action === 'scan-account') { scanPage.select([id]); location.hash = '/scan'; }
  else if (action === 'clear-search') { query = ''; filter = 'all'; render(); }
  else if (action === 'select-all') { filteredAccounts().forEach(a => button.checked ? selected.add(a.id) : selected.delete(a.id)); renderList(); }
  else if (action === 'toggle-password') {
    const input = document.querySelector('#login-password');
    const showing = input.type === 'password';
    input.type = showing ? 'text' : 'password';
    button.innerHTML = icon(showing ? 'eyeOff' : 'eye', 18);
    button.setAttribute('aria-label', showing ? '隐藏密码' : '显示密码');
  } else if (action === 'rename') {
    const a = accountById(id);
    if (!a) { closeSheet(); toast('该账号已移除，请刷新列表。'); return; }
    openSheet('修改账号备注', `<p class="sheet-sub">备注只用于区分账号，不会修改学习通资料。</p><form id="rename-form" data-account-id="${esc(id)}"><label class="field"><span>账号备注</span><input name="name" value="${esc(a.name)}" maxlength="20" required autofocus></label><button class="primary-button" type="submit">保存备注</button></form>`);
  } else if (action === 'revoke') {
    const a = accountById(id);
    if (!a) { closeSheet(); toast('该账号已移除，请刷新列表。'); return; }
    confirmAction('撤销授权', `撤销后将清除<strong>${esc(a.name)}</strong>保留的登录会话。账号仍会留在列表里，其他账号不受影响。`, 'confirm-revoke', id);
  } else if (action === 'remove') {
    const a = accountById(id);
    if (!a) { closeSheet(); toast('该账号已移除，请刷新列表。'); return; }
    confirmAction('移除账号', `将从此列表中移除<strong>${esc(a.name)}</strong>及其会话授权。这不会注销学习通账号。`, 'confirm-remove', id);
  } else if (action === 'remove-selected') confirmAction('移除所选账号', `将移除选中的 <strong>${selected.size} 个账号</strong>及其会话授权。未选中的账号不受影响。`, 'confirm-bulk-remove');
  else if (action === 'confirm-revoke') await perform(() => store.revokeSession(id), () => { closeSheet(); toast('会话授权已撤销'); }, button);
  else if (action === 'confirm-remove') await perform(() => store.removeAccounts([id]), () => { closeSheet(); toast('账号已从列表移除'); }, button);
  else if (action === 'confirm-bulk-remove') await perform(() => store.removeAccounts([...selected]), () => { selected.clear(); bulkMode = false; closeSheet(); render(); toast('所选账号已移除'); }, button);
});

document.addEventListener('click', event => {
  const filterButton = event.target.closest('[data-filter], [data-grant-filter]');
  if (!filterButton || busy) return;
  if (filterButton.dataset.filter) filter = filterButton.dataset.filter;
  else grantFilter = filterButton.dataset.grantFilter;
  render();
});

document.addEventListener('input', event => {
  if (event.target.form?.id === 'rename-form') event.target.setCustomValidity('');
  if (event.target.id === 'account-search') { query = event.target.value; renderList(); }
  if (event.target.id === 'login-identifier' || event.target.id === 'login-password') {
    event.target.removeAttribute('aria-invalid');
    const target = document.querySelector(event.target.id === 'login-identifier' ? '#identifier-error' : '#password-error');
    target.textContent = '';
  }
});
document.addEventListener('change', event => {
  if (!event.target.dataset.select) return;
  const id = event.target.dataset.select;
  if (event.target.checked) selected.add(id); else selected.delete(id);
  event.target.closest('.account-card').classList.toggle('selected', event.target.checked);
  renderBulkBar();
});

document.addEventListener('submit', async event => {
  const form = event.target;
  if (!['login-form', 'rename-form'].includes(form.id)) return;
  event.preventDefault();
  if (busy) return;
  if (store.getConnection().loading) { showError(new Error('账号状态正在同步，请稍候再提交。')); return; }
  if (form.id === 'login-form') {
    const id = form.dataset.accountId;
    const identifier = form.elements.identifier.value.trim();
    const identifierError = !id && !/^[\w@.+-]{4,64}$/.test(identifier);
    const passwordError = !form.elements.password.value;
    document.querySelector('#identifier-error').textContent = identifierError ? '请输入至少 4 位的手机号或学号。' : '';
    document.querySelector('#password-error').textContent = passwordError ? '请输入学习通密码。' : '';
    form.elements.identifier.setAttribute('aria-invalid', String(identifierError));
    form.elements.password.setAttribute('aria-invalid', String(passwordError));
    if (identifierError || passwordError) { (identifierError ? form.elements.identifier : form.elements.password).focus(); return; }
    // Keep the password only for this same-origin request; never put it in state.
    let password = form.elements.password.value;
    form.elements.password.value = '';
    const nickname = form.elements.nickname?.value.trim();
    const submit = form.querySelector('[type=submit]');
    try { await perform(() => id ? store.relogin(id, { password }) : store.login({ identifier, password, nickname }),
      account => showDone(account.id, id ? '登录状态已更新' : '登录与授权已完成'), submit); }
    finally { password = ''; }
  } else {
    const value = form.elements.name.value.trim();
    if (!value) { form.elements.name.setCustomValidity('请填写账号备注。'); form.elements.name.reportValidity(); return; }
    await perform(() => store.renameAccount(form.dataset.accountId, value), () => { closeSheet(); toast('账号备注已更新'); }, form.querySelector('[type=submit]'));
  }
});

window.addEventListener('hashchange', () => {
  scanPage.leave();
  accountDetail.leave();
  monitorPage.leave();
  signHistoryPage.leave();
  bulkMode = false; selected.clear();
  if (sheet.open && !busy) closeSheet();
  render(); window.scrollTo({ top: 0, behavior: 'auto' });
});
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && !busy && !sheet.open) store.refresh().catch(() => {});
});
store.subscribe(render);
if (!location.hash) history.replaceState(null, '', `${location.pathname}${location.search}#/accounts`);
render();
store.bootstrap().catch(() => {});
