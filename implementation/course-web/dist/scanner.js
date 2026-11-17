// Camera frames and selected images are decoded locally, never uploaded.
let fallbackPromise;
async function jsQr() {
  if (!fallbackPromise) fallbackPromise = import('./vendor/jsQR-1.4.0.js').then(() => {
    if (typeof globalThis.jsQR !== 'function') throw new Error('二维码识别组件未能加载。');
    return globalThis.jsQR;
  });
  return fallbackPromise;
}

async function detector() {
  if (typeof globalThis.BarcodeDetector !== 'function') return null;
  try {
    const formats = await BarcodeDetector.getSupportedFormats();
    return formats.includes('qr_code') ? new BarcodeDetector({ formats: ['qr_code'] }) : null;
  } catch { return null; }
}

function pixels(source, canvas) {
  const width = source.videoWidth || source.naturalWidth || source.width;
  const height = source.videoHeight || source.naturalHeight || source.height;
  if (!width || !height) return null;
  const scale = Math.min(1, 1440 / Math.max(width, height));
  canvas.width = Math.round(width * scale); canvas.height = Math.round(height * scale);
  const context = canvas.getContext('2d', { willReadFrequently: true });
  context.drawImage(source, 0, 0, canvas.width, canvas.height);
  return context.getImageData(0, 0, canvas.width, canvas.height);
}

export function cameraError(error) {
  if (error?.name === 'NotAllowedError') return '相机权限未开启。请允许使用相机，或选择二维码图片。';
  if (error?.name === 'NotFoundError') return '没有找到可用相机，请选择二维码图片。';
  if (error?.name === 'NotReadableError') return '相机正在被其他应用使用，请关闭占用后重试。';
  return error?.message || '相机无法启动，请选择二维码图片。';
}

export class QrScanner {
  constructor() { this.generation = 0; this.stream = null; this.frame = null; this.video = null; }
  stop() {
    this.generation++;
    if (this.frame !== null) cancelAnimationFrame(this.frame);
    this.frame = null;
    this.stream?.getTracks().forEach(track => track.stop());
    this.stream = null;
    if (this.video) { this.video.pause(); this.video.srcObject = null; }
    this.video = null;
  }
  async start(video, onDecode, onError) {
    this.stop();
    const generation = this.generation;
    if (!globalThis.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      throw new Error('手机相机需要 HTTPS 页面。当前可选择二维码图片；本机 localhost 也可使用相机。');
    }
    const native = await detector();
    const decode = native ? null : await jsQr();
    if (generation !== this.generation) return;
    const stream = await navigator.mediaDevices.getUserMedia({ audio: false,
      video: { facingMode: { ideal: 'environment' }, width: { ideal: 1280 }, height: { ideal: 720 } } });
    if (generation !== this.generation) { stream.getTracks().forEach(track => track.stop()); return; }
    this.stream = stream; this.video = video; video.srcObject = stream;
    try { await video.play(); }
    catch (error) { this.stop(); throw error; }
    if (generation !== this.generation) return;
    const canvas = document.createElement('canvas');
    let last = 0, activeDetector = native, fallback = decode;
    const tick = async now => {
      if (generation !== this.generation) return;
      if (now - last >= 220 && video.readyState >= 2) {
        last = now;
        try {
          let value;
          if (activeDetector) {
            try { value = (await activeDetector.detect(video))[0]?.rawValue; }
            catch { activeDetector = null; fallback = await jsQr(); }
          }
          if (!activeDetector) {
            const data = pixels(video, canvas);
            if (data) value = fallback(data.data, data.width, data.height, { inversionAttempts: 'attemptBoth' })?.data;
          }
          if (generation !== this.generation) return;
          if (value) { this.stop(); onDecode(value); return; }
        } catch (error) { this.stop(); onError(error); return; }
      }
      if (generation === this.generation) this.frame = requestAnimationFrame(tick);
    };
    this.frame = requestAnimationFrame(tick);
  }
}

export async function decodeQrImage(file) {
  if (!file || !file.type.startsWith('image/')) throw new Error('请选择一张二维码图片。');
  if (file.size > 20 * 1024 * 1024) throw new Error('图片过大，请选择小于 20MB 的二维码图片。');
  const objectUrl = URL.createObjectURL(file);
  try {
    const image = new Image();
    image.src = objectUrl;
    await image.decode();
    const native = await detector();
    if (native) {
      try { const value = (await native.detect(image))[0]?.rawValue; if (value) return value; } catch { /* Local fallback below. */ }
    }
    const decode = await jsQr();
    const canvas = document.createElement('canvas');
    const data = pixels(image, canvas);
    const value = data && decode(data.data, data.width, data.height, { inversionAttempts: 'attemptBoth' })?.data;
    if (!value) throw new Error('没有识别到二维码，请选择清晰、完整的二维码图片。');
    return value;
  } finally { URL.revokeObjectURL(objectUrl); }
}
