import { $, api, esc, state, toast } from './core.js';

let records = [];
let selectedRun = '';
let currentPage = 1;
let totalPages = 1;
let totalRecords = 0;

const fmtTime = value => value ? new Date(value * 1000).toLocaleString() : '-';
const fmtMs = value => value == null ? '-' : (value >= 1000 ? `${(value / 1000).toFixed(2)}s` : `${value}ms`);
const fmtNumber = value => Number(value || 0).toLocaleString();
const fmtBytes = value => {
  const bytes = Number(value || 0);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
};
const json = value => JSON.stringify(value ?? null, null, 2);
const endpointPath = value => {
  if (!value) return '-';
  try {
    const url = new URL(value, location.origin);
    return url.pathname || '/';
  } catch (_) {
    return String(value).startsWith('/') ? String(value).split('?')[0] : '-';
  }
};
const badge = status => `<span class="log-status ${esc(status)}">${esc({ success: '成功', error: '失败', running: '运行中' }[status] || status || '未知')}</span>`;
const section = (title, value) => `<details class="log-section"><summary>${esc(title)}</summary><pre>${esc(json(value))}</pre></details>`;

function openDetail() {
  const modal = $('log-modal');
  modal.classList.add('open');
  modal.setAttribute('aria-hidden', 'false');
  document.body.classList.add('log-modal-open');
}

function closeDetail(clearSelection = true) {
  const modal = $('log-modal');
  modal.classList.remove('open');
  modal.setAttribute('aria-hidden', 'true');
  document.body.classList.remove('log-modal-open');
  if (clearSelection) {
    selectedRun = '';
    renderList();
  }
}

function syncProviders() {
  const node = $('log-filter-provider');
  const selected = node.value;
  node.innerHTML = '<option value="">全部接口</option>' + (state.providers || []).map(item => `<option value="${esc(item.id)}">${esc(item.name || item.id)}</option>`).join('');
  node.value = selected;
}

function renderStats(stats = {}) {
  $('log-total').textContent = stats.total ?? 0;
  $('log-success-rate').textContent = `${stats.success_rate ?? 0}%`;
  $('log-average').textContent = fmtMs(stats.average_duration_ms ?? 0);
}

function renderPagination() {
  $('log-page-info').textContent = '第 ' + currentPage + ' / ' + totalPages + ' 页，共 ' + totalRecords + ' 条';
  $('log-prev').disabled = currentPage <= 1;
  $('log-next').disabled = currentPage >= totalPages;
}

function renderList() {
  const body = $('log-list');
  if (!records.length) {
    body.innerHTML = '<tr><td class="empty" colspan="9">暂无调用日志</td></tr>';
    return;
  }
  body.innerHTML = records.map(item => {
    const endpoint = endpointPath(item.endpoint);
    const kind = item.stream || item.kind === 'stream' ? '流式' : '非流式';
    const latencyClass = item.status === 'error' ? ' error' : '';
    return `<tr class="log-row ${selectedRun === item.run_id ? 'active' : ''}" data-run-id="${esc(item.run_id)}">
      <td><div class="log-main"><b>${esc(item.provider_name || item.provider_id || '未选择')}</b><small>${esc(endpoint)}</small></div></td>
      <td><div class="log-main model"><b>${esc(item.model || '-')}</b><small>${esc(item.provider_id || '')}</small></div></td>
      <td><div class="log-main"><b>${esc(item.consumer_plugin || '系统')}</b><small>${esc(item.kind || 'complete')}</small></div></td>
      <td><span class="log-chip">${kind}</span></td>
      <td>${badge(item.status)}</td>
      <td><div class="log-traffic"><span>↑ ${fmtBytes(item.request_bytes)}</span><small>↓ ${fmtBytes(item.response_bytes)}</small></div></td>
      <td><div class="log-latency${latencyClass}"><span>首字　${esc(fmtMs(item.ttfb_ms))}</span><small>总耗时 ${esc(fmtMs(item.duration_ms))}</small>${item.tokens_per_second == null ? '' : `<small>${esc(item.tokens_per_second)} token/s</small>`}</div></td>
      <td><div class="log-tools"><b>${fmtNumber(item.tool_count || 0)}</b><small>次调用</small></div></td>
      <td class="log-time">${esc(fmtTime(item.started_at))}</td>
    </tr>`;
  }).join('');
  body.querySelectorAll('.log-row').forEach(row => {
    row.onclick = () => loadDetail(row.dataset.runId);
  });
}

function renderDetail(record) {
  const box = $('log-detail');
  if (!record) {
    box.innerHTML = '<div class="empty">选择一条调用记录查看已保存的脱敏数据</div>';
    return;
  }
  const attempts = (record.attempts || []).map((attempt, index) => `<details class="attempt"><summary class="attempt-head"><b>请求 ${index + 1} · ${esc(attempt.provider_name || attempt.provider_id)} / ${esc(attempt.model)}</b>${badge(attempt.status)}</summary><div class="attempt-body"><div class="log-facts"><span>HTTP ${esc(attempt.http_status ?? '-')}</span><span>首字 ${esc(fmtMs(attempt.ttfb_ms))}</span><span>耗时 ${esc(fmtMs(attempt.duration_ms))}</span><span>速度 ${attempt.tokens_per_second == null ? '-' : esc(`${attempt.tokens_per_second} token/s`)}</span><span>请求 ${esc(fmtBytes(attempt.request_bytes))}</span><span>响应 ${esc(fmtBytes(attempt.response_bytes))}</span></div>${section('请求地址与请求头', { endpoint: endpointPath(attempt.endpoint), headers: attempt.request_headers })}${section('请求正文', attempt.request)}${section('响应头', attempt.response_headers)}${section('响应正文', attempt.response)}${attempt.error ? `<pre class="log-error">${esc(attempt.error)}</pre>` : ''}</div></details>`).join('');
  const tools = (record.tools || []).map((tool, index) => `<details class="attempt tool-attempt"><summary class="attempt-head"><b>工具 ${index + 1} · ${esc(tool.name)}</b>${badge(tool.status)}</summary><div class="attempt-body"><div class="log-facts"><span>耗时 ${esc(fmtMs(tool.duration_ms))}</span></div>${section('调用参数', tool.arguments)}${section('执行结果', tool.result)}${tool.error ? `<pre class="log-error">${esc(tool.error)}</pre>` : ''}</div></details>`).join('');
  box.innerHTML = `<div class="log-detail-head"><div><h3>${esc(record.run_id)}</h3><p>${esc(fmtTime(record.started_at))} · ${esc(record.session_id || '无会话')} · ${esc(record.consumer_plugin || record.kind)}</p></div>${badge(record.status)}</div><div class="log-facts prominent"><span>总耗时 ${esc(fmtMs(record.duration_ms))}</span><span>首字 ${esc(fmtMs(record.ttfb_ms))}</span><span>速度 ${record.tokens_per_second == null ? '-' : esc(`${record.tokens_per_second} token/s`)}</span><span>请求 ${record.attempts?.length || 0} 次</span><span>工具 ${record.tools?.length || 0} 次</span></div>${record.error ? `<pre class="log-error">${esc(record.error)}</pre>` : ''}${section('调用入口参数', record.request)}${attempts || '<div class="empty">没有上游请求记录</div>'}${tools ? `<h3 class="log-subtitle">工具调用</h3>${tools}` : ''}${section('最终响应', record.response)}${section('运行事件', record.events || [])}`;
}

export async function loadDetail(id) {
  selectedRun = id;
  renderList();
  openDetail();
  $('log-detail').innerHTML = '<div class="empty">加载详情...</div>';
  try {
    const record = await api(`/logs?run_id=${encodeURIComponent(id)}`);
    if (selectedRun === id) renderDetail(record);
  } catch (error) {
    if (selectedRun === id) {
      closeDetail();
      toast(error.message, true);
    }
  }
}

export async function loadLogs(resetPage = false) {
  if (resetPage) currentPage = 1;
  syncProviders();
  const query = new URLSearchParams({
    page: String(currentPage),
    page_size: $('log-page-size').value || '20',
  });
  const status = $('log-filter-status').value;
  const provider = $('log-filter-provider').value;
  const search = $('log-search').value.trim();
  if (status) query.set('status', status);
  if (provider) query.set('provider', provider);
  if (search) query.set('search', search);
  const data = await api(`/logs?${query}`);
  records = data.items || [];
  currentPage = Number(data.page || 1);
  totalPages = Number(data.pages || 1);
  totalRecords = Number(data.total || 0);
  selectedRun = '';
  closeDetail(false);
  renderStats(data.stats || {});
  renderList();
  renderPagination();
}

export function bindLogs() {
  $('log-modal-close').onclick = () => closeDetail();
  $('log-modal-scrim').onclick = () => closeDetail();
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && $('log-modal').classList.contains('open')) closeDetail();
  });
  $('log-refresh').onclick = () => loadLogs().catch(error => toast(error.message, true));
  $('log-prev').onclick = () => {
    if (currentPage <= 1) return;
    currentPage -= 1;
    loadLogs().catch(error => toast(error.message, true));
  };
  $('log-next').onclick = () => {
    if (currentPage >= totalPages) return;
    currentPage += 1;
    loadLogs().catch(error => toast(error.message, true));
  };
  $('log-page-size').onchange = () => loadLogs(true).catch(error => toast(error.message, true));
  $('log-clear').onclick = async () => {
    if (!confirm('确定清空全部 AI LLM 调用日志？')) return;
    try {
      await api('/logs', { method: 'DELETE' });
      selectedRun = '';
      await loadLogs(true);
      toast('调用日志已清空');
    } catch (error) {
      toast(error.message, true);
    }
  };
  $('log-filter-status').onchange = $('log-filter-provider').onchange = () => loadLogs(true).catch(error => toast(error.message, true));
  let timer;
  $('log-search').oninput = () => {
    clearTimeout(timer);
    timer = setTimeout(() => loadLogs(true).catch(error => toast(error.message, true)), 300);
  };
}
