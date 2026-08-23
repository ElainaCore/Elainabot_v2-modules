'use strict';

const readline = require('node:readline');

function write(message) {
  process.stdout.write(JSON.stringify(message) + '\n');
}

function encode(value) {
  if (Buffer.isBuffer(value)) return { __base64__: value.toString('base64') };
  if (value instanceof Uint8Array) return { __base64__: Buffer.from(value).toString('base64') };
  if (typeof value === 'bigint') return value.toString();
  if (Array.isArray(value)) return value.map(encode);
  if (value && typeof value === 'object') {
    const out = {};
    for (const [key, item] of Object.entries(value)) out[key] = encode(item);
    return out;
  }
  return value;
}

async function collectChunks(chunks) {
  if (chunks && typeof chunks[Symbol.asyncIterator] === 'function') {
    const result = [];
    for await (const chunk of chunks) result.push(chunk);
    return result;
  }
  if (chunks && typeof chunks[Symbol.iterator] === 'function' && typeof chunks !== 'string') return Array.from(chunks);
  return chunks;
}

class RpcError extends Error {
  constructor(message) { super(message); this.name = 'RpcError'; }
}

class Match {
  constructor(payload = {}) {
    Object.assign(this, payload);
    this.captures = Array.isArray(payload.captures) ? payload.captures : [];
    this.named = payload.named && typeof payload.named === 'object' ? payload.named : {};
  }
  group(key = 0) {
    if (key === 0 || key === '0') return this['0'];
    if (typeof key === 'string') return this.named[key];
    return this.captures[Number(key) - 1];
  }
  groupdict() { return { ...this.named }; }
}

class Sender {
  constructor(plugin, event = null, appid = '') { this._plugin = plugin; this._event = event; this._appid = appid; }
  _call(name, args = [], options = {}) {
    const prefix = this._event ? 'event.' : (this._appid ? 'bot.' : 'sender.');
    const params = { args: encode(args), options: encode(options) };
    if (this._event) params.event_id = this._event._invokeId;
    if (this._appid) params.appid = this._appid;
    return this._plugin._request(prefix + name, params);
  }
  reply(content, options = {}) { if (!this._event) throw new Error('reply requires an event'); return this._event.reply(content, options); }
  async replyStream(chunks, options = {}) { return this._call('reply_stream', [await collectChunks(chunks)], options); }
  replyImage(data, content = '', options = {}) { return this._call('reply_image', [data, content], options); }
  replyVoice(data, content = '', options = {}) { return this._call('reply_voice', [data, content], options); }
  replyVideo(data, content = '', options = {}) { return this._call('reply_video', [data, content], options); }
  replyFile(data, content = '', options = {}) { return this._call('reply_file', [data, content], options); }
  replyArk(templateId, kvData, content = '', options = {}) { return this._call('reply_ark', [templateId, kvData, content], options); }
  replyCard(cardType = 'tuwen', data = null, content = '', options = {}) { return this._call('reply_card', [cardType, data, content], options); }
  sendStreamToUser(userId, chunks, options = {}) { return collectChunks(chunks).then((items) => this._call('send_stream_to_user', [userId, items], options)); }
  sendToGroup(groupId, content = null, options = {}) { return this._call('send_to_group', [groupId, content], options); }
  sendToUser(userId, content = null, options = {}) { return this._call('send_to_user', [userId, content], options); }
  sendToChannel(channelId, content = null, options = {}) { return this._call('send_to_channel', [channelId, content], options); }
  sendImage(targetType, targetId, data, content = '', options = {}) { return this._call('send_image', [targetType, targetId, data, content], options); }
  sendWakeup(userId, content = '', buttons = null) { return this._call('send_wakeup', [userId, content, buttons]); }
  forceWakeup(userId, content = '', buttons = null) { return this._call('force_wakeup', [userId, content, buttons]); }
  ackInteraction(code = 0, options = {}) { return this._call('ack_interaction', [code], options); }
  recall(messageId = null) { return this._call('recall', [messageId]); }
  getShareLink(data = null) { return this._call('get_share_link', [data]); }
  getGlobalMenu(options = {}) { return this._call('get_global_menu', [], options); }
  updateGlobalMenu(menu = null) { return this._call('update_global_menu', [menu]); }
  getPanels(scope, options = {}) { return this._call('get_panels', [scope], options); }
  createPanel(scope, panel, options = {}) { return this._call('create_panel', [scope, panel], options); }
  getPanel(panelId, options = {}) { return this._call('get_panel', [panelId], options); }
  updatePanel(panelId, panel) { return this._call('update_panel', [panelId, panel]); }
  deletePanel(panelId) { return this._call('delete_panel', [panelId]); }
  updatePanelTargets(panelId, op, options = {}) { return this._call('update_panel_targets', [panelId, op], options); }
  getGroupMember(groupId, memberId) { return this._call('get_group_member', [groupId, memberId]); }
  getGroupRecord(groupId) { return this._call('get_group_record', [groupId]); }
  getGroupInfo(groupId, options = {}) { return this._call('get_group_info', [groupId], options); }
  getGroupBotState(groupId, options = {}) { return this._call('get_group_bot_state', [groupId], options); }
  refreshGroupInfo(groupId) { return this._call('refresh_group_info', [groupId]); }
  getGroupJoinRequests(groupId, options = {}) { return this._call('get_group_join_requests', [groupId], options); }
  reviewGroupJoinRequest(groupId, memberOpenid, op, options = {}) { return this._call('review_group_join_request', [groupId, memberOpenid, op], options); }
  getGroupRestrictChatSetting(groupId, options = {}) { return this._call('get_group_restrict_chat_setting', [groupId], options); }
  setGroupMemberMute(groupId, members) { return this._call('set_group_member_mute', [groupId, members]); }
  getBotMember(groupId) { return this._call('get_bot_member', [groupId]); }
  getImageSize(input) { return this._call('get_image_size', [input]); }
  uploadMedia(bytes, fileType, options = {}) { return this._call('upload_media', [bytes, fileType], options); }
}

class Event {
  constructor(plugin, invokeId, payload) { this._plugin = plugin; this._invokeId = invokeId; Object.assign(this, payload || {}); this.sender = new Sender(plugin, this); }
  _request(method, args = {}) { return this._plugin._request(method, { event_id: this._invokeId, args: encode(args) }); }
  reply(content, options = {}) { return this._request('event.reply', { content, options }); }
  replyCard(cardType = 'tuwen', data = null, content = '', options = {}) {
    if (typeof cardType !== 'string') {
      options = data || {};
      data = cardType;
      cardType = options.cardType || options.card_type || 'tuwen';
      content = options.content || '';
    }
    return this.sender.replyCard(cardType, data, content, options);
  }
  replyStream(chunks, options = {}) { return this.sender.replyStream(chunks, options); }
  replyImage(data, content = '', options = {}) { return this.sender.replyImage(data, content, options); }
  replyVoice(data, content = '', options = {}) { return this.sender.replyVoice(data, content, options); }
  replyVideo(data, content = '', options = {}) { return this.sender.replyVideo(data, content, options); }
  replyFile(data, content = '', options = {}) { return this.sender.replyFile(data, content, options); }
  replyArk(templateId, kvData, content = '', options = {}) { return this.sender.replyArk(templateId, kvData, content, options); }
  recall(messageId) { return this._request('event.recall', { message_id: messageId }); }
  ackInteraction(code = 0, options = {}) { return this.sender.ackInteraction(code, options); }
  setCallbackCode(code) { return this._request('event.set_callback_code', { code }); }
  setAckTimeout(seconds) { return this._request('event.set_ack_timeout', { seconds }); }
  get(path) { return this._request('event.get', { path }); }
  sendToGroup(groupId, content, options = {}) { return this._request('event.send_to_group', { group_id: groupId, content, options }); }
  sendToUser(userId, content, options = {}) { return this._request('event.send_to_user', { user_id: userId, content, options }); }
}

class ContextProxy {
  constructor(plugin) { this._plugin = plugin; }
  _call(name, args = [], options = {}) { return this._plugin._request('context.' + name, { args: encode(args), options: encode(options) }); }
  getDataPath(filename) { return this._call('get_data_path', [filename]); }
  getResourcePath(filename) { return this._call('get_resource_path', [filename]); }
  readConfig(filename = 'config.yaml') { return this._call('read_config', [filename]); }
  saveConfig(data, filename = 'config.yaml', comments = null) { return this._call('save_config', [data, filename, comments]); }
  ensureConfig(defaults, filename = 'config.yaml', comments = null) { return this._call('ensure_config', [defaults, filename, comments]); }
  readData(filename, encoding = 'utf-8') { return this._call('read_data', [filename, encoding]); }
  saveData(filename, content, encoding = 'utf-8') { return this._call('save_data', [filename, content, encoding]); }
  dataExists(filename) { return this._call('data_exists', [filename]); }
  listData() { return this._call('list_data'); }
  readConfigAsync(filename = 'config.yaml') { return this._call('read_config_async', [filename]); }
  saveConfigAsync(data, filename = 'config.yaml', comments = null) { return this._call('save_config_async', [data, filename, comments]); }
  readDataAsync(filename, encoding = 'utf-8') { return this._call('read_data_async', [filename, encoding]); }
  saveDataAsync(filename, content, encoding = 'utf-8') { return this._call('save_data_async', [filename, content, encoding]); }
}

class ConfigProxy {
  constructor(plugin) { this._plugin = plugin; }
  _call(name, args = []) { return this._plugin._request('config.' + name, { args: encode(args) }); }
  get(section, key, defaultValue = null) { return this._call('get', [section, key, defaultValue]); }
  getBotConfigs() { return this._call('get_bot_configs'); }
  getBotConfig(appid) { return this._call('get_bot_config', [appid]); }
  getBotSetting(appid, key, defaultValue = null) { return this._call('get_bot_setting', [appid, key, defaultValue]); }
  setBotSetting(appid, key, value) { return this._call('set_bot_setting', [appid, key, value]); }
}

class ImageHostingProxy {
  constructor(plugin) { this._plugin = plugin; }
  call(method, args = [], options = {}) {
    return this._plugin._request('module.call', { name: 'image_hosting', method, args: encode(args), options: encode(options) });
  }
  status() { return this.call('status'); }
  uploadAny(imageBytes, filename = 'image.png', options = {}) { return this.call('upload_any', [imageBytes, filename], options); }
}

class ModulesProxy {
  constructor(plugin) {
    this.imageHosting = new Proxy(new ImageHostingProxy(plugin), {
      get(target, property, receiver) {
        if (property === 'then') return undefined;
        if (Reflect.has(target, property)) return Reflect.get(target, property, receiver);
        if (typeof property !== 'string') return undefined;
        return (...values) => {
          let options = {};
          const last = values[values.length - 1];
          if (last && typeof last === 'object' && !Buffer.isBuffer(last) && !(last instanceof Uint8Array) && !Array.isArray(last)) options = values.pop();
          return target.call(property, values, options);
        };
      },
    });
    this.image_hosting = this.imageHosting;
  }
}

class LoggerProxy {
  constructor(plugin) { this._plugin = plugin; }
  _write(level, args) { return this._plugin._request('log.' + level, { args: encode(args) }); }
  debug(...args) { return this._write('debug', args); }
  info(...args) { return this._write('info', args); }
  warning(...args) { return this._write('warning', args); }
  warn(...args) { return this.warning(...args); }
  error(...args) { return this._write('error', args); }
  exception(...args) { return this._write('exception', args); }
}

class Bot extends Sender {
  constructor(plugin, appid) { super(plugin, null, String(appid)); this.appid = String(appid); }
}

class Plugin {
  constructor(meta = {}) {
    this.meta = { ...meta }; this.handlers = []; this.interceptors = []; this._onLoad = []; this._onUnload = [];
    this._pending = new Map(); this._nextId = 1; this._started = false; this._stopping = false; this._rl = null;
    this.ctx = new ContextProxy(this); this.context = this.ctx; this.config = new ConfigProxy(this); this.cfg = this.config;
    this.log = new LoggerProxy(this); this.sender = new Sender(this); this.modules = new ModulesProxy(this);
  }
  command(pattern, callback, options = {}) {
    if (typeof pattern !== 'string' || !pattern) throw new TypeError('pattern must be a non-empty string');
    if (typeof callback !== 'function') throw new TypeError('handler callback must be a function');
    const id = options.id || 'handler-' + (this.handlers.length + 1);
    if (this.handlers.some((handler) => handler.id === id)) throw new Error('duplicate handler id: ' + id);
    this.handlers.push({ id, pattern, name: options.name || id, desc: options.desc || options.description || '', priority: options.priority || 0,
      owner_only: Boolean(options.ownerOnly || options.owner_only), group_only: Boolean(options.groupOnly || options.group_only), direct_only: Boolean(options.directOnly || options.direct_only),
      channel_only: Boolean(options.channelOnly || options.channel_only), event_types: options.eventTypes || options.event_types || null, cooldown: options.cooldown || 0,
      ignore_at_check: Boolean(options.ignoreAtCheck || options.ignore_at_check), block: Boolean(options.block), fallback: Boolean(options.fallback), callback });
    return this;
  }
  on(pattern, callback, options = {}) { return this.command(pattern, callback, options); }
  intercept(callback, options = {}) { if (typeof callback !== 'function') throw new TypeError('interceptor callback must be a function'); const id = options.id || 'interceptor-' + (this.interceptors.length + 1); this.interceptors.push({ id, priority: options.priority == null ? 100 : options.priority, callback }); return this; }
  interceptor(callback, options = {}) { return this.intercept(callback, options); }
  onLoad(callback) { if (typeof callback !== 'function') throw new TypeError('onLoad callback must be a function'); this._onLoad.push(callback); return this; }
  onUnload(callback) { if (typeof callback !== 'function') throw new TypeError('onUnload callback must be a function'); this._onUnload.push(callback); return this; }
  bot(appid) { return new Bot(this, appid); }
  call(method, ...args) { return this._request(method, { args: encode(args) }); }
  async start() {
    if (this._started) return; this._started = true;
    this._rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
    this._rl.on('line', (line) => this._receive(line).catch((error) => process.stderr.write((error.stack || error) + '\n')));
    this._rl.on('close', () => { if (!this._stopping) process.exit(0); });
    for (const callback of this._onLoad) {
      try { await callback(this.ctx); } catch (error) { process.stderr.write((error.stack || error) + '\n'); }
    }
    write({ type: 'ready', meta: this.meta, handlers: this.handlers.map(({ callback, ...declaration }) => declaration), interceptors: this.interceptors.map(({ callback, ...declaration }) => declaration), lifecycle: { onLoad: this._onLoad.length > 0, onUnload: this._onUnload.length > 0 } });
    await new Promise(() => {});
  }
  _request(method, params) { const id = 'rpc-' + (this._nextId++); return new Promise((resolve, reject) => { this._pending.set(id, { resolve, reject }); write({ type: 'request', id, method, params }); }); }
  async _receive(line) {
    if (!line.trim()) return; let message; try { message = JSON.parse(line); } catch (error) { process.stderr.write('Invalid host message: ' + error.message + '\n'); return; }
    if (message.type === 'response') { const pending = this._pending.get(message.id); if (!pending) return; this._pending.delete(message.id); if (message.ok === false) pending.reject(new RpcError(message.error || 'host request failed')); else pending.resolve(message.result); return; }
    if (message.type === 'event') { await this._invoke(message); return; }
    if (message.type === 'interceptor') { await this._invokeInterceptor(message); return; }
    if (message.type === 'shutdown') await this._shutdown();
  }
  async _invoke(message) {
    const handler = this.handlers.find((candidate) => candidate.id === message.handler); if (!handler) { write({ type: 'event_result', id: message.id, ok: false, error: 'unknown handler: ' + message.handler }); return; }
    try { const result = await handler.callback(new Event(this, message.id, message.event), new Match(message.match)); write({ type: 'event_result', id: message.id, ok: true, result: encode(result) }); }
    catch (error) { write({ type: 'event_result', id: message.id, ok: false, error: error && error.stack ? error.stack : String(error) }); }
  }
  async _invokeInterceptor(message) {
    const interceptor = this.interceptors.find((candidate) => candidate.id === message.interceptor); if (!interceptor) { write({ type: 'event_result', id: message.id, ok: false, error: 'unknown interceptor: ' + message.interceptor }); return; }
    try { const result = await interceptor.callback(new Event(this, message.id, message.event)); write({ type: 'event_result', id: message.id, ok: true, result: encode(result) }); }
    catch (error) { write({ type: 'event_result', id: message.id, ok: false, error: error && error.stack ? error.stack : String(error) }); }
  }
  async _shutdown() { if (this._stopping) return; this._stopping = true; for (const callback of this._onUnload) { try { await callback(this.ctx); } catch (error) { process.stderr.write((error.stack || error) + '\n'); } } process.exit(0); }
}

const senderAliases = {
  reply_stream: 'replyStream', send_stream_to_user: 'sendStreamToUser', reply_image: 'replyImage', reply_voice: 'replyVoice',
  reply_video: 'replyVideo', reply_file: 'replyFile', reply_ark: 'replyArk', reply_card: 'replyCard', send_to_group: 'sendToGroup',
  send_to_user: 'sendToUser', send_to_channel: 'sendToChannel', send_image: 'sendImage', send_wakeup: 'sendWakeup',
  force_wakeup: 'forceWakeup', ack_interaction: 'ackInteraction', get_share_link: 'getShareLink', get_global_menu: 'getGlobalMenu',
  update_global_menu: 'updateGlobalMenu', get_panels: 'getPanels', create_panel: 'createPanel', get_panel: 'getPanel',
  update_panel: 'updatePanel', delete_panel: 'deletePanel', update_panel_targets: 'updatePanelTargets', get_group_member: 'getGroupMember',
  get_group_record: 'getGroupRecord', get_group_info: 'getGroupInfo', get_group_bot_state: 'getGroupBotState',
  refresh_group_info: 'refreshGroupInfo', get_group_join_requests: 'getGroupJoinRequests', review_group_join_request: 'reviewGroupJoinRequest',
  get_group_restrict_chat_setting: 'getGroupRestrictChatSetting', set_group_member_mute: 'setGroupMemberMute',
  get_bot_member: 'getBotMember', get_image_size: 'getImageSize', upload_media: 'uploadMedia',
};
for (const [snake, camel] of Object.entries(senderAliases)) {
  Sender.prototype[snake] = Sender.prototype[camel];
  if (!Event.prototype[camel]) Event.prototype[camel] = function (...args) { return this.sender[camel](...args); };
  Event.prototype[snake] = function (...args) { return this.sender[camel](...args); };
}
Event.prototype.reply_card = function (...args) { return this.sender.replyCard(...args); };
Event.prototype.set_callback_code = Event.prototype.setCallbackCode;
Event.prototype.set_ack_timeout = Event.prototype.setAckTimeout;

const contextAliases = { get_data_path: 'getDataPath', get_resource_path: 'getResourcePath', read_config: 'readConfig', save_config: 'saveConfig',
  ensure_config: 'ensureConfig', read_data: 'readData', save_data: 'saveData', data_exists: 'dataExists', list_data: 'listData',
  read_config_async: 'readConfigAsync', save_config_async: 'saveConfigAsync', read_data_async: 'readDataAsync', save_data_async: 'saveDataAsync' };
for (const [snake, camel] of Object.entries(contextAliases)) ContextProxy.prototype[snake] = ContextProxy.prototype[camel];
ConfigProxy.prototype.get_bot_configs = ConfigProxy.prototype.getBotConfigs;
ConfigProxy.prototype.get_bot_config = ConfigProxy.prototype.getBotConfig;
ConfigProxy.prototype.get_bot_setting = ConfigProxy.prototype.getBotSetting;
ConfigProxy.prototype.set_bot_setting = ConfigProxy.prototype.setBotSetting;
Plugin.prototype.on_load = Plugin.prototype.onLoad;
Plugin.prototype.on_unload = Plugin.prototype.onUnload;

module.exports = { Plugin, Event, Match, Sender, Bot, ContextProxy, ConfigProxy, ImageHostingProxy, LoggerProxy, RpcError };
