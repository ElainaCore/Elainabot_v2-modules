export const BASE = '/api/ext/ai-service';
export const $ = id => document.getElementById(id);
export let state = {};
export let skills = [];
export let agents = [];
export let agentMarket = [];
const THEME_MAP = {'--bg':'--host-bg','--bg2':'--host-bg2','--bg3':'--host-bg3','--bg-float':'--host-float','--text':'--host-text','--text2':'--host-text2','--text3':'--host-text3','--border':'--host-border','--accent':'--host-accent','--accent-hover':'--host-accent-hover','--accent-light':'--host-accent-light','--accent-soft':'--host-accent-soft','--success':'--host-success','--danger':'--host-danger','--warning':'--host-warning','--info':'--host-info'};
export function syncHostTheme() {
  try {
    if (window.parent === window) return;
    const parentStyle = window.parent.getComputedStyle(window.parent.document.documentElement);
    const root = document.documentElement;
    Object.entries(THEME_MAP).forEach(([source, target]) => {
      const value = parentStyle.getPropertyValue(source).trim();
      if (value) root.style.setProperty(target, value);
    });
    root.style.colorScheme = parentStyle.colorScheme || 'normal';
  } catch (_) {}
}
syncHostTheme();
try {
  if (window.parent !== window) {
    new MutationObserver(syncHostTheme).observe(window.parent.document.documentElement, {
      attributes: true,
      attributeFilter: ['style', 'class'],
    });
  }
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
export function setState(value) { state = value || {}; return state; }
export function setSkills(value) { skills = Array.isArray(value) ? value : []; }
export function setAgents(value) { agents = Array.isArray(value) ? value : []; }
export function setAgentMarket(value) { agentMarket = Array.isArray(value) ? value : []; }
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
function agentCard(item) {
  return '<article class="agent-card"><div class="agent-card-main"><strong>' + esc(item.name || item.id) + '</strong><small>' + esc(item.description || '无描述') + '</small><div class="agent-meta"><code>' + esc(item.id) + '</code><span>' + esc(item.kind === 'folder' ? '文件夹' : '单文件') + '</span></div></div><button class="btn danger delete-agent" data-agent-id="' + esc(item.id) + '" type="button">删除</button></article>';
}
function marketAgentCard(item) {
  const installed = !!item.installed;
  return '<article class="agent-card"><div class="agent-card-main"><strong>' + esc(item.name || item.id) + '</strong><small>' + esc(item.description || '无描述') + '</small><div class="agent-meta"><code>' + esc(item.id) + '</code><span>' + esc(item.author || '未知作者') + '</span><span>v' + esc(item.version || '-') + '</span><span>' + esc(item.type === 'folder' ? '文件夹' : '单文件') + '</span></div></div><button class="btn '+ (installed ? '' : 'primary') + ' install-agent" data-agent-id="' + esc(item.id) + '" type="button" '+ (installed ? 'disabled' : '') + '>' + (installed ? '已安装' : '下载') + '</button></article>';
}
export function mcpTemplate(item) {
  const headers = JSON.stringify(item.headers || {}, null, 2);
  return '<article class="item" data-id="' + esc(item.id) + '"><div class="item-head"><strong>' + esc(item.name || 'MCP Server') + '</strong><button class="btn danger remove-item" type="button">删除</button></div><div class="item-body"><div class="grid"><div class="field"><label>ID</label>' + field('id', item.id) + '</div><div class="field"><label>名称</label>' + field('name', item.name) + '</div><div class="field"><label>Streamable HTTP 地址</label>' + field('endpoint', item.endpoint || '') + '</div><div class="field"><label>超时（秒）</label>' + field('timeout', item.timeout || 20, 'number') + '</div><div class="field"><label>请求头（JSON）</label><textarea data-key="headers" data-headers-set="' + (item.headers_set ? '1' : '0') + '">' + esc(headers) + '</textarea></div></div><label class="switch compact" style="margin-top:12px"><span><b>启用服务</b></span><input data-key="enabled" type="checkbox" ' + (item.enabled ? 'checked' : '') + '></label></div></article>';
}
export function capabilityTemplate(item) {
  const status = item.online ? '在线' : '离线';
  return '<article class="item capability-item collapsed" data-key="' + esc(item.key) + '"><button class="capability-row" type="button" aria-expanded="false"><span class="capability-main"><strong>' + esc(item.name || item.id) + '</strong><small>' + esc(item.description || '无描述') + '</small></span><span class="capability-tags"><i>' + esc(item.kind) + '</i><i>来源 ' + esc(item.source_plugin) + '</i><i class="' + (item.online ? 'online' : 'offline') + '">' + status + '</i><b>⌄</b></span></button><div class="item-body capability-body"><div class="switches two"><label class="switch compact"><span><b>启用能力</b><small>关闭后任何插件都不能调用</small></span><input data-key="enabled" type="checkbox" ' + (item.enabled ? 'checked' : '') + '></label><label class="switch compact"><span><b>允许其他插件使用</b><small>开启后其他插件可通过中央模块发现并调用</small></span><input data-key="shared" type="checkbox" ' + (item.shared ? 'checked' : '') + '></label></div><div class="field" style="margin-top:12px"><label>能力内容 / Prompt / Skill 指令</label><textarea data-key="content">' + esc(item.content || '') + '</textarea></div></div></article>';
}
export function readPluginCapabilities() {
  return [...document.querySelectorAll('.plugin-capability-list .capability-item')].map(card => {
    const value = {key: card.dataset.key};
    value.enabled = card.querySelector('[data-key="enabled"]').checked;
    value.shared = card.querySelector('[data-key="shared"]').checked;
    value.content = card.querySelector('[data-key="content"]').value;
    return value;
  });
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
  return {enabled: $('enabled').checked, agent_enabled: $('agent_enabled').checked, auto_switch: $('auto_switch').checked, auto_fetch_models: $('auto_fetch_models').checked, audit_include_content: $('audit_include_content').checked, temperature: Number($('temperature').value), max_tokens: Number($('max_tokens').value), max_tool_rounds: Number($('max_tool_rounds').value), request_timeout: Number($('request_timeout').value), runtime_prompt: $('runtime_prompt').value, context: {max_tokens: Number($('context_max_tokens').value), max_turns: Number($('context_max_turns').value), keep_recent_ratio: Number($('context_keep_ratio').value), compress_enabled: $('context_compress').checked}, skills: {enabled: $('skills_enabled').checked, enabled_ids: [...document.querySelectorAll('[data-skill-id]:checked')].map(node => node.dataset.skillId)}, mcp: {enabled: $('mcp_enabled').checked, servers: readItems('#mcp-list .item')}, cron_jobs: readItems('#cron-list .item')};
}
export function sectionPayload(section) {
  const common = collectCommon();
  if (section === 'overview') return {enabled: common.enabled, agent_enabled: common.agent_enabled, auto_switch: common.auto_switch, auto_fetch_models: common.auto_fetch_models, audit_include_content: common.audit_include_content, temperature: common.temperature, max_tokens: common.max_tokens, max_tool_rounds: common.max_tool_rounds, request_timeout: common.request_timeout};
  if (section === 'context') return {runtime_prompt: common.runtime_prompt, context: common.context};
  if (section === 'skills') return {skills: common.skills};
  if (section === 'mcp') return {mcp: common.mcp};
  if (section === 'cron') return {cron_jobs: common.cron_jobs};
  return {};
}
function renderCapabilities(kind, id) {
  const items = (state.plugin_capabilities || []).filter(item => item.kind === kind);
  $(id).innerHTML = items.map(capabilityTemplate).join('') || '<div class="empty">尚无插件注册此类能力</div>';
  $(id).querySelectorAll('.capability-row').forEach(button => button.onclick = () => {
    const card = button.closest('.capability-item');
    const expanded = !card.classList.toggle('collapsed');
    button.setAttribute('aria-expanded', expanded ? 'true' : 'false');
  });
}

export function renderCommon(page = 'overview') {
  if (page === 'overview') {
    ['enabled', 'agent_enabled', 'auto_switch', 'auto_fetch_models', 'audit_include_content'].forEach(key => $(key).checked = !!state[key]);
    ['temperature', 'max_tokens', 'max_tool_rounds', 'request_timeout'].forEach(key => $(key).value = state[key] ?? '');
  }
  const enabled = (state.providers || []).filter(item => item.enabled);
  if (page === 'overview') {
    $('m-providers').textContent = enabled.length;
    $('m-models').textContent = enabled.reduce((sum, item) => sum + (item.models || []).filter(model => !(item.disabled_models || []).includes(model)).length, 0);
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
    renderCapabilities('skill', 'plugin-skill-list');
  } else if (page === 'tools') {
    renderCapabilities('tool', 'plugin-tool-list');
  } else if (page === 'mcp') {
    $('mcp_enabled').checked = !!state.mcp?.enabled;
    $('mcp-list').innerHTML = (state.mcp?.servers || []).map(mcpTemplate).join('') || '<div class="empty">尚未配置 MCP Server</div>';
    renderCapabilities('mcp', 'plugin-mcp-list');
  } else if (page === 'cron') {
    $('cron-list').innerHTML = (state.cron_jobs || []).map(cronTemplate).join('') || '<div class="empty">尚未配置计划任务</div>';
  } else if (page === 'agents') {
    $('agent-list').innerHTML = agents.map(agentCard).join('') || '<div class="empty">尚未安装 Agent，可上传文件或从下方清单下载</div>';
    $('agent-market-list').innerHTML = agentMarket.map(marketAgentCard).join('') || '<div class="empty">下载清单为空或暂时无法获取</div>';
    renderCapabilities('agent', 'plugin-agent-list');
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
export async function loadConfig() {
  const result = await Promise.all([api('/config'), api('/skills').catch(() => []), api('/agents').catch(() => []), api('/agents/market').catch(() => [])]);
  setState(result[0]); setSkills(result[1]); setAgents(result[2]); setAgentMarket(result[3]); return state;
}
export async function saveSection(section) { setState(await api('/config', {method: 'PUT', body: JSON.stringify(sectionPayload(section))})); }
