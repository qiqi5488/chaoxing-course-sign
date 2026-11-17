'use strict';

const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { spawnSync } = require('node:child_process');
const path = require('node:path');
const test = require('node:test');
const { MAX_LINE_BYTES, WATCHDOG_MS, CODE, parseCredentials, normalizeNotice,
  createReceiver, attachInput } = require('../im-receiver.cjs');

const credentials = { username: 'private-test-user', accessToken: 'private-test-token',
  url: 'wss://im-api-wechat-vip6.easemob.com:443/websocket', deviceId: 'course_sign_test_device' };
const attachment = { attachmentType: 15, att_chat_course: {
  aid: '300', atype: 2, type: 1,
  url: 'https://mobilelearn.chaoxing.com/newsign/preSign?activePrimaryId=300&courseId=700&classId=800&' } };

function message(changes = {}) {
  return { id: '300', to: '900', chatType: 'groupChat', from: 'private-sender',
    msg: 'private-chat-text', ext: { attachment }, ...changes };
}

function harness(t, { open = () => Promise.resolve(), close = () => {} } = {}) {
  const savedConsole = globalThis.console;
  t.after(() => { globalThis.console = savedConsole; });
  const lines = [], exits = [], timers = new Map(), options = [], opened = [];
  let handlers, closed = 0, timerId = 0;
  const sdk = {
    addEventHandler(name, events) { assert.equal(name, 'receiver'); handlers = events; },
    open(value) { opened.push(value); return open(value); },
    close() { closed++; close(); handlers?.onDisconnected({ message: 'private-close-error' }); },
  };
  const receiver = createReceiver({
    sdkFactory(value) { options.push(value); console.info('private-sdk-token-prefix'); return sdk; },
    write: line => lines.push(line), exit: status => exits.push(status),
    schedule(fn, delay) { const id = ++timerId; timers.set(id, { fn, delay }); return id; },
    cancel: id => timers.delete(id),
  });
  return { receiver, lines, exits, timers, options, opened, sdk,
    events: () => handlers, closeCount: () => closed,
    output: () => lines.map(line => JSON.parse(line)),
  };
}

test('credentials are validated but never written, and SDK options preserve OS defaults', async t => {
  const h = harness(t, { open(value) { console.log(value); return Promise.resolve(); } });
  const leaked = [];
  globalThis.console = { ...globalThis.console, info: (...args) => leaked.push(args),
    log: (...args) => leaked.push(args) };
  h.receiver.start(JSON.stringify(credentials));
  h.events().onConnected();
  await Promise.resolve();
  assert.deepEqual(h.opened, [{ username: credentials.username, accessToken: credentials.accessToken }]);
  assert.deepEqual(h.options, [{ appKey: 'cx-dev#cxstudy', isHttpDNS: false,
    url: 'wss://im-api-wechat-vip6.easemob.com/websocket', apiUrl: 'https://a3-vip6.easemob.com',
    deviceId: credentials.deviceId, delivery: false, autoReconnectNumMax: 1 }]);
  assert.equal('customOSPlatform' in h.options[0], false);
  assert.equal(h.sdk.deviceId, credentials.deviceId);
  assert.deepEqual(h.output(), [{ type: 'connected' }]);
  assert.deepEqual(leaked, []);
  assert.equal(h.lines.join('').includes('private'), false);
});

for (const changes of [
  { username: '' }, { username: '..' }, { username: 'private\nuser' },
  { accessToken: '' }, { accessToken: 'private\ntoken' }, { accessToken: 'x'.repeat(8193) },
  { deviceId: '../phone' }, { deviceId: '' }, { url: 'ws://im-api-wechat-vip6.easemob.com/websocket' },
  { url: 'wss://evil.example/websocket' },
  { url: 'wss://easemob.com.evil.example/websocket' },
  { url: 'wss://user:pass@im-api-wechat-vip6.easemob.com/websocket' },
  { url: 'wss://im-api-wechat-vip6.easemob.com/websocket?token=private' },
  { url: 'wss://im-api-wechat-vip6.easemob.com:8443/websocket' },
  { url: 'wss://im-api-wechat-vip6.easemob.com/websocket#' },
  { url: 'wss://im-api-wechat-vip6.easemob.com/other' }, { extra: 'private-data' },
]) {
  test(`invalid credential shape ${Object.keys(changes).join(',')} is rejected`, () => {
    assert.throws(() => parseCredentials({ ...credentials, ...changes }));
  });
}

test('notice output contains only the authorized metadata and attachment', t => {
  const h = harness(t);
  h.receiver.start(credentials);
  h.events().onTextMessage(message());
  assert.deepEqual(h.output(), [{ type: 'notice', id: '300', to: '900', chatType: 'groupChat', attachment }]);
  assert.equal(h.lines.join('').includes('private'), false);
  assert.equal(h.lines.join('').includes('from'), false);
  assert.equal(h.lines.join('').includes('msg'), false);
  assert.equal(h.events().onCustomMessage, undefined);
  assert.equal(h.events().onImageMessage, undefined);
});

test('JSON-string attachments and safe integer metadata are accepted', () => {
  const result = normalizeNotice(message({ id: 300, to: 900, ext: { attachment: JSON.stringify(attachment) } }));
  assert.deepEqual(result, { type: 'notice', id: '300', to: '900', chatType: 'groupChat', attachment });
});

test('ordinary chats, nonattendance attachments and malformed metadata are discarded', () => {
  for (const value of [null, [], {}, message({ ext: undefined }), message({ ext: {} }),
    message({ ext: { attachment: '{broken' } }), message({ ext: { attachment: [] } }),
    message({ ext: { attachment: { attachmentType: 18 } } }),
    message({ ext: { attachment: { attachmentType: '15' } } }),
    message({ id: { private: 'token' } }), message({ to: [] }),
    message({ chatType: 'private-chat-text' }), message({ id: 'private\ntext' }),
    message({ id: Number.MAX_SAFE_INTEGER + 1 })]) assert.equal(normalizeNotice(value), null);
});

test('overlong and cyclic attachments never produce partial or oversized lines', t => {
  const h = harness(t);
  h.receiver.start(credentials);
  const cyclic = { attachmentType: 15 };
  cyclic.self = cyclic;
  for (const value of [{ attachmentType: 15, text: 'x'.repeat(MAX_LINE_BYTES) },
    { attachmentType: 15, text: '签'.repeat(40_000) }, cyclic, 'x'.repeat(MAX_LINE_BYTES + 1)]) {
    h.events().onTextMessage(message({ ext: { attachment: value } }));
  }
  assert.deepEqual(h.lines, []);
  h.events().onTextMessage(message());
  assert.ok(h.lines.every(line => Buffer.byteLength(line, 'utf8') <= MAX_LINE_BYTES));
  assert.equal(h.lines.length, 1);
});

for (const code of [56, 206, 207, 214, 216, 217, 220]) {
  test(`terminal SDK code ${code} closes and exits exactly once`, t => {
    const h = harness(t);
    h.receiver.start(credentials);
    const staleWatchdog = [...h.timers.values()][0].fn;
    h.events().onError({ type: code, message: 'private-token-in-error' });
    h.events().onError({ type: code });
    h.events().onTokenExpired();
    h.events().onConnected();
    h.events().onTextMessage(message());
    staleWatchdog();
    h.receiver.stop();
    assert.deepEqual(h.output(), [{ type: 'error', code }]);
    assert.deepEqual(h.exits, [code === 56 ? 21 : 20]);
    assert.equal(h.closeCount(), 1);
    assert.equal(h.timers.size, 0);
    assert.equal(h.receiver.closed, true);
  });
}

test('terminal disconnect and token-expiry callbacks stop the process', t => {
  const h = harness(t);
  h.receiver.start(credentials);
  h.events().onDisconnected({ type: 220 });
  assert.deepEqual(h.exits, [20]);
  assert.deepEqual(h.output(), [{ type: 'error', code: 220 }]);
});

test('unknown SDK errors reveal only a fixed finite code', t => {
  const h = harness(t);
  h.receiver.start(credentials);
  h.events().onError({ type: 999999, code: 'private-token', stack: 'private-stack' });
  assert.deepEqual(h.output(), [{ type: 'error', code: CODE.RUNTIME }]);
  assert.deepEqual(h.exits, [1]);
});

test('asynchronous SDK login failure is handled without an unhandled rejection', async t => {
  const h = harness(t, { open: () => Promise.reject({ type: 56, message: 'private-token' }) });
  h.receiver.start(credentials);
  await Promise.resolve();
  assert.deepEqual(h.output(), [{ type: 'error', code: 56 }]);
  assert.deepEqual(h.exits, [21]);
});

test('synchronous SDK token errors use the same terminal exit code', t => {
  const h = harness(t, { open() { throw { type: 56, message: 'private-token' }; } });
  h.receiver.start(credentials);
  assert.deepEqual(h.output(), [{ type: 'error', code: 56 }]);
  assert.deepEqual(h.exits, [21]);
  assert.equal(h.closeCount(), 1);
});

test('closing errors cannot reveal private data or prevent exit', t => {
  const h = harness(t, { close() { throw new Error('private-close-token'); } });
  h.receiver.start(credentials);
  h.receiver.stop();
  h.receiver.stop();
  assert.deepEqual(h.lines, []);
  assert.deepEqual(h.exits, [0]);
  assert.equal(h.closeCount(), 1);
});

test('initial and disconnect watchdogs are bounded and reconnect events cannot reset them', t => {
  const h = harness(t);
  h.receiver.start(credentials);
  assert.deepEqual([...h.timers.values()].map(timer => timer.delay), [WATCHDOG_MS]);
  h.events().onConnected();
  assert.equal(h.timers.size, 0);
  h.events().onDisconnected();
  const watchdog = [...h.timers.values()][0];
  h.events().onReconnecting();
  h.events().onDisconnected();
  assert.equal(h.timers.size, 1);
  assert.equal([...h.timers.values()][0], watchdog);
  watchdog.fn();
  assert.deepEqual(h.output(), [{ type: 'connected' }, { type: 'disconnected' },
    { type: 'error', code: CODE.WATCHDOG }]);
  assert.deepEqual(h.exits, [1]);
});

test('initial connection watchdog also exits if SDK never connects', t => {
  const h = harness(t);
  h.receiver.start(credentials);
  [...h.timers.values()][0].fn();
  assert.deepEqual(h.output(), [{ type: 'error', code: CODE.WATCHDOG }]);
  assert.deepEqual(h.exits, [1]);
});

for (const command of ['stop', 'end', null]) {
  test(`stdin ${command ?? 'EOF'} closes once despite close callbacks`, t => {
    const h = harness(t);
    const input = new EventEmitter();
    input.setEncoding = value => assert.equal(value, 'utf8');
    attachInput(h.receiver, input);
    const line = JSON.stringify(credentials) + '\r\n';
    input.emit('data', line.slice(0, 12));
    input.emit('data', line.slice(12));
    if (command) input.emit('data', command + '\n');
    input.emit('end');
    assert.deepEqual(h.exits, [0]);
    assert.equal(h.closeCount(), 1);
    assert.deepEqual(h.lines, []);
  });
}

test('overlong stdin and malformed JSON exit before constructing or opening SDK', t => {
  const h = harness(t);
  const input = new EventEmitter();
  input.setEncoding = () => {};
  attachInput(h.receiver, input);
  input.emit('data', 'x'.repeat(MAX_LINE_BYTES + 1));
  assert.deepEqual(h.output(), [{ type: 'error', code: CODE.INVALID_INPUT }]);
  assert.equal(h.options.length, 0);
  assert.equal(h.opened.length, 0);
});

test('CLI malformed-input errors contain no submitted credential or stack', () => {
  const result = spawnSync(process.execPath, [path.join(__dirname, '../im-receiver.cjs')],
    { input: '{"private-test-token":', encoding: 'utf8', timeout: 3000 });
  assert.equal(result.status, 0); // EOF before a complete first line is a stop.
  assert.equal(result.stdout, '');
  assert.equal(result.stderr, '');
  const invalid = spawnSync(process.execPath, [path.join(__dirname, '../im-receiver.cjs')],
    { input: '{"private-test-token":}\n', encoding: 'utf8', timeout: 3000 });
  assert.equal(invalid.status, 1);
  assert.deepEqual(JSON.parse(invalid.stdout), { type: 'error', code: CODE.INVALID_INPUT });
  assert.equal(invalid.stderr, '');
});
