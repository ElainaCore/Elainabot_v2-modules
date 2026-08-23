# SDK工具模块

该模块负责在 Python 主框架中运行外部 SDK 插件。
当前提供 Node.js 适配器，后续可继续添加其他语言适配器。

## 安装与启用

从 ElainaBot 插件市场安装 `sdktool`。框架会在下载完成后默认启用并立即启动，
启用状态会写入 `modules/modules_enabled.json`，后续可在模块管理页手动关闭。

Node.js 插件需要 Node.js 20 或更高版本，并确保 `node` 在系统 PATH 中。

## 插件结构

```text
plugins/hello_js/
├── elaina-plugin.json
└── main.js
```

`elaina-plugin.json`：

```json
{
  "name": "hello_js",
  "runtime": "node",
  "entry": "main.js",
  "version": "0.1.0",
  "description": "JavaScript 示例插件"
}
```

`main.js`：

```js
const { Plugin } = require('elainabot-sdk');

const plugin = new Plugin({
  name: 'Hello JS',
  version: '0.1.0',
});

plugin.command('^你好$', async (event, match) => {
  await event.reply('你好，这条消息来自 JavaScript 插件！');
}, {
  id: 'hello',
  name: 'JS 打招呼',
  desc: '使用 Node.js 回复消息',
});

plugin.start();
```

SDK 由运行时通过 `NODE_PATH` 自动注入，因此插件可以直接
`require('elainabot-sdk')`，不需要在每个插件目录重复复制 SDK。

## Node.js API

事件对象提供 MessageSender 的全部公开方法，并兼容 camelCase 与 snake_case。

- 回复：`reply`、`replyStream`、`replyImage`、`replyVoice`、`replyVideo`、`replyFile`、`replyArk`、`replyCard`、`recall`、`ackInteraction`
- 主动消息：`sendToGroup`、`sendToUser`、`sendToChannel`、`sendImage`、`sendStreamToUser`、`sendWakeup`、`forceWakeup`
- 菜单/面板：`getGlobalMenu`、`updateGlobalMenu`、`getPanels`、`createPanel`、`getPanel`、`updatePanel`、`deletePanel`、`updatePanelTargets`
- 群与媒体工具：`getGroupMember`、`getGroupRecord`、`getGroupInfo`、`getGroupBotState`、`refreshGroupInfo`、`getGroupJoinRequests`、`reviewGroupJoinRequest`、`getGroupRestrictChatSetting`、`setGroupMemberMute`、`getBotMember`、`getImageSize`、`uploadMedia`

脱离事件发送消息时使用 `plugin.bot(appid)`：

```js
await plugin.bot(appid).sendToUser(userId, '定时消息');
```

插件上下文提供配置、数据文件和路径 API，配置对象提供机器人与框架配置 API。

生命周期和拦截器：

```js
plugin.onLoad(async (ctx) => { await ctx.ensureConfig({ enabled: true }); });
plugin.intercept(async (event) => {
  if ((event.content || '').includes('违禁词')) {
    await event.reply('消息被拦截');
    return true;
  }
  return false;
}, { priority: 100 });
plugin.onUnload(async () => { /* 释放插件资源 */ });
```

图床模块可通过 `plugin.modules.imageHosting` 调用，其他模块适配器可继续扩展。

handler 的 `options` 支持 `id`、`name`、`desc`、`priority`、`eventTypes`、
`ownerOnly`、`groupOnly`、`directOnly`、`channelOnly`、`cooldown`、`block`、
`fallback` 和 `ignoreAtCheck`。

插件进程的 stdout 保留给协议使用，调试输出请写 stderr。
