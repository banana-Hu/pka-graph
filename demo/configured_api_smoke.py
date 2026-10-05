"""Bounded, synthetic connectivity test. Never prints or persists credentials."""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pka_graph.local_config import load_local_ai_config


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api-base')
    parser.add_argument('--model')
    parser.add_argument('--report', default='configured_api_smoke.json')
    args = parser.parse_args()
    if Path(args.report).name != args.report or not args.report.endswith('.json'):
        parser.error('--report must be a JSON filename, not a path')
    load_local_ai_config(ROOT / 'ai.local.json')
    base = (args.api_base or os.environ['PKA_QUIZ_API_BASE']).rstrip('/')
    if not base.startswith('https://'):  # 公开版：仅要求 https 端点，不再绑定特定中转站
        parser.error('only the user-approved relay host is allowed')
    model = args.model or os.environ['PKA_QUIZ_MODEL']
    key = os.environ['PKA_QUIZ_API_KEY']
    report = {'time': datetime.now(timezone.utc).isoformat(), 'model': model,
              'configured_model': os.environ['PKA_QUIZ_MODEL'],
              'configured_base': os.environ['PKA_QUIZ_API_BASE'],
              'synthetic_only': True, 'attempts': [], 'working_base': None}
    opener = urllib.request.build_opener(NoRedirect())
    candidates = [base] + ([base + '/v1'] if not base.endswith('/v1') else [])
    for candidate in candidates:
        url = candidate + '/chat/completions'
        payload = {'model': model, 'messages': [
            {'role': 'system', 'content': 'Reply with JSON only. Do not include reasoning.'},
            {'role': 'user', 'content': 'Connectivity test. Return {"ok":true}.'}],
            'max_tokens': 512, 'stream': False}
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
            headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key})
        start = time.monotonic()
        item = {'endpoint': url}
        try:
            with opener.open(req, timeout=45) as resp:
                item['http_status'] = resp.status
                item['content_type'] = resp.headers.get('Content-Type', '')
                raw = resp.read(1_000_001)
            try:
                data = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                data = None
            if isinstance(data, dict) and data.get('choices'):
                choice = data['choices'][0]
                message = choice.get('message') or {}
                content = message.get('content')
                item.update(result='completion_received', content_nonempty=bool(content),
                            finish_reason=choice.get('finish_reason'))
                usage = data.get('usage') or {}
                item['usage'] = {k: v for k, v in usage.items() if isinstance(v, (int, float))}
                item['response_model'] = str(data.get('model', ''))[:100].replace(key, '[redacted]')
                report['working_base'] = candidate
            else:
                item['result'] = 'unexpected_response_shape'
        except urllib.error.HTTPError as exc:
            item.update(http_status=exc.code, result='http_error')
            # Classify without echoing potentially sensitive response bodies.
            raw = exc.read(64000).decode('utf-8', errors='replace').lower()
            categories = []
            for label, needles in (
                ('authentication', ['invalid api key', 'invalid_api_key', 'unauthorized', '无效的令牌', '令牌不存在']),
                ('model_or_channel', ['model_not_found', 'no available channel', '无可用渠道', '模型不存在']),
                ('quota', ['insufficient_quota', '额度不足', '余额不足']),
            ):
                if any(n in raw for n in needles):
                    categories.append(label)
            item['error_categories'] = categories
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            item.update(result='network_error', exception_type=type(exc).__name__)
        item['elapsed_seconds'] = round(time.monotonic() - start, 2)
        report['attempts'].append(item)
        print(json.dumps(item, ensure_ascii=False), flush=True)
        if report['working_base'] or item.get('http_status') in (401, 403, 429):
            break
    out = ROOT / 'test_results' / args.report
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('REPORT=' + str(out), flush=True)


if __name__ == '__main__':
    main()
