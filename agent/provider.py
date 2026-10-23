"""仅从环境读取凭据，不把请求头写入轨迹。重试采用指数退避 + 抖动，尊重 Retry-After。"""
import json
import os
import random
import time
import urllib.error
import urllib.request

RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})
BACKOFF_BASE_SECONDS = 0.2
BACKOFF_CAP_SECONDS = 8.0


class ModelError(RuntimeError):
    pass


def _retry_after_seconds(error):
    """从 429 响应头解析 Retry-After（秒）；缺失或非法时返回 None。"""
    headers = getattr(error, 'headers', None)
    if not headers:
        return None
    raw = headers.get('Retry-After')
    if raw is None:
        return None
    try:
        return min(max(float(raw), 0.0), BACKOFF_CAP_SECONDS)
    except (TypeError, ValueError):
        return None


class DeepSeekProvider:
    def __init__(self, max_calls=8, max_attempts=3, sleep_fn=None, jitter=None):
        self.key = os.environ.get('DEEPSEEK_API_KEY', '')
        if not self.key:
            raise ModelError('DEEPSEEK_API_KEY is required')
        self.model = os.environ.get('DEEPSEEK_MODEL', 'deepseek-flash')
        self.max_calls, self.calls = max_calls, 0
        self.max_attempts = max_attempts
        self.sleep = sleep_fn if sleep_fn is not None else time.sleep
        self.jitter = jitter if jitter is not None else random.uniform
        self.usage, self.responses = [], []
        self.network_errors = []
        self.latencies_ms = []

    def _backoff_delay(self, attempt):
        """第 attempt 次失败后的等待时间：指数退避 + 0..base/2 抖动，封顶 8 秒。"""
        exponential = min(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), BACKOFF_CAP_SECONDS)
        return min(exponential + self.jitter(0, BACKOFF_BASE_SECONDS / 2), BACKOFF_CAP_SECONDS)

    def complete(self, messages, tools=None):
        payload = {'model': self.model, 'messages': messages, 'temperature': 0,
                   'max_tokens': 800, 'thinking': {'type': 'disabled'}}
        if tools:
            payload['tools'] = tools
        request = urllib.request.Request('https://api.deepseek.com/chat/completions',
            data=json.dumps(payload).encode(), headers={
                'Authorization': 'Bearer ' + self.key, 'Content-Type': 'application/json'})
        for attempt in range(1, self.max_attempts + 1):
            if self.calls >= self.max_calls:
                raise ModelError('model call budget exhausted')
            self.calls += 1
            started = time.monotonic()
            try:
                # 每次请求 10 秒上限；退避封顶 8 秒，三次仍在 30 秒租约内。
                with urllib.request.urlopen(request, timeout=10) as response:
                    raw = json.load(response)
                message = raw['choices'][0]['message']
                if not isinstance(message, dict):
                    raise ValueError('message must be object')
                self.usage.append(raw.get('usage', {}))
                elapsed = round((time.monotonic() - started) * 1000)
                self.latencies_ms.append(elapsed)
                self.responses.append({'id': raw.get('id'), 'model': raw.get('model'),
                    'usage': raw.get('usage'), 'elapsedMs': elapsed,
                    'message': message})
                return message
            except (urllib.error.URLError, TimeoutError) as error:
                code = getattr(error, 'code', None)
                self.network_errors.append({'type': type(error).__name__, 'status': code,
                                            'attempt': attempt})
                if attempt < self.max_attempts and (code is None or code in RETRYABLE_STATUSES):
                    retry_after = _retry_after_seconds(error)
                    delay = retry_after if retry_after is not None else self._backoff_delay(attempt)
                    self.sleep(delay)
                    continue
                raise ModelError(f'{type(error).__name__} status={code}') from None
            except (ValueError, KeyError, IndexError, TypeError):
                raise ModelError('malformed model response') from None

    def usage_summary(self):
        """聚合 Token、调用与重试统计；不折算金额，避免价格表失真。"""
        prompt = sum(int(item.get('prompt_tokens') or 0) for item in self.usage)
        completion = sum(int(item.get('completion_tokens') or 0) for item in self.usage)
        latencies = sorted(self.latencies_ms)
        return {
            'modelCalls': self.calls,
            'retries': len(self.network_errors),
            'promptTokens': prompt,
            'completionTokens': completion,
            'totalTokens': prompt + completion,
            'latencyMs': {'min': latencies[0], 'median': latencies[len(latencies) // 2],
                          'max': latencies[-1]} if latencies else None,
        }
