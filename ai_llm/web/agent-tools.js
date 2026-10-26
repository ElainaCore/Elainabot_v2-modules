import {$, api, esc, orderedModels, toast} from './core.js';

let editingId = '';
let providers = [];
const json = value => JSON.stringify(value, null, 2);
const lines = value => value.split(/[\n,，]+/).map(item => item.trim()).filter(Boolean);

function originLabel(item) {
  if (item.origin?.kind === 'ai') return item.origin.plugin ? 'AI 创建 · 插件：' + item.origin.plugin : 'AI 创建 · 面板';
  if (item.origin?.kind === 'download') return '下载工具';
  if (item.origin?.kind === 'import') return '导入工具';
  return '手动创建';
}

export async function loadAgentTools() {
  const [data, config] = await Promise.all([api('/agent-tools'), api('/config?section=providers')]);
  providers = config.providers || [];
  const selected = $('agent-author-provider').value;
  $('agent-author-provider').innerHTML = '<option value="">自动选择</option>' + providers.filter(item => item.enabled).map(item => '<option value="' + esc(item.id) + '">' + esc(item.name) + '</option>').join('');
  $('agent-author-provider').value = providers.some(item => item.id === selected) ? selected : '';
  renderAuthorModels();
  $('agent-tool-list').innerHTML = (data.tools || []).map(item =>
    '<article class="item"><div class="item-head"><strong>' + esc(item.name) + '</strong><div class="actions"><span class="tag">' + (item.enabled ? '已启用' : '未启用') + '</span><button class="btn" data-tool-edit="' + esc(item.id) + '">编辑与配置</button><button class="btn" data-tool-toggle="' + esc(item.id) + '">' + (item.enabled ? '停用' : '启用') + '</button><button class="btn danger" data-tool-delete="' + esc(item.id) + '">删除</button></div></div><div class="agent-tool-meta">' + esc(item.description) + '<br>' + esc(originLabel(item)) + '<br>调用名称：' + esc(item.function_name) + ' · ' + esc(item.timeout) + ' 秒' + (item.models?.length ? '<br>模型：' + esc(item.models.join('、')) : '') + '</div></article>'
  ).join('') || '<div class="empty">尚无公用工具。可以下载、导入或用 AI 创建。</div>';
  $('agent-tool-errors').textContent = (data.errors || []).map(item => item.file + '：' + item.error).join('\n');
  $('agent-download-catalog').innerHTML = (data.downloads || []).map(item => '<button class="btn" data-tool-download="' + esc(item.id) + '">下载 ' + esc(item.name) + '</button>').join('');
}

function renderAuthorModels() {
  const current = $('agent-author-model').value;
  const provider = providers.find(item => item.id === $('agent-author-provider').value);
  const models = provider ? orderedModels(provider).filter(model => !(provider.disabled_models || []).includes(model)) : [];
  $('agent-author-model').innerHTML = '<option value="">默认 / 自动</option>' + models.map(model => '<option value="' + esc(model) + '">' + esc(model) + '</option>').join('');
  $('agent-author-model').value = models.includes(current) ? current : '';
}

function edit(item, existing = true) {
  editingId = existing ? item.id : '';
  $('agent-id').value = item.id || '';
  $('agent-id').readOnly = !!editingId;
  $('agent-name').value = item.name || '';
  $('agent-description').value = item.description || '';
  $('agent-timeout').value = item.timeout || 30;
  $('agent-parameters').value = json(item.parameters || {type: 'object', properties: {}});
  $('agent-config').value = json(item.config || {});
  $('agent-code').value = item.code || 'def run(arguments, config):\n    return {"message": "hello"}\n';
  const scopeIds = item.provider_ids || [];
  const scopeProviders = [...providers, ...scopeIds.filter(id => !providers.some(provider => provider.id === id)).map(id => ({id, name: id + '（接口已移除）'}))];
  $('agent-providers').innerHTML = scopeProviders.map(provider => '<option value="' + esc(provider.id) + '"' + (scopeIds.includes(provider.id) ? ' selected' : '') + '>' + esc(provider.name) + '</option>').join('');
  $('agent-models').value = (item.models || []).join('\n');
  $('agent-enabled').checked = item.enabled === true;
  $('agent-editor-title').textContent = existing ? '编辑公用工具 · ' + item.name : '新建公用工具';
  $('agent-test-result').textContent = '尚未运行';
  $('agent-editor').hidden = false;
  $('agent-editor').scrollIntoView({behavior: 'smooth', block: 'start'});
}

function payload() {
  return {id: $('agent-id').value.trim(), name: $('agent-name').value.trim(), description: $('agent-description').value.trim(),
    parameters: JSON.parse($('agent-parameters').value), config: JSON.parse($('agent-config').value), code: $('agent-code').value,
    timeout: Number($('agent-timeout').value), enabled: $('agent-enabled').checked,
    provider_ids: [...$('agent-providers').selectedOptions].map(option => option.value), models: lines($('agent-models').value)};
}

async function save() {
  const item = await api('/agent-tools', {method: editingId ? 'PUT' : 'POST', body: json(payload())});
  editingId = item.id;
  $('agent-id').value = item.id;
  $('agent-id').readOnly = true;
  $('agent-editor-title').textContent = '编辑公用工具 · ' + item.name;
  await loadAgentTools();
  return item;
}

async function busy(button, action) {
  if (button.disabled) return;
  button.disabled = true;
  try { await action(); } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
}

export function bindAgentTools() {
  $('agent-new').onclick = () => edit({}, false);
  $('agent-author-provider').onchange = renderAuthorModels;
  $('agent-save').onclick = event => busy(event.currentTarget, async () => { await save(); toast('工具已保存'); });
  $('agent-create').onclick = event => busy(event.currentTarget, async () => {
    const requirement = $('agent-requirement').value.trim();
    if (!requirement) throw new Error('请描述工具需求');
    const item = await api('/agent-tools/create', {method: 'POST', body: json({requirement, provider_id: $('agent-author-provider').value, model: $('agent-author-model').value})});
    await loadAgentTools(); edit(item); toast('AI 工具已保存，请检查后启用');
  });
  $('agent-download').onclick = event => busy(event.currentTarget, async () => {
    const item = await api('/agent-tools/download', {method: 'POST', body: json({url: $('agent-download-url').value.trim()})});
    await loadAgentTools(); edit(item); toast('已下载并保存');
  });
  $('agent-import').onchange = async event => {
    try {
      const file = event.target.files[0]; if (!file) return;
      if (file.size > 512 * 1024) throw new Error('文件不能超过 512KB');
      const item = JSON.parse(await file.text()); item.enabled = false; item.config = {}; item.origin = {kind: 'import'};
      const saved = await api('/agent-tools', {method: 'POST', body: json(item)});
      await loadAgentTools(); edit(saved); toast('工具已导入');
    } catch (error) { toast(error.message, true); }
    finally { event.target.value = ''; }
  };
  $('agent-export').onclick = () => {
    try {
      const item = payload(); item.config = {}; item.enabled = false;
      const url = URL.createObjectURL(new Blob([json(item)], {type: 'application/json'}));
      const link = document.createElement('a'); link.href = url; link.download = (item.id || 'agent-tool') + '.json'; link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) { toast(error.message, true); }
  };
  $('agent-test').onclick = event => busy(event.currentTarget, async () => {
    const argumentsValue = JSON.parse($('agent-test-arguments').value);
    const item = await save(); $('agent-test-result').textContent = '运行中…';
    try {
      const result = await api('/agent-tools/test', {method: 'POST', body: json({id: item.id, arguments: argumentsValue})});
      $('agent-test-result').textContent = json(result);
    } catch (error) {
      $('agent-test-result').textContent = '运行失败：' + error.message;
      throw error;
    }
  });
  $('page-agent-tools').addEventListener('click', event => {
    const button = event.target.closest('button'); if (!button) return;
    if (button.dataset.toolEdit) busy(button, async () => edit(await api('/agent-tools?id=' + encodeURIComponent(button.dataset.toolEdit))));
    if (button.dataset.toolDownload) busy(button, async () => {
      const item = await api('/agent-tools/download', {method: 'POST', body: json({builtin: button.dataset.toolDownload})});
      await loadAgentTools(); edit(item); toast('已下载并保存');
    });
    if (button.dataset.toolToggle) busy(button, async () => {
      const item = await api('/agent-tools?id=' + encodeURIComponent(button.dataset.toolToggle)); item.enabled = !item.enabled;
      await api('/agent-tools', {method: 'PUT', body: json(item)}); await loadAgentTools();
      if (editingId === item.id) $('agent-enabled').checked = item.enabled;
    });
    if (button.dataset.toolDelete && confirm('删除这个公用工具？')) busy(button, async () => {
      await api('/agent-tools?id=' + encodeURIComponent(button.dataset.toolDelete), {method: 'DELETE'});
      if (editingId === button.dataset.toolDelete) { editingId = ''; $('agent-editor').hidden = true; }
      await loadAgentTools(); toast('工具已删除');
    });
  });
}
