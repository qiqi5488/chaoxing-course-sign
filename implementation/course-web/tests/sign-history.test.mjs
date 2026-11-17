import test from 'node:test';
import assert from 'node:assert/strict';
import { createSignHistoryPage } from '../dist/sign-history-page.js';

const tick = () => new Promise(resolve => setImmediate(resolve));
const row = (id = '123', accountId = 'a') => ({ requestId: `job-${id}`, accountId, activeId: id,
  activityScope: 'course', status: 'already_signed', statusLabel: '已签', source: 'chaoxing', name: `签到活动${id}`, courseName: '测试课程',
  kind: 'ordinary', message: '上游已确认成功', recordedAt: '2025-11-13T09:00:00Z' });
const result = (id = 'a', records = [row('123', id)], nextOffset = null, total = records.length) =>
  ({ accountId: id, records, total, nextOffset, source: 'chaoxing', snapshot: 'test-snapshot', counts: { course: total, group: 0 }, sources: [{ activityScope: 'course', status: 'ok' }, { activityScope: 'group', status: 'unavailable', name: '群聊历史', message: '尚未核实查询入口' }], fetchedAt: '2026-11-13T10:00:00Z' });
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };

function harness(getSignRecords) {
  let accounts = [{ id: 'a', name: '账号甲', maskedIdentifier: '188****1111' }, { id: 'b', name: '账号乙' }];
  const handlers = new Map();
  globalThis.document = { addEventListener: (name, handler) => handlers.set(name, handler) };
  const main = { innerHTML: '' };
  const page = createSignHistoryPage({ store: { getSignRecords, getState: () => ({ accounts }) },
    esc: value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])),
    icon: () => '', avatar: () => '' });
  const click = action => handlers.get('click')({ target: { closest: () => ({ disabled: false, dataset: { historyAction: action } }) } });
  return { page, main, click, remove: () => { accounts = []; } };
}

test('history opens for the selected account with receipt dates and no sign action', async () => {
  const calls = [], h = harness(async (id, options) => { calls.push([id, options]); return result(); });
  h.page.render(h.main, 'a');
  assert.match(h.main.innerHTML, /正在查询成功记录/);
  await tick();
  assert.deepEqual(calls, [['a', { offset: 0, source: 'chaoxing', refresh: false, snapshot: '' }]]);
  assert.match(h.main.innerHTML, /学习通签到记录/);
  assert.match(h.main.innerHTML, /账号甲/);
  assert.match(h.main.innerHTML, /签到活动123/);
  assert.match(h.main.innerHTML, /签到时间 2025/);
  assert.match(h.main.innerHTML, /群聊：尚未查到可核实的记录/);
  assert.doesNotMatch(h.main.innerHTML, /群聊：已确认 0 条/);
  assert.match(h.main.innerHTML, /href="#\/monitor"/);
  assert.doesNotMatch(h.main.innerHTML, /data-monitor-action="sign"|立即签到|账号乙/);
  h.page.leave();
});

test('local receipts are separate and switching sources ignores an older official response', async () => {
  const official = deferred();
  const calls = [];
  const h = harness((id, options) => {
    calls.push(options);
    if (options.source === 'chaoxing') return official.promise;
    return Promise.resolve({ ...result('a', [{ ...row('local'), source: '' }]), source: 'local' });
  });
  h.page.render(h.main, 'a');
  h.click('local'); await tick();
  assert.match(h.main.innerHTML, /本系统签到回执/);
  assert.match(h.main.innerHTML, /不代表学习通全部历史/);
  official.resolve(result()); await tick();
  assert.match(h.main.innerHTML, /签到活动local/);
  assert.doesNotMatch(h.main.innerHTML, /签到活动123/);
  assert.equal(calls[0].source, 'chaoxing');
  assert.equal(calls[1].source, 'local');
  h.page.leave();
});

test('load more deduplicates shifting pages and refresh queries from the start', async () => {
  const offsets = [], h = harness(async (id, { offset }) => {
    offsets.push(offset);
    return offset ? result('a', [row(), row('456')]) : result('a', [row()], 50, 2);
  });
  h.page.render(h.main, 'a'); await tick();
  h.click('more'); await tick();
  assert.equal((h.main.innerHTML.match(/<h3>签到活动123<\/h3>/g) || []).length, 1);
  assert.match(h.main.innerHTML, /签到活动456/);
  h.click('refresh'); await tick();
  assert.deepEqual(offsets, [0, 50, 0]);
  assert.doesNotMatch(h.main.innerHTML, /签到活动456/);
  h.page.leave();
});

test('late requests cannot overwrite another account or a page that has been left', async () => {
  const old = deferred(), fresh = deferred(), h = harness(id => id === 'a' ? old.promise : fresh.promise);
  h.page.render(h.main, 'a');
  h.page.render(h.main, 'b');
  fresh.resolve(result('b', [row('456', 'b')])); await tick();
  old.resolve(result()); await tick();
  assert.match(h.main.innerHTML, /账号乙/);
  assert.doesNotMatch(h.main.innerHTML, /账号甲|签到活动123/);
  const later = deferred(), left = harness(() => later.promise);
  left.page.render(left.main, 'a'); left.page.leave(); left.main.innerHTML = 'different page';
  later.resolve(result()); await tick();
  assert.equal(left.main.innerHTML, 'different page');
  h.remove(); h.page.render(h.main, 'b');
  assert.match(h.main.innerHTML, /账号已移除/);
  assert.doesNotMatch(h.main.innerHTML, /签到活动456/);
  h.page.leave();
});

test('failed queries show retry, empty results are explicit and receipt text is escaped', async () => {
  let fail = true;
  const h = harness(async () => { if (fail) throw new Error('查询连接失败'); return result('a', []); });
  h.page.render(h.main, 'a'); await tick();
  assert.match(h.main.innerHTML, /role="alert">查询连接失败/);
  assert.doesNotMatch(h.main.innerHTML, /暂无已确认的成功记录/);
  fail = false; h.click('refresh'); await tick();
  assert.match(h.main.innerHTML, /暂无已确认的成功记录/);
  h.page.leave();
  const escaped = harness(async () => result('a', [{ ...row(), name: '<img src=x onerror=alert(1)>', message: '<script>bad</script>' }]));
  escaped.page.render(escaped.main, 'a'); await tick();
  assert.match(escaped.main.innerHTML, /&lt;img/);
  assert.doesNotMatch(escaped.main.innerHTML, /<img|<script>/);
  escaped.page.leave();
});
