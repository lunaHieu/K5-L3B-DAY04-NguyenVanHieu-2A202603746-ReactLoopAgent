"""Bốn provider cùng một interface. Không chứa agent loop."""
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

CONFIG = {
    'openai': ('OPENAI_API_KEY', 'https://api.openai.com/v1/chat/completions'),
    'gemini': ('GEMINI_API_KEY', 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'),
    'openrouter': ('OPENROUTER_API_KEY', 'https://openrouter.ai/api/v1/chat/completions'),
    'anthropic': ('ANTHROPIC_API_KEY', 'https://api.anthropic.com/v1/messages'),
}

_GEMINI_RATE_LOCK = threading.Lock()
_GEMINI_NEXT_REQUEST_AT = 0.0


def wait_for_gemini_rate_limit():
    """Giản cách request Gemini trong một process để tránh vượt RPM.

    Mặc định không chờ. Free tier 15 RPM có thể đặt
    GEMINI_MIN_INTERVAL_SECONDS=4.5 (xấp xỉ 13 request/phút).
    """
    global _GEMINI_NEXT_REQUEST_AT
    raw_interval = os.getenv('GEMINI_MIN_INTERVAL_SECONDS', '0')
    try:
        interval = max(0.0, float(raw_interval))
    except ValueError:
        interval = 0.0
    if interval == 0:
        return
    with _GEMINI_RATE_LOCK:
        delay = _GEMINI_NEXT_REQUEST_AT - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        _GEMINI_NEXT_REQUEST_AT = time.monotonic() + interval


def post_json(url, headers, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json', **headers})
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if attempt == 0 and exc.code in (429, 500, 502, 503, 504):
                retry_after = exc.headers.get('Retry-After') if exc.headers else None
                try:
                    delay = float(retry_after)
                except (TypeError, ValueError):
                    delay = 60.0 if exc.code == 429 else 10.0
                time.sleep(max(1.0, min(delay, 60.0)))
                continue
            # Do not print response bodies: they may contain account or request data.
            raise RuntimeError(f'Provider trả HTTP {exc.code}; kiểm tra model, key và quota.') from exc
        except TimeoutError as exc:
            if attempt == 0:
                time.sleep(10.0)
                continue
            raise RuntimeError(f'Provider timeout sau khi đã retry: {exc}') from exc
        except urllib.error.URLError as exc:
            if attempt == 0 and isinstance(exc.reason, TimeoutError):
                time.sleep(10.0)
                continue
            raise RuntimeError(f'Không kết nối được provider: {exc.reason}') from exc


class Provider:
    """complete(messages, schemas) -> {message, calls, finish, usage}.

    messages: system/user/assistant/tool theo format Chat Completions. message trả về
    cần được append nguyên vẹn; tool result có role, tool_call_id, content (JSON string).
    """
    def __init__(self, name, model):
        if name not in CONFIG:
            raise ValueError(f'Provider không hợp lệ: {name}')
        if not model:
            raise ValueError('Cần đặt MODEL là model có hỗ trợ tool calling.')
        self.name, self.model = name, model
        key_name, self.url = CONFIG[name]
        self.key = os.getenv(key_name)
        if not self.key:
            raise ValueError(f'Thiếu {key_name}; xem README.md.')

    def complete(self, messages, schemas):
        if self.name == 'anthropic':
            return self._anthropic(messages, schemas)
        if self.name == 'gemini':
            wait_for_gemini_rate_limit()
            if os.getenv('GEMINI_USE_NATIVE', '').strip().lower() in ('1', 'true', 'yes'):
                return self._gemini_native(messages, schemas)
        payload = {'model': self.model, 'messages': messages}
        if schemas:
            payload['tools'] = schemas
        data = post_json(self.url, {'Authorization': f'Bearer {self.key}'}, payload)
        choice = data['choices'][0]
        message = choice['message']
        calls = [{'id': c['id'], 'name': c['function']['name'], 'arguments': c['function']['arguments']} for c in message.get('tool_calls', [])]
        return {'message': message, 'calls': calls, 'finish': choice.get('finish_reason'), 'usage': data.get('usage', {})}

    def _gemini_native(self, messages, schemas):
        """Gemini generateContent adapter cho auth key dùng x-goog-api-key.

        Chỉ bật bằng GEMINI_USE_NATIVE=1; đường OpenAI-compatible mặc định được
        giữ nguyên để không thay đổi hành vi của các provider adapter hiện có.
        """
        system = '\n'.join(
            str(message.get('content') or '')
            for message in messages
            if message.get('role') == 'system'
        )
        contents = []
        call_names = {}
        index = 0
        while index < len(messages):
            message = messages[index]
            role = message.get('role')
            if role == 'system':
                index += 1
                continue
            if role == 'assistant':
                parts = message.get('_gemini_parts')
                if not isinstance(parts, list):
                    parts = []
                    if message.get('content'):
                        parts.append({'text': message['content']})
                    for call in message.get('tool_calls', []):
                        function = call.get('function', {})
                        try:
                            arguments = json.loads(function.get('arguments') or '{}')
                        except (json.JSONDecodeError, TypeError):
                            arguments = {}
                        function_call = {
                            'name': function.get('name'),
                            'args': arguments,
                        }
                        if call.get('id'):
                            function_call['id'] = call['id']
                            call_names[call['id']] = function.get('name')
                        parts.append({'functionCall': function_call})
                else:
                    for call in message.get('tool_calls', []):
                        if call.get('id'):
                            call_names[call['id']] = call.get('function', {}).get('name')
                if parts:
                    contents.append({'role': 'model', 'parts': parts})
                index += 1
                continue
            if role == 'tool':
                parts = []
                while index < len(messages) and messages[index].get('role') == 'tool':
                    tool_message = messages[index]
                    call_id = tool_message.get('tool_call_id')
                    try:
                        response = json.loads(tool_message.get('content') or '{}')
                    except (json.JSONDecodeError, TypeError):
                        response = {'result': tool_message.get('content')}
                    if not isinstance(response, dict):
                        response = {'result': response}
                    function_response = {
                        'name': call_names.get(call_id, 'unknown_tool'),
                        'response': response,
                    }
                    if call_id:
                        function_response['id'] = call_id
                    parts.append({'functionResponse': function_response})
                    index += 1
                contents.append({'role': 'user', 'parts': parts})
                continue
            contents.append({
                'role': 'user',
                'parts': [{'text': str(message.get('content') or '')}],
            })
            index += 1

        def native_schema(value):
            """Giữ phần JSON Schema được Gemini function declarations hỗ trợ."""
            if isinstance(value, dict):
                return {
                    key: native_schema(item)
                    for key, item in value.items()
                    if key != 'additionalProperties'
                }
            if isinstance(value, list):
                return [native_schema(item) for item in value]
            return value

        payload = {'contents': contents}
        if system:
            payload['systemInstruction'] = {'parts': [{'text': system}]}
        if schemas:
            payload['tools'] = [{
                'functionDeclarations': [
                    native_schema(schema['function']) for schema in schemas
                ]
            }]
        else:
            # Khi prompt yêu cầu JSON (ví dụ LLM judge), dùng JSON response mode
            # để tránh chuỗi nhiều dòng không escape làm json.loads thất bại.
            prompt_text = system + '\n' + '\n'.join(
                str(message.get('content') or '')
                for message in messages
                if message.get('role') in ('user', 'system')
            )
            if 'json' in prompt_text.casefold():
                payload['generationConfig'] = {
                    'responseMimeType': 'application/json'
                }
        model = urllib.parse.quote(self.model, safe='-._')
        url = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent'
        data = post_json(url, {'x-goog-api-key': self.key}, payload)
        candidates = data.get('candidates') or []
        if not candidates:
            feedback = data.get('promptFeedback') or {}
            raise RuntimeError(f'Gemini native không trả candidate: {feedback}')
        candidate = candidates[0]
        parts = candidate.get('content', {}).get('parts', [])
        text_parts = [
            part.get('text', '') for part in parts
            if part.get('text') and not part.get('thought')
        ]
        calls = []
        tool_calls = []
        for position, part in enumerate(parts):
            function_call = part.get('functionCall')
            if not isinstance(function_call, dict):
                continue
            call_id = function_call.get('id') or f'gemini_call_{position}'
            name = function_call.get('name')
            arguments = json.dumps(function_call.get('args') or {}, ensure_ascii=False)
            calls.append({'id': call_id, 'name': name, 'arguments': arguments})
            tool_calls.append({
                'id': call_id,
                'type': 'function',
                'function': {'name': name, 'arguments': arguments},
            })
        message = {
            'role': 'assistant',
            'content': '\n'.join(text_parts) or None,
            '_gemini_parts': parts,
        }
        if tool_calls:
            message['tool_calls'] = tool_calls
        finish_reason = candidate.get('finishReason')
        if calls:
            finish = 'tool_calls'
        elif finish_reason == 'MAX_TOKENS':
            finish = 'length'
        elif finish_reason in (None, 'STOP'):
            finish = 'stop'
        else:
            finish = str(finish_reason).lower()
        native_usage = data.get('usageMetadata') or {}
        usage = {
            'prompt_tokens': native_usage.get('promptTokenCount', 0),
            'completion_tokens': native_usage.get('candidatesTokenCount', 0),
            'total_tokens': native_usage.get('totalTokenCount', 0),
        }
        return {'message': message, 'calls': calls, 'finish': finish, 'usage': usage}

    def _anthropic(self, messages, schemas):
        system = '\n'.join(m['content'] for m in messages if m['role'] == 'system')
        native = []
        for m in messages:
            if m['role'] == 'system':
                continue
            if m['role'] == 'assistant':
                blocks = []
                if m.get('content'):
                    blocks.append({'type': 'text', 'text': m['content']})
                for call in m.get('tool_calls', []):
                    blocks.append({'type': 'tool_use', 'id': call['id'], 'name': call['function']['name'], 'input': json.loads(call['function']['arguments'])})
                native.append({'role': 'assistant', 'content': blocks})
            elif m['role'] == 'tool':
                block = {'type': 'tool_result', 'tool_use_id': m['tool_call_id'], 'content': m['content']}
                if native and native[-1]['role'] == 'user' and isinstance(native[-1]['content'], list):
                    native[-1]['content'].append(block)
                else:
                    native.append({'role': 'user', 'content': [block]})
            else:
                native.append({'role': 'user', 'content': m['content']})
        tools = [{'name': x['function']['name'], 'description': x['function']['description'], 'input_schema': x['function']['parameters']} for x in schemas]
        payload = {'model': self.model, 'max_tokens': 1024, 'system': system, 'messages': native}
        if tools:
            payload['tools'] = tools
        data = post_json(self.url, {'x-api-key': self.key, 'anthropic-version': '2023-06-01'}, payload)
        blocks = data['content']
        calls = [{'id': b['id'], 'name': b['name'], 'arguments': json.dumps(b['input'], ensure_ascii=False)} for b in blocks if b['type'] == 'tool_use']
        text = '\n'.join(b['text'] for b in blocks if b['type'] == 'text')
        message = {'role': 'assistant', 'content': text or None}
        if calls:
            message['tool_calls'] = [{'id': c['id'], 'type': 'function', 'function': {'name': c['name'], 'arguments': c['arguments']}} for c in calls]
        reason = {'end_turn': 'stop', 'tool_use': 'tool_calls', 'max_tokens': 'length'}.get(data.get('stop_reason'), data.get('stop_reason'))
        return {'message': message, 'calls': calls, 'finish': reason, 'usage': data.get('usage', {})}
