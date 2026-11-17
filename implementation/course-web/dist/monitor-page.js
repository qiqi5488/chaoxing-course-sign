const SUCCESS = new Set(['reported_success', 'already_signed']);
const PENDING = new Set(['queued', 'pending', 'running', 'signing']);
const SIGNABLE = new Set(['discovered', 'monitoring_only', 'needs_input', 'awaiting_action']);
const LABELS = { discovered: '发现签到', monitoring_only: '仅提醒', awaiting_action: '等待处理', awaiting_details: '待核验', needs_input: '需补充资料',
  queued: '等待处理', pending: '正在处理', running: '正在处理', signing: '正在签到', reported_success: '上游回执成功',
  already_signed: '已签到', qr_required: '需要扫码', code_required: '需要签到码', location_required: '需要现场位置',
  photo_required: '需要照片', face_required: '需要人脸验证', requires_validation: '需要验证', invalid_challenge: '验证未完成',
  need_context: '需确认活动要求',
  device_conflict: '设备限制', requires_manual: '需在学习通处理', rejected: '签到未完成', ended: '活动已结束', unknown: '结果未确认',
  outcome_unknown: '结果未确认', http_error: '结果未确认', previous_result_unknown: '此前结果未确认',
  login_required: '需要重新登录', session_expired: '登录已失效', unsupported: '需在学习通处理', skipped: '未自动提交' };
const ACCOUNT_LABELS = { idle: '等待检查', disabled: '监控已关闭', enabled: '监控已开启', monitoring: '监控中',
  scheduled: '等待检查', waiting: '等待检查', queued: '等待检查', checking: '正在检查', running: '正在检查', ok: '检查完成', partial: '部分来源异常',
  error: '检查未完成', rate_limited: '课程操作受限', login_required: '需要重新登录', session_expired: '登录已失效' };
const REALTIME_LABELS = { disabled: '未开启', connecting: '正在连接', connected: '已连接', reconnecting: '正在重连',
  blocked: '连接受限', error: '接收异常' };
const taskVersion = task => `${task.status}:${task.message || ''}`;
const waitingForPhoto = task => task?.activityScope === 'group' && task.resolvedKind === 'photo'
  && task.status === 'photo_required' && task.submitted === false
  && ['photo_missing', 'photo_lookup_incomplete'].includes(task.reasonCode);

// The first snapshot establishes a baseline, so opening the page never alerts old tasks.
export function taskUpdates(previous, tasks) {
  const next = new Map(tasks.map(task => [task.id, taskVersion(task)]));
  return { next, changed: previous ? tasks.filter(task => previous.get(task.id) !== taskVersion(task)) : [] };
}

export function taskLabel(status, task) {
  if (status === 'reported_success' && task?.reasonCode === 'photo_record_verified') return '官方记录已确认';
  if (status === 'photo_required' && waitingForPhoto(task)) return task.reasonCode === 'photo_missing' ? '等待活动照片' : '照片查询待补查';
  if (task?.activityScope === 'course' && task.kind === 'gesture' && status === 'monitoring_only'
    && canSignTask({ ...task, status })) return '可尝试签到';
  if (task?.activityScope === 'group' && ['qr', 'gesture'].includes(task.kind) && ['needs_input', 'qr_required'].includes(status)) {
    return canSignTask({ ...task, status }) ? '等待直接签到' : '需在学习通处理';
  }
  return LABELS[status] || '需要查看';
}
export function taskTone(status) { return SUCCESS.has(status) ? 'success' : PENDING.has(status) ? 'pending' : 'attention'; }
export function canSignTask(task) {
  return !!task && typeof task.id === 'string' && !!task.id && typeof task.accountId === 'string' && !!task.accountId
    && (!task.requestId || waitingForPhoto(task)) && task.submitted !== true
    && (SIGNABLE.has(task.status) || waitingForPhoto(task) || task.activityScope === 'group' && task.kind === 'qr' && task.status === 'qr_required')
    && (task.activityScope == null || ['course', 'group'].includes(task.activityScope))
    && (waitingForPhoto(task) || task.kind === 'ordinary' || task.activityScope === 'group' && ['ordinary_candidate', 'qr', 'gesture'].includes(task.kind)
      || task.activityScope === 'course' && task.kind === 'gesture' && task.status === 'monitoring_only' && task.submitted == null);
}

export function createMonitorPage({ store, esc, icon, avatar, time, toast }) {
  let main, active = false, scope = 0, timer, currentRequest = null, saving = '', data = null, error = '', seen = null;
  let unsubscribeMonitor = null, snapshotVersion = 0, streamConnected = false, streamMessage = '正在连接实时推送…';
  let notifications = false, filter = 'all';
  const requestedTasks = new Set(), uncertainTasks = new Set();
  const stop = () => { clearTimeout(timer); timer = null; };
  const config = id => data?.accounts.find(a => a.accountId === id);
  const schedule = () => {
    if (!active || saving || streamConnected) { stop(); return; }
    if (timer == null) timer = setTimeout(() => { timer = null; load(); }, 30_000);
  };
  const available = account => account.status === 'active' && !!account.grant;

  function acceptSnapshot(result, clearError = true) {
    const changed = !!error || !data || JSON.stringify([data.accounts, data.tasks]) !== JSON.stringify([result.accounts, result.tasks]);
    data = result; if (clearError) error = ''; snapshotVersion++; notify(result.tasks);
    for (const task of result.tasks) {
      if (task.requestId) uncertainTasks.delete(task.id);
      if (waitingForPhoto(task)) requestedTasks.delete(task.id);
    }
    return changed;
  }

  function subscribe() {
    const version = scope;
    unsubscribeMonitor = store.subscribeMonitor(result => {
      if (!active || version !== scope) return;
      const recovered = !streamConnected;
      streamConnected = true; stop();
      if (acceptSnapshot(result) || recovered) draw();
    }, failure => {
      if (!active || version !== scope) return;
      streamConnected = false;
      streamMessage = failure.code === 'monitor_stream_unavailable' ? '每30秒刷新状态' : '推送恢复中 · 每30秒刷新';
      draw(); schedule();
    });
  }

  function notify(tasks) {
    const changes = taskUpdates(seen, tasks); seen = changes.next;
    if (!notifications || globalThis.Notification?.permission !== 'granted') return;
    for (const task of changes.changed.filter(task => task.status !== 'ended').slice(0, 5)) {
      try {
        const notice = new Notification(`${task.courseName || (task.activityScope === 'group' ? '群聊签到' : '课程签到')} · ${taskLabel(task.status, task)}`, {
          body: `${task.accountName || '账号'}：${task.name || '签到任务'}${task.message ? ' · ' + task.message : ''}`,
          tag: `course-sign-${task.id}`,
        });
        notice.onclick = () => { window.focus(); location.hash = '/monitor'; notice.close(); };
      } catch { /* Some mobile browsers require a service worker for notifications. */ }
    }
  }

  function accountCard(account) {
    const value = config(account.id), disabled = !value || !!saving;
    const enabled = value?.enabled || false;
    return `<article class="monitor-account" id="monitor-${esc(account.id)}"><div class="monitor-account-head"><a class="monitor-record-link" href="#/monitor/accounts/${encodeURIComponent(account.id)}/records" aria-label="查看${esc(account.name)}的签到成功记录">${avatar(account)}<span><strong>${esc(account.name)}</strong><small>${esc(account.maskedIdentifier)} · 查看成功记录</small></span>${icon('chevron', 15)}</a><span class="batch-status ${enabled ? 'success' : 'pending'}">${enabled ? '已开启' : '已关闭'}</span></div>
      <label class="monitor-toggle"><span><strong>课程与群聊签到监控</strong><small>检查已支持课程及可读取的群聊活动</small></span><input type="checkbox" role="switch" data-monitor-field="enabled" data-id="${esc(account.id)}" aria-label="${esc(account.name)}的课程与群聊签到监控" ${enabled ? 'checked' : ''} ${disabled || !available(account) && !enabled ? 'disabled' : ''}></label>
      <label class="monitor-toggle"><span><strong>自动签到</strong><small>尝试处理课程普通与手势、群普通、二维码、手势与拍照</small></span><input type="checkbox" role="switch" data-monitor-field="autoSign" data-id="${esc(account.id)}" aria-label="${esc(account.name)}的自动签到" ${value?.autoSign ? 'checked' : ''} ${disabled || !enabled || !available(account) ? 'disabled' : ''}></label>
      <div class="monitor-frequency"><span>补漏检查</span><span>每5分钟 · 断线恢复补查</span></div>
      <div class="monitor-account-status"><p>实时接收：${esc(REALTIME_LABELS[value?.realtimeStatus] || (enabled ? '等待连接' : '未开启'))}</p>${value?.realtimeMessage ? `<p>${esc(value.realtimeMessage)}</p>` : ''}${value?.connectedAt ? `<time>连接建立 ${esc(time(value.connectedAt))}</time>` : ''}${value?.lastMessageAt ? `<time>最近消息 ${esc(time(value.lastMessageAt))}</time>` : ''}<span>${esc(value ? ACCOUNT_LABELS[value.status] || value.status || '等待检查' : '正在读取设置…')}${value?.courseCount != null ? ` · ${value.courseCount} 门课程` : ''}${value?.groupCount != null ? ` · ${value.groupCount} 个群聊` : ''}</span>${value?.lastCheckedAt ? `<time>最近检查 ${esc(time(value.lastCheckedAt))}</time>` : ''}${value?.nextCheckAt && enabled ? `<time>下次检查 ${esc(time(value.nextCheckAt))}</time>` : ''}${value?.cooldownUntil ? `<p>课程查询暂缓至 ${esc(time(value.cooldownUntil))}，群聊继续检查。</p>` : ''}${value?.courseListCooldownUntil ? `<p>课程活动列表查询暂缓至 ${esc(time(value.courseListCooldownUntil))}，课程通知与群聊继续接收。</p>` : ''}${value?.message ? `<p>${esc(value.message)}</p>` : ''}${value?.groupMessage ? `<p>${esc(value.groupMessage)}</p>` : ''}${!available(account) ? '<p>请先重新登录并授权此账号。</p>' : ''}</div>
      ${value?.courseErrors.length ? `<details class="monitor-errors"><summary>${value.courseErrors.length} 项来源读取未完成</summary>${value.courseErrors.map(e => `<p><strong>${esc(e.name || '签到来源')}</strong>：${esc(e.message || '请稍后重试')}</p>`).join('')}</details>` : ''}
      <button class="text-button" data-monitor-action="check" data-id="${esc(account.id)}" ${disabled || !enabled || !available(account) ? 'disabled' : ''}>${icon('refresh', 14)}${saving === account.id ? '正在保存…' : '立即检查签到'}</button></article>`;
  }

  function taskCard(task, accounts) {
    const direct = task.activityScope === 'group' && ['qr', 'gesture'].includes(task.kind)
      || task.activityScope === 'course' && task.kind === 'gesture';
    const qr = task.activityScope !== 'group' && (task.status === 'qr_required' || task.kind === 'qr'
      && ['discovered', 'monitoring_only', 'needs_input', 'awaiting_action', 'skipped'].includes(task.status));
    const submitting = saving === `sign:${task.id}` && (!task.requestId || waitingForPhoto(task)), uncertain = uncertainTasks.has(task.id) && !task.requestId;
    const status = uncertain ? 'outcome_unknown' : submitting ? 'pending' : task.status;
    const source = task.activityScope === 'group' ? '群聊' : '课程';
    const signable = canSignTask(task) && (!requestedTasks.has(task.id) || submitting);
    const account = accounts.find(account => account.id === task.accountId);
    const message = uncertain ? '签到请求结果待确认，正在查询回执；请勿重复提交。'
      : submitting ? '正在登记签到请求，请稍候。' : task.message || '请查看学习通中的活动与考勤记录。';
    const title = task.kind === 'photo' && ['签到', '群签到活动'].includes(task.name) ? '拍照签到' : task.name || `${source}签到`;
    return `<article class="monitor-task"><div class="monitor-task-head"><h3>${esc(title)}</h3><span class="batch-status ${taskTone(status)}">${esc(taskLabel(status, task))}</span></div><p class="monitor-task-course">${source} · ${esc(task.courseName || `${source}名称未提供`)} · ${esc(task.accountName || '账号')}</p><p ${uncertain || submitting ? 'role="status"' : ''}>${esc(message)}</p><div class="monitor-task-foot"><time>${esc(time(task.startTime ?? (task.updatedAt || task.discoveredAt)))}</time>${signable ? `<button class="text-button" data-monitor-action="sign" data-task-id="${esc(task.id)}" aria-label="${esc(`为${task.accountName || '该账号'}${direct ? '直接' : ''}提交${task.name || '签到'}`)}" ${submitting ? 'aria-busy="true"' : ''} ${saving || !account || !available(account) ? 'disabled' : ''}>${submitting ? '正在提交…' : waitingForPhoto(task) ? '重新查找照片' : direct ? '直接签到' : '立即签到'}</button>` : qr ? `<button class="text-button" data-action="scan-account" data-id="${esc(task.accountId)}">${icon('qr', 14)}去扫码</button>` : ''}</div></article>`;
  }

  function draw() {
    if (!active || !main) return;
    const focused = main.contains(document.activeElement) ? document.activeElement.dataset : null;
    const expanded = new Set([...main.querySelectorAll('.monitor-account:has(.monitor-errors[open])')].map(node => node.id));
    const accounts = store.getState().accounts;
    const tasks = data?.tasks || [], displayed = tasks.filter(task => filter === 'all' || task.status !== 'ended' && !SUCCESS.has(task.status));
    const enabled = data?.accounts.filter(a => a.enabled).length || 0;
    const supported = 'Notification' in globalThis && globalThis.isSecureContext;
    main.innerHTML = `<div class="page-top"><div><p class="eyebrow">SIGN-IN WATCH</p><h1>签到监控</h1><p>发现课程与群聊签到，及时处理并查看回执。</p></div><span class="page-mark">${icon('eye', 23)}</span></div>
      <div class="monitor-intro"><span>${icon('clock', 22)}</span><div><strong>实时消息立即处理 · 每5分钟补漏</strong><p>断线恢复后自动补查。关闭网页后服务端继续接收消息，服务必须保持启动。</p></div></div>
      <div class="stats monitor-stats"><div class="stat"><strong>${enabled}<span>个</span></strong><p>已开启监控</p></div><div class="stat"><strong>${tasks.filter(t => SUCCESS.has(t.status)).length}<span>项</span></strong><p>上游成功或已签</p></div><div class="stat"><strong>${tasks.filter(t => t.status !== 'ended' && !SUCCESS.has(t.status) && !PENDING.has(t.status)).length}<span>项</span></strong><p>待查看或处理</p></div></div>
      ${error ? `<div class="form-error" role="alert">${esc(error)}<p>上次显示的数据可能已过期，请刷新后核实。</p></div>` : ''}
      <div class="monitor-notice"><div><strong>本机系统通知</strong><p>${notifications ? '本页打开期间提醒新任务与结果变化。' : '点击开启，仅在本页打开时提醒。'}</p></div><button class="text-button" data-monitor-action="notifications" ${!supported ? 'disabled' : ''}>${notifications ? '关闭通知' : supported ? '开启通知' : '浏览器不支持'}</button></div>
      <section class="monitor-section"><div class="section-heading"><h2>监控账号<small>${accounts.length}</small></h2><button class="text-button" data-monitor-action="refresh" ${saving || currentRequest ? 'disabled' : ''}>${icon('refresh', 14)}刷新</button></div><p class="monitor-hint">普通签到可点“立即签到”，群二维码与手势、课程手势可点“直接签到”；结果以官方回执为准。</p><div class="monitor-accounts">${accounts.length ? accounts.map(accountCard).join('') : '<div class="empty"><h3>先添加一个账号</h3><p>登录授权后，可为账号开启课程与群聊监控。</p><a class="text-button" href="#/accounts">去添加账号</a></div>'}</div></section>
      <section class="monitor-section"><div class="section-heading"><h2>签到任务<small>${tasks.length}</small></h2><span class="monitor-updating" role="status">${!data ? '正在读取…' : esc(streamConnected ? '实时推送更新' : streamMessage)}</span></div><div class="filters" aria-label="筛选签到任务">${[['all', '全部'], ['attention', '待查看']].map(([key, name]) => `<button class="filter" data-monitor-filter="${key}" aria-pressed="${filter === key}">${name}</button>`).join('')}</div><div class="monitor-tasks">${displayed.length ? displayed.map(task => taskCard(task, accounts)).join('') : `<div class="empty"><span>${icon('book', 25)}</span><h3>${tasks.length ? '暂时没有待查看任务' : '暂未发现签到任务'}</h3><p>${enabled ? '发现课程或可读取的群聊签到后，会显示在这里。' : '开启账号监控后，服务端会持续检查已支持的签到来源。'}</p></div>`}</div></section>
      <p class="monitor-footnote">群拍照使用本次活动中已有的签到照片；暂时没有照片时，开启自动签到的账号会在补漏时再查，也可点“重新查找照片”。课程拍照、口令、定位、人脸与验证仍需相应资料。课程手势会尝试直接提交，兼容性待新活动确认，结果以官方回执为准。课程二维码需扫码。群聊较早活动可能不在当前读取范围内；提交结果未确认时不会自动补发。</p>`;
    for (const node of main.querySelectorAll('.monitor-account')) {
      if (expanded.has(node.id)) node.querySelector('.monitor-errors')?.setAttribute('open', '');
    }
    if (focused) {
      const target = [...main.querySelectorAll('[data-monitor-field], [data-monitor-action], [data-monitor-filter]')].find(node =>
        ['id', 'taskId', 'monitorField', 'monitorAction', 'monitorFilter'].every(key => node.dataset[key] === focused[key]));
      target?.focus({ preventScroll: true });
    }
  }

  async function load() {
    if (!active || saving) return;
    if (currentRequest) { schedule(); return; }
    const version = scope, revision = snapshotVersion, operation = store.getMonitor();
    currentRequest = operation;
    try {
      const result = await operation;
      if (!active || version !== scope || revision !== snapshotVersion) return;
      acceptSnapshot(result);
    } catch (failure) {
      if (active && version === scope && revision === snapshotVersion) error = failure.message || '监控状态未能读取。';
    } finally {
      if (version === scope) {
        if (currentRequest === operation) currentRequest = null;
        if (active) draw();
        schedule();
      }
    }
  }

  async function modify(id, changes) {
    if (saving) return;
    const version = scope; saving = id; stop(); draw();
    try {
      if (currentRequest) await currentRequest.catch(() => {});
      if (!active || version !== scope) return;
      const revision = snapshotVersion;
      const operation = changes ? store.setMonitor(id, { ...config(id), ...changes, intervalSeconds: 300 }) : store.checkMonitor(id);
      currentRequest = operation;
      const result = await operation;
      if (!active || version !== scope) return;
      if (revision === snapshotVersion) acceptSnapshot(result);
      toast(changes ? '监控设置已保存' : '已安排签到检查');
    } catch (failure) { if (active && version === scope) error = failure.message || '监控设置未能保存，请刷新核实。'; }
    finally { if (version === scope) { currentRequest = null; saving = ''; if (active) draw(); schedule(); } }
  }

  async function signTask(id) {
    if (saving || requestedTasks.has(id)) return;
    const task = data?.tasks.find(task => task.id === id);
    if (!canSignTask(task)) return;
    const version = scope;
    requestedTasks.add(id); saving = `sign:${id}`; stop(); draw();
    try {
      if (currentRequest) await currentRequest.catch(() => {});
      if (!active || version !== scope) { requestedTasks.delete(id); return; }
      if (!canSignTask(data?.tasks.find(task => task.id === id))) { requestedTasks.delete(id); return; }
      const revision = snapshotVersion;
      currentRequest = store.signMonitorTask(id);
      const result = await currentRequest;
      if (!active || version !== scope) return;
      if (revision === snapshotVersion) acceptSnapshot(result);
      uncertainTasks.delete(id);
      toast('签到请求已登记，正在查询回执');
    } catch (failure) {
      const rejected = failure.statusCode >= 400 && failure.statusCode < 500
        && failure.code !== 'activity_already_attempted';
      if (rejected) requestedTasks.delete(id);
      else uncertainTasks.add(id);
      if (active && version === scope) {
        error = failure.message || '签到请求结果待确认，请刷新查询回执。';
        try {
          const revision = snapshotVersion;
          const result = await store.getMonitor();
          if (active && version === scope && revision === snapshotVersion) acceptSnapshot(result, false);
        } catch { /* Keep the local uncertainty guard until a later GET confirms the request. */ }
      }
    } finally { if (version === scope) { currentRequest = null; saving = ''; if (active) draw(); schedule(); } }
  }

  document.addEventListener('change', event => {
    const input = event.target.closest('[data-monitor-field]');
    if (!active || !input || input.disabled) return;
    const key = input.dataset.monitorField;
    if (!['enabled', 'autoSign'].includes(key)) return;
    const changes = { [key]: input.checked };
    if (key === 'enabled' && !input.checked) changes.autoSign = false;
    modify(input.dataset.id, changes);
  });
  document.addEventListener('click', async event => {
    const filterButton = event.target.closest('[data-monitor-filter]');
    if (active && filterButton) { filter = filterButton.dataset.monitorFilter; draw(); return; }
    const button = event.target.closest('[data-monitor-action]');
    if (!active || !button || button.disabled) return;
    if (button.dataset.monitorAction === 'refresh') { stop(); load(); }
    else if (button.dataset.monitorAction === 'check') modify(button.dataset.id);
    else if (button.dataset.monitorAction === 'sign') await signTask(button.dataset.taskId);
    else if (button.dataset.monitorAction === 'notifications') {
      if (notifications) notifications = false;
      else {
        try { notifications = await Notification.requestPermission() === 'granted'; }
        catch { notifications = false; }
        if (!notifications) toast('通知未开启，可在浏览器设置中管理权限');
      }
      draw();
    }
  });
  return {
    render(target) {
      const entered = !active; main = target; active = true;
      if (entered) { streamConnected = false; streamMessage = '正在连接实时推送…'; }
      draw();
      if (entered) { load(); subscribe(); }
    },
    leave() {
      if (!active) return;
      active = false; scope++; stop(); unsubscribeMonitor?.(); unsubscribeMonitor = null;
      currentRequest = null; saving = ''; data = null; seen = null; error = ''; streamConnected = false;
    },
  };
}
