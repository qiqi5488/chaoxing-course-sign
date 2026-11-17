import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
const source = await readFile(new URL('../dist/scanner.js', import.meta.url), 'utf8');
const { QrScanner } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

function environment(getUserMedia) {
  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { mediaDevices: { getUserMedia } } });
  globalThis.isSecureContext = true;
  globalThis.document = { createElement: () => ({}) };
  globalThis.BarcodeDetector = class { static async getSupportedFormats() { return ['qr_code']; }
    async detect() { return [{ rawValue: 'https://mobilelearn.chaoxing.com/widget/sign/e?id=123&enc=fixture' }]; } };
  const frames = [];
  globalThis.requestAnimationFrame = callback => { frames.push(callback); return frames.length; };
  globalThis.cancelAnimationFrame = () => {};
  return frames;
}

test('closing while camera permission is pending stops the eventual stream', async () => {
  let release, stops = 0;
  const frames = environment(() => new Promise(resolve => { release = resolve; }));
  const scanner = new QrScanner();
  const video = { play: async () => {}, pause() {}, srcObject: null };
  const starting = scanner.start(video, () => assert.fail('must not decode after close'), () => {});
  await new Promise(resolve => setImmediate(resolve));
  scanner.stop();
  release({ getTracks: () => [{ stop() { stops++; } }] });
  await starting;
  assert.equal(stops, 1);
  assert.equal(video.srcObject, null);
  assert.equal(frames.length, 0);
});

test('a decoded camera frame triggers once and releases the camera before submission', async () => {
  let stops = 0, decoded = 0;
  const frames = environment(async () => ({ getTracks: () => [{ stop() { stops++; } }] }));
  const video = { readyState: 2, play: async () => {}, pause() {}, srcObject: null };
  const scanner = new QrScanner();
  await scanner.start(video, () => { decoded++; assert.equal(stops, 1); }, error => assert.fail(error.message));
  await frames[0](300);
  await frames[0](600);
  assert.equal(decoded, 1);
  assert.equal(video.srcObject, null);
  assert.equal(frames.length, 1);
});
