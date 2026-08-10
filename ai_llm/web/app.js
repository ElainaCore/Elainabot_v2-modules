import {
  $, api, renderCommon, renderTestModels, saveSection as saveConfigSection,
  readPluginCapabilities, setModelTools, setModelToolMarket, setSkills, setState, state, streamApi, toast,
} from './core.js';
import { bindProviders, renderProviders } from './providers.js';
import { bindLogs, loadLogs } from './logs.js';

const pages = new Set([...document.querySelectorAll('.nav button')].map(button => button.dataset.page));
let activePage = 'overview';
const loadedPages = new Set();
const pageRequests = new Map();
const pageEpoch = new Map();
const sidebarMedia = window.matchMedia('(max-width: 600px)');
const sidebarStorageKey = 'ai-sidebar-collapsed';

function setSidebarCollapsed(collapsed, persist = true) {
  $('app').classList.toggle('sidebar-collapsed', collapsed);
  document.body.classList.toggle('sidebar-open', sidebarMedia.matches && !collapsed);
  const toggle = $('sidebar-toggle');
  toggle.textContent = sidebarMedia.matches ? (collapsed ? '☰' : '×') : (collapsed ? '›' : '‹');
  toggle.title = collapsed ? '展开侧边栏' : '收起侧边栏';
  toggle.setAttribute('aria-label', toggle.title);
  toggle.setAttribute('aria-expanded', String(!collapsed));
  if (persist) localStorage.setItem(sidebarStorageKey, collapsed ? '1' : '0');
}

function renderPage(page = activePage) {
  if (page === 'providers') renderProviders(renderPage, () => loadPage('providers', true));
  else if (page !== 'logs') renderCommon(page);
  bindItems();
}
function commitPage(page, epoch, apply) {
  if (pageEpoch.get(page) !== epoch) return;
  apply();
  if (activePage === page) renderPage(page);
}
function mergeCapabilities(kind, items) {
  const others = (state.plugin_capabilities || []).filter(item => item.kind !== kind);
  setState({plugin_capabilities: [...others, ...(Array.isArray(items) ? items : [])]});
}
function pageTasks(page, epoch, refreshMarket) {
  const config = section => api('/config?section=' + encodeURIComponent(section)).then(data => commitPage(page, epoch, () => setState(data)));
  const capabilities = kind => api('/plugin-capabilities?kind=' + encodeURIComponent(kind)).then(data => commitPage(page, epoch, () => mergeCapabilities(kind, data)));
  if (['overview', 'providers', 'context', 'cron', 'test'].includes(page)) return [config(page)];
  if (page === 'skills') return [
    config('skills'),
    api('/skills').then(data => commitPage(page, epoch, () => setSkills(data))),
    capabilities('skill'),
  ];
  if (page === 'tools') return [capabilities('tool')];
  if (page === 'model-tools') return [
    config('model-tools'),
    api('/tools').then(data => commitPage(page, epoch, () => setModelTools(data))),
    api('/tools/market' + (refreshMarket ? '?refresh=1' : '')).then(data => commitPage(page, epoch, () => setModelToolMarket(data))),
  ];
  if (page === 'mcp') return [config('mcp'), capabilities('mcp')];
  if (page === 'agents') return [capabilities('agent')];
  if (page === 'logs') return [config('logs').then(() => {
    if (pageEpoch.get(page) === epoch) return loadLogs();
    return undefined;
  })];
  return [];
}
async function loadPage(page = activePage, force = false, refreshMarket = false) {
  if (!force && loadedPages.has(page)) return;
  if (!force && pageRequests.has(page)) return pageRequests.get(page);
  const epoch = (pageEpoch.get(page) || 0) + 1;
  pageEpoch.set(page, epoch);
  const request = Promise.allSettled(pageTasks(page, epoch, refreshMarket)).then(results => {
    if (pageEpoch.get(page) !== epoch) return;
    const failed = results.find(result => result.status === 'rejected');
    if (failed) throw failed.reason;
    loadedPages.add(page);
  }).finally(() => {
    if (pageRequests.get(page) === request) pageRequests.delete(page);
  });
  pageRequests.set(page, request);
  return request;
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
        await loadPage('skills', true);
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
        await loadPage('model-tools', true);
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
        await loadPage('model-tools', true);
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
function showPage(page) {
  activePage = pages.has(page) ? page : 'overview';
  document.querySelectorAll('.nav button').forEach(button => button.classList.toggle('active', button.dataset.page === activePage));
  document.querySelectorAll('.page').forEach(section => section.classList.toggle('active', section.id === 'page-' + activePage));
  const active = document.querySelector('.nav button.active');
  $('page-title').textContent = active ? active.textContent.trim() : activePage;
  renderPage(activePage);
  if (sidebarMedia.matches) setSidebarCollapsed(true, false);
  loadPage(activePage).catch(error => toast(error.message, true));
}

document.querySelectorAll('.nav button').forEach(button => button.onclick = () => {
  const target = button.dataset.page;
  if (target === activePage) showPage(target);
  else location.hash = target;
});
window.addEventListener('hashchange', () => showPage(pageFromLocation()));
$('sidebar-toggle').onclick = () => setSidebarCollapsed(!$('app').classList.contains('sidebar-collapsed'));
$('sidebar-scrim').onclick = () => setSidebarCollapsed(true, false);
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && sidebarMedia.matches && !$('app').classList.contains('sidebar-collapsed')) {
    setSidebarCollapsed(true, false);
    $('sidebar-toggle').focus();
  }
});
sidebarMedia.addEventListener('change', event => {
  if (event.matches) setSidebarCollapsed(true, false);
  else setSidebarCollapsed(localStorage.getItem(sidebarStorageKey) !== '0', false);
});
setSidebarCollapsed(sidebarMedia.matches || localStorage.getItem(sidebarStorageKey) !== '0', false);
$('reload').onclick = () => loadPage(activePage, true).then(() => {
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
    await loadPage('skills', true);
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
    await loadPage('model-tools', true);
    toast('模型工具 ' + item.id + ' 已上传');
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
};
$('refresh-model-tool-market').onclick = () => loadPage('model-tools', true, true).then(() => toast('模型工具市场已刷新')).catch(error => toast(error.message, true));
$('add-mcp').onclick = () => {
  setState({...state, mcp: {...state.mcp, servers: [...(state.mcp?.servers || []), {id: 'mcp-' + Date.now(), name: '新 MCP', endpoint: '', headers: {}, timeout: 20, enabled: true}]}});
  renderPage();
};
$('add-cron').onclick = () => addItem('cron_jobs', {id: 'cron-' + Date.now(), name: '新任务', cron: '', interval_seconds: 3600, prompt: '', provider_id: '', model: '', enabled: true});
$('refresh-mcp').onclick = async () => {
  try { await saveConfigSection('mcp'); const data = await api('/mcp/refresh', {method: 'POST'}); await loadPage('mcp', true); toast('已注册 ' + data.tools.length + ' 个 MCP 工具'); }
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
showPage(activePage);
