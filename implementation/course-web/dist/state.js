// Account metadata comes only from the same-origin backend. Passwords and
// upstream cookies are never copied into this store or browser persistence.
const API_BASE = new URL('./api/', document.baseURI);
const listeners = new Set();
const copy = value => JSON.parse(JSON.stringify(value));
let state = { accounts: [], defaultAccountId: null, events: [] };
let connection = { loading: true, connected: false, error: '' };
let csrfToken = '';
let loadingRequest;
const monitorSignRequests = new Map();

export class ApiError extends Error {
  constructor(code, message, statusCode) { super(message); this.code = code; this.statusCode = statusCode; }
}

function publish() {
  for (const listener of listeners) listener(copy(state));
}

function acceptState(value) {
  if (!value || !Array.isArray(value.accounts) || !Array.isArray(value.events)) {
    throw new ApiError('invalid_response', '服务器返回的账号数据不完整，请刷新重试。');
  }
  const accounts = value.accounts.map(account => ({
    id: account.id, name: account.name, maskedIdentifier: account.maskedIdentifier,
    school: account.school, status: account.status, grant: account.grant ? {
      days: account.grant.days, grantedAt: account.grant.grantedAt,
      expiresAt: account.grant.expiresAt, scopes: account.grant.scopes,
    } : null,
    lastLogin: account.lastLogin, sessionExpiresAt: account.sessionExpiresAt,
    color: account.color,
  }));
  if (accounts.some(a => typeof a.id !== 'string' || typeof a.name !== 'string'
    || !['active', 'expired', 'unauthorized'].includes(a.status))) {
    throw new ApiError('invalid_response', '服务器返回的账号状态无法识别，请刷新重试。');
  }
  state = { accounts, defaultAccountId: value.defaultAccountId || null,
    events: value.events.map(e => ({ id: e.id, accountName: e.accountName, action: e.action, at: e.at })) };
  if (typeof value.csrfToken === 'string' && value.csrfToken) csrfToken = value.csrfToken;
  connection = { loading: false, connected: true, error: '' };
  publish();
}

async function request(path, { method = 'GET', body, idempotencyKey } = {}) {
  if (method !== 'GET' && !csrfToken) {
    throw new ApiError('session_not_ready', '网站会话尚未建立，请先重新连接服务。');
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 120_000);
  try {
    const response = await fetch(new URL(path, API_BASE), {
      method, credentials: 'same-origin', mode: 'same-origin', cache: 'no-store', redirect: 'error',
      headers: { Accept: 'application/json', ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
        ...(method === 'GET' ? {} : { 'X-CSRF-Token': csrfToken }),
        ...(idempotencyKey ? { 'Idempotency-Key': idempotencyKey } : {}) },
      body: body === undefined ? undefined : JSON.stringify(body), signal: controller.signal,
    });
    let data;
    try { data = await response.json(); }
    catch { throw new ApiError('invalid_response', '服务返回了无法识别的响应，请检查后端是否正常运行。'); }
    if (!response.ok) {
      throw new ApiError(typeof data?.error?.code === 'string' ? data.error.code : 'request_failed',
        typeof data?.error?.message === 'string' ? data.error.message : '操作未完成，请稍后重试。', response.status);
    }
    return data;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    throw new ApiError(error?.name === 'AbortError' ? 'request_timeout' : 'network_error',
      method === 'GET' ? '无法连接账号服务，请检查网络或后端是否启动。'
        : '连接中断，操作结果尚未确认。请刷新账号状态后再决定是否重试。');
  } finally { clearTimeout(timer); }
}

async function load(path) {
  if (loadingRequest) return loadingRequest;
  connection = { ...connection, loading: true, error: '' };
  publish();
  loadingRequest = (async () => {
    try {
      const result = await request(path);
      if (!csrfToken && !(typeof result.csrfToken === 'string' && result.csrfToken)) {
        throw new ApiError('invalid_response', '账号服务未建立有效的网站会话，请重新连接。');
      }
      acceptState(result);
      return copy(state);
    } catch (error) {
      csrfToken = '';
      connection = { loading: false, connected: false, error: error.message };
      publish();
      throw error;
    } finally { loadingRequest = null; }
  })();
  return loadingRequest;
}

async function mutate(path, body, method = 'POST') {
  const result = await request(path, { method, body });
  acceptState(result.state);
  return result;
}

function acceptBatch(value) {
  if (!value || typeof value.batchId !== 'string' || !Array.isArray(value.items)
    || !['queued', 'running', 'completed', 'interrupted'].includes(value.status)
    || value.items.some(item => typeof item.accountId !== 'string' || typeof item.status !== 'string')) {
    throw new ApiError('invalid_response', '服务器返回的签到批次无法识别，请查询原批次，不要重复提交。');
  }
  return { batchId: value.batchId, activeId: value.activeId, status: value.status, createdAt: value.createdAt,
    items: value.items.map(item => ({ accountId: item.accountId, name: item.name, status: item.status,
      message: item.message, submitted: item.submitted, phase: item.phase })),
    summary: value.summary ? { total: value.summary.total, completed: value.summary.completed,
      succeeded: value.summary.succeeded, failed: value.summary.failed, needsAttention: value.summary.needsAttention } : null };
}

function acceptDetails(value, id) {
  if (!value || value.account?.id !== id || typeof value.account.name !== 'string'
    || !['active', 'expired', 'unauthorized'].includes(value.account.status)
    || !value.profile || typeof value.profile !== 'object' || Array.isArray(value.profile) || !Array.isArray(value.courses)
    || !Array.isArray(value.sources) || value.courses.some(c => !c || c.name != null && typeof c.name !== 'string')
    || value.sources.some(s => !s || !['ok', 'error', 'unsupported'].includes(s.status))) {
    throw new ApiError('invalid_response', '服务器返回的账号详情无法识别，请刷新重试。');
  }
  const a = value.account;
  const profile = {};
  for (const key of ['name', 'school', 'studentNumberMasked', 'department', 'major', 'grade']) {
    if (typeof value.profile[key] === 'string' && value.profile[key].trim()) profile[key] = value.profile[key].slice(0, 300);
  }
  if (profile.studentNumberMasked && !profile.studentNumberMasked.includes('*')) {
    profile.studentNumberMasked = '****' + profile.studentNumberMasked.slice(-2);
  }
  return {
    account: { id: a.id, name: a.name, maskedIdentifier: a.maskedIdentifier, school: a.school,
      status: a.status, grant: a.grant ? { days: a.grant.days, grantedAt: a.grant.grantedAt,
        expiresAt: a.grant.expiresAt, scopes: a.grant.scopes } : null,
      lastLogin: a.lastLogin, sessionExpiresAt: a.sessionExpiresAt, color: a.color },
    profile,
    courses: value.courses.map(c => Object.fromEntries([
      ['key', typeof c.key === 'string' ? c.key : `${c.courseId || ''}:${c.classId || ''}`],
      ...['courseId', 'classId', 'name', 'teacher', 'className', 'term', 'coverUrl', 'status'].map(key =>
        [key, typeof c[key] === 'string' ? c[key].slice(0, 1000) : '']),
      ['sourceLabels', Array.isArray(c.sourceLabels) ? c.sourceLabels.filter(s => typeof s === 'string').slice(0, 8) : []],
    ])),
    sources: value.sources.map(s => ({ id: s.id, label: s.label, status: s.status,
      message: typeof s.message === 'string' ? s.message.slice(0, 500) : '' })),
    fetchedAt: typeof value.fetchedAt === 'string' && Number.isFinite(Date.parse(value.fetchedAt)) ? value.fetchedAt : null,
  };
}

function acceptMonitor(value) {
  if (!value || !Array.isArray(value.accounts) || !Array.isArray(value.tasks)
    || value.accounts.some(a => typeof a?.accountId !== 'string' || typeof a.enabled !== 'boolean'
      || typeof a.autoSign !== 'boolean' || !Number.isInteger(a.intervalSeconds) || a.intervalSeconds < 10 || a.intervalSeconds > 300)
    || value.tasks.some(t => typeof t?.id !== 'string' || typeof t.accountId !== 'string' || typeof t.status !== 'string'
      || t.activityScope != null && !['course', 'group'].includes(t.activityScope))) {
    throw new ApiError('invalid_response', '服务器返回的监控状态无法识别，请刷新后核实。');
  }
  const safeText = (value, limit = 500) => typeof value === 'string' ? value.slice(0, limit) : '';
  return {
    accounts: value.accounts.map(a => ({ accountId: a.accountId, enabled: a.enabled, autoSign: a.autoSign,
      intervalSeconds: a.intervalSeconds, lastCheckedAt: safeText(a.lastCheckedAt), nextCheckAt: safeText(a.nextCheckAt),
      cooldownUntil: safeText(a.cooldownUntil),
      courseListCooldownUntil: typeof a.courseListCooldownUntil === 'string' ? safeText(a.courseListCooldownUntil) : null,
      realtimeStatus: ['disabled', 'connecting', 'connected', 'reconnecting', 'blocked', 'error'].includes(a.realtimeStatus)
        ? a.realtimeStatus : a.enabled ? 'connecting' : 'disabled',
      realtimeMessage: safeText(a.realtimeMessage), connectedAt: safeText(a.connectedAt), lastMessageAt: safeText(a.lastMessageAt),
      reconcileSeconds: 300,
      status: safeText(a.status), message: safeText(a.message), fetchedAt: safeText(a.fetchedAt),
      courseCount: Number.isInteger(a.courseCount) && a.courseCount >= 0 ? a.courseCount : null,
      groupCount: Number.isInteger(a.groupCount) && a.groupCount >= 0 ? a.groupCount : null,
      groupMessage: safeText(a.groupMessage),
      courseErrors: Array.isArray(a.courseErrors) ? a.courseErrors.slice(0, 100).map(e => ({ name: safeText(e?.name), message: safeText(e?.message) })) : [] })),
    tasks: value.tasks.slice(0, 500).map(t => ({ ...Object.fromEntries(['id', 'accountId', 'accountName', 'courseName', 'name',
      'activeId', 'courseId', 'classId', 'kind', 'status', 'message', 'discoveredAt', 'updatedAt', 'requestId', 'notificationType', 'reasonCode', 'resolvedKind'].map(key => [key, safeText(t[key])])),
      activityScope: t.activityScope || 'course', submitted: typeof t.submitted === 'boolean' ? t.submitted : null,
      startTime: Number.isFinite(t.startTime) ? t.startTime : null })),
    serverTime: safeText(value.serverTime),
  };
}

function subscribeMonitor(onSnapshot, onError) {
  let closed = false, source;
  const fail = error => { if (!closed) onError(error); };
  try {
    if (typeof globalThis.EventSource !== 'function') throw new Error();
    source = new EventSource(new URL('monitor/events', API_BASE), { withCredentials: true });
  } catch {
    fail(new ApiError('monitor_stream_unavailable', '实时状态推送不可用，改为每30秒刷新。'));
    return () => { closed = true; };
  }
  const receive = event => {
    if (closed) return;
    let snapshot;
    try { snapshot = acceptMonitor(JSON.parse(event.data)); }
    catch { fail(new ApiError('invalid_response', '实时监控状态无法识别，正在等待恢复。')); return; }
    onSnapshot(snapshot);
  };
  const disconnected = () => fail(new ApiError('monitor_stream_disconnected', '实时状态连接中断，正在重连；每30秒刷新。'));
  source.addEventListener('monitor', receive);
  source.addEventListener('error', disconnected);
  return () => {
    if (closed) return;
    closed = true;
    source.removeEventListener('monitor', receive);
    source.removeEventListener('error', disconnected);
    source.close();
  };
}

function acceptSignRecords(value, id) {
  if (!value || value.accountId !== id || !Array.isArray(value.records) || value.records.length > 100
    || !Number.isInteger(value.total) || value.total < value.records.length
    || value.nextOffset !== null && (!Number.isInteger(value.nextOffset) || value.nextOffset < 1)
    || !['chaoxing', 'local'].includes(value.source)
    || value.source === 'chaoxing' && (!Array.isArray(value.sources) || typeof value.snapshot !== 'string')
    || value.records.some(row => !row || row.accountId !== id
      || typeof row.activeId !== 'string' || !['course', 'group'].includes(row.activityScope)
      || !['reported_success', 'already_signed'].includes(row.status))) {
    throw new ApiError('invalid_response', '服务器返回的成功记录无法识别，请重新查询。');
  }
  const text = (value, limit = 500) => typeof value === 'string' ? value.slice(0, limit) : '';
  return { accountId: id, total: value.total, nextOffset: value.nextOffset, fetchedAt: text(value.fetchedAt),
    source: value.source, snapshot: text(value.snapshot, 64),
    counts: Object.fromEntries(['course', 'group'].map(scope => [scope, Number.isInteger(value.counts?.[scope]) ? value.counts[scope] : 0])),
    sources: (value.sources || []).filter(row => row && ['course', 'group'].includes(row.activityScope)).map(row => ({
      activityScope: row.activityScope, name: text(row.name), status: text(row.status), message: text(row.message),
      count: Number.isInteger(row.count) ? row.count : 0 })),
    records: value.records.map(row => ({ ...Object.fromEntries(['requestId', 'accountId', 'activeId', 'activityScope',
      'status', 'statusLabel', 'name', 'courseName', 'kind', 'message', 'recordedAt', 'source'].map(key => [key, text(row[key])])) })) };
}

async function signMonitorTask(id) {
  if (typeof id !== 'string' || !id.trim() || id.length > 128) {
    throw new ApiError('invalid_input', '未找到可提交的签到任务，请刷新后重试。');
  }
  if (monitorSignRequests.has(id)) return monitorSignRequests.get(id);
  const operation = (async () => {
    try {
      const result = acceptMonitor(await request(`monitor/tasks/${encodeURIComponent(id)}/sign`, { method: 'POST', body: {} }));
      const task = result.tasks.find(task => task.id === id);
      if (!task?.requestId) {
        throw new ApiError('sign_unconfirmed', '签到请求尚未取得登记回执，请刷新查询结果，不要重复提交。');
      }
      return result;
    } finally { monitorSignRequests.delete(id); }
  })();
  monitorSignRequests.set(id, operation);
  return operation;
}

export const store = {
  getState: () => copy(state),
  getConnection: () => ({ ...connection }),
  subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn); },
  bootstrap: () => load('bootstrap'),
  refresh: () => load(csrfToken ? 'state' : 'bootstrap'),
  async login({ identifier, password, nickname }) {
    const result = await mutate('accounts/login', { identifier, password, nickname });
    return loggedInAccount(result.accountId);
  },
  async relogin(id, { password }) {
    const result = await mutate(`accounts/${encodeURIComponent(id)}/relogin`, { password });
    return loggedInAccount(result.accountId);
  },
  grantSession: id => mutate(`accounts/${encodeURIComponent(id)}/grant`, {}),
  revokeSession: id => mutate(`accounts/${encodeURIComponent(id)}/revoke`),
  setDefault: id => mutate(`accounts/${encodeURIComponent(id)}/default`),
  renameAccount: (id, name) => mutate(`accounts/${encodeURIComponent(id)}`, { name }, 'PATCH'),
  removeAccounts: ids => mutate('accounts/remove', { ids }),
  getAccountDetails: async (id, { refresh = false } = {}) => acceptDetails(await request(`accounts/${encodeURIComponent(id)}/details${refresh ? '?refresh=true' : ''}`), id),
  getSignRecords: async (id, { offset = 0, source = 'chaoxing', refresh = false, snapshot = '' } = {}) =>
    acceptSignRecords(await request(`accounts/${encodeURIComponent(id)}/sign-records?${new URLSearchParams({
      offset, limit: 50, source, refresh, snapshot })}`), id),
  getMonitor: async () => acceptMonitor(await request('monitor')),
  subscribeMonitor,
  setMonitor: async (id, { enabled, autoSign, intervalSeconds }) => acceptMonitor(await request(`accounts/${encodeURIComponent(id)}/monitor`, { method: 'PUT', body: { enabled, autoSign, intervalSeconds } })),
  checkMonitor: async id => acceptMonitor(await request(`accounts/${encodeURIComponent(id)}/monitor/check`, { method: 'POST', body: {} })),
  signMonitorTask,
  createQrBatch: async (body, idempotencyKey) => acceptBatch(await request('qr/batches', { method: 'POST', body, idempotencyKey })),
  getQrBatch: async id => acceptBatch(await request(`qr/batches/${encodeURIComponent(id)}`)),
};

function loggedInAccount(id) {
  const account = state.accounts.find(a => a.id === id);
  if (!account || account.status !== 'active' || !account.grant) {
    throw new ApiError('login_unconfirmed', '服务尚未确认登录与授权完成，请刷新账号状态。');
  }
  return copy(account);
}
