# AI LLM

AI LLM 是 ElainaBot 的统一大模型服务模块。接口密钥、模型、优先级和 Agent 能力由模块集中管理，业务插件只负责提交消息和处理自己的业务逻辑。

## 优点

- **统一管理接口**：集中配置 OpenAI、OpenAI 兼容中转、Gemini、Grok、Agnes、NovelAI、Jimeng2API、Z-Image Gitee 等接口和 API Key。
- **自动获取模型**：可从接口同步模型列表，并保留已有模型的启用状态与优先级。
- **双层优先级**：同时支持接口优先级和接口内模型优先级。
- **自动故障切换**：当前模型或整个接口不可用时，可按优先级尝试下一个候选。
- **流式响应**：支持 SSE 增量输出，适合聊天、开发助手和实时生成场景。
- **统一 Agent 运行时**：支持工具调用、多轮工具执行、子代理、运行时 Prompt 和中断。
- **Skills 与 MCP**：支持从面板上传、启用和删除 Skills，并支持公网 HTTPS MCP 工具发现与调用。
- **插件能力注入**：插件可以注册自己的 Skill、Agent、MCP 和工具，供 Agent 或其他插件调用。
- **上下文控制**：支持会话标识、上下文裁剪和 Token 预算控制。
- **调用可观测性**：统一记录模型、Token、首字延迟、总耗时、故障切换和工具调用。
- **安全保护**：公开配置会隐藏 API Key；MCP 拒绝回环、内网、链路本地和云元数据地址。

## 启用模块

1. 将 `ai_llm` 放入 ElainaBot 的模块目录。
2. 在框架的模块管理中启用 **AI LLM**。
3. 打开 AI LLM Web 面板，选择接口类型并填写 Base URL、API Key 与模型。
4. 获取模型，启用需要使用的模型并调整接口、模型优先级。
5. 启用 **LLM 服务**；需要工具调用时同时启用 **Agent**。

旧版接口配置会自动按“OpenAI 兼容 / 中转”读取。Gemini 原生类型会转换消息、工具调用和模型列表；其他 OpenAI 兼容类型可在面板覆盖模型、对话和生图路径。NovelAI 生图使用其原生请求与 ZIP 图片响应。Skills 页面支持上传 UTF-8 `SKILL.md` 或包含单个 `SKILL.md` 的 ZIP，单个上传文件最大 5 MB。

插件不应自行保存大模型接口或密钥。需要让用户选择接口、模型时，应从 `service.config(public=True)` 返回的公开配置中生成选项。

## 获取服务

```python
from modules.ai_llm import get_service


service = get_service()
if service is None:
    raise RuntimeError('请前往插件市场下载并启用 AI LLM 模块')
```

建议插件在实际调用前获取服务，不要在模块导入阶段永久缓存 `None`。这样 AI LLM 重载后，插件可以重新取得当前服务实例。

## 基础对话

```python
from modules.ai_llm import get_service


async def ask_ai(text: str, user_id: str) -> str:
    service = get_service()
    if service is None:
        raise RuntimeError('请前往插件市场下载并启用 AI LLM 模块')

    result = await service.complete(
        messages=[{'role': 'user', 'content': text}],
        system_prompt='你是一个简洁、可靠的助手。',
        session_id=f'my_plugin:{user_id}',
        consumer_plugin='my_plugin',
    )
    return result['text']
```

未指定接口和模型时，模块会按照面板中的接口优先级与模型优先级自动选择，并在失败时执行故障切换。

## 选择接口和模型

插件可以提供“自动选择”以及指定接口、模型的选项，但选项必须来自中央模块：

```python
config = service.config(public=True)
providers = config.get('providers', [])

result = await service.run_agent(
    messages=[{'role': 'user', 'content': '你好'}],
    provider_id='provider-id',  # 留空时自动选择接口
    model='model-name',         # 留空时按模型优先级选择
    consumer_plugin='my_plugin',
)
```

- `provider_id` 和 `model` 都为空：使用中央自动策略。
- 只指定 `provider_id`：在该接口内按模型优先级选择和切换。
- 同时指定两者：优先使用指定组合；该模型失败时不会擅自改成其他指定模型。

## 流式输出

```python
async def stream_reply(messages):
    service = get_service()
    if service is None:
        raise RuntimeError('请前往插件市场下载并启用 AI LLM 模块')

    async for event in service.stream_complete(
        messages=messages,
        session_id='my_plugin:conversation-1',
    ):
        if event.get('type') == 'delta':
            yield event.get('text', '')
        elif event.get('type') == 'done':
            usage = event.get('usage', {})
```

`delta` 到达后应立即转发给前端或聊天平台，不要等待整个生成过程结束。

## 接入插件工具

工具定义使用 OpenAI Chat Completions 格式，工具处理器可以是同步或异步函数：

```python
TOOLS = [{
    'type': 'function',
    'function': {
        'name': 'get_plugin_status',
        'description': '读取当前插件状态',
        'parameters': {
            'type': 'object',
            'properties': {},
            'additionalProperties': False,
        },
    },
}]


async def run_tool(name: str, arguments: dict):
    if name == 'get_plugin_status':
        return {'enabled': True}
    return {'ok': False, 'error': '未知工具'}


result = await service.complete(
    messages=[{'role': 'user', 'content': '检查插件状态'}],
    tools=TOOLS,
    tool_handler=run_tool,
    max_tool_rounds=4,
    consumer_plugin='my_plugin',
)
```

插件必须自行完成工具权限检查、参数校验和结果过滤。模型声称调用了工具不代表工具已经执行，业务结果应以 `tool_handler` 的真实返回值为准。

## 使用中央 Agent 能力

需要中央 Skills、Agent、MCP 或插件共享工具时，传入插件身份和允许使用的能力类型：

```python
result = await service.complete(
    messages=[{'role': 'user', 'content': '完成这项任务'}],
    consumer_plugin='my_plugin',
    runtime_capabilities=['skill', 'agent', 'mcp', 'tool'],
)
```

`consumer_plugin` 用于能力发现、权限判断和调用日志归属。没有插件身份的请求不会加载插件注入能力。

## 注册插件能力

插件可以注册 `skill`、`agent`、`mcp` 或 `tool`。注册能力默认启用但仅供来源插件使用；管理员可显式共享给所有插件，也可通过 `allowed_consumers` 只授权指定插件。

### 注册 Skill

```python
service.register_plugin_capability('my_plugin', 'skill', {
    'id': 'deployment-guide',
    'name': '部署规范',
    'description': '提供本插件的部署与检查流程。',
    'content': '执行部署任务时需要遵守的完整说明。',
})
```

### 注册工具

```python
async def capability_handler(capability_id: str, arguments: dict):
    if capability_id == 'query-status':
        return {'ok': True, 'status': 'running'}
    return {'ok': False, 'error': '不支持的能力'}


service.register_plugin_capability(
    'my_plugin',
    'tool',
    {
        'id': 'query-status',
        'name': '查询状态',
        'description': '查询插件的实时运行状态。',
        'config': {
            'schema': {
                'type': 'object',
                'properties': {},
                'additionalProperties': False,
            },
        },
    },
    handler=capability_handler,
)
```

### 注册 MCP

```python
service.register_plugin_capability('my_plugin', 'mcp', {
    'id': 'public-mcp',
    'name': '插件 MCP',
    'description': '由插件提供的公网 HTTPS MCP 服务。',
    'config': {
        'endpoint': 'https://example.com/mcp',
        'headers': {'Authorization': 'Bearer ...'},
        'timeout': 20,
    },
})
```

插件卸载或停止时，应将运行能力标记为离线：

```python
service.unregister_plugin_capabilities('my_plugin')
```

## 发现和调用能力

```python
capabilities = service.list_capabilities('my_plugin')
mcp_capabilities = await service.discover_capabilities('my_plugin', 'mcp')

result = await service.call_capability(
    'my_plugin',
    capability_key='source_plugin:tool:capability-id',
    arguments={'key': 'value'},
)
```

只能调用当前在线、已启用且允许该插件使用的能力。`capability_key` 应使用发现接口返回的值，不要由插件自行拼接。

## 接入注意事项

- 为每个对话传入稳定且隔离的 `session_id`，不要让不同用户共享上下文。
- `complete()` 是纯模型入口，不会隐式加载中央运行时能力；需要 Agent 行为时使用 `run_agent()`。
- 已经自行管理历史、摘要或记忆的插件应传入 `prepare_context=False`，避免中央层再次裁剪。
- 为调用传入固定的 `consumer_plugin`，便于权限判断和日志定位。
- 插件面板只保存 `provider_id`、`model` 等选择，不保存 API Key。
- 不要把公开配置中的脱敏密钥当作真实密钥再次提交。
- 网络工具必须拒绝回环、内网、链路本地和云元数据地址，避免泄露服务器 IP 与网络信息。
- MCP 只应使用可信的公网 HTTPS 服务，并限制可调用工具和超时时间。
- 在插件界面检测不到模块时，应灰化相关功能并提示用户前往插件市场下载 AI LLM。
