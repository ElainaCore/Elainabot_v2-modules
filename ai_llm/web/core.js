export const BASE = '/api/ext/ai-service';
export const $ = id => document.getElementById(id);
export let state = {};
export let skills = [];
try {
  const theme = window.parent !== window && window.parent.ElainaWebPanelTheme;
  if (theme) theme.bind(document.documentElement, {variables: [], schemeAttributes: ['data-theme'], map: {
    '--host-bg':'--bg','--host-bg2':'--bg2','--host-bg3':'--bg3','--host-float':'--bg-float',
    '--host-text':'--text','--host-text2':'--text2','--host-text3':'--text3','--host-border':'--border',
    '--host-accent':'--accent','--host-accent-hover':'--accent-hover','--host-accent-light':'--accent-light',
    '--host-accent-soft':'--accent-soft','--host-success':'--success','--host-danger':'--danger',
    '--host-warning':'--warning','--host-info':'--info'
  }});
} catch (_) {}
function requestUrl(path) { return new URL(path.startsWith('http') ? path : BASE + path, location.origin).toString(); }
export async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (!(options.body instanceof FormData)) headers.set('Content-Type', 'application/json');
  const response = await fetch(requestUrl(path), {...options, headers, credentials: 'same-origin'});
  const raw = await response.text();
  let payload = {};
  try { payload = raw ? JSON.parse(raw) : {}; } catch (_) { payload = {error: raw}; }
  if (!response.ok || payload.success === false) throw new Error(payload.error || ('HTTP ' + response.status));
  return payload.data === undefined ? payload : payload.data;
}
export async function streamApi(path, body, onEvent) {
  const headers = {'Content-Type': 'application/json', Accept: 'text/event-stream'};
  const response = await fetch(requestUrl(path), {method: 'POST', headers, credentials: 'same-origin', body: JSON.stringify(body)});
  if (!response.ok) throw new Error((await response.text()) || ('HTTP ' + response.status));
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  const consume = block => {
    const data = block.split(/\r?\n/).filter(line => line.startsWith('data:')).map(line => line.slice(5).trim()).join('\n');
    if (!data) return;
    try { onEvent(JSON.parse(data)); } catch (_) {}
  };
  while (true) {
    const result = await reader.read();
    if (result.done) break;
    buffer += decoder.decode(result.value, {stream: true});
    const blocks = buffer.split(/\r?\n\r?\n/);
    buffer = blocks.pop() || '';
    blocks.forEach(consume);
  }
  buffer += decoder.decode();
  if (buffer.trim()) consume(buffer);
}
export function setState(value) { state = {...state, ...(value || {})}; return state; }
export function setSkills(value) { skills = Array.isArray(value) ? value : []; }
export function esc(value) { return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
export function toast(message, error = false) {
  const node = $('toast'); node.textContent = message; node.className = 'toast show' + (error ? ' error' : '');
  clearTimeout(toast.timer); toast.timer = setTimeout(() => { node.className = 'toast'; }, 2600);
}
export function orderedModels(provider) {
  const models = provider?.models || [], order = provider?.model_priority || [];
  return [...new Set([...order, ...models])].filter(Boolean);
}
export function providerOptions(current = '', allowAuto = false) {
  let result = allowAuto ? '<option value="">自动选择</option>' : '';
  result += (state.providers || []).filter(item => item.enabled).map(item =>
    '<option value="' + esc(item.id) + '"' + (item.id === current ? ' selected' : '') + '>' + esc(item.name) + '</option>'
  ).join('');
  return result;
}
function field(key, value, type = 'text') {
  return '<input data-key="' + key + '" type="' + type + '" value="' + esc(value) + '">';
}
export function mcpTemplate(item) {
  const headers = JSON.stringify(item.headers || {}, null, 2);
  return '<article class="item" data-id="' + esc(item.id) + '"><div class="item-head"><strong>' + esc(item.name || 'MCP Server') + '</strong><button class="btn danger remove-item" type="button">删除</button></div><div class="item-body"><div class="grid"><div class="field"><label>ID</label>' + field('id', item.id) + '</div><div class="field"><label>名称</label>' + field('name', item.name) + '</div><div class="field"><label>Streamable HTTP 地址</label>' + field('endpoint', item.endpoint || '') + '</div><div class="field"><label>超时（秒）</label>' + field('timeout', item.timeout || 20, 'number') + '</div><div class="field"><label>请求头（JSON）</label><textarea data-key="headers" data-headers-set="' + (item.headers_set ? '1' : '0') + '">' + esc(headers) + '</textarea></div></div><label class="switch compact" style="margin-top:12px"><span><b>启用服务</b></span><input data-key="enabled" type="checkbox" ' + (item.enabled ? 'checked' : '') + '></label></div></article>';
}
export function readItems(selector) {
  return [...document.querySelectorAll(selector)].map(card => {
    const item = {};
    card.querySelectorAll('[data-key]').forEach(input => {
      let value = input.type === 'checkbox' ? input.checked : input.value.trim();
      if (input.dataset.key === 'headers') {
        try { value = JSON.parse(value || '{}'); } catch (_) { value = {}; }
        if (input.dataset.headersSet === '1') item.headers_set = true;
      }
      if (['priority', 'timeout'].includes(input.dataset.key)) value = Number(value);
      item[input.dataset.key] = value;
    });
    return item;
  });
}
export function sectionPayload(section) {
  if (section === 'overview') return {enabled: $('enabled').checked, auto_switch: $('auto_switch').checked, auto_fetch_models: $('auto_fetch_models').checked, audit_include_content: $('audit_include_content').checked, temperature: Number($('temperature').value), max_tokens: Number($('max_tokens').value), max_tool_rounds: Number($('max_tool_rounds').value), request_timeout: Number($('request_timeout').value)};
  if (section === 'context') return {runtime_prompt: $('runtime_prompt').value, context: {max_tokens: Number($('context_max_tokens').value), max_turns: Number($('context_max_turns').value), keep_recent_ratio: Number($('context_keep_ratio').value), compress_enabled: $('context_compress').checked}};
  if (section === 'skills') return {skills: {enabled: $('skills_enabled').checked, enabled_ids: [...document.querySelectorAll('[data-skill-id]:checked')].map(node => node.dataset.skillId)}};
  if (section === 'mcp') return {mcp: {enabled: $('mcp_enabled').checked, servers: readItems('#mcp-list .item')}};
  return {};
}

export function renderCommon(page = 'overview') {
  if (page === 'overview') {
    ['enabled', 'auto_switch', 'auto_fetch_models', 'audit_include_content'].forEach(key => $(key).checked = !!state[key]);
    ['temperature', 'max_tokens', 'max_tool_rounds', 'request_timeout'].forEach(key => $(key).value = state[key] ?? '');
  }
  if (page === 'overview') {
    $('m-providers').textContent = state.metrics?.providers ?? 0;
    $('m-models').textContent = state.metrics?.models ?? 0;
    $('m-runs').textContent = state.runtime_status?.running ?? 0;
    $('m-tools').textContent = state.runtime_status?.mcp_tools ?? 0;
  } else if (page === 'context') {
    $('runtime_prompt').value = state.runtime_prompt || '';
    $('context_max_tokens').value = state.context?.max_tokens ?? 65536;
    $('context_max_turns').value = state.context?.max_turns ?? 30;
    $('context_keep_ratio').value = state.context?.keep_recent_ratio ?? .25;
    $('context_compress').checked = !!state.context?.compress_enabled;
  } else if (page === 'skills') {
    $('skills_enabled').checked = !!state.skills?.enabled;
    $('skill-list').innerHTML = skills.length ? skills.map(item => '<article class="skill"><input type="checkbox" data-skill-id="' + esc(item.id) + '" ' + ((state.skills?.enabled_ids || []).includes(item.id) ? 'checked' : '') + '><span><b>' + esc(item.name) + '</b><small>' + esc(item.description) + '</small><code>' + esc(item.id) + '</code></span><button class="btn danger delete-skill" data-delete-skill="' + esc(item.id) + '" type="button">删除</button></article>').join('') : '<div class="empty">暂无 Skill，可上传 SKILL.md 或 Skill 压缩包</div>';
  } else if (page === 'mcp') {
    $('mcp_enabled').checked = !!state.mcp?.enabled;
    $('mcp-list').innerHTML = (state.mcp?.servers || []).map(mcpTemplate).join('') || '<div class="empty">尚未配置 MCP Server</div>';
  } else if (page === 'test') {
    $('test-provider').innerHTML = providerOptions(state.active_provider);
    $('test-provider').value = state.active_provider || '';
    renderTestModels();
  }
}
export function renderTestModels() {
  const provider = (state.providers || []).find(item => item.id === $('test-provider').value);
  const models = provider ? orderedModels(provider).filter(model => !(provider.disabled_models || []).includes(model)) : [];
  $('test-model').innerHTML = '<option value="">默认 / 自动</option>' + models.map(model => '<option value="' + esc(model) + '">' + esc(model) + '</option>').join('');
}
export async function loadConfig(section = 'overview') {
  return setState(await api('/config?section=' + encodeURIComponent(section)));
}
export async function saveSection(section) { setState(await api('/config?section=' + encodeURIComponent(section), {method: 'PUT', body: JSON.stringify(sectionPayload(section))})); }
