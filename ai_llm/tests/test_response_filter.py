import sys
import types
import unittest


def _stub(name: str, **values):
    module = types.ModuleType(name)
    for key, value in values.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


_stub('aiohttp', ClientError=RuntimeError)
_stub('core')
_stub('core.base')
_stub('core.base.config', cfg=object())
_stub('core.message')
_stub('core.message.event', Event=type('Event', (), {}))
_stub('ai_llm.app.model_tool_store', ModelToolStore=type('ModelToolStore', (), {}))
_stub('ai_llm.app.audit', InvocationAudit=type('InvocationAudit', (), {}))
_stub('ai_llm.app.runtime', AgentRuntime=type('AgentRuntime', (), {}))

from ai_llm.app.service import (  # noqa: E402
    AIService,
    DEFAULT_CONFIG,
    _HiddenReasoningFilter,
    _strip_hidden_reasoning,
    _strip_tool_protocol,
    _text_tool_protocol,
    _xml_tool_calls,
)


TOOLS = [
    {
        'type': 'function',
        'function': {
            'name': 'tool_music',
            'parameters': {
                'type': 'object',
                'properties': {
                    'query': {'type': 'string'},
                    'selection': {'type': 'integer'},
                },
                'required': ['query'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'generate_image',
            'parameters': {
                'type': 'object',
                'properties': {'prompt': {'type': 'string'}},
                'required': ['prompt'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'tool_random_picker',
            'parameters': {
                'type': 'object',
                'properties': {
                    'items': {'type': 'array'},
                    'count': {'type': 'integer'},
                    'allow_duplicates': {'type': 'boolean'},
                },
                'required': ['items'],
            },
        },
    },
]


class ResponseFilterTests(unittest.TestCase):
    def test_default_provider_waits_for_the_real_model_catalog(self):
        provider = DEFAULT_CONFIG['providers'][0]
        self.assertEqual((provider['model'], provider['models']), ('', []))

    def test_removes_hidden_reasoning_from_final_text(self):
        value = '<think>internal reasoning</think>Visible answer'
        self.assertEqual(_strip_hidden_reasoning(value), 'Visible answer')

    def test_removes_reasoning_split_across_stream_chunks(self):
        output_filter = _HiddenReasoningFilter()
        visible = ''.join((
            output_filter.feed('<thi'),
            output_filter.feed('nk>secret</th'),
            output_filter.feed('ink>Visible'),
            output_filter.finish(),
        ))
        self.assertEqual(visible, 'Visible')

    def test_text_tool_protocol_uses_concrete_xml_names(self):
        prompt = _text_tool_protocol(TOOLS)
        self.assertIn('<tool_music><query>', prompt)
        self.assertNotIn('<工具名>', prompt)
        self.assertNotIn('<参数名>', prompt)

    def test_parses_attribute_and_json_body_tool_call(self):
        content = (
            '<tool_music query="aaa" selection="1">'
            '{"query":"aaa","selection":1}'
            '</tool_music>'
        )
        calls, cleaned = _xml_tool_calls(content, TOOLS)
        self.assertEqual(cleaned, '')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['function']['name'], 'tool_music')
        self.assertEqual(
            calls[0]['function']['arguments'],
            '{"query": "aaa", "selection": 1}',
        )

    def test_parses_self_closing_attribute_tool_call(self):
        calls, cleaned = _xml_tool_calls(
            '<tool_music query="aaa" selection="1"/>', TOOLS,
        )
        self.assertEqual(cleaned, '')
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0]['function']['arguments'],
            '{"query": "aaa", "selection": 1}',
        )

    def test_parses_invalid_attribute_syntax_from_json_body(self):
        content = (
            '<tool_random_picker items=["a","b"] count="1" '
            'allow_duplicates="false">'
            '{"items":["a","b"],"count":1,"allow_duplicates":false}'
            '</tool_random_picker>'
        )
        calls, cleaned = _xml_tool_calls(content, TOOLS)
        self.assertEqual(cleaned, '')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['function']['name'], 'tool_random_picker')
        self.assertEqual(
            calls[0]['function']['arguments'],
            '{"items": ["a", "b"], "count": 1, "allow_duplicates": false}',
        )

    def test_parses_invoke_parameter_dialect_with_normalized_tool_name(self):
        content = (
            '<工具名>generateimage</parametername>\n'
            '<parameter name="prompt">白发猫耳少女在卧室自拍</parameter>\n'
            '</invoke>'
        )
        calls, cleaned = _xml_tool_calls(content, TOOLS)
        self.assertEqual(cleaned, '')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['function']['name'], 'generate_image')
        self.assertEqual(
            calls[0]['function']['arguments'],
            '{"prompt": "白发猫耳少女在卧室自拍"}',
        )

    def test_parses_malformed_placeholder_parameter_dialect(self):
        content = (
            '<工具名>generateimage</parametername>\n'
            '<参数名>prompt</parametername>\n'
            '<参数名>参数名</parametername>\n'
            '可爱猫娘自拍，柔和光线\n'
            '</参数_name>\n'
            '</invoke>'
        )
        calls, cleaned = _xml_tool_calls(content, TOOLS)
        self.assertEqual(cleaned, '')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['function']['name'], 'generate_image')
        self.assertEqual(
            calls[0]['function']['arguments'],
            '{"prompt": "可爱猫娘自拍，柔和光线"}',
        )

    def test_parses_unknown_wrapper_names_from_registered_schema(self):
        content = (
            '<future-envelope><slot>tool_music</anything>'
            '<field>query</different>夜曲</future-envelope>'
        )
        calls, cleaned = _xml_tool_calls(content, TOOLS)
        self.assertEqual(cleaned, '')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['function']['name'], 'tool_music')
        self.assertEqual(
            calls[0]['function']['arguments'],
            '{"query": "夜曲"}',
        )

    def test_removes_unknown_tool_protocol_from_final_text(self):
        content = (
            'before<tool_unknown value="1">{"value":1}</tool_unknown>after'
        )
        self.assertEqual(_strip_tool_protocol(content), 'beforeafter')

    def test_removes_unparseable_invoke_protocol_from_final_text(self):
        content = (
            'before<工具名>missingtool</parametername>'
            '<parameter name="prompt">internal</parameter></invoke>after'
        )
        self.assertEqual(_strip_tool_protocol(content), 'beforeafter')

    def test_preserves_angle_brackets_when_no_tools_are_enabled(self):
        self.assertEqual(
            _strip_tool_protocol('普通文本 <b>加粗</b>', enabled=False),
            '普通文本 <b>加粗</b>',
        )


class CandidateExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_executes_xml_tool_call_and_returns_followup_answer(self):
        service = object.__new__(AIService)
        service._config = {
            'temperature': 0.8,
            'max_tokens': 1024,
            'max_tool_rounds': 3,
        }
        service._health = {}
        responses = [
            {
                'choices': [{'message': {'content': (
                    '<tool_music query="aaa" selection="1">'
                    '{"query":"aaa","selection":1}'
                    '</tool_music>'
                )}}],
                'usage': {},
            },
            {
                'choices': [{'message': {'content': '已经为你找到对应的音乐。'}}],
                'usage': {'completion_tokens': 8},
            },
        ]
        payloads = []

        async def request(_provider, payload, _run_id):
            payloads.append(payload.copy())
            return responses.pop(0)

        calls = []

        async def tool_handler(name, arguments):
            calls.append((name, arguments))
            return {'ok': True, 'title': 'aaa'}

        service._request = request
        result = await service._complete_candidate(
            {'id': 'test', 'name': 'Test Provider'},
            'test-model',
            [{'role': 'user', 'content': 'aaa'}],
            '', None, None, TOOLS, tool_handler, 3,
        )

        self.assertEqual(calls, [('tool_music', {'query': 'aaa', 'selection': 1})])
        self.assertEqual(result['text'], '已经为你找到对应的音乐。')
        self.assertEqual(len(payloads), 2)
        self.assertNotIn('<tool_music', result['text'])

    async def test_executes_malformed_invoke_tool_call_without_leaking_xml(self):
        service = object.__new__(AIService)
        service._config = {
            'temperature': 0.8,
            'max_tokens': 1024,
            'max_tool_rounds': 3,
        }
        service._health = {}
        responses = [
            {
                'choices': [{'message': {'content': (
                    '<工具名>generateimage</parametername>\n'
                    '<参数名>prompt</parametername>\n'
                    '<参数名>参数名</parametername>\n'
                    '可爱猫娘自拍，柔和光线\n'
                    '</参数_name>\n</invoke>'
                )}}],
                'usage': {},
            },
            {
                'choices': [{'message': {'content': '给你拍好啦。'}}],
                'usage': {'completion_tokens': 6},
            },
        ]

        async def request(_provider, _payload, _run_id):
            return responses.pop(0)

        calls = []

        async def tool_handler(name, arguments):
            calls.append((name, arguments))
            return {'ok': True, 'sent': True}

        service._request = request
        result = await service._complete_candidate(
            {'id': 'test', 'name': 'Test Provider'},
            'test-model',
            [{'role': 'user', 'content': '你的自拍'}],
            '', None, None, TOOLS, tool_handler, 3,
        )

        self.assertEqual(calls, [(
            'generate_image', {'prompt': '可爱猫娘自拍，柔和光线'},
        )])
        self.assertEqual(result['text'], '给你拍好啦。')
        self.assertNotIn('<invoke', result['text'])
        self.assertNotIn('<工具名>', result['text'])


if __name__ == '__main__':
    unittest.main()
