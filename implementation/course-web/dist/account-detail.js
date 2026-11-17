// Account details stay in memory and are scoped to the currently viewed account.
export function createAccountDetail({ store, esc, icon, avatar, status, time, hasGrant }) {
  let main, currentId = '', active = false, generation = 0, accountSession = '';
  let details = null, loading = false, error = '', query = '';
  const current = () => store.getState().accounts.find(a => a.id === currentId);
  const text = value => typeof value === 'string' ? value.trim() : '';
  const safeCover = value => {
    try {
      const url = new URL(value);
      return url.protocol === 'https:' && !url.username && !url.password
        && ['chaoxing.com', 'cxwfw.com', 'xuexitong.com'].some(d => url.hostname === d || url.hostname.endsWith('.' + d)) ? url.href : '';
    } catch { return ''; }
  };
  const row = (label, value) => `<div class="detail-row"><span>${label}</span><span>${esc(value)}</span></div>`;

  function courseCard(course) {
    const cover = safeCover(course.coverUrl);
    const lines = [['教师', course.teacher], ['班级', course.className], ['学期', course.term]].filter(([, value]) => text(value));
    return `<article class="course-card"><div class="course-card-head"><span class="course-cover">${cover ? `<img src="${esc(cover)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : icon('book', 24)}</span><div><h3>${esc(course.name || '课程名称未提供')}</h3>${text(course.status) ? `<span class="course-state">${esc(course.status)}</span>` : ''}</div></div>
      ${lines.length ? `<dl class="course-meta">${lines.map(([label, value]) => `<div><dt>${label}</dt><dd>${esc(value)}</dd></div>`).join('')}</dl>` : '<p class="course-missing">学习通暂未提供教师、班级或学期信息。</p>'}
      ${course.sourceLabels.length ? `<div class="course-source-tags">${course.sourceLabels.map(label => `<span>${esc(label)}</span>`).join('')}</div>` : ''}</article>`;
  }

  function renderCourses() {
    const target = main?.querySelector('#detail-course-list');
    if (!target || !details) return;
    const searched = query.trim().toLowerCase();
    const rows = details.courses.filter(c => `${c.name} ${c.teacher} ${c.className} ${c.term}`.toLowerCase().includes(searched));
    const count = main.querySelector('#course-search-count');
    if (count) count.textContent = searched ? `找到 ${rows.length} / ${details.courses.length} 门` : '';
    const successful = details.sources.some(s => s.status === 'ok');
    target.innerHTML = rows.length ? rows.map(courseCard).join('') : `<div class="empty"><span>${icon('book', 25)}</span><h3>${searched ? '没有找到这门课程' : successful ? '当前没有返回课程' : '暂时无法确认课程列表'}</h3><p>${searched ? '试试课程名、教师或班级名称。' : successful ? '学习通本次查询未返回可展示的课程。' : '课程来源尚未成功返回，不能据此判断账号没有课程。'}</p>${searched ? '<button class="text-button" data-detail-action="clear-search">查看全部课程</button>' : '<button class="text-button" data-detail-action="refresh">重新读取课程</button>'}</div>`;
  }

  function draw() {
    if (!active || !main) return;
    const account = current();
    if (!account) {
      main.innerHTML = `<a class="detail-back text-button" href="#/accounts">${icon('back', 15)}返回账号列表</a><div class="empty"><h2>账号已移除或不可用</h2><p>请从当前账号列表中重新选择。</p></div>`;
      return;
    }
    const profile = details?.profile || {};
    const isDefault = account.id === store.getState().defaultAccountId;
    const available = details && details.sources.some(s => s.status === 'ok');
    const partial = details && details.sources.some(s => s.status !== 'ok');
    const profileRows = [['姓名', profile.name], ['学校', profile.school || account.school],
      ['学号 / 工号', profile.studentNumberMasked], ['院系', profile.department], ['专业', profile.major], ['年级', profile.grade]]
      .filter(([, value]) => text(value) && value !== '学校信息未提供');
    main.innerHTML = `<a class="detail-back text-button" href="#/accounts">${icon('back', 15)}返回账号列表</a>
      <div class="page-top account-detail-title"><div><p class="eyebrow">ACCOUNT DETAILS</p><h1>账号详情</h1><p>账号资料与课程，一处查看。</p></div><button class="icon-button" data-action="account-actions" data-id="${esc(currentId)}" aria-label="账号操作">${icon('more', 21)}</button></div>
      <section class="account-detail-hero"><div class="detail-summary">${avatar(account)}<div><h2>${esc(account.name)}</h2><p>${esc(account.maskedIdentifier)}${isDefault ? '<span class="default-badge">默认账号</span>' : ''}</p></div>${status(account)}</div><div class="detail-quick-actions"><button data-action="${account.status === 'active' ? 'grant' : 'relogin'}" data-id="${esc(currentId)}">${icon(account.status === 'active' ? 'shield' : 'refresh', 15)}${account.status === 'active' ? '会话授权' : '重新登录'}</button>${account.status === 'active' && hasGrant(account) ? `<button data-action="scan-account" data-id="${esc(currentId)}">${icon('qr', 15)}为此账号扫码</button>` : ''}</div></section>
      <a class="detail-monitor-link" href="#/monitor">${icon('eye', 18)}<span><strong>课程签到监控</strong><small>查看签到任务，管理监控与自动签到</small></span>${icon('chevron', 16)}</a>
      <section class="detail-section"><div class="section-heading"><h2>基本资料</h2>${loading ? '<span class="detail-updating">正在读取…</span>' : ''}</div><div class="detail-rows account-profile">${profileRows.length ? profileRows.map(([label, value]) => row(label, value)).join('') : '<p class="course-missing">尚未获取学习通基本资料。</p>'}${row('最近登录', time(account.lastLogin))}${row('会话保留', account.grant ? '持续保留，可随时撤销' : '尚未授权')}${account.sessionExpiresAt ? row('学习通会话有效至', time(account.sessionExpiresAt)) : row('登录会话', '由学习通维护，失效后需要重新登录')}</div></section>
      <section class="detail-section" aria-labelledby="detail-courses-title"><div class="section-heading"><h2 id="detail-courses-title">${partial ? '已获取课程' : '我的课程'}${details ? `<small>${details.courses.length} 门</small>` : ''}</h2><button class="text-button" data-detail-action="refresh" ${loading ? 'disabled' : ''}>${icon('refresh', 14)}${loading ? '正在刷新' : '刷新课程'}</button></div>
      ${error ? `<div class="form-error detail-load-error" role="alert">${esc(error)}${details ? '<p>下方仍为上次同步的数据，请以最近同步时间为准。</p>' : ''}<button class="text-button" data-detail-action="refresh">重试读取</button></div>` : ''}
      ${loading && !details ? `<div class="detail-loading" role="status">${icon('clock', 22)}<strong>正在读取该账号的课程</strong><p>正在通过服务端同步学习通资料。</p></div>` : ''}
      ${details ? `${partial ? `<div class="course-warning">${icon('info', 15)}<span>${available ? '部分课程来源暂不可用，下面显示已成功获取的课程。' : '课程来源尚未成功返回，请查看来源状态或稍后重试。'}</span></div>` : ''}<label class="search-box">${icon('search', 17)}<span class="visually-hidden">搜索课程</span><input id="detail-course-search" type="search" placeholder="搜索课程、教师或班级" value="${esc(query)}" autocomplete="off"></label><p class="course-search-count" id="course-search-count" aria-live="polite"></p><div id="detail-course-list" class="detail-course-list"></div>
      ${details.sources.length ? `<details class="course-sources"><summary>课程来源状态<span>${details.sources.filter(s => s.status === 'ok').length} / ${details.sources.length} 可用</span></summary>${details.sources.map(s => `<div class="course-source-row"><div><strong>${esc(s.label || '课程来源')}</strong><span class="${s.status === 'ok' ? 'source-ok' : 'source-warning'}">${s.status === 'ok' ? '已读取' : s.status === 'unsupported' ? '暂不支持' : '读取失败'}</span></div>${s.status === 'unsupported' ? '<p>该课程来源暂不可用，当前列表可能不完整。</p>' : s.message ? `<p>${esc(s.message)}</p>` : ''}</div>`).join('')}</details>` : ''}<p class="detail-sync-time">${details.fetchedAt ? `最近同步 ${esc(time(details.fetchedAt))}` : '学习通返回的资料可能不完整'}${loading ? ' · 正在重新读取' : ''}</p>` : ''}</section>`;
    renderCourses();
  }

  async function load(force = false) {
    if (!active || loading || !current()) return;
    const id = currentId, requestGeneration = ++generation;
    loading = true; error = ''; draw();
    try {
      const result = await store.getAccountDetails(id, { refresh: force });
      if (!active || currentId !== id || requestGeneration !== generation) return;
      details = result;
    } catch (failure) {
      if (!active || currentId !== id || requestGeneration !== generation) return;
      error = failure.message || '账号详情未能读取，请稍后重试。';
      if (['login_required', 'session_expired', 'workspace_changed', 'workspace_expired'].includes(failure.code)) store.refresh().catch(() => {});
    } finally {
      if (active && currentId === id && requestGeneration === generation) { loading = false; draw(); }
    }
  }

  document.addEventListener('click', event => {
    const button = event.target.closest('[data-detail-action]');
    if (!active || !button || button.disabled) return;
    if (button.dataset.detailAction === 'refresh') load(true);
    else if (button.dataset.detailAction === 'clear-search') { query = ''; draw(); }
  });
  document.addEventListener('input', event => {
    if (active && event.target.id === 'detail-course-search') { query = event.target.value; renderCourses(); }
  });
  return {
    render(target, id) {
      const changed = !active || currentId !== id;
      main = target; active = true;
      const account = store.getState().accounts.find(a => a.id === id);
      const session = account ? `${account.status}:${account.grant?.grantedAt || ''}` : 'missing';
      const sessionChanged = accountSession !== session;
      if (changed || sessionChanged) { currentId = id; details = null; loading = false; error = ''; query = ''; generation++; }
      accountSession = session;
      if (account && account.status !== 'active') error = '请先重新登录并授权此账号，再读取学习通课程。';
      draw();
      if ((changed || sessionChanged) && account?.status === 'active') load();
    },
    leave() { active = false; generation++; currentId = ''; accountSession = ''; details = null; loading = false; error = ''; query = ''; },
  };
}
