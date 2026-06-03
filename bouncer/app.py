# Authors: Phan Nguyen Huu Phuoc (2212720), Pham Vo Quang Minh (2111762)
import json
import os
import uuid
import logging
import time
from datetime import datetime, timezone
from collections import defaultdict
import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger()
logger.setLevel(logging.INFO)

lambda_client = boto3.client('lambda')
ssm           = boto3.client('ssm')
dynamodb      = boto3.resource('dynamodb')

CODE_RUNNER_FUNCTION_NAME = os.environ['CODE_RUNNER_FUNCTION_NAME']
SSM_KEY_PARAM  = os.environ['SSM_KEY_PARAM']   # e.g. /code-exec/encryption-key
HISTORY_TABLE  = os.environ.get('HISTORY_TABLE', 'code-exec-submissions')

SUPPORTED_LANGUAGES = {'python3', 'cpp', 'java'}

_rate_store: dict[str, list[float]] = defaultdict(list)
RATE_LIMIT_MAX    = 10
RATE_LIMIT_WINDOW = 60

# Cache the encryption key in-memory for the lifetime of the warm Lambda
_cached_key: str | None = None

PROBLEMS = [
    {'id': 'hello_world',    'title': 'Hello World',        'difficulty': 'Easy',   'description': 'Print "Hello, World!" (exactly).', 'sample_input': '', 'sample_output': 'Hello, World!'},
    {'id': 'sum_two',        'title': 'Sum of Two Numbers', 'difficulty': 'Easy',   'description': 'Read two integers, print their sum.', 'sample_input': '3 7', 'sample_output': '10'},
    {'id': 'fibonacci',      'title': 'Fibonacci Number',   'difficulty': 'Easy',   'description': 'Read n (1≤n≤30), print nth Fibonacci.', 'sample_input': '6', 'sample_output': '8'},
    {'id': 'prime_check',    'title': 'Prime Check',        'difficulty': 'Medium', 'description': 'Read n, print YES if prime, NO otherwise.', 'sample_input': '17', 'sample_output': 'YES'},
    {'id': 'reverse_string', 'title': 'Reverse String',     'difficulty': 'Easy',   'description': 'Read a string, print it reversed.', 'sample_input': 'hello', 'sample_output': 'olleh'},
    {'id': 'sort_array',     'title': 'Sort Array',         'difficulty': 'Medium', 'description': 'Read n then n integers, print sorted ascending.', 'sample_input': '5\n3 1 4 1 5', 'sample_output': '1 1 3 4 5'},
]
PROBLEMS_BY_ID = {p['id']: p for p in PROBLEMS}


def lambda_handler(event, context):
    method = event.get('httpMethod', '')
    path   = event.get('path', '')

    if method == 'OPTIONS':
        return _cors(200, {})

    if   path == '/problems' and method == 'GET':  return _cors(200, {'problems': PROBLEMS})
    elif path == '/submit'   and method == 'POST': return _handle_submit(event)
    elif path == '/history'  and method == 'GET':  return _handle_history(event)
    elif path == '/stats'    and method == 'GET':  return _handle_stats(event)

    return _cors(404, {'error': 'Not found'})


# ──────────────────────────── HELPERS ────────────────────────────

def _get_encryption_key() -> str:
    global _cached_key
    if _cached_key:
        return _cached_key
    resp = ssm.get_parameter(Name=SSM_KEY_PARAM, WithDecryption=True)
    _cached_key = resp['Parameter']['Value']
    return _cached_key


def _check_rate_limit(ip: str) -> bool:
    now   = time.time()
    calls = _rate_store[ip]
    calls[:] = [t for t in calls if now - t < RATE_LIMIT_WINDOW]
    if len(calls) >= RATE_LIMIT_MAX:
        return False
    calls.append(now)
    return True


# ──────────────────────────── ROUTES ────────────────────────────

def _handle_submit(event):
    ip = (event.get('requestContext', {}).get('identity', {}) or {}).get('sourceIp', '0.0.0.0')
    if not _check_rate_limit(ip):
        return _cors(429, {'error': 'Rate limit exceeded (10 submissions/min)'})

    try:
        body = json.loads(event.get('body') or '{}')
    except json.JSONDecodeError:
        return _cors(400, {'error': 'Invalid JSON'})

    language    = body.get('language', '').lower().strip()
    source_code = body.get('source_code', '').strip()
    problem_id  = body.get('problem_id', '').strip()

    if language not in SUPPORTED_LANGUAGES:
        return _cors(400, {'error': f'Unsupported language. Choose: {", ".join(SUPPORTED_LANGUAGES)}'})
    if not source_code:
        return _cors(400, {'error': 'source_code is required'})
    if not problem_id:
        return _cors(400, {'error': 'problem_id is required'})

    try:
        encryption_key = _get_encryption_key()
    except Exception as e:
        logger.error('SSM fetch failed: %s', e)
        return _cors(500, {'error': 'Internal server error (key fetch)'})

    payload = {
        'language':       language,
        'source_code':    source_code,
        'problem_id':     problem_id,
        'encryption_key': encryption_key,
    }

    try:
        resp   = lambda_client.invoke(
            FunctionName=CODE_RUNNER_FUNCTION_NAME,
            InvocationType='RequestResponse',
            Payload=json.dumps(payload),
        )
        result = json.loads(resp['Payload'].read())
        if resp.get('FunctionError'):
            logger.error('CodeRunner error: %s', result)
            return _cors(500, {'error': 'Execution engine error'})
    except Exception as e:
        logger.error('CodeRunner invoke failed: %s', e)
        return _cors(500, {'error': 'Internal server error (invoke)'})

    # Persist to DynamoDB
    try:
        dynamodb.Table(HISTORY_TABLE).put_item(Item={
            'id':            str(uuid.uuid4()),
            'timestamp':     datetime.now(timezone.utc).isoformat(),
            'problem_id':    problem_id,
            'problem_title': PROBLEMS_BY_ID.get(problem_id, {}).get('title', problem_id),
            'language':      language,
            'status':        result.get('status', 'UNKNOWN'),
            'passed':        result.get('passed', 0),
            'total':         result.get('total', 0),
        })
    except Exception as e:
        logger.warning('DynamoDB write failed: %s', e)

    return _cors(200, result)


def _handle_history(event):
    params = event.get('queryStringParameters') or {}
    limit  = min(int(params.get('limit', 50)), 200)
    try:
        resp  = dynamodb.Table(HISTORY_TABLE).scan(Limit=limit * 2)
        items = sorted(resp.get('Items', []), key=lambda x: x.get('timestamp', ''), reverse=True)
        return _cors(200, {'submissions': items[:limit]})
    except Exception as e:
        logger.error('History scan failed: %s', e)
        return _cors(500, {'error': 'Could not load history'})


def _handle_stats(event):
    try:
        items = dynamodb.Table(HISTORY_TABLE).scan().get('Items', [])
    except Exception as e:
        logger.error('Stats scan failed: %s', e)
        return _cors(500, {'error': 'Could not load stats'})

    stats = {p['id']: {'title': p['title'], 'total': 0, 'accepted': 0, 'acceptance_rate': 0.0}
             for p in PROBLEMS}
    for sub in items:
        pid = sub.get('problem_id', '')
        if pid in stats:
            stats[pid]['total'] += 1
            if sub.get('status') == 'ACCEPTED':
                stats[pid]['accepted'] += 1
    for s in stats.values():
        if s['total'] > 0:
            s['acceptance_rate'] = round(s['accepted'] / s['total'] * 100, 1)
    return _cors(200, {'stats': stats})


def _cors(status, body):
    return {
        'statusCode': status,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin':  '*',
            'Access-Control-Allow-Headers': 'Content-Type',
            'Access-Control-Allow-Methods': 'GET,POST,OPTIONS',
        },
        'body': json.dumps(body),
    }
