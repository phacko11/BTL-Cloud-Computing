"""
Local demo server — simulates the full AWS pipeline without cloud costs.

Architecture mirrored:
  POST /submit        → Bouncer → CodeRunner (with timeout + cleanup)
  GET  /problems      → Bouncer (static list)
  GET  /history       → Bouncer → DynamoDB (local: history.json)
  GET  /stats         → aggregated submission statistics
  POST /admin/upload  → Sync Lambda simulation (unzip → encrypt → EFS)

Usage:
    pip install flask cryptography
    python server.py          # auto-setup + start
    python server.py setup    # re-encrypt problems only
"""

import sys
import json
import os
import uuid
import gzip
import shutil
import subprocess
import tempfile
import zipfile
import io
import time
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict
from cryptography.fernet import Fernet, InvalidToken

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────────
BASE_DIR       = Path(__file__).parent
EFS_DIR        = BASE_DIR / 'efs'
SECRETS_FILE   = BASE_DIR / 'secrets.json'
HISTORY_FILE   = BASE_DIR / 'history.json'
PROBLEMS_DIR   = BASE_DIR.parent / 'problems'

EXECUTION_TIMEOUT   = 5.0
MAX_OUTPUT_BYTES    = 64 * 1024

# Use the current interpreter — avoids Windows Store 'python3' stub
PYTHON_EXE = sys.executable

# Simple in-memory rate limiter: max 10 submissions/min per IP
_rate_limit_store: dict[str, list[float]] = defaultdict(list)
_rate_lock = threading.Lock()
RATE_LIMIT_MAX    = 10
RATE_LIMIT_WINDOW = 60  # seconds

# ── Problem metadata ───────────────────────────────────────────────────────
PROBLEMS_META = [
    {
        'id': 'hello_world',
        'title': 'Hello World',
        'difficulty': 'Easy',
        'description': 'Print the string "Hello, World!" (exactly, with comma and exclamation mark).',
        'constraints': 'No input.',
        'sample_input': '',
        'sample_output': 'Hello, World!',
    },
    {
        'id': 'sum_two',
        'title': 'Sum of Two Numbers',
        'difficulty': 'Easy',
        'description': 'Read two integers a and b on a single line separated by a space. Print their sum.',
        'constraints': '-10⁹ ≤ a, b ≤ 10⁹',
        'sample_input': '3 7',
        'sample_output': '10',
    },
    {
        'id': 'fibonacci',
        'title': 'Fibonacci Number',
        'difficulty': 'Easy',
        'description': 'Read integer n, print the nth Fibonacci number (F₁=1, F₂=1).',
        'constraints': '1 ≤ n ≤ 30',
        'sample_input': '6',
        'sample_output': '8',
    },
    {
        'id': 'prime_check',
        'title': 'Prime Check',
        'difficulty': 'Medium',
        'description': 'Read integer n. Print "YES" if n is prime, "NO" otherwise.',
        'constraints': '2 ≤ n ≤ 10⁶',
        'sample_input': '17',
        'sample_output': 'YES',
    },
    {
        'id': 'reverse_string',
        'title': 'Reverse String',
        'difficulty': 'Easy',
        'description': 'Read a string s. Print it reversed.',
        'constraints': '1 ≤ |s| ≤ 10⁵',
        'sample_input': 'hello',
        'sample_output': 'olleh',
    },
    {
        'id': 'sort_array',
        'title': 'Sort Array',
        'difficulty': 'Medium',
        'description': 'Read integer n, then n space-separated integers. Print them sorted in ascending order, space-separated.',
        'constraints': '1 ≤ n ≤ 10⁵, -10⁹ ≤ aᵢ ≤ 10⁹',
        'sample_input': '5\n3 1 4 1 5',
        'sample_output': '1 1 3 4 5',
    },
]

PROBLEMS_BY_ID = {p['id']: p for p in PROBLEMS_META}

# ── Secret / Key management ────────────────────────────────────────────────

def _get_or_create_key() -> str:
    if SECRETS_FILE.exists():
        return json.loads(SECRETS_FILE.read_text())['encryption_key']
    key = Fernet.generate_key().decode()
    SECRETS_FILE.write_text(json.dumps({'encryption_key': key}, indent=2))
    logger.info('Generated new Fernet key → %s', SECRETS_FILE)
    return key


# ── History (local DynamoDB simulation) ───────────────────────────────────

_history_lock = threading.Lock()

def _load_history() -> list:
    if not HISTORY_FILE.exists():
        return []
    try:
        return json.loads(HISTORY_FILE.read_text())
    except Exception:
        return []

def _save_submission(record: dict):
    with _history_lock:
        history = _load_history()
        history.insert(0, record)
        history = history[:500]  # keep last 500
        HISTORY_FILE.write_text(json.dumps(history, indent=2, ensure_ascii=False))


# ── Problem setup (Sync Lambda simulation) ────────────────────────────────

def setup_problems():
    EFS_DIR.mkdir(exist_ok=True)
    key = _get_or_create_key()
    fernet = Fernet(key.encode())

    if not PROBLEMS_DIR.exists():
        logger.error('Problems directory not found: %s', PROBLEMS_DIR)
        return

    count = 0
    for problem_dir in sorted(PROBLEMS_DIR.iterdir()):
        if not problem_dir.is_dir():
            continue
        problem_id = problem_dir.name
        input_files = sorted(problem_dir.glob('*.in'))
        test_cases = []
        for inp in input_files:
            out = inp.with_suffix('.out')
            if out.exists():
                test_cases.append({
                    'input': inp.read_text(encoding='utf-8'),
                    'output': out.read_text(encoding='utf-8').strip(),
                })

        if not test_cases:
            logger.warning('No test cases in %s — skipping', problem_id)
            continue

        raw = json.dumps(test_cases).encode('utf-8')
        compressed = gzip.compress(raw)
        encrypted = fernet.encrypt(compressed)
        enc_path = EFS_DIR / f'{problem_id}.enc'
        enc_path.write_bytes(encrypted)
        logger.info('Synced "%s" → %d test cases → %s', problem_id, len(test_cases), enc_path.name)
        count += 1

    logger.info('Setup complete: %d problems ready in %s', count, EFS_DIR)


def sync_from_zip(zip_bytes: bytes, problem_id: str):
    """Sync Lambda: parse zip → encrypt → write to EFS."""
    key = _get_or_create_key()
    fernet = Fernet(key.encode())

    test_cases = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = set(zf.namelist())
        for inp in sorted(n for n in names if n.endswith('.in')):
            out = inp[:-3] + '.out'
            if out in names:
                test_cases.append({
                    'input': zf.read(inp).decode('utf-8'),
                    'output': zf.read(out).decode('utf-8').strip(),
                })

    if not test_cases:
        raise ValueError('No valid .in/.out pairs found in zip')

    raw = json.dumps(test_cases).encode('utf-8')
    encrypted = fernet.encrypt(gzip.compress(raw))
    EFS_DIR.mkdir(exist_ok=True)
    (EFS_DIR / f'{problem_id}.enc').write_bytes(encrypted)
    logger.info('Admin upload: "%s" → %d test cases', problem_id, len(test_cases))
    return len(test_cases)


# ── CodeRunner ────────────────────────────────────────────────────────────

def load_test_cases(problem_id: str) -> list:
    path = EFS_DIR / f'{problem_id}.enc'
    if not path.exists():
        raise FileNotFoundError(f'No encrypted test file for: {problem_id}')
    key = _get_or_create_key()
    fernet = Fernet(key.encode())
    compressed = fernet.decrypt(path.read_bytes())
    return json.loads(gzip.decompress(compressed).decode('utf-8'))


def run_code(language: str, source_code: str, test_cases: list) -> dict:
    work_dir = tempfile.mkdtemp(prefix='exec_')
    try:
        if language == 'python3':
            return _run_python(source_code, test_cases, work_dir)
        elif language == 'cpp':
            return _run_cpp(source_code, test_cases, work_dir)
        elif language == 'java':
            return _run_java(source_code, test_cases, work_dir)
        return {'status': 'ERROR', 'error': f'Unsupported language: {language}',
                'passed': 0, 'total': len(test_cases), 'results': []}
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _run_python(code: str, test_cases: list, work_dir: str) -> dict:
    src = Path(work_dir) / 'solution.py'
    src.write_text(code, encoding='utf-8')
    return _run_all(test_cases, [PYTHON_EXE, str(src)])


def _run_cpp(code: str, test_cases: list, work_dir: str) -> dict:
    src = Path(work_dir) / 'solution.cpp'
    exe = Path(work_dir) / ('solution.exe' if os.name == 'nt' else 'solution')
    src.write_text(code, encoding='utf-8')

    try:
        cp = subprocess.run(
            ['g++', '-O2', '-std=c++17', '-o', str(exe), str(src)],
            capture_output=True, text=True, timeout=30,
        )
    except FileNotFoundError:
        return {'status': 'ERROR', 'error': 'g++ not found. Install MinGW-w64.',
                'passed': 0, 'total': len(test_cases), 'results': []}
    except subprocess.TimeoutExpired:
        return {'status': 'COMPILATION_ERROR', 'error': 'Compilation timed out.',
                'passed': 0, 'total': len(test_cases), 'results': []}

    if cp.returncode != 0:
        return {'status': 'COMPILATION_ERROR', 'error': cp.stderr[:2048],
                'passed': 0, 'total': len(test_cases), 'results': []}

    return _run_all(test_cases, [str(exe)])


def _run_java(code: str, test_cases: list, work_dir: str) -> dict:
    # Java requires the filename to match the public class name
    # We enforce class name "Solution"
    if 'class Solution' not in code:
        return {'status': 'COMPILATION_ERROR',
                'error': 'Java class must be named "Solution" (public class Solution {...})',
                'passed': 0, 'total': len(test_cases), 'results': []}

    src = Path(work_dir) / 'Solution.java'
    src.write_text(code, encoding='utf-8')

    try:
        cp = subprocess.run(
            ['javac', str(src)],
            capture_output=True, text=True, timeout=30, cwd=work_dir,
        )
    except FileNotFoundError:
        return {'status': 'ERROR', 'error': 'javac not found. Install JDK.',
                'passed': 0, 'total': len(test_cases), 'results': []}
    except subprocess.TimeoutExpired:
        return {'status': 'COMPILATION_ERROR', 'error': 'Compilation timed out.',
                'passed': 0, 'total': len(test_cases), 'results': []}

    if cp.returncode != 0:
        return {'status': 'COMPILATION_ERROR', 'error': cp.stderr[:2048],
                'passed': 0, 'total': len(test_cases), 'results': []}

    return _run_all(test_cases, ['java', '-cp', work_dir, 'Solution'])


def _run_all(test_cases: list, cmd: list) -> dict:
    results = []
    passed = 0
    for i, tc in enumerate(test_cases):
        expected = tc.get('output', '').strip()
        try:
            proc = subprocess.run(
                cmd,
                input=tc.get('input', ''),
                capture_output=True, text=True,
                timeout=EXECUTION_TIMEOUT,
            )
            actual = proc.stdout[:MAX_OUTPUT_BYTES].strip()
            stderr = proc.stderr[:512].strip() if proc.stderr else ''

            if actual == expected:
                status = 'ACCEPTED'
                passed += 1
            elif proc.returncode != 0 and not actual:
                status = 'RUNTIME_ERROR'
            else:
                status = 'WRONG_ANSWER'

            results.append({
                'test_case': i + 1,
                'status': status,
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


# ── Single execution (for /run endpoint) ──────────────────────────────────

def _exec_once(language: str, source_code: str, stdin_input: str, work_dir: str):
    """Compile (if needed) and run once with given stdin. Returns (status, stdout, stderr)."""
    try:
        if language == 'python3':
            src = Path(work_dir) / 'solution.py'
            src.write_text(source_code, encoding='utf-8')
            cmd = [PYTHON_EXE, str(src)]
        elif language == 'cpp':
            src = Path(work_dir) / 'solution.cpp'
            exe = Path(work_dir) / ('solution.exe' if os.name == 'nt' else 'solution')
            src.write_text(source_code, encoding='utf-8')
            try:
                cp = subprocess.run(['g++', '-O2', '-std=c++17', '-o', str(exe), str(src)],
                                    capture_output=True, text=True, timeout=30)
            except FileNotFoundError:
                return 'ERROR', '', 'g++ not found. Install MinGW-w64.'
            if cp.returncode != 0:
                return 'COMPILATION_ERROR', '', cp.stderr[:2048]
            cmd = [str(exe)]
        else:  # java
            if 'class Solution' not in source_code:
                return 'COMPILATION_ERROR', '', 'Java class must be named "Solution"'
            src = Path(work_dir) / 'Solution.java'
            src.write_text(source_code, encoding='utf-8')
            try:
                cp = subprocess.run(['javac', str(src)], capture_output=True, text=True,
                                    timeout=30, cwd=work_dir)
            except FileNotFoundError:
                return 'ERROR', '', 'javac not found. Install JDK.'
            if cp.returncode != 0:
                return 'COMPILATION_ERROR', '', cp.stderr[:2048]
            cmd = ['java', '-cp', work_dir, 'Solution']

        proc = subprocess.run(cmd, input=stdin_input, capture_output=True, text=True,
                              timeout=EXECUTION_TIMEOUT)
        stdout = proc.stdout[:MAX_OUTPUT_BYTES]
        stderr = proc.stderr[:512].strip() if proc.stderr else ''
        if proc.returncode != 0:
            return 'RUNTIME_ERROR', stdout, stderr
        return 'OK', stdout, stderr
    except subprocess.TimeoutExpired:
        return 'TIME_LIMIT_EXCEEDED', '', ''


# ── Rate limiter ───────────────────────────────────────────────────────────

def _check_rate_limit(ip: str) -> bool:
    now = time.time()
    with _rate_lock:
        calls = _rate_limit_store[ip]
        calls = [t for t in calls if now - t < RATE_LIMIT_WINDOW]
        _rate_limit_store[ip] = calls
        if len(calls) >= RATE_LIMIT_MAX:
            return False
        calls.append(now)
        return True


# ── Flask application ──────────────────────────────────────────────────────

def make_app():
    from flask import Flask, request, jsonify

    app = Flask(__name__)
    app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16 MB upload limit

    def cors(resp):
        resp.headers['Access-Control-Allow-Origin']  = '*'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        resp.headers['Access-Control-Allow-Methods'] = 'GET,POST,OPTIONS'
        return resp

    @app.after_request
    def after(resp):
        return cors(resp)

    # ── Problems ──
    @app.route('/problems', methods=['GET', 'OPTIONS'])
    def get_problems():
        return jsonify({'problems': PROBLEMS_META})

    # ── Submit ──
    @app.route('/submit', methods=['POST', 'OPTIONS'])
    def submit():
        if request.method == 'OPTIONS':
            return jsonify({})

        ip = request.remote_addr or '127.0.0.1'
        if not _check_rate_limit(ip):
            return jsonify({'error': 'Rate limit exceeded (10 submissions/min)'}), 429

        data = request.get_json(force=True, silent=True) or {}
        language   = data.get('language', '').lower().strip()
        source_code = data.get('source_code', '').strip()
        problem_id  = data.get('problem_id', '').strip()

        if language not in ('python3', 'cpp', 'java'):
            return jsonify({'error': 'Unsupported language. Choose: python3, cpp, java'}), 400
        if not source_code:
            return jsonify({'error': 'source_code is required'}), 400
        if not problem_id:
            return jsonify({'error': 'problem_id is required'}), 400

        try:
            test_cases = load_test_cases(problem_id)
        except FileNotFoundError:
            return jsonify({'error': f'Problem "{problem_id}" not found. Run: python server.py setup'}), 404
        except InvalidToken:
            return jsonify({'error': 'Decryption failed (bad key)'}), 500

        result = run_code(language, source_code, test_cases)

        # Persist to history (DynamoDB simulation)
        problem_meta = PROBLEMS_BY_ID.get(problem_id, {})
        record = {
            'id':            str(uuid.uuid4()),
            'timestamp':     datetime.now(timezone.utc).isoformat(),
            'problem_id':    problem_id,
            'problem_title': problem_meta.get('title', problem_id),
            'language':      language,
            'status':        result.get('status'),
            'passed':        result.get('passed', 0),
            'total':         result.get('total', 0),
            'source_code':   source_code,
        }
        _save_submission(record)

        return jsonify(result)

    # ── History ──
    @app.route('/history', methods=['GET', 'OPTIONS'])
    def get_history():
        history = _load_history()
        limit = min(int(request.args.get('limit', 50)), 200)
        problem_filter = request.args.get('problem_id')
        if problem_filter:
            history = [h for h in history if h.get('problem_id') == problem_filter]
        return jsonify({'submissions': history[:limit]})

    # ── Statistics ──
    @app.route('/stats', methods=['GET', 'OPTIONS'])
    def get_stats():
        history = _load_history()
        stats: dict[str, dict] = {}

        for p in PROBLEMS_META:
            stats[p['id']] = {'title': p['title'], 'total': 0, 'accepted': 0, 'acceptance_rate': 0.0}

        for sub in history:
            pid = sub.get('problem_id', '')
            if pid in stats:
                stats[pid]['total'] += 1
                if sub.get('status') == 'ACCEPTED':
                    stats[pid]['accepted'] += 1

        for pid, s in stats.items():
            if s['total'] > 0:
                s['acceptance_rate'] = round(s['accepted'] / s['total'] * 100, 1)

        return jsonify({'stats': stats})

    # ── Run (free execution with custom stdin) ──
    @app.route('/run', methods=['POST', 'OPTIONS'])
    def run_free():
        if request.method == 'OPTIONS':
            return jsonify({})

        ip = request.remote_addr or '127.0.0.1'
        if not _check_rate_limit(ip):
            return jsonify({'error': 'Rate limit exceeded (10 runs/min)'}), 429

        data = request.get_json(force=True, silent=True) or {}
        language    = data.get('language', '').lower().strip()
        source_code = data.get('source_code', '').strip()
        stdin_input = data.get('stdin', '')

        if language not in ('python3', 'cpp', 'java'):
            return jsonify({'error': 'Unsupported language'}), 400
        if not source_code:
            return jsonify({'error': 'source_code is required'}), 400

        work_dir = tempfile.mkdtemp(prefix='exec_')
        t0 = time.time()
        try:
            status, output, stderr = _exec_once(language, source_code, stdin_input, work_dir)
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
        elapsed = round((time.time() - t0) * 1000)

        record = {
            'id': str(uuid.uuid4()),
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'problem_id': '__free_run__',
            'problem_title': 'Free Run',
            'language': language,
            'status': status,
            'passed': 0, 'total': 0,
            'source_code': source_code,
        }
        _save_submission(record)

        return jsonify({'status': status, 'output': output, 'stderr': stderr, 'time_ms': elapsed})

    # ── Admin: upload problem zip ──
    @app.route('/admin/upload', methods=['POST', 'OPTIONS'])
    def admin_upload():
        if request.method == 'OPTIONS':
            return jsonify({})

        problem_id = request.form.get('problem_id', '').strip()
        if not problem_id:
            return jsonify({'error': 'problem_id is required'}), 400
        if 'file' not in request.files:
            return jsonify({'error': 'No file uploaded'}), 400

        zip_file = request.files['file']
        if not zip_file.filename.endswith('.zip'):
            return jsonify({'error': 'File must be a .zip archive'}), 400

        zip_bytes = zip_file.read()
        try:
            n = sync_from_zip(zip_bytes, problem_id)
        except (zipfile.BadZipFile, ValueError) as e:
            return jsonify({'error': str(e)}), 400

        return jsonify({'message': f'Problem "{problem_id}" uploaded with {n} test cases.'})

    return app


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'setup':
        setup_problems()
    else:
        if not EFS_DIR.exists() or not any(EFS_DIR.glob('*.enc')):
            logger.info('First run — setting up encrypted problems...')
            setup_problems()
        app = make_app()
        logger.info('Local demo server: http://localhost:5000')
        logger.info('Open frontend/index.html in your browser.')
        app.run(host='0.0.0.0', port=5000, debug=False)
