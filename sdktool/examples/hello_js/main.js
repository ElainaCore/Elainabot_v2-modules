const { Plugin } = require('elainabot-sdk');

const plugin = new Plugin({ name: 'Hello JS', version: '0.1.0' });

plugin.command('^你好$', async (event) => {
  await event.reply('你好，这条消息来自 JavaScript 插件！');
}, { id: 'hello', name: 'JS 打招呼', desc: 'Node.js 示例' });

plugin.start();
