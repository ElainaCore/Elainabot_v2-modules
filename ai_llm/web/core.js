export const BASE = '/api/ext/ai-service';
export const $ = id => document.getElementById(id);
export let state = {};
export let skills = [];
const token = new URLSearchParams(location.search).get('token') || '';
function requestUrl(path) {
  const value = new URL(path.startsWith('http') ? path : BASE + path, location.origin);
  if (token) value.searchParams.set('token', token);
  return value.toString();
}
export async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set('Content-Type', 'application/json');
  if (token) headers.set('Authorization', 'Bearer ' + token);
  const response = await fetch(requestUrl(path), {...options, headers});
  const raw = await response.text();
  let payload = {};
  try { payload = raw ? JSON.parse(raw) : {}; } catch (_) { payload = {error: raw}; }
  if (!response.ok || payload.success === false) throw new Error(payload.error || ('HTTP ' + response.status));
  return payload.data === undefined ? payload : payload.data;
}
export async function streamApi(path, body, onEvent) {
  const headers = {'Content-Type': 'application/json', Accept: 'text/event-stream'};
  if (token) headers.Authorization = 'Bearer ' + token;
  const response = await fetch(requestUrl(path), {method: 'POST', headers, body: JSON.stringify(body)});
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
export function setState(value) { state = value || {}; return state; }
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
export function agentTemplate(item) {
  return '<article class="item" data-id="' + esc(item.id) + '"><div class="item-head"><strong>' + esc(item.name || '新子代理') + '</strong><button class="btn danger remove-item" type="button">删除</button></div><div class="item-body"><div class="grid"><div class="field"><label>ID</label>' + field('id', item.id) + '</div><div class="field"><label>名称</label>' + field('name', item.name) + '</div><div class="field"><label>公开描述</label>' + field('description', item.description || '') + '</div><div class="field"><label>指定接口</label><select data-key="provider_id">' + providerOptions(item.provider_id, true) + '</select></div><div class="field"><label>指定模型</label>' + field('model', item.model || '') + '</div></div><div class="field" style="margin-top:12px"><label>系统 Prompt</label><textarea data-key="system_prompt">' + esc(item.system_prompt || '') + '</textarea></div><label class="switch compact" style="margin-top:12px"><span><b>启用子代理</b></span><input data-key="enabled" type="checkbox" ' + (item.enabled ? 'checked' : '') + '></label></div></article>';
}
export function mcpTemplate(item) {
  const headers = JSON.stringify(item.headers || {}, null, 2);
  return '<article class="item" data-id="' + esc(item.id) + '"><div class="item-head"><strong>' + esc(item.name || 'MCP Server') + '</strong><button class="btn danger remove-item" type="button">删除</button></div><div class="item-body"><div class="grid"><div class="field"><label>ID</label>' + field('id', item.id) + '</div><div class="field"><label>名称</label>' + field('name', item.name) + '</div><div class="field"><label>Streamable HTTP 地址</label>' + field('endpoint', item.endpoint || '') + '</div><div class="field"><label>超时（秒）</label>' + field('timeout', item.timeout || 20, 'number') + '</div><div class="field"><label>请求头（JSON）</label><textarea data-key="headers" data-headers-set="' + (item.headers_set ? '1' : '0') + '">' + esc(headers) + '</textarea></div></div><label class="switch compact" style="margin-top:12px"><span><b>启用服务</b></span><input data-key="enabled" type="checkbox" ' + (item.enabled ? 'checked' : '') + '></label></div></article>';
}
export function cronTemplate(item) {
  return '<article class="item" data-id="' + esc(item.id) + '"><div class="item-head"><strong>' + esc(item.name || '计划任务') + '</strong><button class="btn danger remove-item" type="button">删除</button></div><div class="item-body"><div class="grid"><div class="field"><label>ID</label>' + field('id', item.id) + '</div><div class="field"><label>名称</label>' + field('name', item.name) + '</div><div class="field"><label>Cron（五段）</label>' + field('cron', item.cron || '') + '</div><div class="field"><label>间隔秒数</label>' + field('interval_seconds', item.interval_seconds || 0, 'number') + '</div><div class="field"><label>接口</label><select data-key="provider_id">' + providerOptions(item.provider_id, true) + '</select></div><div class="field"><label>模型</label>' + field('model', item.model || '') + '</div></div><div class="field" style="margin-top:12px"><label>任务 Prompt</label><textarea data-key="prompt">' + esc(item.prompt || '') + '</textarea></div><label class="switch compact" style="margin-top:12px"><span><b>启用任务</b></span><input data-key="enabled" type="checkbox" ' + (item.enabled ? 'checked' : '') + '></label></div></article>';
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
      if (['priority', 'timeout', 'interval_seconds'].includes(input.dataset.key)) value = Number(value);
      item[input.dataset.key] = value;
    });
    return item;
  });
}
export function collectCommon() {
  return {enabled: $('enabled').checked, agent_enabled: $('agent_enabled').checked, auto_switch: $('auto_switch').checked, auto_fetch_models: $('auto_fetch_models').checked, temperature: Number($('temperature').value), max_tokens: Number($('max_tokens').value), max_tool_rounds: Number($('max_tool_rounds').value), request_timeout: Number($('request_timeout').value), runtime_prompt: $('runtime_prompt').value, context: {max_tokens: Number($('context_max_tokens').value), max_turns: Number($('context_max_turns').value), keep_recent_ratio: Number($('context_keep_ratio').value), compress_enabled: $('context_compress').checked}, skills: {enabled: $('skills_enabled').checked, enabled_ids: [...document.querySelectorAll('[data-skill-id]:checked')].map(node => node.dataset.skillId)}, mcp: {enabled: $('mcp_enabled').checked, servers: readItems('#mcp-list .item')}, sandbox: {...state.sandbox, enabled: $('sandbox_enabled').checked, endpoint: $('sandbox_endpoint').value.trim(), token: $('sandbox_token').value.trim(), execution_timeout: Number($('sandbox_exec_timeout').value)}, subagents: readItems('#agent-list .item'), cron_jobs: readItems('#cron-list .item')};
}
export function sectionPayload(section) {
  const common = collectCommon();
  if (section === 'overview') return {enabled: common.enabled, agent_enabled: common.agent_enabled, auto_switch: common.auto_switch, auto_fetch_models: common.auto_fetch_models, temperature: common.temperature, max_tokens: common.max_tokens, max_tool_rounds: common.max_tool_rounds, request_timeout: common.request_timeout};
  if (section === 'agents') return {subagents: common.subagents};
  if (section === 'cron') return {cron_jobs: common.cron_jobs};
  return {runtime_prompt: common.runtime_prompt, context: common.context, skills: common.skills, mcp: common.mcp, sandbox: common.sandbox};
}
export function renderCommon() {
  ['enabled', 'agent_enabled', 'auto_switch', 'auto_fetch_models'].forEach(key => $(key).checked = !!state[key]);
  ['temperature', 'max_tokens', 'max_tool_rounds', 'request_timeout'].forEach(key => $(key).value = state[key] ?? '');
  $('runtime_prompt').value = state.runtime_prompt || '';
  $('context_max_tokens').value = state.context?.max_tokens ?? 65536;
  $('context_max_turns').value = state.context?.max_turns ?? 30;
  $('context_keep_ratio').value = state.context?.keep_recent_ratio ?? .25;
  $('context_compress').checked = !!state.context?.compress_enabled;
  $('skills_enabled').checked = !!state.skills?.enabled;
  $('mcp_enabled').checked = !!state.mcp?.enabled;
  $('sandbox_enabled').checked = !!state.sandbox?.enabled;
  $('sandbox_endpoint').value = state.sandbox?.endpoint || '';
  $('sandbox_token').value = state.sandbox?.token || '';
  $('sandbox_exec_timeout').value = state.sandbox?.execution_timeout ?? 20;
  $('agent-list').innerHTML = (state.subagents || []).map(agentTemplate).join('') || '<div class="empty">尚未配置子代理</div>';
  $('mcp-list').innerHTML = (state.mcp?.servers || []).map(mcpTemplate).join('') || '<div class="empty">尚未配置 MCP Server</div>';
  $('cron-list').innerHTML = (state.cron_jobs || []).map(cronTemplate).join('') || '<div class="empty">尚未配置计划任务</div>';
  const enabled = (state.providers || []).filter(item => item.enabled);
  $('m-providers').textContent = enabled.length;
  $('m-models').textContent = enabled.reduce((sum, item) => sum + (item.models || []).filter(model => !(item.disabled_models || []).includes(model)).length, 0);
  $('m-runs').textContent = state.runtime_status?.running ?? 0;
  $('m-tools').textContent = state.runtime_status?.mcp_tools ?? 0;
  $('skill-list').innerHTML = skills.length ? skills.map(item => '<label class="skill"><input type="checkbox" data-skill-id="' + esc(item.id) + '" ' + ((state.skills?.enabled_ids || []).includes(item.id) ? 'checked' : '') + '><span><b>' + esc(item.name) + '</b><small>' + esc(item.description) + '</small></span></label>').join('') : '<div class="empty">请将 Skill 放入 data/skills/&lt;id&gt;/SKILL.md</div>';
  $('test-provider').innerHTML = providerOptions(state.active_provider);
  $('test-provider').value = state.active_provider || '';
  renderTestModels();
}
export function renderTestModels() {
  const provider = (state.providers || []).find(item => item.id === $('test-provider').value);
  const models = provider ? orderedModels(provider).filter(model => !(provider.disabled_models || []).includes(model)) : [];
  $('test-model').innerHTML = '<option value="">默认 / 自动</option>' + models.map(model => '<option value="' + esc(model) + '">' + esc(model) + '</option>').join('');
}
export async function loadConfig() {
  const result = await Promise.all([api('/config'), api('/skills').catch(() => [])]);
  setState(result[0]); setSkills(result[1]); return state;
}
export async function saveSection(section) { setState(await api('/config', {method: 'PUT', body: JSON.stringify(sectionPayload(section))})); }
