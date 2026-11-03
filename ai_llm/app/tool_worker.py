"""Execute an explicitly installed tool in a disposable process, never in the service."""
import asyncio
import contextlib
import inspect
import json
import sys
from jsonschema import Draft202012Validator


def main():
    request = json.loads(sys.stdin.buffer.read().decode('utf-8'))
    errors = Draft202012Validator(request['parameters']).iter_errors(request['arguments'])
    error = next(errors, None)
    if error is not None:
        path = '.'.join(str(part) for part in error.absolute_path) or 'arguments'
        print(json.dumps({'ok': False, 'error': f'工具参数不符合 Schema：{path} ({error.validator})'}), flush=True)
        return
    namespace = {'__name__': '__agent_tool__'}
    with contextlib.redirect_stdout(sys.stderr):
        exec(compile(request['code'], '<agent-tool>', 'exec'), namespace)
        value = namespace['run'](request['arguments'], request['config'])
        if inspect.isawaitable(value):
            value = asyncio.run(value)
    print(json.dumps({'ok': True, 'result': value}, allow_nan=False), flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'ok': False, 'error': f'{type(error).__name__}: {error}'}), flush=True)
