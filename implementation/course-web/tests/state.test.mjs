import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const source = await readFile(new URL('../dist/state.js', import.meta.url), 'utf8');
let instance = 0;
const baseState = () => ({ accounts: [], defaultAccountId: null, events: [], csrfToken: 'test-csrf', mode: 'live' });
const response = (body, status = 200) => ({ ok: status >= 200 && status < 300, status, json: async () => body });
const account = () => ({ id: 'account-1', name: '测试账号', maskedIdentifier: '188****1111',
  school: '测试学校', color: 'green', status: 'active', lastLogin: new Date().toISOString(), sessionExpiresAt: null,
  grant: { days: null, grantedAt: new Date().toISOString(), expiresAt: null, scopes: ['session', 'profile'] } });

async function load(handler) {
  globalThis.document = { baseURI: 'https://example.test/course/index.html' };
  globalThis.fetch = handler;
  return (await import(`data:text/javascript;base64,${Buffer.from(`${source}\n// instance ${++instance}`).toString('base64')}`)).store;
}

test('starts empty and blocks mutations until bootstrap provides a CSRF session', async () => {
  let calls = 0;
  const store = await load(async () => { calls++; return response(baseState()); });
  assert.deepEqual(store.getState(), { accounts: [], defaultAccountId: null, events: [] });
  await assert.rejects(store.setDefault('account-1'), { code: 'session_not_ready' });
  assert.equal(calls, 0);
  await store.bootstrap();
  assert.equal(store.getConnection().connected, true);
  assert.equal(calls, 1);
});

test('successful history uses a read-only account route and strips private receipt fields', async () => {
  let body = { accountId: 'account/1', records: [{ requestId: 'saved-job', accountId: 'account/1', activeId: '123',
    activityScope: 'group', status: 'already_signed', name: '已签到', recordedAt: '2026-11-13T10:00:00Z',
    challenge: 'private-challenge' }], total: 1, nextOffset: null, source: 'chaoxing', snapshot: 'test-snapshot', sources: [], fetchedAt: '2026-11-13T10:10:00Z', cookie: 'private-cookie' };
  const calls = [], store = await load(async (url, options) => { calls.push([String(url), options]); return response(body); });
  const records = await store.getSignRecords('account/1', { offset: 50 });
  assert.equal(calls[0][0], 'https://example.test/course/api/accounts/account%2F1/sign-records?offset=50&limit=50&source=chaoxing&refresh=false&snapshot=');
  assert.equal(calls[0][1].method, 'GET');
  assert.equal(records.records[0].status, 'already_signed');
  assert.ok(!JSON.stringify(records).includes('private-'));
  body = { ...body, accountId: 'another-account' };
  await assert.rejects(store.getSignRecords('account/1'), { code: 'invalid_response' });
  body = { ...body, accountId: 'account/1', records: [{ ...body.records[0], status: 'outcome_unknown' }] };
  await assert.rejects(store.getSignRecords('account/1'), { code: 'invalid_response' });
});

test('monitor API keeps account settings scoped and mutations use CSRF with exact fields', async () => {
  const calls = [];
  const monitor = { accounts: [{ accountId: 'account-1', enabled: false, autoSign: false, intervalSeconds: 10,
    status: 'disabled', courseCount: 0, courseErrors: [], cookies: 'private-cookie' }], tasks: [{ id: 'task-1',
      accountId: 'account-1', status: 'outcome_unknown', message: '结果未确认', uid: 'private-uid' }], serverTime: '2026-11-12T00:00:00Z', cookie: 'private-cookie' };
  const store = await load(async (url, options) => {
    calls.push({ url: String(url), options });
    return response(String(url).endsWith('/bootstrap') ? baseState() : monitor);
  });
  await assert.rejects(store.setMonitor('account-1', { enabled: true, autoSign: true, intervalSeconds: 10 }), { code: 'session_not_ready' });
  await store.bootstrap();
  const result = await store.getMonitor();
  assert.equal(calls.at(-1).url, 'https://example.test/course/api/monitor');
  assert.equal(calls.at(-1).options.method, 'GET');
  assert.equal(result.tasks[0].status, 'outcome_unknown');
  assert.ok(!JSON.stringify(result).includes('private-'));
  await store.setMonitor('account/1', { enabled: true, autoSign: true, intervalSeconds: 30, unexpected: 'drop' });
  assert.equal(calls.at(-1).url, 'https://example.test/course/api/accounts/account%2F1/monitor');
  assert.equal(calls.at(-1).options.method, 'PUT');
  assert.equal(calls.at(-1).options.headers['X-CSRF-Token'], 'test-csrf');
  assert.deepEqual(JSON.parse(calls.at(-1).options.body), { enabled: true, autoSign: true, intervalSeconds: 30 });
  await store.checkMonitor('account-1');
  assert.equal(calls.at(-1).options.method, 'POST');
  assert.equal(calls.at(-1).url, 'https://example.test/course/api/accounts/account-1/monitor/check');
  assert.deepEqual(JSON.parse(calls.at(-1).options.body), {});
});

test('group photo task retains lookup state without exposing photo or member identifiers', async () => {
  const store = await load(async () => response({ accounts: [], tasks: [{
    id: 'photo-task', accountId: 'account-1', activityScope: 'group', kind: 'photo', resolvedKind: 'photo',
    status: 'photo_required', submitted: false, reasonCode: 'photo_lookup_incomplete', requestId: 'photo-request',
    objectId: 'private-photo', subjectPuid: 'private-member', photoResult: 'private-upload',
  }] }));
  const result = await store.getMonitor();
  assert.equal(result.tasks[0].resolvedKind, 'photo');
  assert.equal(result.tasks[0].reasonCode, 'photo_lookup_incomplete');
  assert.equal(result.tasks[0].submitted, false);
  assert.ok(!JSON.stringify(result).includes('private-'));
});

test('monitor SSE uses the scoped API URL, sanitizes named snapshots and recovers without recreating its stream', async () => {
  const previous = globalThis.EventSource, sources = [], snapshots = [], errors = [];
  globalThis.EventSource = class {
    listeners = new Map(); closed = false;
    constructor(url, options) { this.url = String(url); this.options = options; sources.push(this); }
    addEventListener(type, listener) { this.listeners.set(type, listener); }
    removeEventListener(type, listener) { if (this.listeners.get(type) === listener) this.listeners.delete(type); }
    emit(type, data) { this.listeners.get(type)?.({ data }); }
    close() { this.closed = true; }
  };
  let unsubscribe;
  try {
    const store = await load(async () => response(baseState()));
    await store.bootstrap();
    unsubscribe = store.subscribeMonitor(value => snapshots.push(value), error => errors.push(error));
    const stream = sources[0];
    assert.equal(stream.url, 'https://example.test/course/api/monitor/events');
    assert.deepEqual(stream.options, { withCredentials: true });
    const body = { accounts: [{ accountId: 'account-1', enabled: true, autoSign: true, intervalSeconds: 300,
      realtimeStatus: 'connected', realtimeMessage: '已接收实时消息', connectedAt: '2026-11-13T10:00:00Z',
      lastMessageAt: '2026-11-13T10:01:00Z', reconcileSeconds: 300, cookies: 'private-cookie', hxToken: 'private-token' }],
      tasks: [{ id: 'task-1', accountId: 'account-1', status: 'pending', requestId: 'live-sign', uid: 'private-uid' }] };
    stream.emit('message', JSON.stringify(body));
    assert.equal(snapshots.length, 0);
    stream.emit('monitor', JSON.stringify(body));
    assert.equal(snapshots.length, 1);
    assert.equal(snapshots[0].accounts[0].realtimeStatus, 'connected');
    assert.equal(snapshots[0].accounts[0].realtimeMessage, '已接收实时消息');
    assert.equal(snapshots[0].accounts[0].connectedAt, body.accounts[0].connectedAt);
    assert.equal(snapshots[0].accounts[0].lastMessageAt, body.accounts[0].lastMessageAt);
    assert.equal(snapshots[0].accounts[0].reconcileSeconds, 300);
    assert.ok(!JSON.stringify(snapshots).includes('private-'));
    stream.emit('monitor', '{invalid-json');
    assert.equal(errors.at(-1).code, 'invalid_response');
    stream.emit('monitor', JSON.stringify({ accounts: [], tasks: [{ id: 'bad', accountId: 'a', status: 'pending', activityScope: 'other' }] }));
    assert.equal(errors.at(-1).code, 'invalid_response');
    assert.equal(snapshots.length, 1);
    stream.emit('error', 'private-transport-detail');
    assert.equal(errors.at(-1).code, 'monitor_stream_disconnected');
    assert.equal(stream.closed, false);
    assert.equal(sources.length, 1);
    body.tasks[0].status = 'reported_success';
    stream.emit('monitor', JSON.stringify(body));
    assert.equal(snapshots.at(-1).tasks[0].status, 'reported_success');
    const lateEvent = stream.listeners.get('monitor');
    unsubscribe(); unsubscribe();
    assert.equal(stream.closed, true);
    assert.equal(stream.listeners.size, 0);
    lateEvent({ data: JSON.stringify(body) });
    stream.emit('error');
    assert.equal(snapshots.length, 2);
    assert.equal(errors.length, 3);
  } finally {
    unsubscribe?.();
    if (previous === undefined) delete globalThis.EventSource;
    else globalThis.EventSource = previous;
  }
});

test('unsupported or unavailable EventSource reports fallback without exposing transport errors', async () => {
  const previous = globalThis.EventSource;
  try {
    for (const implementation of [undefined, class { constructor() { throw new Error('private-session'); } }]) {
      globalThis.EventSource = implementation;
      const store = await load(async () => { throw new Error('SSE fallback must not send mutations'); });
      const errors = [];
      const unsubscribe = store.subscribeMonitor(() => assert.fail('No stream exists'), error => errors.push(error));
      assert.equal(errors.length, 1);
      assert.equal(errors[0].code, 'monitor_stream_unavailable');
      assert.ok(!errors[0].message.includes('private-'));
      unsubscribe();
    }
  } finally {
    if (previous === undefined) delete globalThis.EventSource;
    else globalThis.EventSource = previous;
  }
});

test('monitor realtime fields keep known statuses and bounded display metadata with fixed reconciliation', async () => {
  const monitor = { accounts: [{ accountId: 'account-1', enabled: true, autoSign: false, intervalSeconds: 10,
    realtimeMessage: 'x'.repeat(501), connectedAt: {}, lastMessageAt: 123, reconcileSeconds: 'private-setting' }], tasks: [] };
  const store = await load(async () => response(monitor));
  for (const status of ['disabled', 'connecting', 'connected', 'reconnecting', 'blocked', 'error']) {
    monitor.accounts[0].realtimeStatus = status;
    const account = (await store.getMonitor()).accounts[0];
    assert.equal(account.realtimeStatus, status);
    assert.equal(account.realtimeMessage, 'x'.repeat(500));
    assert.equal(account.connectedAt, '');
    assert.equal(account.lastMessageAt, '');
    assert.equal(account.reconcileSeconds, 300);
  }
  monitor.accounts[0].realtimeStatus = 'private-unknown-status';
  assert.equal((await store.getMonitor()).accounts[0].realtimeStatus, 'connecting');
});

test('monitor settings reject ambiguous enabled flags or invalid intervals', async () => {
  let body = { accounts: [{ accountId: 'a', enabled: 'false', autoSign: false, intervalSeconds: 10 }], tasks: [] };
  const store = await load(async () => response(body));
  await assert.rejects(store.getMonitor(), { code: 'invalid_response' });
  body.accounts[0].enabled = false;
  body.accounts[0].intervalSeconds = 0;
  await assert.rejects(store.getMonitor(), { code: 'invalid_response' });
  body = { accounts: [], tasks: [{ id: 't', accountId: 'a', status: true }] };
  await assert.rejects(store.getMonitor(), { code: 'invalid_response' });
});

test('monitor course cooldown retains only bounded display text', async () => {
  const monitor = { accounts: [{ accountId: 'account-1', enabled: true, autoSign: true, intervalSeconds: 10,
    status: 'rate_limited', cooldownUntil: '2026-11-13T10:10:00Z', courseListCooldownUntil: '2026-11-13T10:15:00Z' }], tasks: [] };
  const store = await load(async () => response(monitor));
  assert.equal((await store.getMonitor()).accounts[0].cooldownUntil, monitor.accounts[0].cooldownUntil);
  assert.equal((await store.getMonitor()).accounts[0].courseListCooldownUntil, monitor.accounts[0].courseListCooldownUntil);
  for (const value of [null, {}, 123]) {
    monitor.accounts[0].cooldownUntil = value;
    monitor.accounts[0].courseListCooldownUntil = value;
    assert.equal((await store.getMonitor()).accounts[0].cooldownUntil, '');
    assert.equal((await store.getMonitor()).accounts[0].courseListCooldownUntil, null);
  }
  monitor.accounts[0].cooldownUntil = 'x'.repeat(501);
  monitor.accounts[0].courseListCooldownUntil = 'x'.repeat(501);
  assert.equal((await store.getMonitor()).accounts[0].cooldownUntil, 'x'.repeat(500));
  assert.equal((await store.getMonitor()).accounts[0].courseListCooldownUntil, 'x'.repeat(500));
});

test('monitor course notifications retain verified start milliseconds and public course metadata', async () => {
  const startTime = Date.UTC(2026, 10, 12, 8, 30);
  const monitor = { accounts: [], tasks: [{ id: 'course-notification', accountId: 'account-1', activityScope: 'course',
    notificationType: 'course_attendance', courseName: '管理学', kind: 'ordinary', status: 'ended', startTime,
    updatedAt: '2026-11-13T10:00:00Z', rawNotification: 'private-notification' }] };
  const store = await load(async () => response(monitor));
  const task = (await store.getMonitor()).tasks[0];
  assert.equal(task.activityScope, 'course');
  assert.equal(task.notificationType, 'course_attendance');
  assert.equal(task.courseName, '管理学');
  assert.equal(task.status, 'ended');
  assert.equal(task.startTime, startTime);
  assert.ok(!JSON.stringify(task).includes('private-'));
  for (const value of [null, {}, true, String(startTime), NaN, Infinity]) {
    monitor.tasks[0].startTime = value;
    assert.equal((await store.getMonitor()).tasks[0].startTime, null);
  }
});

test('ordinary task signing shares an in-flight request and preserves group metadata without secrets', async () => {
  const calls = [];
  let resolveSign;
  const signing = new Promise(resolve => { resolveSign = resolve; });
  const snapshot = { accounts: [{ accountId: 'account-1', enabled: true, autoSign: false, intervalSeconds: 10,
    courseCount: 2, groupCount: 7, groupMessage: '已读取最近群聊活动', courseErrors: [], hxToken: 'private-token' }],
    tasks: [{ id: 'task/1', accountId: 'account-1', status: 'pending', requestId: 'ordinary-request-1',
      activityScope: 'group', kind: 'ordinary_candidate', submitted: false, cookie: 'private-cookie' }] };
  const store = await load(async (url, options) => {
    calls.push({ url: String(url), options });
    return String(url).endsWith('/bootstrap') ? response(baseState()) : signing;
  });
  await assert.rejects(store.signMonitorTask('task/1'), { code: 'session_not_ready' });
  assert.equal(calls.length, 0);
  await store.bootstrap();
  const first = store.signMonitorTask('task/1'), duplicate = store.signMonitorTask('task/1');
  assert.equal(calls.filter(c => c.options.method === 'POST').length, 1);
  const request = calls.at(-1);
  assert.equal(request.url, 'https://example.test/course/api/monitor/tasks/task%2F1/sign');
  assert.equal(request.options.method, 'POST');
  assert.equal(request.options.headers['X-CSRF-Token'], 'test-csrf');
  assert.equal(request.options.credentials, 'same-origin');
  assert.deepEqual(JSON.parse(request.options.body), {});
  resolveSign(response(snapshot, 202));
  const results = await Promise.all([first, duplicate]);
  assert.equal(results[0].tasks[0].status, 'pending');
  assert.equal(results[0].tasks[0].activityScope, 'group');
  assert.equal(results[0].tasks[0].submitted, false);
  assert.equal(results[0].accounts[0].groupCount, 7);
  assert.equal(results[0].accounts[0].groupMessage, snapshot.accounts[0].groupMessage);
  assert.ok(!JSON.stringify(results).includes('private-'));
});

test('ordinary task signing never accepts an old snapshot or another task as a registration receipt', async () => {
  let monitor = { accounts: [], tasks: [] };
  const store = await load(async url => response(String(url).endsWith('/bootstrap') ? baseState() : monitor));
  await store.bootstrap();
  await assert.rejects(store.signMonitorTask('task-1'), { code: 'sign_unconfirmed' });
  monitor = { accounts: [], tasks: [{ id: 'task-2', accountId: 'account-1', status: 'reported_success', requestId: 'ordinary-request-2' }] };
  await assert.rejects(store.signMonitorTask('task-1'), { code: 'sign_unconfirmed' });
  monitor.tasks[0] = { id: 'task-1', accountId: 'account-1', status: 'reported_success' };
  await assert.rejects(store.signMonitorTask('task-1'), { code: 'sign_unconfirmed' });
  monitor.tasks[0].requestId = 'ordinary-request-1';
  monitor.tasks[0].activityScope = 'invalid-scope';
  await assert.rejects(store.signMonitorTask('task-1'), { code: 'invalid_response' });
});

test('group QR direct signing sends only the task ID and retains no QR credentials', async () => {
  const calls = [];
  const snapshot = { accounts: [], tasks: [{ id: 'group-qr-task', accountId: 'account-1', activityScope: 'group',
    kind: 'qr', status: 'pending', requestId: 'group-qr-request-1', qrText: 'private-qr',
    enc: 'private-enc', Code: 'private-code', cookies: 'private-cookie' }] };
  const store = await load(async (url, options) => {
    calls.push({ url: String(url), options });
    return response(String(url).endsWith('/bootstrap') ? baseState() : snapshot, options.method === 'POST' ? 202 : 200);
  });
  await store.bootstrap();
  const result = await store.signMonitorTask('group-qr-task');
  const request = calls.at(-1);
  assert.equal(request.url, 'https://example.test/course/api/monitor/tasks/group-qr-task/sign');
  assert.equal(request.options.method, 'POST');
  assert.deepEqual(JSON.parse(request.options.body), {});
  assert.equal(request.options.headers['X-CSRF-Token'], 'test-csrf');
  assert.equal(result.tasks[0].kind, 'qr');
  assert.equal(result.tasks[0].activityScope, 'group');
  assert.ok(!JSON.stringify(result).includes('private-'));
});

test('course gesture direct signing sends no gesture value and never exposes a returned sign code', async () => {
  const calls = [];
  const snapshot = { accounts: [], tasks: [{ id: 'course-gesture-task', accountId: 'account-1', activityScope: 'course',
    kind: 'gesture', status: 'pending', requestId: 'gesture-request-1', signCode: 'private-pattern',
    gesture: 'private-gesture', cookies: 'private-cookie' }] };
  const store = await load(async (url, options) => {
    calls.push({ url: String(url), options });
    return response(String(url).endsWith('/bootstrap') ? baseState() : snapshot, options.method === 'POST' ? 202 : 200);
  });
  await store.bootstrap();
  const result = await store.signMonitorTask('course-gesture-task');
  const request = calls.at(-1);
  assert.equal(request.url, 'https://example.test/course/api/monitor/tasks/course-gesture-task/sign');
  assert.equal(request.options.method, 'POST');
  assert.deepEqual(JSON.parse(request.options.body), {});
  assert.equal(request.options.headers['X-CSRF-Token'], 'test-csrf');
  assert.equal(result.tasks[0].kind, 'gesture');
  assert.equal(result.tasks[0].activityScope, 'course');
  assert.ok(!JSON.stringify(result).includes('private-'));
});

test('account details use the selected ID and retain only display fields', async () => {
  const calls = [];
  const selected = account();
  const store = await load(async (url, options) => {
    calls.push({ url: String(url), options });
    if (String(url).endsWith('/bootstrap')) return response({ ...baseState(), accounts: [selected] });
    return response({ account: { ...selected, cookies: 'private-cookie' },
      profile: { name: '真实姓名', school: '测试学校', studentNumberMasked: '202400123', token: 'private-token' },
      courses: [{ key: 'course-1:class-1', courseId: 'course-1', classId: 'class-1', name: '测试课程', teacher: '测试教师', rawCookie: 'private-cookie', sourceLabels: ['课程列表'] }],
      sources: [{ id: 'normal', label: '课程列表', status: 'ok', message: '' }], fetchedAt: new Date().toISOString(),
      cookies: 'private-cookie', uid: 'private-uid' });
  });
  await store.bootstrap();
  const result = await store.getAccountDetails(selected.id);
  assert.equal(calls.at(-1).url, 'https://example.test/course/api/accounts/account-1/details');
  assert.equal(calls.at(-1).options.method, 'GET');
  assert.equal(result.courses[0].teacher, '测试教师');
  assert.equal(result.profile.studentNumberMasked, '****23');
  assert.ok(!JSON.stringify(result).includes('private-'));
  assert.equal(store.getState().accounts[0].name, selected.name);
});

test('details from a different account or malformed courses are rejected', async () => {
  let body = { account: { ...account(), id: 'another-account' }, profile: {}, courses: [], sources: [] };
  const store = await load(async () => response(body));
  await assert.rejects(store.getAccountDetails('account-1'), { code: 'invalid_response' });
  body = { account: account(), profile: {}, courses: [{ name: { unexpected: true } }], sources: [] };
  await assert.rejects(store.getAccountDetails('account-1'), { code: 'invalid_response' });
  assert.deepEqual(store.getState().accounts, []);
});

test('details distinguish successful empty courses, partial sources and server failure', async () => {
  let body = { account: account(), profile: {}, courses: [], sources: [{ id: 'normal', status: 'ok' }, { id: 'meet', status: 'error', message: '课程来源暂不可用' }] };
  let fail = false;
  const store = await load(async () => fail ? response({ error: { code: 'course_fetch_failed', message: '无法读取课程' } }, 502) : response(body));
  const result = await store.getAccountDetails('account-1');
  assert.deepEqual(result.courses, []);
  assert.equal(result.sources[0].status, 'ok');
  assert.equal(result.sources[1].status, 'error');
  fail = true;
  await assert.rejects(store.getAccountDetails('account-1'), { code: 'course_fetch_failed', message: '无法读取课程' });
});

test('explicit details refresh bypasses the server cache and unnamed courses remain visible', async () => {
  let calledUrl;
  const store = await load(async url => {
    calledUrl = String(url);
    return response({ account: account(), profile: {}, courses: [{ key: 'missing-title', name: null, teacher: '测试教师' }], sources: [{ id: 'normal', status: 'ok' }] });
  });
  const result = await store.getAccountDetails('account-1', { refresh: true });
  assert.equal(calledUrl, 'https://example.test/course/api/accounts/account-1/details?refresh=true');
  assert.equal(result.courses.length, 1);
  assert.equal(result.courses[0].name, '');
  assert.equal(result.courses[0].teacher, '测试教师');
});

test('uses same-origin credentials, CSRF, JSON and a subdirectory-safe API URL', async () => {
  const calls = [];
  const store = await load(async (url, options) => {
    calls.push({ url: String(url), options });
    return options.method === 'GET' ? response(baseState())
      : response({ accountId: 'account-1', state: { ...baseState(), accounts: [account()] } });
  });
  await store.bootstrap();
  const result = await store.login({ identifier: '18800001111', password: 'synthetic-password', grantDays: 7 });
  const request = calls.at(-1);
  assert.equal(request.url, 'https://example.test/course/api/accounts/login');
  assert.equal(request.options.credentials, 'same-origin');
  assert.equal(request.options.mode, 'same-origin');
  assert.equal(request.options.redirect, 'error');
  assert.equal(request.options.headers['X-CSRF-Token'], 'test-csrf');
  assert.equal(JSON.parse(request.options.body).password, 'synthetic-password');
  assert.equal('grantDays' in JSON.parse(request.options.body), false);
  assert.equal(result.id, 'account-1');
});

test('persistent grants remain null-ended and login/relogin/verification send no duration', async () => {
  const calls = [];
  const saved = { ...baseState(), accounts: [account()] };
  const store = await load(async (url, options) => {
    calls.push({ url: String(url), options });
    return options.method === 'GET' ? response(saved) : response({ accountId: 'account-1', state: saved });
  });
  await store.bootstrap();
  await store.login({ identifier: '18800001111', password: 'synthetic-password' });
  await store.relogin('account-1', { password: 'synthetic-password' });
  await store.grantSession('account-1');
  for (const { options } of calls.filter(call => call.options.method !== 'GET')) {
    const body = JSON.parse(options.body);
    assert.equal('grantDays' in body, false);
    assert.equal('days' in body, false);
  }
  assert.equal(store.getState().accounts[0].grant.expiresAt, null);
  assert.equal(store.getState().accounts[0].grant.days, null);
});

test('a rejected login leaves account state unchanged and preserves the server error', async () => {
  const store = await load(async (_, options) => options.method === 'GET' ? response(baseState())
    : response({ error: { code: 'login_rejected', message: '账号或密码不正确' } }, 401));
  await store.bootstrap();
  await assert.rejects(store.login({ identifier: '18800001111', password: 'synthetic-password' }),
    { code: 'login_rejected', message: '账号或密码不正确' });
  assert.equal(store.getState().accounts.length, 0);
});

test('an HTTP success without an active authorized account cannot complete login', async () => {
  const pending = { ...account(), status: 'unauthorized', grant: null };
  const store = await load(async (_, options) => options.method === 'GET' ? response(baseState())
    : response({ accountId: pending.id, state: { ...baseState(), accounts: [pending] } }));
  await store.bootstrap();
  await assert.rejects(store.login({ identifier: '18800001111', password: 'synthetic-password' }), { code: 'login_unconfirmed' });
  assert.equal(store.getState().accounts[0].status, 'unauthorized');
});

test('only display metadata is copied into state, and returned snapshots are isolated', async () => {
  const upstreamAccount = { ...account(), password: 'do-not-copy', cookies: [{ value: 'do-not-copy' }], token: 'do-not-copy' };
  const store = await load(async (_, options) => options.method === 'GET' ? response(baseState())
    : response({ accountId: upstreamAccount.id, state: { ...baseState(), accounts: [upstreamAccount], rawSecret: 'do-not-copy' } }));
  await store.bootstrap();
  const result = await store.login({ identifier: '18800001111', password: 'synthetic-password' });
  const serialized = JSON.stringify(store.getState());
  assert.ok(!serialized.includes('do-not-copy'));
  assert.ok(!serialized.includes('synthetic-password'));
  assert.ok(!serialized.includes('18800001111'));
  result.name = 'changed';
  assert.equal(store.getState().accounts[0].name, '测试账号');
});

test('failed revoke preserves state; confirmed revoke adopts only server state', async () => {
  let fail = true;
  const saved = { ...baseState(), accounts: [account()], defaultAccountId: 'account-1' };
  const store = await load(async (_, options) => options.method === 'GET' ? response(saved)
    : fail ? response({ error: { code: 'temporary', message: '暂不可用' } }, 502)
      : response({ state: { ...baseState(), accounts: [{ ...account(), status: 'unauthorized', grant: null }] } }));
  await store.bootstrap();
  await assert.rejects(store.revokeSession('account-1'), { code: 'temporary' });
  assert.equal(store.getState().accounts[0].status, 'active');
  fail = false;
  await store.revokeSession('account-1');
  assert.equal(store.getState().accounts[0].status, 'unauthorized');
  assert.equal(store.getState().defaultAccountId, null);
});

test('malformed refresh disconnects the store and disables subsequent mutations', async () => {
  let invalid = false;
  let calls = 0;
  const store = await load(async () => { calls++; return response(invalid ? { unexpected: true } : baseState()); });
  await store.bootstrap();
  invalid = true;
  await assert.rejects(store.refresh(), { code: 'invalid_response' });
  assert.equal(store.getConnection().connected, false);
  const previousCalls = calls;
  await assert.rejects(store.removeAccounts(['account-1']), { code: 'session_not_ready' });
  assert.equal(calls, previousCalls);
});

test('transport failures are explicit and never retried automatically', async () => {
  let calls = 0;
  const store = await load(async (_, options) => {
    calls++;
    if (options.method === 'GET') return response(baseState());
    throw new TypeError('synthetic transport interruption');
  });
  await store.bootstrap();
  await assert.rejects(store.relogin('account-1', { password: 'synthetic-password' }), { code: 'network_error' });
  assert.equal(calls, 2);
  assert.equal(store.getState().accounts.length, 0);
});

test('QR batch creation uses CSRF and idempotency without changing account state or retaining secrets', async () => {
  const calls = [];
  const store = await load(async (url, options) => {
    calls.push({ url: String(url), options });
    return options.method === 'GET' ? response(baseState()) : response({ batchId: 'batch-one', activeId: '123',
      status: 'queued', items: [{ accountId: 'account-1', name: '测试账号', status: 'queued', cookies: 'private-cookie' }],
      qrText: 'private-qr', cookies: 'private-cookie' }, 202);
  });
  await store.bootstrap();
  const batch = await store.createQrBatch({ accountIds: ['account-1'], qrText: 'private-qr' }, 'batch-one');
  const sent = calls.at(-1);
  assert.equal(sent.url, 'https://example.test/course/api/qr/batches');
  assert.equal(sent.options.headers['Idempotency-Key'], 'batch-one');
  assert.equal(sent.options.headers['X-CSRF-Token'], 'test-csrf');
  assert.equal(batch.status, 'queued');
  assert.ok(!JSON.stringify(batch).includes('private'));
  assert.equal(store.getState().accounts.length, 0);
});

test('QR resume only queries the same batch and malformed responses cannot become a successful batch', async () => {
  const calls = [];
  const store = await load(async (url, options) => {
    calls.push({ url: String(url), method: options.method });
    return response(String(url).endsWith('bootstrap') ? baseState() : { status: 'completed' });
  });
  await store.bootstrap();
  await assert.rejects(store.getQrBatch('batch-one'), { code: 'invalid_response' });
  assert.equal(calls.at(-1).url, 'https://example.test/course/api/qr/batches/batch-one');
  assert.ok(calls.every(call => call.method === 'GET'));
});
