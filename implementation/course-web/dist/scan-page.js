import { QrScanner, decodeQrImage, cameraError } from './scanner.js';

const BATCH_KEY = 'keqian.qr.batch.v1';
const ACTIVE = new Set(['queued', 'running']);
const SUCCESS = new Set(['reported_success', 'already_signed']);
const LABELS = { queued: '等待处理', running: '正在签到', reported_success: '上游返回成功', already_signed: '已签到',
  requires_validation: '需要验证', invalid_challenge: '验证资料不完整', location_required: '需要现场位置',
  need_context: '需要签到资料', device_conflict: '设备限制', rejected: '签到未完成', login_required: '需要重新登录',
  face_required: '需要人脸验证', previous_result_unknown: '此前结果未确认',
  session_expired: '登录已失效', unknown: '结果未确认', outcome_unknown: '结果未确认', http_error: '结果未确认',
  interrupted: '处理已中断', skipped: '未提交', account_unavailable: '账号不可用' };
const eligible = a => a.status === 'active' && a.grant && (!a.grant.expiresAt || Date.parse(a.grant.expiresAt) > Date.now())
  && (!a.sessionExpiresAt || Date.parse(a.sessionExpiresAt) > Date.now());
const batchId = () => {
  if (globalThis.crypto?.randomUUID) return crypto.randomUUID();
  const bytes = new Uint8Array(16); crypto.getRandomValues(bytes);
  return Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
};

export function createScanPage({ store, esc, icon, avatar, toast }) {
  let chosen = new Set(), batch = null, currentBatchId = '', submitting = false, error = '', poll = null, batchMissing = false;
  let main, active = false, scope = 0, cameraDialog, scanner = new QrScanner();
  try { currentBatchId = sessionStorage.getItem(BATCH_KEY) || ''; } catch { /* Session storage may be disabled. */ }
  const remember = id => { try { if (id) sessionStorage.setItem(BATCH_KEY, id); else sessionStorage.removeItem(BATCH_KEY); } catch {} };
  const accounts = () => store.getState().accounts;
  const isRunning = () => submitting || (batch && ACTIVE.has(batch.status));
  const validChosen = () => accounts().filter(a => chosen.has(a.id) && eligible(a));
  const setError = message => {
    error = message;
    const node = main?.querySelector('#scan-error');
    if (node) { node.textContent = message; node.hidden = !message; }
  };

  function stopCamera() {
    scanner.stop();
    if (cameraDialog?.open) cameraDialog.close();
  }
  function stopPolling() { clearTimeout(poll); poll = null; }
  function selectedIds() {
    const rows = validChosen();
    if (!rows.length) throw new Error('请先选择至少一个已登录并授权的账号。');
    return rows.map(a => a.id);
  }
  function renderResult() {
    const target = main?.querySelector('#batch-results');
    if (!target) return;
    if (!currentBatchId) { target.innerHTML = ''; return; }
    if (!batch) {
      target.innerHTML = `<section class="batch-panel"><h2>本次签到请求</h2><p>正在查询已登记的批次。刷新或返回页面不会自动补发签到。</p><button class="secondary-button" data-scan-action="resume">重新查询本次结果</button>${batchMissing ? '<p class="scan-note">若已在学习通核实本次结果，可移除本机提示后开始新的扫码。这不会撤销服务端可能正在处理的请求。</p><button class="text-button scan-new" data-scan-action="forget-missing">我已核实，开始新的扫码</button>' : ''}</section>`;
      return;
    }
    const items = Array.isArray(batch.items) ? batch.items : [];
    const completed = items.filter(i => !ACTIVE.has(i.status)).length;
    const succeeded = items.filter(i => SUCCESS.has(i.status)).length;
    const needsAttention = items.filter(i => !SUCCESS.has(i.status) && !ACTIVE.has(i.status)).length;
    target.innerHTML = `<section class="batch-panel" aria-labelledby="batch-title"><div class="section-heading"><h2 id="batch-title">${ACTIVE.has(batch.status) ? '批量签到进行中' : '批量签到结果'}</h2><span class="scan-count">${completed} / ${items.length}</span></div>
      <p class="batch-activity">活动 ${esc(batch.activeId || '待识别')}<span>${ACTIVE.has(batch.status) ? '正在处理' : batch.status === 'interrupted' ? '处理已中断' : '处理结束'}</span></p>
      <div class="batch-summary"><span><b>${succeeded}</b>成功或已签</span><span><b>${needsAttention}</b>需处理</span><span><b>${items.length - completed}</b>待完成</span></div>
      <div class="batch-progress"><span style="width:${items.length ? completed / items.length * 100 : 0}%"></span></div>
      <div class="batch-items" aria-live="polite">${items.map(item => `<article class="batch-item"><div><strong>${esc(item.name || '账号')}</strong><span class="batch-status ${SUCCESS.has(item.status) ? 'success' : ACTIVE.has(item.status) ? 'pending' : 'attention'}">${esc(LABELS[item.status] || '需要处理')}</span></div><p>${esc(item.message || (ACTIVE.has(item.status) ? '正在等待服务端回执…' : '请查看该账号在学习通中的签到记录。'))}</p></article>`).join('')}</div>
      <p class="scan-note">每个账号分别提交并核对回执。结果未确认时，请先在学习通查看记录，不会自动补发。</p>
      <button class="secondary-button" data-scan-action="resume">刷新批次结果</button>${!ACTIVE.has(batch.status) ? '<button class="text-button scan-new" data-scan-action="new">为下一次签到重新选择账号</button>' : ''}</section>`;
  }
  function render(target = main) {
    main = target; active = true;
    const rows = accounts();
    chosen = new Set([...chosen].filter(id => rows.some(a => a.id === id && eligible(a))));
    const selectable = rows.filter(eligible);
    const selected = validChosen();
    main.innerHTML = `<div class="page-top"><div><p class="eyebrow">SCAN ONCE, SIGN TOGETHER</p><h1>批量扫码签到</h1><p>选好账号，扫一次二维码，分别完成签到。</p></div><span class="page-mark">${icon('qr', 23)}</span></div>
      <div class="scan-intro">${icon('shield', 22)}<p>手机只识别二维码内容，签到由服务端使用各账号的独立会话直接提交。</p></div>
      <div class="section-heading"><h2>选择签到账号<small>${selected.length} / ${selectable.length}</small></h2><button class="text-button" data-scan-action="select-all" ${isRunning() || !selectable.length ? 'disabled' : ''}>${selected.length === selectable.length && selectable.length ? '取消全选' : '全选可用账号'}</button></div>
      <div class="scan-accounts">${rows.length ? rows.map(a => `<label class="scan-account ${!eligible(a) ? 'unavailable' : ''}"><input class="selection-box" type="checkbox" data-scan-account="${esc(a.id)}" ${chosen.has(a.id) ? 'checked' : ''} ${!eligible(a) || isRunning() ? 'disabled' : ''}><span class="scan-account-main">${avatar(a)}<span><strong>${esc(a.name)}</strong><small>${esc(a.maskedIdentifier)}</small></span></span><span class="scan-account-state">${eligible(a) ? '可签到' : '需登录授权'}</span></label>`).join('') : '<div class="empty"><h3>还没有可签到的账号</h3><p>先在账号页登录并授权，再回来选择。</p><a class="text-button" href="#/accounts">去添加账号</a></div>'}</div>
      <div id="scan-error" class="form-error" role="alert" ${error ? '' : 'hidden'}>${esc(error)}</div>
      <div class="scan-start"><p>已选 <strong>${selected.length}</strong> 个账号${isRunning() ? ' · 本次处理中，请等待结果' : ' · 扫到二维码后立即为所选账号提交'}</p><button class="primary-button" data-scan-action="camera" ${!selected.length || isRunning() || !!currentBatchId && !batch ? 'disabled' : ''}>${icon('qr', 20)}${submitting ? '正在登记签到…' : '开始扫码并签到'}</button>
      <div class="scan-alternatives"><button class="text-button" data-scan-action="image" ${!selected.length || isRunning() || !!currentBatchId && !batch ? 'disabled' : ''}>${icon('image', 16)}选择二维码图片</button><button class="text-button" data-scan-action="text" ${!selected.length || isRunning() || !!currentBatchId && !batch ? 'disabled' : ''}>粘贴二维码内容</button></div><input type="file" id="qr-image-file" accept="image/*" class="visually-hidden" aria-label="选择签到二维码图片"><p class="scan-note">图片只在本机识别，不上传。手机相机需要 HTTPS；服务端限制按实际回执显示。</p></div>
      <div id="batch-results"></div>`;
    renderResult();
    if (currentBatchId && !poll && !document.hidden) schedulePoll(0);
  }

  function ensureCameraDialog() {
    if (cameraDialog) return cameraDialog;
    cameraDialog = document.createElement('dialog');
    cameraDialog.className = 'sheet qr-camera-dialog'; cameraDialog.setAttribute('aria-labelledby', 'qr-camera-title');
    document.body.append(cameraDialog);
    cameraDialog.addEventListener('close', () => scanner.stop());
    cameraDialog.addEventListener('cancel', () => scanner.stop());
    cameraDialog.addEventListener('click', event => {
      if (event.target !== cameraDialog) return;
      const r = cameraDialog.getBoundingClientRect();
      if (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom) stopCamera();
    });
    return cameraDialog;
  }
  async function startCamera() {
    const ids = selectedIds();
    setError('');
    const dialog = ensureCameraDialog();
    dialog.innerHTML = `<div class="sheet-head"><h2 id="qr-camera-title">扫描学习通签到二维码</h2><button class="icon-button" data-scan-action="stop-camera" aria-label="关闭相机">${icon('close', 18)}</button></div><div class="sheet-body"><p class="sheet-sub">识别成功后，将为已选 ${ids.length} 个账号直接提交本次签到。</p><div class="qr-video-wrap"><video playsinline muted aria-label="二维码相机预览"></video><div class="qr-viewfinder"></div></div><p class="scan-camera-message" role="status">正在打开后置相机…</p><button class="secondary-button" data-scan-action="camera-image">改用二维码图片</button></div>`;
    dialog.showModal();
    const generation = scope;
    try {
      await scanner.start(dialog.querySelector('video'), value => {
        stopCamera(); if (active && generation === scope) submit(value, ids);
      }, err => { stopCamera(); if (active) { setError(cameraError(err)); render(); } });
      const message = dialog.querySelector('.scan-camera-message');
      if (dialog.open && message) message.textContent = '请将二维码完整放入框内，识别成功后相机会自动关闭。';
    } catch (err) { stopCamera(); if (active && generation === scope) { setError(cameraError(err)); render(); } }
  }
  function showText() {
    const dialog = ensureCameraDialog();
    dialog.innerHTML = `<div class="sheet-head"><h2 id="qr-camera-title">粘贴二维码内容</h2><button class="icon-button" data-scan-action="stop-camera" aria-label="关闭弹窗">${icon('close', 18)}</button></div><div class="sheet-body"><p class="sheet-sub">粘贴从学习通签到二维码中识别到的完整内容。提交后将为当前选择的 ${validChosen().length} 个账号签到。</p><form id="qr-text-form"><label class="field"><span>二维码内容</span><textarea name="qrText" maxlength="8192" required placeholder="https://mobilelearn.chaoxing.com/…" autofocus></textarea></label><button class="primary-button" type="submit">为所选账号提交签到</button></form></div>`;
    dialog.showModal(); dialog.querySelector('textarea').focus();
  }

  async function submit(qrText, ids) {
    if (submitting || (batch && ACTIVE.has(batch.status))) return;
    const value = String(qrText || '').trim();
    if (!value || value.length > 8192) { setError('二维码内容为空或过长，请重新扫描。'); render(); return; }
    submitting = true; error = ''; batch = null; batchMissing = false;
    currentBatchId = batchId(); remember(currentBatchId);
    const requestId = currentBatchId;
    if (active) { render(); main.querySelector('#batch-results')?.scrollIntoView({ behavior: 'smooth', block: 'start' }); }
    try {
      const result = await store.createQrBatch({ accountIds: ids, qrText: value }, requestId);
      batch = result; currentBatchId = result.batchId; remember(currentBatchId);
    } catch (err) {
      // A missing HTTP response does not establish that the server did not start.
      if (err.statusCode >= 500 || ['network_error', 'request_timeout', 'invalid_response'].includes(err.code)) {
        error = '批次登记结果尚未确认。请查询本次请求，系统不会再次提交二维码。';
      } else { error = err.message; currentBatchId = ''; remember(''); }
    } finally {
      submitting = false;
      if (active) {
        render();
        if (currentBatchId) { main.querySelector('#batch-results')?.scrollIntoView({ behavior: 'smooth', block: 'start' }); schedulePoll(0); }
      }
    }
  }
  function schedulePoll(delay = 1000) {
    stopPolling();
    if (!active || document.hidden || !currentBatchId || submitting || batch && !ACTIVE.has(batch.status)) return;
    poll = setTimeout(readBatch, delay);
  }
  async function readBatch() {
    poll = null;
    const id = currentBatchId, generation = scope;
    if (!active || document.hidden || !id || submitting) return;
    try {
      const result = await store.getQrBatch(id);
      if (!active || generation !== scope || id !== currentBatchId) return;
      batch = result; batchMissing = false; setError(''); renderResult();
      if (ACTIVE.has(batch.status)) schedulePoll();
      else { render(); store.refresh().catch(() => {}); }
    } catch (err) {
      if (!active || generation !== scope || id !== currentBatchId) return;
      batchMissing = err.code === 'batch_not_found' || err.code === 'not_found';
      setError(batchMissing
        ? '暂未找到本次批次。为避免重复签到，不会自动补发；请刷新查询或在学习通核实。' : err.message);
      renderResult();
    }
  }

  document.addEventListener('click', async event => {
    const button = event.target.closest('[data-scan-action]');
    if (!button || button.disabled || !active) return;
    const action = button.dataset.scanAction;
    try {
      if (action === 'select-all') {
        const rows = accounts().filter(eligible);
        chosen = validChosen().length === rows.length ? new Set() : new Set(rows.map(a => a.id)); render();
      } else if (action === 'camera') await startCamera();
      else if (action === 'stop-camera') stopCamera();
      else if (action === 'image' || action === 'camera-image') { selectedIds(); stopCamera(); main.querySelector('#qr-image-file').click(); }
      else if (action === 'text') { selectedIds(); setError(''); showText(); }
      else if (action === 'resume') { stopPolling(); await readBatch(); }
      else if (action === 'new' && !isRunning()) { batch = null; currentBatchId = ''; remember(''); error = ''; render(); }
      else if (action === 'forget-missing' && batchMissing && !submitting) {
        stopPolling(); batch = null; currentBatchId = ''; batchMissing = false; remember(''); error = ''; render();
      }
    } catch (err) { setError(err.message); }
  });
  document.addEventListener('change', async event => {
    if (!active) return;
    if (event.target.dataset.scanAccount) {
      const id = event.target.dataset.scanAccount;
      if (event.target.checked && accounts().some(a => a.id === id && eligible(a))) chosen.add(id); else chosen.delete(id);
      render();
    } else if (event.target.id === 'qr-image-file') {
      const file = event.target.files[0]; event.target.value = '';
      if (!file) return;
      const generation = scope;
      try {
        const ids = selectedIds(); setError('');
        const value = await decodeQrImage(file);
        if (active && generation === scope) await submit(value, ids);
      } catch (err) { if (active && generation === scope) { setError(err.message); render(); } }
    }
  });
  document.addEventListener('submit', event => {
    if (event.target.id !== 'qr-text-form' || !active) return;
    event.preventDefault();
    try { const ids = selectedIds(), value = event.target.elements.qrText.value; stopCamera(); submit(value, ids); }
    catch (err) { setError(err.message); }
  });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { stopCamera(); stopPolling(); scope++; }
    else if (active && currentBatchId) schedulePoll(0);
  });
  window.addEventListener('pagehide', () => { stopCamera(); stopPolling(); scope++; });

  return {
    render,
    leave() { active = false; scope++; stopCamera(); stopPolling(); },
    select(ids) {
      if (isRunning()) { toast('已有签到批次正在处理，请先查看本次结果。'); return; }
      chosen = new Set(accounts().filter(a => ids.includes(a.id) && eligible(a)).map(a => a.id));
      if (chosen.size < ids.length) toast('已排除尚未登录或授权失效的账号。');
    },
    canSelect: eligible,
  };
}
