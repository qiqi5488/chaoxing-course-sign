const KINDS = { ordinary: '普通签到', ordinary_candidate: '普通签到', gesture: '手势签到',
  qr: '二维码签到', code: '签到码', position: '位置签到', located_code: '位置签到码', photo: '拍照签到', face: '人脸签到' };
const recordTime = value => value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString('zh-CN', {
  year: 'numeric', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false }) : '时间未记录';

export function createSignHistoryPage({ store, esc, icon, avatar }) {
  let main, active = false, currentId = '', generation = 0, data = null, loading = false, error = '', retryMore = false, source = 'chaoxing';
  const current = () => store.getState().accounts.find(account => account.id === currentId);

  function recordCard(record) {
    const source = record.activityScope === 'group' ? '群聊' : '课程';
    return `<article class="monitor-task"><div class="monitor-task-head"><h3>${esc(record.name || `${source}签到`)}</h3><span class="batch-status success">${esc(record.statusLabel || (record.status === 'already_signed' ? '已签到' : '上游回执成功'))}</span></div>
      <p class="monitor-task-course">${[source, record.courseName, KINDS[record.kind]].filter(Boolean).map(esc).join(' · ')}</p><p>${esc(record.message || '学习通已返回成功回执。')}</p>
      <div class="sign-record-meta"><span>活动 ${esc(record.activeId)}</span><time>${record.source === 'chaoxing' ? '签到时间' : '回执时间'} ${esc(recordTime(record.recordedAt))}</time></div></article>`;
  }

  function coverage() {
    if (!data || source !== 'chaoxing') return '';
    return ['course', 'group'].map(scope => {
      const sources = data.sources.filter(row => row.activityScope === scope);
      const complete = sources.length > 0 && sources.every(row => row.status === 'ok');
      const count = data.counts[scope];
      const label = scope === 'course' ? '课程' : '群聊';
      const failures = sources.filter(row => row.status !== 'ok');
      return `<div class="monitor-hint"><strong>${label}：${count || complete ? `已确认 ${count} 条` : '尚未查到可核实的记录'}${complete ? '' : ' · 查询尚不完整'}</strong>
        ${failures.map(row => `<p>${esc(row.name)}：${esc(row.message)}</p>`).join('')}</div>`;
    }).join('');
  }

  function draw() {
    if (!active || !main) return;
    const account = current();
    const back = `<a class="detail-back text-button" href="#/monitor">${icon('back', 15)}返回签到监控</a>`;
    if (!account) {
      main.innerHTML = `${back}<div class="empty"><h2>账号已移除或不可用</h2><p>请返回监控页选择账号。</p></div>`;
      return;
    }
    main.innerHTML = `${back}<div class="page-top"><div><p class="eyebrow">SIGN-IN RECORDS</p><h1>${source === 'chaoxing' ? '学习通签到记录' : '本系统签到回执'}</h1><p>${source === 'chaoxing' ? '查询学习通保存的以往已签记录，包含教师代签。' : '查看本系统执行签到后保存的成功回执。'}</p></div><span class="page-mark">${icon('check', 23)}</span></div>
      <section class="account-detail-hero"><div class="detail-summary">${avatar(account)}<div><h2>${esc(account.name)}</h2><p>${esc(account.maskedIdentifier)}</p></div></div><a class="text-button" href="#/accounts/${encodeURIComponent(currentId)}">查看账号资料${icon('chevron', 13)}</a></section>
      <section class="monitor-section" aria-labelledby="sign-history-title"><div class="section-heading"><h2 id="sign-history-title">成功记录${data ? `<small>${data.total} 条</small>` : ''}</h2><button class="text-button" data-history-action="refresh" ${loading ? 'disabled' : ''}>${icon('refresh', 14)}${loading ? '正在查询…' : '刷新记录'}</button></div>
      <p class="monitor-hint">${source === 'chaoxing' ? '数据来自学习通个人签到记录；迟到、请假、未签等状态不计入成功。' : '这里只显示本系统保存的回执，不代表学习通全部历史。'}
        <button class="text-button" data-history-action="${source === 'chaoxing' ? 'local' : 'chaoxing'}">${source === 'chaoxing' ? '查看本系统回执' : '返回学习通历史'}</button></p>
      ${coverage()}
      ${error ? `<div class="form-error" role="alert">${esc(error)}${data ? '<p>下方为上次查询结果。</p>' : ''}<button class="text-button" data-history-action="${retryMore ? 'more' : 'refresh'}" ${loading ? 'disabled' : ''}>重新查询</button></div>` : ''}
      ${loading && !data ? '<div class="detail-loading" role="status"><strong>正在查询成功记录…</strong></div>' : ''}
      <div class="monitor-tasks">${data?.records.length ? data.records.map(recordCard).join('') : data && !error ? `<div class="empty"><h3>暂无已确认的成功记录</h3><p>${source === 'chaoxing' ? '请结合上方查询范围查看，未完成的查询不代表没有记录。' : '本系统保存的成功回执会显示在这里。'}</p></div>` : ''}</div>
      ${data?.nextOffset != null ? `<button class="secondary-button" data-history-action="more" ${loading ? 'disabled' : ''}>${loading ? '正在加载…' : '加载更多记录'}</button>` : ''}
      ${data?.fetchedAt ? `<p class="detail-sync-time">查询时间 ${esc(recordTime(data.fetchedAt))}</p>` : ''}</section>`;
  }

  async function load(more = false, refresh = false) {
    if (!active || loading || !current()) return;
    const id = currentId, version = ++generation;
    const offset = more && data?.nextOffset != null ? data.nextOffset : 0;
    loading = true; error = ''; retryMore = more; draw();
    try {
      const result = await store.getSignRecords(id, { offset, source, refresh, snapshot: more ? data?.snapshot || '' : '' });
      if (!active || id !== currentId || version !== generation || !current()) return;
      data = offset ? { ...result, records: [...new Map([...data.records, ...result.records].map(
        row => [`${row.activityScope}:${row.activeId}`, row])).values()] } : result;
    } catch (failure) {
      if (active && id === currentId && version === generation) error = failure.message || '成功记录查询失败，请重试。';
    } finally {
      if (active && id === currentId && version === generation) { loading = false; draw(); }
    }
  }

  document.addEventListener('click', event => {
    const button = event.target.closest('[data-history-action]');
    if (!active || !button || button.disabled) return;
    if (button.dataset.historyAction === 'refresh') load(false, true);
    else if (button.dataset.historyAction === 'more') load(true);
    else if (['chaoxing', 'local'].includes(button.dataset.historyAction)) {
      source = button.dataset.historyAction; generation++; data = null; loading = false; error = ''; load();
    }
  });
  return {
    render(target, id) {
      const changed = !active || currentId !== id;
      main = target; active = true;
      if (changed) { currentId = id; generation++; data = null; loading = false; error = ''; source = 'chaoxing'; }
      draw();
      if (changed) load();
    },
    leave() { active = false; currentId = ''; generation++; data = null; loading = false; error = ''; },
  };
}
