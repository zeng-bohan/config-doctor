"""仅从环境读取凭据，不把请求头写入轨迹。"""
import json
import os
import time
import urllib.error
import urllib.request


class ModelError(RuntimeError):
    pass


class DeepSeekProvider:
    def __init__(self, max_calls=8):
        self.key = os.environ.get('DEEPSEEK_API_KEY', '')
        if not self.key:
            raise ModelError('DEEPSEEK_API_KEY is required')
        self.model = os.environ.get('DEEPSEEK_MODEL', 'deepseek-flash')
        self.max_calls, self.calls = max_calls, 0
        self.usage, self.responses = [], []
        self.network_errors = []

    def complete(self, messages, tools=None):
        payload = {'model': self.model, 'messages': messages, 'temperature': 0,
                   'max_tokens': 800, 'thinking': {'type': 'disabled'}}
        if tools:
            payload['tools'] = tools
        request = urllib.request.Request('https://api.deepseek.com/chat/completions',
            data=json.dumps(payload).encode(), headers={
                'Authorization': 'Bearer ' + self.key, 'Content-Type': 'application/json'})
        for attempt in range(2):
            if self.calls >= self.max_calls:
                raise ModelError('model call budget exhausted')
            self.calls += 1
            started = time.monotonic()
            try:
                # 两次请求最坏20秒，小于Java的30秒租约。
                with urllib.request.urlopen(request, timeout=10) as response:
                    raw = json.load(response)
                message = raw['choices'][0]['message']
                if not isinstance(message, dict):
                    raise ValueError('message must be object')
                self.usage.append(raw.get('usage', {}))
                self.responses.append({'id': raw.get('id'), 'model': raw.get('model'),
                    'usage': raw.get('usage'), 'elapsedMs': round((time.monotonic()-started)*1000),
                    'message': message})
                return message
            except (urllib.error.URLError, TimeoutError) as error:
                code = getattr(error,'code',None)
                self.network_errors.append({'type':type(error).__name__,'status':code,'attempt':attempt+1})
                retryable = code is None or code in (429,502,503,504)
                if attempt == 0 and retryable:
                    time.sleep(0.2)
                    continue
                raise ModelError(f'{type(error).__name__} status={code}') from None
            except (ValueError,KeyError,IndexError,TypeError):
                raise ModelError('malformed model response') from None
