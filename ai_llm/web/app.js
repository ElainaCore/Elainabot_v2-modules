import {
  $, api, loadConfig, renderCommon, renderTestModels, saveSection as saveConfigSection,
  readPluginCapabilities, setState, state, streamApi, toast,
} from './core.js';
import { bindProviders, renderProviders } from './providers.js';

function renderAll() {
  renderCommon();
  renderProviders(renderAll, loadAll);
  bindItems();
}
async function loadAll() {
  await loadConfig();
  renderAll();
}
function bindItems() {
  document.querySelectorAll('.remove-item').forEach(button => {
    button.onclick = () => button.closest('.item').remove();
  });
}
function addItem(key, value) {
  setState({...state, [key]: [...(state[key] || []), value]});
  renderAll();
}
function showPage(page) {
  document.querySelectorAll('.nav button').forEach(button => button.classList.toggle('active', button.dataset.page === page));
  document.querySelectorAll('.page').forEach(section => section.classList.toggle('active', section.id === 'page-' + page));
  const active = document.querySelector('.nav button.active');
  $('page-title').textContent = active ? active.textContent.trim() : page;
  if (window.matchMedia('(max-width: 600px)').matches) $('app').classList.add('sidebar-collapsed');
}

document.querySelectorAll('.nav button').forEach(button => button.onclick = () => showPage(button.dataset.page));
$('sidebar-toggle').onclick = () => {
  const collapsed = $('app').classList.toggle('sidebar-collapsed');
  $('sidebar-toggle').textContent = collapsed ? '+' : '≡';
  $('sidebar-toggle').title = collapsed ? '展开侧边栏' : '收起侧边栏';
  $('sidebar-toggle').setAttribute('aria-label', $('sidebar-toggle').title);
  localStorage.setItem('ai-sidebar-collapsed', collapsed ? '1' : '0');
};
if (localStorage.getItem('ai-sidebar-collapsed') === '1') $('sidebar-toggle').click();
$('reload').onclick = () => loadAll().then(() => toast('已刷新')).catch(error => toast(error.message, true));
document.querySelectorAll('.save-section').forEach(button => button.onclick = () =>
  saveConfigSection(button.dataset.section).then(() => { renderAll(); toast('配置已保存'); }).catch(error => toast(error.message, true))
);
$('add-provider').onclick = () => addItem('providers', {id: 'provider-' + Date.now(), name: '新接口', base_url: 'https://api.openai.com/v1', api_key: '', model: 'gpt-4o-mini', models: ['gpt-4o-mini'], model_priority: ['gpt-4o-mini'], disabled_models: [], model_priority_enabled: true, priority: 50, enabled: true});
$('add-agent').onclick = () => addItem('subagents', {id: 'agent-' + Date.now(), name: '新子代理', description: '', system_prompt: '', provider_id: '', model: '', enabled: true});
$('add-mcp').onclick = () => {
  setState({...state, mcp: {...state.mcp, servers: [...(state.mcp?.servers || []), {id: 'mcp-' + Date.now(), name: '新 MCP', endpoint: '', headers: {}, timeout: 20, enabled: true}]}});
  renderAll();
};
$('add-cron').onclick = () => addItem('cron_jobs', {id: 'cron-' + Date.now(), name: '新任务', cron: '', interval_seconds: 3600, prompt: '', provider_id: '', model: '', enabled: true});
$('refresh-mcp').onclick = async () => {
  try { await saveConfigSection('capabilities'); const data = await api('/mcp/refresh', {method: 'POST'}); await loadAll(); toast('已注册 ' + data.tools.length + ' 个 MCP 工具'); }
  catch (error) { toast(error.message, true); }
};
$('save-plugin-capabilities').onclick = async () => {
  try {
    const data = await api('/plugin-capabilities', {method: 'PUT', body: JSON.stringify({items: readPluginCapabilities()})});
    setState({...state, plugin_capabilities: data.items || []});
    renderAll();
    toast('插件能力授权已保存');
  } catch (error) { toast(error.message, true); }
};
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
loadAll().catch(error => toast(error.message, true));
