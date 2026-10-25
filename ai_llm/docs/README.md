# AI LLM

ElainaBot 的统一模型服务。集中管理接口、密钥、模型目录、优先级、请求执行和工具调用。业务插件管理自己的配置、消息路由、人格、会话、记忆和调度。

## 能力

- OpenAI Chat Completions、Responses、兼容中转、Gemini 原生等协议适配。
- 模型获取与测活、接口和模型优先级、故障切换、流式响应。
- 文本和多模态消息输入、生图接口、审核接口。
- 调用级工具与插件私有工具注册；完整保留 JSON Schema，支持同步函数、异步函数、实例绑定方法。
- 工具执行轮数、超时、必要工具校验、按运行或会话 ID 中断。
- 可选的中央 Skills 和公网 HTTPS MCP 工具发现与调用。
- 调用日志、Token 用量、延迟和故障切换记录。
- 可选的无状态上下文预算控制；只处理本次传入的消息，不保存历史。

## 启用

1. 安装并启用 `ai_llm` 模块。
2. 在模块面板配置接口、API Key、模型与优先级。
3. 业务插件从公开配置中选择 `provider_id` 和 `model`，不重复保存接口密钥。

```python
from modules.ai_llm import get_service

service = get_service()
if service is None or not service.available():
    raise RuntimeError('请安装、启用并配置 AI LLM 模块')

result = await service.complete(
    messages=[{'role': 'user', 'content': '你好'}],
    system_prompt='请简洁回答。',
    consumer_plugin='my_plugin',
    session_id='my_plugin:user-1',
    prepare_context=False,  # 业务插件已管理历史时关闭中央裁剪
)
text = result['text']
```

`session_id` 用于运行标识、中断和日志关联；不会自动加载或保存聊天历史。不同业务与用户应使用不同 ID。默认情况下，同一 ID 的新调用会中断仍在运行的旧调用。

## 工具调用

调用级工具随本次请求提供：

```python
tools = [{
    'type': 'function',
    'function': {
        'name': 'query_status',
        'description': '查询当前状态',
        'parameters': {
            'type': 'object', 'properties': {}, 'additionalProperties': False,
        },
    },
}]

async def handle_tool(name, arguments):
    if name == 'query_status':
        return {'enabled': True}
    return {'ok': False, 'error': '未知工具'}

result = await service.complete(
    messages=[{'role': 'user', 'content': '现在状态如何？'}],
    tools=tools,
    tool_handler=handle_tool,
    consumer_plugin='my_plugin',
    max_tool_rounds=3,
    tool_timeout=30,
)
```

跨请求使用的工具可以注册到该服务实例：

```python
service.register_plugin_tools('my_plugin', tools, handle_tool)
result = await service.complete(
    messages=[{'role': 'user', 'content': '查询状态'}],
    consumer_plugin='my_plugin',
)
# 插件卸载时注销；中央服务重载后需要重新注册。
service.unregister_plugin_tools('my_plugin')
```

- 注册工具只会注入相同 `consumer_plugin` 的请求；不存在跨插件工具市场或全局工具单例。
- 直接传入绑定方法作为 `handler` 即可，无需继承中央插件基类。
- 同名调用级工具优先于注册工具；模型只能执行本次请求中公开的工具。
- `include_plugin_tools=False` 可关闭注册工具注入。
- `execute_tools=False` 只生成并返回 `tool_calls`，不会执行工具。
- `tool_timeout` 默认 120 秒；超时终止本次调用，不触发模型故障切换重复执行工具。同步处理器应保持快速，耗时工作需提供可取消的异步处理器。
- `result['tool_events']` 包含真实工具调用的名称、ID、参数和结果。

## Skills 与 MCP

面板管理 Skills 和 MCP，但普通 `complete()` 不会自动注入它们。需要时使用：

```python
result = await service.run_agent(
    messages=[{'role': 'user', 'content': '完成这项任务'}],
    consumer_plugin='my_plugin',
    runtime_capabilities=['skill', 'mcp'],
)
```

`run_agent()` 是启用中央运行工具的便捷入口；不管理插件生命周期、不创建平台消息、不定时运行。Skills 支持上传 UTF-8 `SKILL.md` 或包含单个 `SKILL.md` 的 ZIP。MCP 只接受公网 HTTPS 地址，受请求超时与网络地址检查限制。

## 流式输出

```python
async for event in service.stream_complete(
    messages=[{'role': 'user', 'content': '你好'}],
    session_id='my_plugin:user-1',
    prepare_context=False,
):
    if event.get('type') == 'delta':
        yield event['text']
    elif event.get('type') == 'done':
        usage = event.get('usage', {})
```

流式入口用于文本生成；工具执行使用 `complete()`。流式输出开始后不会切换到另一个模型重放正文。

## 职责精简与升级

已移除中央人格/会话存储、Cron 调度、平台消息组件、兼容插件加载器、配置存储、命令/权限/事件占位装饰器及 `service.api` / `activate_plugin()`。模块不创建或接管 `astrbot` 命名空间，也不依赖 AstrBot。

人格、上下文持久化、用户记忆和主动问候由聊天陪伴等业务插件负责；AstrBot 适配由独立的基座插件负责。原有 `complete()`、`stream_complete()`、`run_agent()` 和原生工具注册保留。

旧 `persona`、`cron_jobs` 设置不再生效，保存新配置前会在数据目录归档为 `retired_features.json`，便于迁移；既有 `ai_service.db` 与 `plugin_configs` 文件不会删除或修改。旧计划任务不会继续运行。使用已移除接口的第三方插件需要迁移到原生调用，移除的关键字参数会明确报错。

## 验证

在模块仓库根目录运行：

```text
python -B -m unittest discover -s ai_llm/tests -v
node ai_llm/tests/test_panel.cjs
```

测试不连接外部模型，覆盖参数保留、绑定方法、插件隔离、注册原子性、单轮生成、工具循环与超时、故障切换、流式输出、卸载取消及面板页面完整性。
