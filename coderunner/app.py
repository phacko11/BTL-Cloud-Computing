import json
import os
import subprocess
import shutil
import tempfile
import logging
import gzip
from pathlib import Path
from cryptography.fernet import Fernet, InvalidToken
import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client('s3')

TEST_CASES_BUCKET  = os.environ['TEST_CASES_BUCKET']
EXECUTION_TIMEOUT  = float(os.environ.get('EXECUTION_TIMEOUT', '5'))
MAX_OUTPUT_BYTES   = 64 * 1024  # 64 KB


def lambda_handler(event, context):
    language       = event.get('language')
    source_code    = event.get('source_code')
    problem_id     = event.get('problem_id')
    encryption_key = event.get('encryption_key')

    if not all([language, source_code, problem_id, encryption_key]):
        return {'status': 'ERROR', 'error': 'Missing required fields'}

    try:
        test_cases = _load_test_cases(problem_id, encryption_key)
    except FileNotFoundError:
        return {'status': 'ERROR', 'error': f'Problem not found: {problem_id}'}
    except InvalidToken:
        return {'status': 'ERROR', 'error': 'Decryption failed (bad key)'}
    except Exception as e:
        logger.error('load_test_cases failed: %s', e)
        return {'status': 'ERROR', 'error': 'Could not load test cases'}

    work_dir = tempfile.mkdtemp(prefix='exec_')
    try:
        return _execute(language, source_code, test_cases, work_dir)
    finally:
        _cleanup(work_dir)


# ──────────────────────────── LOAD FROM S3 ────────────────────────────

def _load_test_cases(problem_id: str, encryption_key: str) -> list:
    s3_key = f'testcases/{problem_id}.enc'
    try:
        response  = s3.get_object(Bucket=TEST_CASES_BUCKET, Key=s3_key)
        encrypted = response['Body'].read()
    except ClientError as e:
        if e.response['Error']['Code'] in ('NoSuchKey', '404'):
            raise FileNotFoundError(problem_id)
        raise

    # Decrypt + decompress entirely in RAM — never touch disk
    fernet     = Fernet(encryption_key.encode())
    compressed = fernet.decrypt(encrypted)
    return json.loads(gzip.decompress(compressed).decode('utf-8'))


# ──────────────────────────── EXECUTION ────────────────────────────

def _execute(language: str, source_code: str, test_cases: list, work_dir: str) -> dict:
    if language == 'python3':
        return _run_python(source_code, test_cases, work_dir)
    elif language == 'cpp':
        return _run_cpp(source_code, test_cases, work_dir)
    elif language == 'java':
        return _run_java(source_code, test_cases, work_dir)
    return {'status': 'ERROR', 'error': f'Unsupported language: {language}'}


def _run_python(code: str, test_cases: list, work_dir: str) -> dict:
    src = os.path.join(work_dir, 'solution.py')
    with open(src, 'w') as f:
        f.write(code)
    return _run_all(test_cases, ['python3', src])


def _run_cpp(code: str, test_cases: list, work_dir: str) -> dict:
    src = os.path.join(work_dir, 'solution.cpp')
    exe = os.path.join(work_dir, 'solution')
    with open(src, 'w') as f:
        f.write(code)

    try:
        cp = subprocess.run(
            ['g++', '-O2', '-std=c++17', '-o', exe, src],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return {'status': 'COMPILATION_ERROR', 'error': 'Compilation timed out.',
                'passed': 0, 'total': len(test_cases), 'results': []}

    if cp.returncode != 0:
        return {'status': 'COMPILATION_ERROR', 'error': cp.stderr[:2048],
                'passed': 0, 'total': len(test_cases), 'results': []}
    return _run_all(test_cases, [exe])


def _run_java(code: str, test_cases: list, work_dir: str) -> dict:
    if 'class Solution' not in code:
        return {'status': 'COMPILATION_ERROR',
                'error': 'Java class must be named "Solution".',
                'passed': 0, 'total': len(test_cases), 'results': []}

    src = os.path.join(work_dir, 'Solution.java')
    with open(src, 'w') as f:
        f.write(code)

    try:
        cp = subprocess.run(
            ['javac', src], capture_output=True, text=True, timeout=30, cwd=work_dir,
        )
    except subprocess.TimeoutExpired:
        return {'status': 'COMPILATION_ERROR', 'error': 'Compilation timed out.',
                'passed': 0, 'total': len(test_cases), 'results': []}

    if cp.returncode != 0:
        return {'status': 'COMPILATION_ERROR', 'error': cp.stderr[:2048],
                'passed': 0, 'total': len(test_cases), 'results': []}
    return _run_all(test_cases, ['java', '-cp', work_dir, 'Solution'])


def _run_all(test_cases: list, cmd: list) -> dict:
    results = []
    passed  = 0
    for i, tc in enumerate(test_cases):
        expected = tc.get('output', '').strip()
        try:
            proc   = subprocess.run(cmd, input=tc.get('input', ''), capture_output=True,
                                    text=True, timeout=EXECUTION_TIMEOUT)
            actual = proc.stdout[:MAX_OUTPUT_BYTES].strip()
            stderr = proc.stderr[:512].strip() if proc.stderr else ''

            if actual == expected:
                status, passed = 'ACCEPTED', passed + 1
            elif proc.returncode != 0 and not actual:
                status = 'RUNTIME_ERROR'
            else:
                status = 'WRONG_ANSWER'

            results.append({
                'test_case': i + 1, 'status': status,
                'expected': expected if status != 'ACCEPTED' else None,
                'actual':   actual   if status != 'ACCEPTED' else None,
                'stderr':   stderr or None,
            })
        except subprocess.TimeoutExpired:
            results.append({'test_case': i + 1, 'status': 'TIME_LIMIT_EXCEEDED'})

    statuses = {r['status'] for r in results}
    if   'TIME_LIMIT_EXCEEDED' in statuses: overall = 'TIME_LIMIT_EXCEEDED'
    elif 'RUNTIME_ERROR'        in statuses: overall = 'RUNTIME_ERROR'
    elif passed == len(test_cases):          overall = 'ACCEPTED'
    else:                                    overall = 'WRONG_ANSWER'

    return {'status': overall, 'passed': passed, 'total': len(test_cases), 'results': results}


# ──────────────────────────── CLEANUP ────────────────────────────

def _cleanup(work_dir: str):
    try:
        shutil.rmtree(work_dir, ignore_errors=False)
    except Exception as e:
        logger.error('CRITICAL: cleanup failed for %s: %s', work_dir, e)
        raise RuntimeError('Sandbox cleanup failed — forcing cold start') from e

    try:
        for item in Path('/tmp').iterdir():
            if item.name.startswith('exec_') or item.suffix in ('.py', '.cpp', '.java', '.class', '.out'):
                shutil.rmtree(item) if item.is_dir() else item.unlink()
    except Exception as e:
        logger.warning('Stray-file sweep failed: %s', e)
