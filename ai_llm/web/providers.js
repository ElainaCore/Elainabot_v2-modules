import { $, api, esc, orderedModels, setState, state, toast } from './core.js';

const collapsed = new Set();

export function providerTemplate(provider) {
  const models = orderedModels(provider);
  const health = provider.health || {};
  const rows = models.length ? models.map((model, index) => {
    const result = health[model] || {};
    const dot = result.ok === true ? ' ok' : result.ok === false ? ' fail' : '';
    return '<div class="model-row" draggable="true" data-model="' + esc(model) + '"><span class="handle">::</span><input class="model-enabled" type="checkbox" ' + (!(provider.disabled_models || []).includes(model) ? 'checked' : '') + '><span class="model-name" title="' + esc(model) + '">' + esc(model) + '</span><span class="rank">P' + (index + 1) + '</span><span class="health-dot' + dot + '" title="' + esc(result.error || (result.ok ? '可用' : '尚未测活')) + '"></span></div>';
  }).join('') : '<div class="empty">点击“获取模型”同步列表</div>';
  const apiKey = provider.api_key_set ? '********' : (provider.api_key || '');
  return '<article class="provider' + (provider.id === state.active_provider ? ' active' : '') + (collapsed.has(provider.id) ? ' collapsed' : '') + '" data-provider-id="' + esc(provider.id) + '"><div class="provider-head"><div class="provider-title"><input type="radio" name="active-provider" ' + (provider.id === state.active_provider ? 'checked' : '') + '><strong>' + esc(provider.name || provider.id) + '</strong>' + (provider.builtin ? '<span class="tag">内置</span>' : '') + '</div><div class="actions"><button class="btn primary save-provider" type="button">保存接口</button><button class="btn fetch-models" type="button">获取模型</button><button class="btn probe-models" type="button">一键测活</button><button class="btn danger remove-provider" type="button">删除</button><button class="btn fold-provider" type="button" aria-label="展开或收起接口设置">' + (collapsed.has(provider.id) ? '+' : '-') + '</button></div></div><div class="provider-body"><div class="grid"><div class="field"><label>接口名称</label><input data-key="name" value="' + esc(provider.name) + '"></div><div class="field"><label>Base URL</label><input data-key="base_url" value="' + esc(provider.base_url) + '"></div><div class="field"><label>API Key</label><input data-key="api_key" data-key-set="' + (provider.api_key_set ? '1' : '0') + '" type="password" value="' + esc(apiKey) + '"></div><div class="field"><label>默认模型</label><input data-key="model" value="' + esc(provider.model) + '"></div><div class="field"><label>接口优先级</label><input data-key="priority" type="number" min="0" max="10000" value="' + (provider.priority ?? 100) + '"></div></div><div class="switches" style="margin-top:12px"><label class="switch compact"><span><b>启用接口</b></span><input data-key="enabled" type="checkbox" ' + (provider.enabled ? 'checked' : '') + '></label><label class="switch compact"><span><b>启用模型优先级</b></span><input data-key="model_priority_enabled" type="checkbox" ' + (provider.model_priority_enabled ? 'checked' : '') + '></label></div><div class="model-zone"><div class="model-toolbar"><span>模型优先级（拖动排序，关闭后自动跳过）</span><span class="health-summary"></span></div><div class="model-list">' + rows + '</div></div></div></article>';
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
  item.models = rows.map(row => row.dataset.model);
  item.model_priority = [...item.models];
  item.disabled_models = rows.filter(row => !row.querySelector('.model-enabled').checked).map(row => row.dataset.model);
  return item;
}

async function saveProvider(card, rerender, renderAll) {
  const oldId = card.dataset.providerId;
  const item = providerFromCard(card);
  const providers = state.providers.map(provider => provider.id === oldId ? item : provider);
  const active = card.querySelector('input[type=radio]').checked ? item.id : state.active_provider;
  setState(await api('/config', {method: 'PUT', body: JSON.stringify({providers, active_provider: active})}));
  collapsed.delete(oldId);
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
  } catch (error) { toast(error.message, true); } finally { button.disabled = false; }
}

async function probeModels(card, button, loadAll) {
  button.disabled = true;
  const dots = [...card.querySelectorAll('.health-dot')];
  const summary = card.querySelector('.health-summary');
  dots.forEach(dot => { dot.className = 'health-dot testing'; });
  summary.textContent = '正在用“你好”逐模型测活...';
  try {
    const item = await saveProvider(card, false, () => {});
    const data = await api('/health', {method: 'POST', body: JSON.stringify({provider_id: item.id, models: item.models})});
    const results = Object.fromEntries(data.results.map(result => [result.model, result]));
    card.querySelectorAll('.model-row').forEach(row => {
      const result = results[row.dataset.model], dot = row.querySelector('.health-dot');
      dot.className = 'health-dot ' + (result?.ok ? 'ok' : 'fail');
      dot.title = result?.ok ? ('可用 · ' + result.latency_ms + 'ms') : (result?.error || '不可用');
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
  setState(await api('/config', {method: 'PUT', body: JSON.stringify({providers: state.providers.filter(item => item.id !== card.dataset.providerId)})}));
  renderAll(); toast('接口已删除');
}

export function renderProviders(renderAll, loadAll) {
  $('provider-list').innerHTML = (state.providers || []).map(providerTemplate).join('');
  bindProviders(renderAll, loadAll);
}

export function bindProviders(renderAll, loadAll) {
  document.querySelectorAll('.provider input[type=radio]').forEach(input => input.onchange = () => {
    const card = input.closest('.provider');
    setState({...state, active_provider: card.dataset.providerId});
    document.querySelectorAll('.provider').forEach(item => item.classList.toggle('active', item === card));
  });
  document.querySelectorAll('.fold-provider').forEach(button => button.onclick = () => {
    const card = button.closest('.provider'), id = card.dataset.providerId;
    card.classList.toggle('collapsed');
    button.textContent = card.classList.contains('collapsed') ? '+' : '-';
    card.classList.contains('collapsed') ? collapsed.add(id) : collapsed.delete(id);
  });
  document.querySelectorAll('.save-provider').forEach(button => button.onclick = () => saveProvider(button.closest('.provider'), true, renderAll).catch(error => toast(error.message, true)));
  document.querySelectorAll('.fetch-models').forEach(button => button.onclick = () => fetchModels(button.closest('.provider'), button, loadAll));
  document.querySelectorAll('.probe-models').forEach(button => button.onclick = () => probeModels(button.closest('.provider'), button, loadAll));
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
