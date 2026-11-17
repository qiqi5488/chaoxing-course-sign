import test from 'node:test';
import assert from 'node:assert/strict';
import { taskUpdates, taskLabel, taskTone, canSignTask, createMonitorPage } from '../dist/monitor-page.js';

test('first monitor snapshot establishes history without notifying', () => {
  const result = taskUpdates(null, [{ id: 'old', status: 'reported_success' }]);
  assert.deepEqual(result.changed, []);
  assert.equal(result.next.size, 1);
});

test('only new tasks and material result changes notify, not poll timestamps', () => {
  const original = [{ id: 'a', status: 'pending', message: '正在处理', updatedAt: 'first' }];
  const first = taskUpdates(null, original);
  const same = taskUpdates(first.next, [{ ...original[0], updatedAt: 'later' }]);
  assert.deepEqual(same.changed, []);
  const complete = { ...original[0], status: 'reported_success', message: '上游返回成功' };
  const incoming = { id: 'b', status: 'qr_required', message: '请扫码' };
  const changes = taskUpdates(same.next, [complete, incoming]);
  assert.deepEqual(changes.changed.map(t => t.id), ['a', 'b']);
  assert.deepEqual(taskUpdates(changes.next, [complete, incoming]).changed, []);
});

test('unknown and validation results never get a success label or treatment', () => {
  for (const status of ['outcome_unknown', 'unknown', 'http_error', 'requires_validation', 'need_context', 'future_unknown_state']) {
    assert.notEqual(taskTone(status), 'success');
    assert.ok(!taskLabel(status).includes('成功'));
  }
  assert.equal(taskLabel('reported_success'), '上游回执成功');
  assert.equal(taskTone('reported_success'), 'success');
  assert.equal(taskTone('pending'), 'pending');
});

test('ordinary course and group actions require an unsubmitted actionable task', () => {
  const task = { id: 'task-1', accountId: 'account-1', kind: 'ordinary', status: 'monitoring_only' };
  assert.equal(canSignTask(task), true); // Existing course tasks omit activityScope.
  assert.equal(canSignTask({ ...task, activityScope: 'group', kind: 'ordinary_candidate', status: 'needs_input' }), true);
  assert.equal(canSignTask({ ...task, activityScope: 'course', kind: 'ordinary_candidate' }), false);
  for (const status of ['pending', 'running', 'reported_success', 'already_signed', 'outcome_unknown', 'unknown',
    'http_error', 'requires_validation', 'need_context', 'rejected', 'requires_manual', 'ended', 'scheduled']) {
    assert.equal(canSignTask({ ...task, status }), false, status);
  }
  for (const change of [{ requestId: 'existing-request' }, { submitted: true }, { id: '' }, { accountId: '' },
    { activityScope: 'unrecognized' }, { kind: 'qr' }, { kind: 'photo' }, { kind: 'position' }, { kind: 'code' }]) {
    assert.equal(canSignTask({ ...task, ...change }), false, JSON.stringify(change));
  }
});

test('group QR tasks allow direct signing while course QR tasks still require scanning', () => {
  const task = { id: 'group-qr-task', accountId: 'account-1', activityScope: 'group', kind: 'qr', status: 'needs_input' };
  for (const refreshMode of ['fixed', 'dynamic']) {
    for (const status of ['needs_input', 'qr_required', 'discovered', 'monitoring_only', 'awaiting_action']) {
      assert.equal(canSignTask({ ...task, status, refreshMode }), true, `${refreshMode}/${status}`);
    }
  }
  assert.equal(canSignTask({ ...task, activityScope: 'course' }), false);
  assert.equal(canSignTask({ ...task, activityScope: undefined }), false);
  assert.equal(canSignTask({ ...task, kind: 'ordinary', status: 'qr_required' }), false);
  for (const status of ['pending', 'reported_success', 'already_signed', 'unknown', 'outcome_unknown', 'http_error', 'ended']) {
    assert.equal(canSignTask({ ...task, status }), false, status);
  }
  assert.equal(canSignTask({ ...task, requestId: 'existing-request' }), false);
  assert.equal(canSignTask({ ...task, submitted: true }), false);
  assert.equal(taskLabel('qr_required', task), '等待直接签到');
  assert.equal(taskLabel('qr_required', { ...task, requestId: 'existing-request' }), '需在学习通处理');
  assert.equal(taskLabel('qr_required', { ...task, activityScope: 'course' }), '需要扫码');
});

test('course gesture direct signing is limited to the verified monitoring-only state', () => {
  const task = { id: 'course-gesture-task', accountId: 'account-1', activityScope: 'course', kind: 'gesture', status: 'monitoring_only' };
  assert.equal(canSignTask(task), true);
  assert.equal(canSignTask({ ...task, submitted: null }), true);
  assert.equal(taskLabel('monitoring_only', task), '可尝试签到');
  for (const status of ['needs_input', 'code_required', 'discovered', 'awaiting_action', 'pending', 'running',
    'reported_success', 'already_signed', 'outcome_unknown', 'unknown', 'http_error', 'requires_manual', 'ended']) {
    assert.equal(canSignTask({ ...task, status }), false, status);
  }
  for (const change of [{ activityScope: undefined }, { requestId: 'existing-request' },
    { submitted: true }, { submitted: false }]) {
    assert.equal(canSignTask({ ...task, ...change }), false, JSON.stringify(change));
  }
  assert.equal(taskLabel('code_required', { ...task, status: 'code_required' }), '需要签到码');
});

test('group gesture direct signing uses safe unsubmitted states without widening course gesture eligibility', () => {
  const task = { id: 'group-gesture-task', accountId: 'account-1', activityScope: 'group', kind: 'gesture', status: 'needs_input' };
  for (const status of ['needs_input', 'monitoring_only', 'discovered', 'awaiting_action']) {
    assert.equal(canSignTask({ ...task, status }), true, status);
  }
  for (const status of ['pending', 'running', 'reported_success', 'already_signed', 'unknown', 'outcome_unknown',
    'http_error', 'code_required', 'requires_manual', 'ended']) {
    assert.equal(canSignTask({ ...task, status }), false, status);
  }
  for (const change of [{ requestId: 'existing-request' }, { submitted: true }]) {
    assert.equal(canSignTask({ ...task, ...change }), false);
  }
  assert.equal(taskLabel('needs_input', task), '等待直接签到');
  assert.equal(taskLabel('needs_input', { ...task, requestId: 'existing-request' }), '需在学习通处理');
  assert.equal(canSignTask({ ...task, activityScope: 'course' }), false);
  assert.equal(canSignTask({ ...task, activityScope: 'course', status: 'monitoring_only' }), true);
});

test('group photo lookup is actionable only before submission and remains separate from course photos', () => {
  const task = { id: 'photo-task', accountId: 'account-1', activityScope: 'group', kind: 'photo',
    resolvedKind: 'photo', requestId: 'photo-request', status: 'photo_required', submitted: false, reasonCode: 'photo_missing' };
  assert.equal(canSignTask(task), true);
  assert.equal(taskLabel(task.status, task), '等待活动照片');
  assert.equal(canSignTask({ ...task, reasonCode: 'photo_lookup_incomplete' }), true);
  assert.equal(taskLabel(task.status, { ...task, reasonCode: 'photo_lookup_incomplete' }), '照片查询待补查');
  for (const change of [{ activityScope: 'course' }, { submitted: true }, { submitted: null },
    { reasonCode: 'photo_required' }, { resolvedKind: '' }, { status: 'outcome_unknown' }, { status: 'reported_success' }]) {
    assert.equal(canSignTask({ ...task, ...change }), false);
  }
  assert.equal(taskLabel('reported_success', { ...task, reasonCode: 'photo_record_verified' }), '官方记录已确认');
});

const nextTurn = () => new Promise(resolve => setImmediate(resolve));
const clone = value => JSON.parse(JSON.stringify(value));

async function pageHarness(signMonitorTask, taskChanges = {}, formatTime = () => '刚刚') {
  const handlers = new Map(), toasts = [], subscriptions = [];
  globalThis.document = { activeElement: null, addEventListener(type, handler) { handlers.set(type, handler); } };
  const task = { id: 'task-1', accountId: 'account-1', accountName: '测试账号', name: '普通签到',
    activityScope: 'group', kind: 'ordinary_candidate', status: 'needs_input', message: '等待处理', ...taskChanges };
  let snapshot = { accounts: [{ accountId: 'account-1', enabled: true, autoSign: false, intervalSeconds: 10,
    courseCount: 2, groupCount: 1, courseErrors: [], groupMessage: '较早活动可能未显示' }], tasks: [task] };
  let gets = 0;
  const store = { getState: () => ({ accounts: [{ id: 'account-1', name: '测试账号', status: 'active', grant: {} }] }),
    getMonitor: async () => { gets++; return clone(snapshot); }, signMonitorTask,
    subscribeMonitor(onSnapshot, onError) {
      const subscription = { onSnapshot, onError, closed: false }; subscriptions.push(subscription);
      return () => { subscription.closed = true; };
    } };
  const main = { innerHTML: '', contains: () => false, querySelectorAll: () => [] };
  const page = createMonitorPage({ store, esc: value => String(value), icon: () => '', avatar: () => '',
    time: formatTime, toast: text => toasts.push(text) });
  page.render(main);
  await nextTurn();
  const click = (action = 'sign') => handlers.get('click')({ target: { closest(selector) {
    if (selector === '[data-monitor-filter]' && ['all', 'attention'].includes(action)) return { dataset: { monitorFilter: action } };
    return selector === '[data-monitor-action]' ? { disabled: false, dataset: { monitorAction: action, taskId: task.id } } : null;
  } } });
  const change = (monitorField, checked) => handlers.get('change')({ target: { closest() {
    return { disabled: false, checked, dataset: { monitorField, id: task.accountId } };
  } } });
  return { page, main, task, click, change, store, subscriptions, toasts, get gets() { return gets; }, setSnapshot(value) { snapshot = value; }, snapshot };
}

test('clicking the monitored account leads to its successful sign records', async () => {
  const h = await pageHarness(async () => { throw new Error('No signing expected'); });
  assert.match(h.main.innerHTML, /href="#\/monitor\/accounts\/account-1\/records"/);
  assert.match(h.main.innerHTML, /aria-label="查看测试账号的签到成功记录"/);
  assert.match(h.main.innerHTML, /查看成功记录/);
  h.page.leave();
});

test('photo lookup can be checked again after a confirmed readonly result and stops after submission', async () => {
  let calls = 0, h;
  h = await pageHarness(async () => { calls++; return clone(h.snapshot); }, {
    name: '签到', kind: 'photo', resolvedKind: 'photo', status: 'photo_required',
    requestId: 'photo-request', submitted: false, reasonCode: 'photo_missing' });
  try {
    assert.match(h.main.innerHTML, /<h3>拍照签到<\/h3>/);
    assert.match(h.main.innerHTML, /重新查找照片/);
    await h.click();
    await h.click();
    assert.equal(calls, 2);
    h.subscriptions[0].onSnapshot({ ...h.snapshot, tasks: [{ ...h.task, submitted: true,
      status: 'reported_success', reasonCode: 'photo_record_verified' }] });
    assert.match(h.main.innerHTML, /官方记录已确认/);
    assert.doesNotMatch(h.main.innerHTML, /data-monitor-action="sign"/);
    await h.click();
    assert.equal(calls, 2);
  } finally { h.page.leave(); }
});

test('SSE owns live updates, falls back every 30 seconds on error and closes across page visits', async () => {
  const originalSetTimeout = globalThis.setTimeout, originalClearTimeout = globalThis.clearTimeout, timers = new Map();
  let timerId = 0, h;
  globalThis.setTimeout = (run, delay) => { const id = ++timerId; timers.set(id, { run, delay }); return id; };
  globalThis.clearTimeout = id => timers.delete(id);
  try {
    h = await pageHarness(async () => { throw new Error('No signing expected'); });
    assert.equal(h.subscriptions.length, 1);
    assert.deepEqual([...timers.values()].map(timer => timer.delay), [30_000]);
    const live = { ...h.snapshot, accounts: [{ ...h.snapshot.accounts[0], realtimeStatus: 'connected',
      realtimeMessage: '实时通道可用', connectedAt: '2026-11-13T10:00:00Z', lastMessageAt: '2026-11-13T10:01:00Z' }] };
    h.setSnapshot(live); h.subscriptions[0].onSnapshot(clone(live));
    assert.equal(timers.size, 0);
    assert.equal(h.gets, 1);
    assert.match(h.main.innerHTML, /实时消息立即处理 · 每5分钟补漏/);
    assert.match(h.main.innerHTML, /断线恢复后自动补查/);
    assert.match(h.main.innerHTML, /实时接收：已连接/);
    assert.match(h.main.innerHTML, /实时通道可用/);
    assert.match(h.main.innerHTML, /连接建立 刚刚/);
    assert.match(h.main.innerHTML, /最近消息 刚刚/);
    assert.match(h.main.innerHTML, /实时推送更新/);
    assert.doesNotMatch(h.main.innerHTML, /data-monitor-field="intervalSeconds"|每 3 秒|每 10 秒/);
    h.subscriptions[0].onError({ code: 'monitor_stream_disconnected' });
    assert.match(h.main.innerHTML, /推送恢复中 · 每30秒刷新/);
    const [id, fallback] = [...timers.entries()][0];
    assert.equal(fallback.delay, 30_000);
    h.subscriptions[0].onError({ code: 'monitor_stream_disconnected' });
    assert.deepEqual([...timers.keys()], [id]);
    timers.delete(id); fallback.run(); await nextTurn();
    assert.equal(h.gets, 2);
    assert.deepEqual([...timers.values()].map(timer => timer.delay), [30_000]);
    h.subscriptions[0].onSnapshot(clone(live));
    assert.equal(timers.size, 0);
    const previous = h.subscriptions[0];
    h.page.leave();
    assert.equal(previous.closed, true);
    assert.equal(timers.size, 0);
    h.page.render(h.main); await nextTurn();
    assert.equal(h.subscriptions.length, 2);
    const html = h.main.innerHTML;
    previous.onSnapshot({ ...live, tasks: [{ ...h.task, name: '旧连接不应出现' }] });
    assert.equal(h.main.innerHTML, html);
    h.subscriptions[1].onSnapshot(clone(live));
    assert.equal(timers.size, 0);
  } finally {
    h?.page.leave(); globalThis.setTimeout = originalSetTimeout; globalThis.clearTimeout = originalClearTimeout;
  }
});

test('pending GETs from older visits cannot overwrite a newer SSE snapshot or clear a current request', async () => {
  const h = await pageHarness(async () => { throw new Error('No signing expected'); });
  const pending = [];
  try {
    h.store.getMonitor = () => new Promise(resolve => pending.push(resolve));
    h.page.leave(); h.page.render(h.main);
    h.page.leave(); h.page.render(h.main);
    assert.equal(pending.length, 2);
    const newest = { ...h.snapshot, tasks: [{ ...h.task, name: '实时成功任务', status: 'reported_success', requestId: 'live-request' }] };
    h.subscriptions[2].onSnapshot(newest);
    pending[0](clone(h.snapshot)); await nextTurn();
    assert.match(h.main.innerHTML, /实时成功任务/);
    assert.match(h.main.innerHTML, /<button[^>]*data-monitor-action="refresh"[^>]*disabled/);
    pending[1](clone(h.snapshot)); await nextTurn();
    assert.match(h.main.innerHTML, /实时成功任务/);
    assert.match(h.main.innerHTML, /上游回执成功/);
    assert.doesNotMatch(h.main.innerHTML, /data-monitor-action="sign"/);
    const refresh = h.main.innerHTML.match(/<button[^>]*data-monitor-action="refresh"[^>]*>/);
    assert.ok(refresh); assert.doesNotMatch(refresh[0], /disabled/);
  } finally { h.page.leave(); }
});

test('a late POST receipt cannot replace a completed SSE sign result or trigger another submission', async () => {
  let resolveSign, calls = 0;
  const pending = new Promise(resolve => { resolveSign = resolve; });
  const h = await pageHarness(async () => { calls++; return pending; });
  try {
    const operation = h.click();
    assert.equal(calls, 1);
    h.subscriptions[0].onSnapshot({ ...h.snapshot, tasks: [{ ...h.task, status: 'reported_success', requestId: 'live-sign' }] });
    assert.match(h.main.innerHTML, /上游回执成功/);
    resolveSign({ ...h.snapshot, tasks: [{ ...h.task, status: 'pending', requestId: 'live-sign' }] });
    await operation;
    assert.match(h.main.innerHTML, /上游回执成功/);
    assert.doesNotMatch(h.main.innerHTML, /data-monitor-action="sign"/);
    await h.click();
    assert.equal(calls, 1);
  } finally { h.page.leave(); }
});

test('automatic settings remain usable with the fixed five-minute reconciliation interval', async () => {
  const h = await pageHarness(async () => { throw new Error('No signing expected'); });
  const calls = [];
  try {
    let resolveSettings;
    const pending = new Promise(resolve => { resolveSettings = resolve; });
    h.store.setMonitor = async (id, settings) => { calls.push({ id, settings }); return pending; };
    h.change('autoSign', true); await nextTurn();
    assert.equal(calls.length, 1);
    assert.equal(calls[0].id, 'account-1');
    assert.equal(calls[0].settings.autoSign, true);
    assert.equal(calls[0].settings.enabled, true);
    assert.equal(calls[0].settings.intervalSeconds, 300);
    h.subscriptions[0].onSnapshot({ ...h.snapshot, accounts: [{ ...h.snapshot.accounts[0], autoSign: true }] });
    resolveSettings(clone(h.snapshot)); await nextTurn();
    assert.match(h.main.innerHTML, /data-monitor-field="autoSign"[^>]*checked/);
    h.change('intervalSeconds', true); await nextTurn();
    assert.equal(calls.length, 1);
  } finally { h.page.leave(); }
});

test('course rate limiting shows its cooldown while keeping group checks and signing available', async () => {
  const h = await pageHarness(async () => { throw new Error('No signing expected'); });
  try {
    h.setSnapshot({ ...h.snapshot, accounts: [{ ...h.snapshot.accounts[0], status: 'rate_limited',
      message: '学习通提示请勿频繁操作，稍后继续检查。', nextCheckAt: '2026-11-13T10:05:00Z',
      cooldownUntil: '2026-11-13T10:10:00Z', courseListCooldownUntil: '2026-11-13T10:15:00Z' }] });
    await h.click('refresh');
    await nextTurn();
    assert.match(h.main.innerHTML, /课程操作受限/);
    assert.match(h.main.innerHTML, /学习通提示请勿频繁操作，稍后继续检查。/);
    assert.match(h.main.innerHTML, /下次检查 刚刚/);
    assert.match(h.main.innerHTML, /课程查询暂缓至 刚刚，群聊继续检查。/);
    assert.match(h.main.innerHTML, /课程活动列表查询暂缓至 刚刚，课程通知与群聊继续接收。/);
    const limitedCheckButton = h.main.innerHTML.match(/<button[^>]*data-monitor-action="check"[^>]*>/);
    assert.ok(limitedCheckButton);
    assert.doesNotMatch(limitedCheckButton[0], /disabled/);
    const signButton = h.main.innerHTML.match(/<button[^>]*data-monitor-action="sign"[^>]*>/);
    assert.ok(signButton);
    assert.doesNotMatch(signButton[0], /disabled/);
    assert.match(h.main.innerHTML, /data-monitor-field="enabled"[^>]*checked/);
    const autoSwitch = h.main.innerHTML.match(/<input[^>]*data-monitor-field="autoSign"[^>]*>/);
    assert.ok(autoSwitch);
    assert.doesNotMatch(autoSwitch[0], /disabled/);
    h.setSnapshot({ ...h.snapshot, accounts: [{ ...h.snapshot.accounts[0], status: 'monitoring' }] });
    await h.click('refresh');
    await nextTurn();
    assert.doesNotMatch(h.main.innerHTML, /课程操作受限|课程查询暂缓至|课程活动列表查询暂缓至/);
    assert.match(h.main.innerHTML, /监控中/);
    const checkButton = h.main.innerHTML.match(/<button[^>]*data-monitor-action="check"[^>]*>/);
    assert.ok(checkButton);
    assert.doesNotMatch(checkButton[0], /disabled/);
  } finally { h.page.leave(); }
});

test('an ended course notification shows its course and verified start time without a signing action', async () => {
  let calls = 0;
  const startTime = Date.UTC(2026, 10, 12, 8, 30);
  const h = await pageHarness(async () => { calls++; }, { activityScope: 'course', notificationType: 'course_attendance',
    courseName: '管理学', name: '历史课程普通签到', kind: 'ordinary', status: 'ended', startTime,
    updatedAt: '2026-11-13T10:00:00Z', message: '签到活动已结束。' }, value => `时间:${value}`);
  try {
    assert.match(h.main.innerHTML, /课程 · 管理学 · 测试账号/);
    assert.match(h.main.innerHTML, /活动已结束/);
    assert.match(h.main.innerHTML, new RegExp(`<time>时间:${startTime}</time>`));
    assert.doesNotMatch(h.main.innerHTML, /时间:2026-11-13T10:00:00Z/);
    assert.doesNotMatch(h.main.innerHTML, /data-monitor-action="sign"|data-action="scan-account"/);
    await h.click();
    assert.equal(calls, 0);
  } finally { h.page.leave(); }
});

test('a course notification awaiting details is pending verification without sign or scan actions', async () => {
  let calls = 0;
  const message = '已收到课程签到通知，活动详情尚未核验，将继续检查。';
  const h = await pageHarness(async () => { calls++; }, { activityScope: 'course', notificationType: 'course_attendance',
    courseName: '待核验课程', kind: 'qr', status: 'awaiting_details', startTime: null,
    updatedAt: '2026-11-13T10:00:00Z', message }, value => `时间:${value}`);
  try {
    assert.match(h.main.innerHTML, /课程 · 待核验课程 · 测试账号/);
    for (const kind of ['ordinary', 'gesture', 'qr']) assert.equal(canSignTask({ ...h.task, kind }), false, kind);
    assert.match(h.main.innerHTML, />待核验<\/span>/);
    assert.ok(h.main.innerHTML.includes(message));
    assert.match(h.main.innerHTML, /<time>时间:2026-11-13T10:00:00Z<\/time>/);
    assert.doesNotMatch(h.main.innerHTML, /data-monitor-action="sign"|data-action="scan-account"/);
    await h.click();
    assert.equal(calls, 0);
  } finally { h.page.leave(); }
});

test('ended course history stays in all tasks without inflating attention counts or hiding uncertain and failed tasks', async () => {
  const h = await pageHarness(async () => { throw new Error('No signing expected'); });
  try {
    const history = Array.from({ length: 39 }, (_, index) => ({ ...h.task, id: `history-${index}`,
      activityScope: 'course', notificationType: 'course_attendance', name: `历史课程${index}`, status: 'ended' }));
    const current = [['unknown', '未知任务'], ['outcome_unknown', '待确认任务'], ['rejected', '失败任务'],
      ['pending', '处理中任务'], ['reported_success', '成功任务']].map(([status, name]) => ({ ...h.task, id: status, status, name }));
    h.setSnapshot({ ...h.snapshot, tasks: [...history, ...current] });
    await h.click('refresh');
    await nextTurn();
    assert.match(h.main.innerHTML, /历史课程0/);
    assert.match(h.main.innerHTML, /历史课程38/);
    assert.match(h.main.innerHTML, /<strong>3<span>项<\/span><\/strong><p>待查看或处理<\/p>/);
    assert.match(h.main.innerHTML, /<strong>1<span>项<\/span><\/strong><p>上游成功或已签<\/p>/);
    await h.click('attention');
    assert.doesNotMatch(h.main.innerHTML, /历史课程\d|成功任务/);
    for (const name of ['未知任务', '待确认任务', '失败任务', '处理中任务']) assert.ok(h.main.innerHTML.includes(name), name);
    await h.click('all');
    assert.match(h.main.innerHTML, /历史课程0/);
    assert.match(h.main.innerHTML, /历史课程38/);
    assert.match(h.main.innerHTML, /成功任务/);
  } finally { h.page.leave(); }
});

test('new ended course history never raises desktop alerts or crowds out an active course notification', async () => {
  const notices = [], previousNotification = globalThis.Notification, previousSecureContext = globalThis.isSecureContext;
  globalThis.Notification = class {
    static permission = 'granted';
    static async requestPermission() { return 'granted'; }
    constructor(title) { notices.push(title); }
  };
  globalThis.isSecureContext = true;
  let h;
  try {
    h = await pageHarness(async () => { throw new Error('No signing expected'); });
    await h.click('notifications');
    const history = Array.from({ length: 6 }, (_, index) => ({ ...h.task, id: `history-${index}`,
      activityScope: 'course', notificationType: 'course_attendance', courseName: '历史课程', status: 'ended' }));
    const current = { ...h.task, id: 'current-course', activityScope: 'course', notificationType: 'course_attendance',
      courseName: '当前课程', kind: 'ordinary', status: 'monitoring_only' };
    const pushed = { ...h.snapshot, tasks: [...history, current] };
    h.subscriptions[0].onSnapshot(clone(pushed));
    assert.deepEqual(notices, ['当前课程 · 仅提醒']);
    assert.match(h.main.innerHTML, /历史课程/);
    h.subscriptions[0].onSnapshot(clone(pushed));
    assert.equal(notices.length, 1);
  } finally {
    h?.page.leave();
    if (previousNotification === undefined) delete globalThis.Notification;
    else globalThis.Notification = previousNotification;
    if (previousSecureContext === undefined) delete globalThis.isSecureContext;
    else globalThis.isSecureContext = previousSecureContext;
  }
});

test('one click registers once and replaces the action with the pending receipt', async () => {
  let resolveSign, calls = 0;
  const result = new Promise(resolve => { resolveSign = resolve; });
  const h = await pageHarness(async id => { calls++; assert.equal(id, 'task-1'); return result; });
  try {
    assert.match(h.main.innerHTML, /群聊 · 群聊名称未提供/);
    assert.match(h.main.innerHTML, /data-monitor-action="sign"/);
    const first = h.click(), repeated = h.click();
    assert.equal(calls, 1);
    assert.match(h.main.innerHTML, /aria-busy="true"[^>]*disabled/);
    assert.match(h.main.innerHTML, /正在提交…/);
    resolveSign({ ...h.snapshot, tasks: [{ ...h.task, status: 'pending', requestId: 'normal-request-1' }] });
    await Promise.all([first, repeated]);
    assert.equal(calls, 1);
    assert.doesNotMatch(h.main.innerHTML, /data-monitor-action="sign"/);
    assert.match(h.main.innerHTML, /正在处理/);
    h.setSnapshot({ ...h.snapshot, tasks: [{ ...h.task, status: 'reported_success', requestId: 'normal-request-1' }] });
    await h.click('refresh');
    await nextTurn();
    assert.match(h.main.innerHTML, /上游回执成功/);
    assert.equal(calls, 1);
  } finally { h.page.leave(); }
});

test('a lost POST response only queries progress and cannot be resubmitted on page reentry', async () => {
  let calls = 0;
  const h = await pageHarness(async () => { calls++; throw Object.assign(new Error('连接中断'), { code: 'network_error' }); });
  try {
    await h.click();
    assert.equal(calls, 1);
    assert.ok(h.gets >= 2);
    assert.match(h.main.innerHTML, /签到请求结果待确认/);
    assert.doesNotMatch(h.main.innerHTML, /data-monitor-action="sign"/);
    await h.click();
    assert.equal(calls, 1);
    h.page.leave(); h.page.render(h.main);
    await nextTurn();
    assert.match(h.main.innerHTML, /签到请求结果待确认/);
    assert.doesNotMatch(h.main.innerHTML, /data-monitor-action="sign"/);
    await h.click();
    assert.equal(calls, 1);
  } finally { h.page.leave(); }
});

test('fixed and dynamic group QR cards use the direct action without opening a scanner', async () => {
  for (const refreshMode of ['fixed', 'dynamic']) {
    let calls = 0, h;
    h = await pageHarness(async id => {
      calls++; assert.equal(id, 'task-1');
      return { ...h.snapshot, tasks: [{ ...h.task, status: 'pending', requestId: 'group-qr-request-1' }] };
    }, { kind: 'qr', status: 'qr_required', name: '群二维码签到', refreshMode });
    try {
      assert.match(h.main.innerHTML, />直接签到<\/button>/);
      assert.match(h.main.innerHTML, /等待直接签到/);
      assert.doesNotMatch(h.main.innerHTML, /data-action="scan-account"/);
      await h.click();
      assert.equal(calls, 1);
      assert.match(h.main.innerHTML, /正在处理/);
      assert.doesNotMatch(h.main.innerHTML, /data-monitor-action="sign"|data-action="scan-account"/);
    } finally { h.page.leave(); }
  }
  const course = await pageHarness(async () => { throw new Error('Must not directly submit a course QR task'); },
    { activityScope: 'course', kind: 'qr', status: 'qr_required' });
  try {
    assert.match(course.main.innerHTML, /data-action="scan-account"/);
    assert.doesNotMatch(course.main.innerHTML, /data-monitor-action="sign"/);
  } finally { course.page.leave(); }
});

test('an uncertain group QR request never switches to scan or exposes a second direct action', async () => {
  let calls = 0;
  const h = await pageHarness(async () => { calls++; throw Object.assign(new Error('连接中断'), { code: 'network_error' }); },
    { kind: 'qr', status: 'qr_required', name: '群二维码签到' });
  try {
    await h.click();
    assert.equal(calls, 1);
    assert.match(h.main.innerHTML, /签到请求结果待确认/);
    assert.doesNotMatch(h.main.innerHTML, /data-monitor-action="sign"|data-action="scan-account"/);
    await h.click();
    assert.equal(calls, 1);
    assert.ok(h.gets >= 2);
  } finally { h.page.leave(); }
});

test('an eligible course gesture card submits by task ID without drawing or typing a gesture', async () => {
  let calls = 0, h;
  h = await pageHarness(async id => {
    calls++; assert.equal(id, 'task-1');
    return { ...h.snapshot, tasks: [{ ...h.task, status: 'pending', requestId: 'gesture-request-1' }] };
  }, { activityScope: 'course', kind: 'gesture', status: 'monitoring_only', name: '课程手势签到' });
  try {
    assert.match(h.main.innerHTML, />直接签到<\/button>/);
    assert.match(h.main.innerHTML, /可尝试签到/);
    assert.match(h.main.innerHTML, /兼容性待新活动确认，结果以官方回执为准/);
    assert.doesNotMatch(h.main.innerHTML, /data-action="scan-account"|name="signCode"/);
    await Promise.all([h.click(), h.click()]);
    assert.equal(calls, 1);
    assert.match(h.main.innerHTML, /正在处理/);
    assert.doesNotMatch(h.main.innerHTML, /data-monitor-action="sign"/);
  } finally { h.page.leave(); }
  for (const change of [{ status: 'needs_input' }, { status: 'code_required' },
    { requestId: 'existing-request' }, { status: 'outcome_unknown' }]) {
    const blocked = await pageHarness(async () => { throw new Error('This gesture task must not be directly submitted'); },
      { activityScope: 'course', kind: 'gesture', status: 'monitoring_only', ...change });
    try { assert.doesNotMatch(blocked.main.innerHTML, /data-monitor-action="sign"/); }
    finally { blocked.page.leave(); }
  }
});

test('an uncertain course gesture response only queries and never offers another submission', async () => {
  let calls = 0;
  const h = await pageHarness(async () => { calls++; throw Object.assign(new Error('连接中断'), { code: 'network_error' }); },
    { activityScope: 'course', kind: 'gesture', status: 'monitoring_only', name: '课程手势签到' });
  try {
    await h.click();
    assert.equal(calls, 1);
    assert.ok(h.gets >= 2);
    assert.match(h.main.innerHTML, /签到请求结果待确认/);
    assert.doesNotMatch(h.main.innerHTML, /data-monitor-action="sign"/);
    await h.click();
    assert.equal(calls, 1);
  } finally { h.page.leave(); }
});

test('a group gesture card submits directly once without a gesture form or scanner', async () => {
  let calls = 0, h;
  h = await pageHarness(async id => {
    calls++; assert.equal(id, 'task-1');
    return { ...h.snapshot, tasks: [{ ...h.task, status: 'pending', requestId: 'group-gesture-request-1' }] };
  }, { activityScope: 'group', kind: 'gesture', status: 'needs_input', name: '群手势签到' });
  try {
    assert.match(h.main.innerHTML, />直接签到<\/button>/);
    assert.match(h.main.innerHTML, /等待直接签到/);
    assert.doesNotMatch(h.main.innerHTML, /data-action="scan-account"|name="signCode"/);
    await Promise.all([h.click(), h.click()]);
    assert.equal(calls, 1);
    assert.match(h.main.innerHTML, /正在处理/);
    assert.doesNotMatch(h.main.innerHTML, /data-monitor-action="sign"/);
  } finally { h.page.leave(); }
});

test('an uncertain group gesture request queries only and suppresses both repeat submission and scanning', async () => {
  let calls = 0;
  const h = await pageHarness(async () => { calls++; throw Object.assign(new Error('连接中断'), { code: 'network_error' }); },
    { activityScope: 'group', kind: 'gesture', status: 'needs_input', name: '群手势签到' });
  try {
    await h.click();
    assert.equal(calls, 1);
    assert.ok(h.gets >= 2);
    assert.match(h.main.innerHTML, /签到请求结果待确认/);
    assert.doesNotMatch(h.main.innerHTML, /data-monitor-action="sign"|data-action="scan-account"/);
    await h.click();
    assert.equal(calls, 1);
  } finally { h.page.leave(); }
});
