import {
  $, api, loadConfig, renderCommon, renderTestModels, saveSection as saveConfigSection,
  readPluginCapabilities, setState, state, streamApi, toast,
} from './core.js';
import { bindProviders, renderProviders } from './providers.js';
import { bindLogs, loadLogs } from './logs.js';

const pages = new Set([...document.querySelectorAll('.nav button')].map(button => button.dataset.page));
let activePage = 'overview';

function renderPage(page = activePage) {
  if (page === 'providers') renderProviders(renderPage, loadAll);
  else if (page !== 'logs') renderCommon(page);
  bindItems();
}
async function loadAll() {
  await loadConfig();
  renderPage();
}
function bindItems() {
  document.querySelectorAll('.remove-item').forEach(button => {
    button.onclick = () => button.closest('.item').remove();
  });
  document.querySelectorAll('.delete-skill').forEach(button => {
    button.onclick = async () => {
      const skillId = button.dataset.deleteSkill;
      if (!confirm('确定删除 Skill “' + skillId + '”？此操作会删除其全部文件。')) return;
      button.disabled = true;
      try {
        await api('/skills?skill_id=' + encodeURIComponent(skillId), {method: 'DELETE'});
        await loadAll();
        toast('Skill 已删除');
      } catch (error) { toast(error.message, true); }
      finally { button.disabled = false; }
    };
  });
  document.querySelectorAll('.delete-model-tool').forEach(button => {
    button.onclick = async () => {
      const toolId = button.dataset.toolId;
      if (!confirm('确定删除模型工具“' + toolId + '”？此操作会删除其全部文件。')) return;
      button.disabled = true;
      try {
        await api('/tools?tool_id=' + encodeURIComponent(toolId), {method: 'DELETE'});
        await loadAll();
        toast('模型工具已删除');
      } catch (error) { toast(error.message, true); }
      finally { button.disabled = false; }
    };
  });
  document.querySelectorAll('.install-model-tool:not(:disabled)').forEach(button => {
    button.onclick = async () => {
      button.disabled = true;
      try {
        const item = await api('/tools/install', {method: 'POST', body: JSON.stringify({tool_id: button.dataset.toolId})});
        await loadAll();
        toast('模型工具 ' + item.id + ' 已安装');
      } catch (error) { toast(error.message, true); button.disabled = false; }
    };
  });
}
function addItem(key, value) {
  setState({...state, [key]: [...(state[key] || []), value]});
  renderPage();
}
function pageFromLocation() {
  const value = location.hash.startsWith('#') ? location.hash.slice(1) : location.hash;
  return pages.has(value) ? value : 'overview';
}
function showPage(page, loadPageData = true) {
  activePage = pages.has(page) ? page : 'overview';
  document.querySelectorAll('.nav button').forEach(button => button.classList.toggle('active', button.dataset.page === activePage));
  document.querySelectorAll('.page').forEach(section => section.classList.toggle('active', section.id === 'page-' + activePage));
  const active = document.querySelector('.nav button.active');
  $('page-title').textContent = active ? active.textContent.trim() : activePage;
  renderPage(activePage);
  if (window.matchMedia('(max-width: 600px)').matches) $('app').classList.add('sidebar-collapsed');
  if (activePage === 'logs' && loadPageData) loadLogs().catch(error => toast(error.message, true));
}

document.querySelectorAll('.nav button').forEach(button => button.onclick = () => {
  const target = button.dataset.page;
  if (target === activePage) showPage(target);
  else location.hash = target;
});
window.addEventListener('hashchange', () => showPage(pageFromLocation()));
$('sidebar-toggle').onclick = () => {
  const collapsed = $('app').classList.toggle('sidebar-collapsed');
  $('sidebar-toggle').textContent = collapsed ? '+' : '≡';
  $('sidebar-toggle').title = collapsed ? '展开侧边栏' : '收起侧边栏';
  $('sidebar-toggle').setAttribute('aria-label', $('sidebar-toggle').title);
  localStorage.setItem('ai-sidebar-collapsed', collapsed ? '1' : '0');
};
if (localStorage.getItem('ai-sidebar-collapsed') === '1') $('sidebar-toggle').click();
$('reload').onclick = () => loadAll().then(async () => {
  if (activePage === 'logs') await loadLogs();
  toast('已刷新');
}).catch(error => toast(error.message, true));
document.querySelectorAll('.save-section').forEach(button => button.onclick = () =>
  saveConfigSection(button.dataset.section).then(() => { renderPage(); toast('配置已保存'); }).catch(error => toast(error.message, true))
);
$('add-provider').onclick = () => addItem('providers', {id: 'provider-' + Date.now(), name: '新接口', api_type: 'openai_compatible', base_url: '', api_key: '', model: '', models: [], model_priority: [], disabled_models: [], chat_path: '/chat/completions', models_path: '/models', image_path: '/images/generations', image_edit_path: '/images/edits', model_priority_enabled: true, priority: 50, enabled: true});
$('skill-upload-form').onsubmit = async event => {
  event.preventDefault();
  const file = $('skill-file').files[0];
  if (!file) return toast('请选择 Skill 文件', true);
  const button = $('upload-skill');
  const form = new FormData();
  form.append('file', file, file.name);
  form.append('skill_id', $('skill-upload-id').value.trim());
  button.disabled = true;
  try {
    const item = await api('/skills', {method: 'POST', body: form});
    $('skill-upload-form').reset();
    await loadAll();
    toast('Skill ' + item.id + ' 已上传');
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
};
$('model-tool-upload-form').onsubmit = async event => {
  event.preventDefault();
  const file = $('model-tool-file').files[0];
  if (!file) return toast('请选择模型工具文件', true);
  const button = $('upload-model-tool');
  const form = new FormData();
  form.append('file', file, file.name);
  button.disabled = true;
  try {
    const item = await api('/tools', {method: 'POST', body: form});
    $('model-tool-upload-form').reset();
    await loadAll();
    toast('模型工具 ' + item.id + ' 已上传');
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
};
$('refresh-model-tool-market').onclick = () => loadAll().then(() => toast('模型工具市场已刷新')).catch(error => toast(error.message, true));
$('add-mcp').onclick = () => {
  setState({...state, mcp: {...state.mcp, servers: [...(state.mcp?.servers || []), {id: 'mcp-' + Date.now(), name: '新 MCP', endpoint: '', headers: {}, timeout: 20, enabled: true}]}});
  renderPage();
};
$('add-cron').onclick = () => addItem('cron_jobs', {id: 'cron-' + Date.now(), name: '新任务', cron: '', interval_seconds: 3600, prompt: '', provider_id: '', model: '', enabled: true});
$('refresh-mcp').onclick = async () => {
  try { await saveConfigSection('mcp'); const data = await api('/mcp/refresh', {method: 'POST'}); await loadAll(); toast('已注册 ' + data.tools.length + ' 个 MCP 工具'); }
  catch (error) { toast(error.message, true); }
};
document.querySelectorAll('.save-plugin-capabilities').forEach(button => button.onclick = async () => {
  button.disabled = true;
  try {
    const data = await api('/plugin-capabilities', {method: 'PUT', body: JSON.stringify({items: readPluginCapabilities()})});
    setState({...state, plugin_capabilities: data.items || []});
    renderPage();
    toast('插件能力设置已保存');
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
});
$('test-provider').onchange = renderTestModels;
$('run-test').onclick = async () => {
  const button = $('run-test');
  button.disabled = true;
  const result = $('test-result');
  result.textContent = '';
  let meta = '';
  try {
    await streamApi('/test', {provider_id: $('test-provider').value, model: $('test-model').value, runtime_prompt: $('test-runtime-prompt').value, message: $('test-message').value}, payload => {
      const event = payload.data || payload;
      if (event.type === 'meta') {
        meta = 'Run ' + event.run_id + '\n' + event.provider_name + ' / ' + event.model + '\n\n';
        result.textContent = meta;
      } else if (event.type === 'delta') {
        result.textContent += event.text || '';
      } else if (event.type === 'done') {
        result.textContent += '\n\n[流式完成]';
      } else if (event.type === 'error') {
        result.textContent += '\n\n[错误] ' + (event.error || '请求失败');
      }
    });
  } catch (error) { result.textContent += '\n\n[错误] ' + error.message; }
  finally { button.disabled = false; }
};
$('interrupt-test').onclick = async () => {
  try { const data = await api('/interrupt', {method: 'POST', body: JSON.stringify({session_id: 'web:ai-service-test'})}); toast(data.interrupted ? '已发送中断' : '当前没有运行中的任务'); }
  catch (error) { toast(error.message, true); }
};
bindLogs();
activePage = pageFromLocation();
if (!location.hash) history.replaceState(null, '', '#overview');
loadAll().then(() => showPage(activePage, true)).catch(error => toast(error.message, true));
