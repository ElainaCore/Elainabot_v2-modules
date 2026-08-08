# AI LLM 服务

该模块集中管理 OpenAI 兼容接口、密钥、模型列表、故障切换和 Agent 运行能力。插件只提交消息与可选工具，不再保存独立的接口密钥。

## Agent 能力

- Provider 与接口内模型双层优先级、自动故障切换
- Web 测试接口使用 SSE 增量转发，首个响应分片到达即显示
- 一键测活会自动启用并前置可用模型，取消勾选失败模型，并持久化最后结果
- OpenAI Chat Completions 工具循环与多模态消息透传
- 子代理 handoff，每个子代理可指定 Prompt、接口和模型
- 自动上下文轮次裁剪与 Token 预算压缩
- 运行时 Prompt 全局注入和单次请求注入
- 按运行 ID 或会话 ID 中断正在进行的请求
- `SKILL.md` 渐进披露与按需读取
- 公网 HTTPS Streamable HTTP MCP 工具发现与调用
- 远程 Agent 沙箱执行，禁止在机器人宿主机直接执行模型代码
- Cron/间隔计划任务，通过 `ai_cron_result` Hook 广播结果

能力边界参考 AstrBot 的 Agent 架构重新实现，未依赖或复制 AstrBot 的运行时代码。AstrBot 使用 AGPL-3.0，参考源码位于仓库的 `AstrBot-master/` 目录。

## 插件接入

```python
from modules.ai_llm import get_service


async def ask_ai(text: str) -> str:
    service = get_service()
    if service is None:
        raise RuntimeError('请先在模块管理中启用 AI LLM 服务')

    result = await service.complete(
        [{'role': 'user', 'content': text}],
        system_prompt='你是一个简洁可靠的助手。',
    )
    return result['text']
```

`complete()` 会根据面板中的接口优先级和模型优先级自动选择链路，并在请求失败时切换到下一个候选。返回值还包含实际使用的 `provider_id` 和 `model`。

调用方还可传入 `session_id`、`runtime_prompt`、`enable_runtime_tools` 和 `allow_handoff`。中断运行使用 `service.runtime.interrupt(run_id_or_session_id)`。

## 工具调用

插件可以传入 OpenAI 格式的 `tools`，并提供异步 `tool_handler(name, arguments)`：

```python
result = await service.complete(
    messages,
    tools=tools,
    tool_handler=run_tool,
    max_tool_rounds=4,
)
```

工具的权限控制、参数校验和返回内容过滤仍由插件负责。涉及网络访问时，应拒绝回环、内网、链路本地及云元数据地址，避免泄露服务器网络信息。

## 已接入插件

- `ai_dev`：开发 Agent、工具调用循环和兼容 `relay` 均直接使用本服务；插件面板只显示中央活动接口和模型。
- `AI聊天陪伴`：普通对话、内容审核和面板测试均直接使用本服务；插件只保存人格、上下文、回复概率、安全与 Skills 设置。

两个插件都不会在中央服务不可用时回退到自己的接口配置。

插件可以保存自己的 `provider_id` 和 `model` 偏好，但这些值只能从 `service.config(public=True)` 返回的接口与模型中选择。两个值都为空时使用中央自动策略；指定接口且模型为空时，仍会在该接口内部按模型优先级切换；同时指定时使用精确组合。

## 启用方式

首次部署后，在 Web 面板的模块管理中启用 `AI LLM 服务` 并重启模块。中央配置首次创建时会只读迁移 `ai_dev` 的当前接口、多站点预设和服务端密钥；已有中央配置不会被覆盖。没有旧配置时使用 YTea OpenAI 兼容接口模板，但不会预置密钥。
