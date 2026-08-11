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

    def test_removes_unknown_tool_protocol_from_final_text(self):
        content = (
            'before<tool_unknown value="1">{"value":1}</tool_unknown>after'
        )
        self.assertEqual(_strip_tool_protocol(content), 'beforeafter')


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


if __name__ == '__main__':
    unittest.main()
