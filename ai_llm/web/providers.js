import { $, api, esc, orderedModels, setState, state, toast } from './core.js';

const expanded = new Set();
const modelsExpanded = new Set();
let balances = {};
let balanceRequest;
let balancesAt = 0;
let balanceEpoch = 0;
export function resetProviderExpansion() { expanded.clear(); modelsExpanded.clear(); }
function balanceText(id) {
  if ((state.providers || []).find(item => item.id === id)?.balance_enabled === false) return '';
  const value = balances[id];
  return value?.available ? '额度余额 $' + Number(value.remaining).toLocaleString(undefined, {maximumFractionDigits: 4}) : '';
}
function refreshBalances() {
  if (balanceRequest || Date.now() - balancesAt < 300000) return;
  const epoch = balanceEpoch;
  balanceRequest = api('/balances').then(data => {
    if (epoch !== balanceEpoch) return;
    balances = data || {}; balancesAt = Date.now();
    document.querySelectorAll('[data-provider-balance]').forEach(node => {
      node.textContent = balanceText(node.dataset.providerBalance);
      node.hidden = !node.textContent;
    });
  }).catch(() => {}).finally(() => { balanceRequest = null; if (epoch !== balanceEpoch) refreshBalances(); });
}
const PROVIDER_TYPES = [
  ['openai', 'OpenAI'], ['openai_compatible', 'OpenAI 兼容 / 中转'],
  ['openai_responses', 'OpenAI Responses'],
  ['gemini', 'Gemini 原生'], ['gemini_openai', 'Gemini OpenAI 兼容'],
  ['grok', 'Grok / xAI'], ['agnes', 'Agnes'], ['novelai', 'NovelAI'],
  ['jimeng2api', 'Jimeng2API'], ['z_image_gitee', 'Z-Image Gitee'],
];
const TYPE_DEFAULTS = {
  openai: {base_url: 'https://api.openai.com/v1'},
  openai_responses: {base_url: 'https://api.openai.com/v1', chat_path: '/responses'},
  openai_compatible: {base_url: ''},
  gemini: {base_url: 'https://generativelanguage.googleapis.com/v1beta'},
  gemini_openai: {base_url: 'https://generativelanguage.googleapis.com/v1beta/openai'},
  grok: {base_url: 'https://api.x.ai/v1'},
  agnes: {base_url: ''},
  novelai: {base_url: 'https://image.novelai.net', image_path: '/ai/generate-image'},
  jimeng2api: {base_url: ''},
  z_image_gitee: {base_url: ''},
};

function typeOptions(current) {
  return PROVIDER_TYPES.map(([value, label]) => '<option value="' + value + '" ' + (value === current ? 'selected' : '') + '>' + label + '</option>').join('');
}

function pathDefaults(type) {
  return {models_path: '/models', chat_path: type === 'gemini' ? '/models/{model}:generateContent' : type === 'openai_responses' ? '/responses' : '/chat/completions', image_path: type === 'novelai' ? '/ai/generate-image' : '/images/generations', image_edit_path: '/images/edits'};
}
function pathInput(provider, key, label) {
  return '<div class="field"><label>' + label + '</label><input data-key="' + key + '" value="' + esc(provider[key] || '') + '" placeholder="自动：' + esc(pathDefaults(provider.api_type)[key]) + '"></div>';
}
export function providerTemplate(provider) {
  const models = orderedModels(provider);
  const health = provider.health || {};
  const rows = models.length ? models.map((model, index) => {
    const result = health[model] || {};
    const dot = result.ok === true ? ' ok' : result.ok === false ? ' fail' : '';
    return '<div class="model-row" draggable="true" data-model="' + esc(model) + '"><span class="handle">::</span><input class="model-enabled" type="checkbox" ' + (!(provider.disabled_models || []).includes(model) ? 'checked' : '') + '><span class="model-name" title="' + esc(model) + '">' + esc(model) + '</span><span class="rank">P' + (index + 1) + '</span><span class="health-dot' + dot + '" title="' + esc(result.error || (result.ok ? '可用' : '尚未测活')) + '"></span></div>';
  }).join('') : '<div class="empty">点击“获取模型”同步列表；不支持时可在高级设置手动添加模型。</div>';
  const apiKey = provider.api_key_set ? '********' : (provider.api_key || '');
  const isExpanded = expanded.has(provider.id);
  return '<article class="provider' + (provider.id === state.active_provider ? ' active' : '') + (isExpanded ? '' : ' collapsed') + '" data-provider-id="' + esc(provider.id) + '">'
    + '<div class="provider-head"><div class="provider-title"><input type="radio" name="active-provider" ' + (provider.id === state.active_provider ? 'checked' : '') + '><strong>' + esc(provider.name || provider.id) + '</strong><span class="tag">' + esc((PROVIDER_TYPES.find(item => item[0] === provider.api_type) || ['', 'OpenAI 兼容'])[1]) + '</span><span class="tag">' + models.length + ' 个模型</span><span class="tag provider-balance" data-provider-balance="' + esc(provider.id) + '"' + (balanceText(provider.id) ? '' : ' hidden') + '>' + esc(balanceText(provider.id)) + '</span></div>'
    + '<div class="actions"><button class="btn primary save-provider" type="button">保存接口</button><button class="btn fetch-models" type="button">获取模型</button><button class="btn probe-models" type="button">一键测活</button><button class="btn danger remove-provider" type="button">删除</button><button class="btn fold-provider" type="button" aria-expanded="' + isExpanded + '" aria-label="展开或收起接口设置">' + (isExpanded ? '收起' : '展开') + '</button></div></div>'
    + '<div class="provider-body"><div class="grid provider-basic">'
    + '<div class="field"><label>接口名称</label><input data-key="name" value="' + esc(provider.name) + '"></div>'
    + '<div class="field"><label>接口类型</label><select data-key="api_type" class="provider-type">' + typeOptions(provider.api_type || 'openai_compatible') + '</select></div>'
    + '<div class="field"><label>URL</label><input data-key="base_url" placeholder="例如 https://api.example.com/v1" value="' + esc(provider.base_url) + '"></div>'
    + '<div class="field"><label>API Key</label><input data-key="api_key" data-key-set="' + (provider.api_key_set ? '1' : '0') + '" type="password" autocomplete="off" value="' + esc(apiKey) + '"></div>'
    + '<div class="field"><label>接口优先级</label><input data-key="priority" type="number" min="0" max="10000" value="' + (provider.priority ?? 100) + '"></div></div>'
    + '<details class="provider-advanced"><summary>高级设置 · 自定义路径与模型</summary><p class="provider-help">路径留空即按接口类型自动适配。仅在获取失败或服务商要求不同路径时填写。</p><div class="grid">'
    + pathInput(provider, 'models_path', '获取模型路径') + pathInput(provider, 'chat_path', '对话路径') + pathInput(provider, 'image_path', '生图路径') + pathInput(provider, 'image_edit_path', '图片编辑路径')
    + '<div class="field"><label>指定模型（可选）</label><input data-key="model" placeholder="留空使用模型优先级" value="' + esc(provider.model) + '"></div>'
    + '<div class="field"><label>手动添加模型（每行一个）</label><textarea class="manual-models" placeholder="获取不到模型时填写"></textarea></div></div>'
    + '<div class="switches"><label class="switch compact"><span><b>启用接口</b></span><input data-key="enabled" type="checkbox" ' + (provider.enabled ? 'checked' : '') + '></label><label class="switch compact"><span><b>启用模型优先级</b></span><input data-key="model_priority_enabled" type="checkbox" ' + (provider.model_priority_enabled ? 'checked' : '') + '></label><label class="switch compact"><span><b>自动查询余额</b><small>不支持时自动跳过，不影响调用</small></span><input data-key="balance_enabled" type="checkbox" ' + (provider.balance_enabled !== false ? 'checked' : '') + '></label></div></details>'
    + '<details class="model-zone"' + (modelsExpanded.has(provider.id) ? ' open' : '') + '><summary>模型列表与优先级 · ' + models.length + ' 个模型</summary><div class="model-toolbar"><span>拖动排序，关闭后自动跳过</span><div class="actions"><button class="btn select-all-models" type="button">全选</button><button class="btn select-none-models" type="button">全不选</button><span class="health-summary"></span></div></div><div class="model-list">' + rows + '</div></details></div></article>';
}

function providerFromCard(card) {
  const old = state.providers.find(item => item.id === card.dataset.providerId) || {};
  const item = {...old};
  card.querySelectorAll('[data-key]').forEach(input => {
    let value = input.type === 'checkbox' ? input.checked : input.value.trim();
    if (input.dataset.key === 'priority') value = Number(value);
    item[input.dataset.key] = value;
    if (input.dataset.key === 'api_key') item.api_key_set = input.dataset.keySet === '1';
  });
  const rows = [...card.querySelectorAll('.model-row')];
  const manual = card.querySelector('.manual-models').value.split(/[\n,，]+/).map(value => value.trim()).filter(Boolean);
  item.models = [...new Set([...rows.map(row => row.dataset.model), ...manual])];
  item.custom_paths = true;
  item.model_priority = [...item.models];
  item.disabled_models = rows.filter(row => !row.querySelector('.model-enabled').checked).map(row => row.dataset.model);
  return item;
}

async function saveProvider(card, rerender, renderAll) {
  const oldId = card.dataset.providerId;
  const item = providerFromCard(card);
  const providers = state.providers.map(provider => provider.id === oldId ? item : provider);
  const active = card.querySelector('input[type=radio]').checked ? item.id : state.active_provider;
  setState(await api('/config?section=providers', {method: 'PUT', body: JSON.stringify({providers, active_provider: active})}));
  delete balances[oldId]; balancesAt = 0; balanceEpoch++;
  if (rerender) renderAll();
  toast(item.name + ' 已保存');
  return item;
}

async function fetchModels(card, button, loadAll) {
  button.disabled = true;
  try {
    const item = await saveProvider(card, false, () => {});
    const data = await api('/models', {method: 'POST', body: JSON.stringify({provider_id: item.id})});
    await loadAll();
    toast('已获取 ' + data.models.length + ' 个模型');
  } catch (error) {
    expanded.add(card.dataset.providerId); card.classList.remove('collapsed');
    const fold = card.querySelector('.fold-provider'); fold.textContent = '收起'; fold.setAttribute('aria-expanded', 'true');
    card.querySelector('.provider-advanced').open = true;
    toast(error.message + '；可在高级设置修改获取模型路径或手动添加模型。', true);
  } finally { button.disabled = false; }
}

async function probeModels(card, button, loadAll) {
  button.disabled = true;
  const dots = [...card.querySelectorAll('.health-dot')];
  const summary = card.querySelector('.health-summary');
  dots.forEach(dot => { dot.className = 'health-dot testing'; });
  summary.textContent = '正在测活（会消耗少量 token，失败不自动停用）...';
  try {
    const item = await saveProvider(card, false, () => {});
    const data = await api('/health', {method: 'POST', body: JSON.stringify({provider_id: item.id, models: item.models, apply_results: true})});
    const results = Object.fromEntries(data.results.map(result => [result.model, result]));
    card.querySelectorAll('.model-row').forEach(row => {
      const result = results[row.dataset.model], dot = row.querySelector('.health-dot');
      if (result?.ok) row.querySelector('.model-enabled').checked = true;
      dot.className = 'health-dot ' + (result?.ok ? 'ok' : 'fail');
      dot.title = result?.ok ? ((result.warning || '可用') + ' · ' + result.latency_ms + 'ms') : (result?.error || '不可用');
    });
    const ok = data.results.filter(result => result.ok).length;
    summary.textContent = '测活完成：' + ok + '/' + data.results.length + ' 可用';
    toast(summary.textContent, ok === 0);
    if (loadAll) await loadAll();
  } catch (error) {
    dots.forEach(dot => { dot.className = 'health-dot fail'; });
    summary.textContent = error.message;
    toast(error.message, true);
  } finally { button.disabled = false; }
}

async function removeProvider(card, renderAll) {
  if (state.providers.length <= 1) throw new Error('至少保留一个接口');
  setState(await api('/config?section=providers', {method: 'PUT', body: JSON.stringify({providers: state.providers.filter(item => item.id !== card.dataset.providerId)})}));
  renderAll(); toast('接口已删除');
}

export function renderProviders(renderAll, loadAll) {
  $('provider-list').innerHTML = (state.providers || []).map(providerTemplate).join('');
  bindProviders(renderAll, loadAll);
  refreshBalances();
}

export function bindProviders(renderAll, loadAll) {
  document.querySelectorAll('.provider-type').forEach(select => select.onchange = () => {
    const defaults = TYPE_DEFAULTS[select.value] || {};
    const card = select.closest('.provider');
    const url = card.querySelector('[data-key="base_url"]');
    const knownUrls = Object.values(TYPE_DEFAULTS).map(item => item.base_url).filter(Boolean);
    if (!url.value || knownUrls.includes(url.value)) url.value = defaults.base_url || '';
    for (const [key, path] of Object.entries(pathDefaults(select.value))) {
      const input = card.querySelector('[data-key="' + key + '"]');
      input.value = ''; input.placeholder = '自动：' + path;
    }
  });
  document.querySelectorAll('.provider input[type=radio]').forEach(input => input.onchange = () => {
    const card = input.closest('.provider');
    setState({...state, active_provider: card.dataset.providerId});
    document.querySelectorAll('.provider').forEach(item => item.classList.toggle('active', item === card));
  });
  document.querySelectorAll('.fold-provider').forEach(button => button.onclick = () => {
    const card = button.closest('.provider'), id = card.dataset.providerId;
    card.classList.toggle('collapsed');
    const open = !card.classList.contains('collapsed');
    button.textContent = open ? '收起' : '展开';
    button.setAttribute('aria-expanded', String(open));
    open ? expanded.add(id) : expanded.delete(id);
  });
  document.querySelectorAll('.model-zone').forEach(details => details.ontoggle = () => {
    const id = details.closest('.provider').dataset.providerId;
    details.open ? modelsExpanded.add(id) : modelsExpanded.delete(id);
  });
  document.querySelectorAll('.save-provider').forEach(button => button.onclick = () => saveProvider(button.closest('.provider'), true, renderAll).catch(error => toast(error.message, true)));
  document.querySelectorAll('.fetch-models').forEach(button => button.onclick = () => fetchModels(button.closest('.provider'), button, loadAll));
  document.querySelectorAll('.probe-models').forEach(button => button.onclick = () => probeModels(button.closest('.provider'), button, loadAll));
  document.querySelectorAll('.select-all-models').forEach(button => button.onclick = () => {
    button.closest('.provider').querySelectorAll('.model-enabled').forEach(input => { input.checked = true; });
  });
  document.querySelectorAll('.select-none-models').forEach(button => button.onclick = () => {
    button.closest('.provider').querySelectorAll('.model-enabled').forEach(input => { input.checked = false; });
  });
  document.querySelectorAll('.remove-provider').forEach(button => button.onclick = () => removeProvider(button.closest('.provider'), renderAll).catch(error => toast(error.message, true)));
  document.querySelectorAll('.model-row').forEach(row => {
    row.ondragstart = () => row.classList.add('dragging');
    row.ondragend = () => { row.classList.remove('dragging'); rerank(row.closest('.model-list')); };
    row.ondragover = event => {
      event.preventDefault();
      const list = row.parentElement, drag = list.querySelector('.dragging');
      if (drag && drag !== row) {
        const box = row.getBoundingClientRect();
        list.insertBefore(drag, event.clientY < box.top + box.height / 2 ? row : row.nextSibling);
      }
    };
  });
}
function rerank(list) { [...list.querySelectorAll('.model-row')].forEach((row, index) => { row.querySelector('.rank').textContent = 'P' + (index + 1); }); }
