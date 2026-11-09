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

视觉输入沿用 OpenAI `content: [{type: "text", text: "..."}, {type: "image_url", image_url: {url: "..."}}]` 格式。Chat Completions 保留该结构，Responses 转换为 `input_image` 并保留 `detail`；Gemini 原生将 `data:image/...;base64,...` 转为 `inlineData`，远程图片请调用方先下载为 data URL。Gemini 不会静默丢弃不支持的远程图片输入。模型本身需支持视觉。

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

- 插件注册的私有工具只会注入相同 `consumer_plugin` 的请求，不上报公用工具目录或面板；每个服务实例独立管理。
- 直接传入绑定方法作为 `handler` 即可，无需继承中央插件基类。
- 同名调用级工具优先于注册工具；模型只能执行本次请求中公开的工具。
- `include_plugin_tools=False` 可关闭注册工具注入。
- `execute_tools=False` 只生成并返回 `tool_calls`，不会执行工具。
- `tool_timeout` 默认 120 秒；超时终止本次调用，不触发模型故障切换重复执行工具。同步处理器应保持快速，耗时工作需提供可取消的异步处理器。
- `result['tool_events']` 包含真实工具调用的名称、ID、参数和结果。

## 公用 Agent 工具

面板「Agent 工具」独立管理中央公用库：

- **下载**：选择内置的当前时间、文本统计工具，或填写工具 JSON 的公网 HTTPS 直接下载地址。也可导入本地 JSON。
- **配置**：编辑名称、用途说明、参数 Schema、Python 代码和本地配置，可限制适用接口及模型名称。
- **AI 创作**：选择已有模型，描述需求，服务调用模型生成代码并自动保存；生成过程不执行工具，也不注入现有公用或私有工具。
- **启停与试运行**：新下载、导入和 AI 创建的工具默认停用。保存后可使用参数 JSON 试运行，再决定是否启用。
- **分享**：导出 JSON 会清空本地配置并关闭启用状态，避免将配置中的凭据一起分享。导入或下载也会清空来源包配置。

工具保存在模块数据目录的 `public_tools/<id>.json`，重启后继续加载。ID 不可修改；手动新建不会覆盖同名工具，AI 未指定 ID 时自动为同名结果追加后缀。读取失败的文件会在面板列出，其他工具仍可用。

### 插件请求 AI 创建公用工具

插件直接调用 `service.create_public_tool()` 即可，无需注册私有工具、上报回调函数或调用 Web 面板：

```python
from modules.ai_llm import get_service

async def create_shared_date_tool():
    service = get_service()
    if service is None or not service.available():
        raise RuntimeError('请先安装、启用并配置 AI LLM 服务')

    tool = await service.create_public_tool(
        '制作日期差工具：输入 YYYY-MM-DD 日期，返回它与今天相差的天数，过去为负、未来为正。',
        consumer_plugin='my_plugin',  # 创建来源与模型调用日志归属
        tool_id='date_distance',      # 可选：指定新工具 ID，已存在时明确报错
        provider_id='', model='',     # 可选：创作接口和模型；留空按中央优先级选择
    )
    return {
        'id': tool['id'],
        'function_name': tool['function_name'],  # tool_date_distance
        'enabled': tool['enabled'],             # False，尚未执行或自动启用
    }
```

生成结果会立即写入中央 `public_tools/` 并显示在「Agent 工具」页，面板标记“AI 创建 · 插件：my_plugin”。启用后，其他插件可以通过 `model_tool_definitions([tool['id']])` 和 `call_model_tool()` 选择、调用它；发起插件的私有工具仍不会进入公用库。

- `consumer_plugin`：发起插件名称，用于来源记录与调用日志，不会因此注入该插件的私有工具。
- `tool_id`：可选，指定时不覆盖已存在的 ID；未指定时使用 AI 生成的 ID，同名自动添加 `_2`、`_3` 等后缀，多插件并发创建也不会覆盖。
- `provider_id`、`model`：仅决定使用哪个模型创作工具。
- `provider_ids`、`models`：可选字符串数组，限制成品工具适用的接口和模型；默认不限，与创作模型选择独立。
- 返回值是已保存的完整工具定义，额外含 `key`、`function_name`；`origin` 记录创建插件、实际创作接口/模型和时间，重启及编辑后仍保留。
- 代码和 Schema 校验失败时抛出 `ValueError`；模型调用失败按 `complete()` 的错误约定抛出，已有工具不受影响。

同一能力也可经已鉴权的 `POST /api/ext/ai-service/agent-tools/create` 调用，请求字段与上述参数一致，需求字段为 `requirement`。

工具格式：

```json
{
  "id": "echo",
  "name": "回声",
  "description": "返回输入文字",
  "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
  "code": "def run(arguments, config):\n    return arguments[\"text\"]\n",
  "config": {},
  "enabled": false,
  "timeout": 30,
  "provider_ids": [],
  "models": []
}
```

`run(arguments, config)` 支持同步或异步函数，返回值必须可 JSON 序列化。参数按 JSON Schema 校验；Schema 支持内部引用，不加载远程引用。模块通过 `requirements.txt` 声明 `jsonschema` 依赖，框架启用模块时安装。

每次执行使用独立 Python 进程，最多并行 4 个，超时 1–120 秒，输入和输出各限制 512KB；取消和卸载会清理运行进程。独立进程用于生命周期管理，**不是系统沙箱**，工具仍拥有机器人进程对应用户的文件和网络权限，只应启用可信代码。本地配置不会出现在工具目录或发送给模型的参数定义中；工具自身的返回值和错误仍会回填给模型，因此代码应避免返回敏感配置。

`run_agent()` 默认注入已启用、适用于当前实际接口与模型的公用工具：

```python
result = await service.run_agent(messages=messages, consumer_plugin='my_plugin')
# 普通 complete() 保持按需；显式启用公用工具：
result = await service.complete(messages=messages, enable_public_tools=True)
```

业务插件也可以只选择所需公用工具。聊天陪伴的原有公用工具选择页已接通：

```python
definitions = service.model_tool_definitions(['echo'], context={'provider_id': 'my-provider', 'model': 'my-model'})
result = await service.call_model_tool('tool_echo', {'text': '你好'}, context={'provider_id': 'my-provider', 'model': 'my-model'})
```

`model_tool_catalog()` 仅返回公用工具，绝不返回插件私有注册。公用工具的函数名统一为 `tool_<id>`；调用级工具和插件私有工具同名时优先使用它们。模型故障切换时重新筛选适用的公用工具。

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
node ai_llm/tests/test_providers.cjs
node ai_llm/tests/test_agent_tools.cjs
```

测试不连接外部模型，覆盖参数保留、绑定方法、插件隔离、注册原子性、单轮生成、工具循环与超时、故障切换、流式输出、卸载取消及面板页面完整性。

## 简化接口设置

接口卡片和模型列表默认收起，进入接口页时重新收起。展开后只显示名称、类型、URL、API Key 和优先级；默认按模型优先级自动选择模型。

模型列表、对话、生图和图片编辑路径由接口类型自动适配，例如 Responses 使用 `/responses`，Gemini 原生使用 `/models/{model}:generateContent`。高级设置中的路径留空表示自动；只有非标准服务需要自定义。获取模型失败时可以在高级设置中手动添加模型或指定模型。

进入接口页时会尝试查询兼容中转的额度余额，使用同一接口地址和 Key，不发送模型请求。成功结果缓存 5 分钟，不支持或无权限时隐藏余额并缓存 1 小时，避免反复查询；高级设置可关闭自动查询。该数值是服务商计费接口返回的额度余额，不保证等于网站钱包现金余额。

计费格式依据 [New API 账户计费面板文档](https://doc.newapi.pro/api/fei-account-billing-panel/)：`hard_limit_usd - total_usage / 100`。不要求填写管理员令牌，也不为不支持的接口推算余额。
