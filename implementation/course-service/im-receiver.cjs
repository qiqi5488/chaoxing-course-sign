'use strict';

// Official WebIM 4.24.3.1 evidence: output/im-wire-evidence-20261113.txt.
// Credentials arrive on stdin only. This child never reads account files/env.
const { writeSync } = require('node:fs');

const MAX_LINE_BYTES = 100_000;
const WATCHDOG_MS = 25_000;
const CODE = Object.freeze({ INVALID_INPUT: 1, RUNTIME: 2, WATCHDOG: 3 });
const BLOCKED_CODES = new Set([206, 207, 214, 216, 217, 220]);
const TERMINAL_CODES = new Set([56, ...BLOCKED_CODES]);
const CHAT_TYPES = new Set(['singleChat', 'groupChat', 'chatRoom']);
const noop = () => {};

function silenceSdkLogs() {
  // generateProvision logs a token prefix even at info level. Mute before require.
  globalThis.console = new Proxy(Object.create(null), { get: () => noop });
}

function plain(value, limit) {
  return typeof value === 'string' && value.length > 0 && value.length <= limit
    && value === value.trim() && !/[\x00-\x1f\x7f-\x9f]/u.test(value);
}

function parseCredentials(value) {
  let data = value;
  if (typeof value === 'string') {
    if (Buffer.byteLength(value, 'utf8') > MAX_LINE_BYTES) throw new Error('invalid input');
    data = JSON.parse(value);
  }
  if (!data || typeof data !== 'object' || Array.isArray(data)
      || Object.keys(data).sort().join(',') !== 'accessToken,deviceId,url,username'
      || !plain(data.username, 128) || !/^[A-Za-z0-9_.@-]+$/.test(data.username)
      || ['.', '..'].includes(data.username)
      || !plain(data.accessToken, 8192) || !/^[\x21-\x7e]+$/.test(data.accessToken)
      || !plain(data.deviceId, 128) || !/^[A-Za-z0-9_-]+$/.test(data.deviceId)
      || !plain(data.url, 2048)) throw new Error('invalid input');
  const url = new URL(data.url);
  if (url.protocol !== 'wss:' || !url.hostname.endsWith('.easemob.com')
      || !['', '443'].includes(url.port) || url.username || url.password
      || url.search || data.url.includes('#')
      || url.pathname !== '/websocket') throw new Error('invalid input');
  return { username: data.username, accessToken: data.accessToken,
    url: url.href, deviceId: data.deviceId };
}

function normalizeNotice(message) {
  try {
    if (!message || typeof message !== 'object' || Array.isArray(message)) return null;
    const id = typeof message.id === 'number' && Number.isSafeInteger(message.id)
      ? String(message.id) : message.id;
    const to = typeof message.to === 'number' && Number.isSafeInteger(message.to)
      ? String(message.to) : message.to;
    if (!plain(id, 128) || !plain(to, 512) || !CHAT_TYPES.has(message.chatType)) return null;
    let attachment = message.ext?.attachment;
    if (typeof attachment === 'string') {
      if (Buffer.byteLength(attachment, 'utf8') > MAX_LINE_BYTES) return null;
      attachment = JSON.parse(attachment);
    }
    if (!attachment || typeof attachment !== 'object' || Array.isArray(attachment)
        || attachment.attachmentType !== 15) return null;
    const result = { type: 'notice', id, to, chatType: message.chatType, attachment };
    const line = JSON.stringify(result) + '\n';
    return Buffer.byteLength(line, 'utf8') <= MAX_LINE_BYTES ? result : null;
  } catch {
    return null;
  }
}

function officialSdk(options) {
  globalThis.self = globalThis;
  const MiniCore = require('easemob-websdk/miniCore/miniCore.js');
  return new MiniCore(options);
}

function errorCode(error) {
  const code = typeof error === 'number' ? error : error?.type ?? error?.code;
  return TERMINAL_CODES.has(code) ? code : CODE.RUNTIME;
}

function createReceiver({ sdkFactory = officialSdk,
  write = line => writeSync(1, line), exit = code => process.exit(code),
  schedule = setTimeout, cancel = clearTimeout } = {}) {
  let sdk, timer = null, started = false, closed = false, connected = false;
  let disconnected = false;

  function output(event) {
    try {
      const line = JSON.stringify(event) + '\n';
      if (Buffer.byteLength(line, 'utf8') > MAX_LINE_BYTES) return false;
      write(line);
      return true;
    } catch {
      return false;
    }
  }

  function clearWatchdog() {
    if (timer !== null) cancel(timer);
    timer = null;
  }

  function finish(status, event) {
    if (closed) return;
    closed = true;
    clearWatchdog();
    if (event) output(event);
    try {
      const closing = sdk?.close();
      if (closing && typeof closing.catch === 'function') closing.catch(noop);
    } catch {
      // Closing failure must not print the SDK error or leave timers running.
    }
    exit(status);
  }

  function fail(code = CODE.RUNTIME) {
    if (![CODE.INVALID_INPUT, CODE.RUNTIME, CODE.WATCHDOG, ...TERMINAL_CODES].includes(code)) {
      code = CODE.RUNTIME;
    }
    finish(code === 56 ? 21 : BLOCKED_CODES.has(code) ? 20 : 1, { type: 'error', code });
  }

  function emit(event) {
    if (!closed && !output(event)) finish(1);
  }

  function watchdog() {
    if (!closed && timer === null) timer = schedule(() => fail(CODE.WATCHDOG), WATCHDOG_MS);
  }

  const events = {
    onConnected() {
      if (closed) return;
      clearWatchdog();
      disconnected = false;
      if (!connected) emit({ type: 'connected' });
      connected = true;
    },
    onDisconnected(reason) {
      if (closed) return;
      const code = errorCode(reason);
      if (TERMINAL_CODES.has(code)) return fail(code);
      connected = false;
      if (!disconnected) emit({ type: 'disconnected' });
      disconnected = true;
      watchdog();
    },
    onReconnecting() { if (!closed) watchdog(); },
    onTokenExpired() { fail(56); },
    onError(error) { fail(errorCode(error)); },
    onTextMessage(message) {
      if (closed) return;
      const notice = normalizeNotice(message);
      if (notice) emit(notice);
    },
  };

  return {
    get closed() { return closed; },
    start(value) {
      if (closed) return;
      if (started) return fail(CODE.INVALID_INPUT);
      started = true;
      let credentials;
      try {
        credentials = parseCredentials(value);
      } catch {
        return fail(CODE.INVALID_INPUT);
      }
      silenceSdkLogs();
      try {
        sdk = sdkFactory({ appKey: 'cx-dev#cxstudy', isHttpDNS: false,
          url: credentials.url, apiUrl: 'https://a3-vip6.easemob.com',
          deviceId: credentials.deviceId, delivery: false, autoReconnectNumMax: 1 });
        sdk.deviceId = credentials.deviceId;
        sdk.addEventHandler('receiver', events);
        watchdog();
        Promise.resolve(sdk.open({ username: credentials.username,
          accessToken: credentials.accessToken })).catch(error => {
          if (!closed) fail(errorCode(error));
        });
      } catch (error) {
        fail(errorCode(error));
      }
    },
    stop() { finish(0); },
    fail,
  };
}

function attachInput(receiver, input) {
  let buffer = '', initialized = false;
  input.setEncoding('utf8');
  input.on('data', chunk => {
    if (receiver.closed) return;
    if (typeof chunk !== 'string' || Buffer.byteLength(chunk, 'utf8') > MAX_LINE_BYTES) {
      return receiver.fail(CODE.INVALID_INPUT);
    }
    buffer += chunk;
    let boundary;
    while (!receiver.closed && (boundary = buffer.indexOf('\n')) !== -1) {
      const line = buffer.slice(0, boundary).replace(/\r$/, '');
      buffer = buffer.slice(boundary + 1);
      if (Buffer.byteLength(line, 'utf8') > MAX_LINE_BYTES) return receiver.fail(CODE.INVALID_INPUT);
      if (!initialized) {
        initialized = true;
        receiver.start(line);
      } else if (line === 'stop' || line === 'end') {
        receiver.stop();
      } else {
        receiver.fail(CODE.INVALID_INPUT);
      }
    }
    if (Buffer.byteLength(buffer, 'utf8') > MAX_LINE_BYTES) receiver.fail(CODE.INVALID_INPUT);
  });
  input.on('end', () => receiver.stop());
  input.on('error', () => receiver.fail(CODE.RUNTIME));
}

function main() {
  const receiver = createReceiver();
  // No exception text or stack may reach stdout/stderr, including token errors.
  process.on('uncaughtException', () => receiver.fail(CODE.RUNTIME));
  process.on('unhandledRejection', () => receiver.fail(CODE.RUNTIME));
  process.stdout.on('error', () => receiver.fail(CODE.RUNTIME));
  process.on('SIGTERM', () => receiver.stop());
  process.on('SIGINT', () => receiver.stop());
  attachInput(receiver, process.stdin);
}

module.exports = { MAX_LINE_BYTES, WATCHDOG_MS, CODE, parseCredentials, normalizeNotice,
  createReceiver, attachInput, silenceSdkLogs };
if (require.main === module) main();
