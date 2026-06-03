# Authors: Phan Nguyen Huu Phuoc (2212720), Pham Vo Quang Minh (2111762)
"""
Local demo server — simulates the full AWS pipeline without cloud costs.

Usage:
    pip install flask cryptography flask-socketio
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

EXECUTION_TIMEOUT   = 30.0   # longer for interactive sessions
MAX_OUTPUT_BYTES    = 64 * 1024

PYTHON_EXE = sys.executable

_rate_limit_store: dict[str, list[float]] = defaultdict(list)
_rate_lock = threading.Lock()
RATE_LIMIT_MAX    = 300
RATE_LIMIT_WINDOW = 60

# ── Running processes (interactive terminal) ───────────────────────────────
_running_procs: dict[str, subprocess.Popen] = {}
_procs_lock = threading.Lock()

# ── Problem metadata ───────────────────────────────────────────────────────
PROBLEMS_META = [
    {
        'id': 'hello_world', 'title': 'Hello World', 'difficulty': 'Easy',
        'description': 'Print "Hello, World!"', 'constraints': 'No input.',
        'sample_input': '', 'sample_output': 'Hello, World!',
    },
    {
        'id': 'sum_two', 'title': 'Sum of Two Numbers', 'difficulty': 'Easy',
        'description': 'Read two integers a and b. Print their sum.',
        'constraints': '-10⁹ ≤ a, b ≤ 10⁹', 'sample_input': '3 7', 'sample_output': '10',
    },
    {
        'id': 'fibonacci', 'title': 'Fibonacci Number', 'difficulty': 'Easy',
        'description': 'Read n, print nth Fibonacci number (F₁=1, F₂=1).',
        'constraints': '1 ≤ n ≤ 30', 'sample_input': '6', 'sample_output': '8',
    },
    {
        'id': 'prime_check', 'title': 'Prime Check', 'difficulty': 'Medium',
        'description': 'Print YES if n is prime, NO otherwise.',
        'constraints': '2 ≤ n ≤ 10⁶', 'sample_input': '17', 'sample_output': 'YES',
    },
    {
        'id': 'reverse_string', 'title': 'Reverse String', 'difficulty': 'Easy',
        'description': 'Read a string s. Print it reversed.',
        'constraints': '1 ≤ |s| ≤ 10⁵', 'sample_input': 'hello', 'sample_output': 'olleh',
    },
    {
        'id': 'sort_array', 'title': 'Sort Array', 'difficulty': 'Medium',
        'description': 'Read n then n integers. Print sorted ascending.',
        'constraints': '1 ≤ n ≤ 10⁵', 'sample_input': '5\n3 1 4 1 5', 'sample_output': '1 1 3 4 5',
    },
]
PROBLEMS_BY_ID = {p['id']: p for p in PROBLEMS_META}

# ── Key management ─────────────────────────────────────────────────────────
def _get_or_create_key() -> str:
    if SECRETS_FILE.exists():
        return json.loads(SECRETS_FILE.read_text())['encryption_key']
    key = Fernet.generate_key().decode()
    SECRETS_FILE.write_text(json.dumps({'encryption_key': key}, indent=2))
    logger.info('Generated new Fernet key → %s', SECRETS_FILE)
    return key

# ── History ────────────────────────────────────────────────────────────────
_history_lock = threading.Lock()

def _load_history() -> list:
    if not HISTORY_FILE.exists():
        return []
    try:
        return json.loads(HISTORY_FILE.read_text(encoding='utf-8'))
    except Exception:
        return []

def _save_submission(record: dict):
    with _history_lock:
        history = _load_history()
        history.insert(0, record)
        HISTORY_FILE.write_text(
            json.dumps(history[:500], indent=2, ensure_ascii=False), encoding='utf-8')

# ── Problem setup ──────────────────────────────────────────────────────────
def setup_problems():
    EFS_DIR.mkdir(exist_ok=True)
    fernet = Fernet(_get_or_create_key().encode())
    if not PROBLEMS_DIR.exists():
        logger.error('Problems directory not found: %s', PROBLEMS_DIR)
        return
    count = 0
    for problem_dir in sorted(PROBLEMS_DIR.iterdir()):
        if not problem_dir.is_dir():
            continue
        test_cases = []
        for inp in sorted(problem_dir.glob('*.in')):
            out = inp.with_suffix('.out')
            if out.exists():
                test_cases.append({
                    'input': inp.read_text(encoding='utf-8'),
                    'output': out.read_text(encoding='utf-8').strip(),
                })
        if not test_cases:
            continue
        encrypted = fernet.encrypt(gzip.compress(json.dumps(test_cases).encode('utf-8')))
        (EFS_DIR / f'{problem_dir.name}.enc').write_bytes(encrypted)
        logger.info('Synced "%s" → %d test cases', problem_dir.name, len(test_cases))
        count += 1
    logger.info('Setup complete: %d problems ready', count)

def sync_from_zip(zip_bytes: bytes, problem_id: str):
    fernet = Fernet(_get_or_create_key().encode())
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
    encrypted = fernet.encrypt(gzip.compress(json.dumps(test_cases).encode('utf-8')))
    EFS_DIR.mkdir(exist_ok=True)
    (EFS_DIR / f'{problem_id}.enc').write_bytes(encrypted)
    logger.info('Admin upload: "%s" → %d test cases', problem_id, len(test_cases))
    return len(test_cases)

# ── Compile helper ─────────────────────────────────────────────────────────
def _compile(language: str, source_code: str, work_dir: str):
    """Returns (cmd, error_msg). error_msg is None on success."""
    env = os.environ.copy()
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUTF8'] = '1'

    if language == 'python3':
        src = Path(work_dir) / 'solution.py'
        src.write_text(source_code, encoding='utf-8')
        return [PYTHON_EXE, '-u', str(src)], None, env

    elif language == 'cpp':
        src = Path(work_dir) / 'solution.cpp'
        exe = Path(work_dir) / ('solution.exe' if os.name == 'nt' else 'solution')
        src.write_text(source_code, encoding='utf-8')
        try:
            cp = subprocess.run(['g++', '-O2', '-std=c++17', '-o', str(exe), str(src)],
                                capture_output=True, text=True, timeout=30)
        except FileNotFoundError:
            return None, 'g++ not found. Install MinGW-w64.', env
        if cp.returncode != 0:
            return None, cp.stderr[:2048], env
        return [str(exe)], None, env

    else:  # java
        if 'class Solution' not in source_code:
            return None, 'Java class must be named "Solution"', env
        src = Path(work_dir) / 'Solution.java'
        src.write_text(source_code, encoding='utf-8')
        try:
            cp = subprocess.run(['javac', str(src)], capture_output=True, text=True,
                                timeout=30, cwd=work_dir)
        except FileNotFoundError:
            return None, 'javac not found. Install JDK.', env
        if cp.returncode != 0:
            return None, cp.stderr[:2048], env
        return ['java', '-cp', work_dir, 'Solution'], None, env

# ── Batch run (for /run REST endpoint) ────────────────────────────────────
def _exec_once(language, source_code, stdin_input, work_dir):
    cmd, err, env = _compile(language, source_code, work_dir)
    if err:
        status = 'COMPILATION_ERROR' if language != 'python3' or 'not found' not in err else 'ERROR'
        return status, '', err
    try:
        proc = subprocess.run(cmd, input=stdin_input, capture_output=True, text=True,
                              timeout=EXECUTION_TIMEOUT, encoding='utf-8', env=env)
        stdout = proc.stdout[:MAX_OUTPUT_BYTES]
        stderr = proc.stderr[:512].strip() if proc.stderr else ''
        if proc.returncode != 0:
            return 'RUNTIME_ERROR', stdout, stderr
        return 'OK', stdout, stderr
    except subprocess.TimeoutExpired:
        return 'TIME_LIMIT_EXCEEDED', '', ''

# ── Batch code execution (test-case based) ────────────────────────────────
def _run_all(test_cases, cmd, env=None):
    results, passed = [], 0
    for i, tc in enumerate(test_cases):
        expected = tc.get('output', '').strip()
        try:
            proc = subprocess.run(cmd, input=tc.get('input', ''), capture_output=True,
                                  text=True, timeout=EXECUTION_TIMEOUT, env=env)
            actual = proc.stdout[:MAX_OUTPUT_BYTES].strip()
            stderr = proc.stderr[:512].strip() if proc.stderr else ''
            if actual == expected:
                status = 'ACCEPTED'; passed += 1
            elif proc.returncode != 0 and not actual:
                status = 'RUNTIME_ERROR'
            else:
                status = 'WRONG_ANSWER'
            results.append({'test_case': i+1, 'status': status,
                            'expected': expected if status != 'ACCEPTED' else None,
                            'actual': actual if status != 'ACCEPTED' else None,
                            'stderr': stderr or None})
        except subprocess.TimeoutExpired:
            results.append({'test_case': i+1, 'status': 'TIME_LIMIT_EXCEEDED'})
    statuses = {r['status'] for r in results}
    if 'TIME_LIMIT_EXCEEDED' in statuses: overall = 'TIME_LIMIT_EXCEEDED'
    elif 'RUNTIME_ERROR' in statuses: overall = 'RUNTIME_ERROR'
    elif passed == len(test_cases): overall = 'ACCEPTED'
    else: overall = 'WRONG_ANSWER'
    return {'status': overall, 'passed': passed, 'total': len(test_cases), 'results': results}

def run_code(language, source_code, test_cases):
    work_dir = tempfile.mkdtemp(prefix='exec_')
    try:
        cmd, err, env = _compile(language, source_code, work_dir)
        if err:
            return {'status': 'COMPILATION_ERROR', 'error': err,
                    'passed': 0, 'total': len(test_cases), 'results': []}
        return _run_all(test_cases, cmd, env)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

# ── Rate limiter ───────────────────────────────────────────────────────────
def _check_rate_limit(ip):
    now = time.time()
    with _rate_lock:
        calls = [t for t in _rate_limit_store[ip] if now - t < RATE_LIMIT_WINDOW]
        _rate_limit_store[ip] = calls
        if len(calls) >= RATE_LIMIT_MAX:
            return False
        calls.append(now)
        return True

# ── Flask + SocketIO application ───────────────────────────────────────────
def make_app():
    from flask import Flask, request, jsonify, send_file
    from flask_socketio import SocketIO, emit

    app = Flask(__name__)
    app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024
    app.config['SECRET_KEY'] = 'cee-local-demo'
    socketio = SocketIO(app, cors_allowed_origins='*', async_mode='threading')

    @app.route('/')
    def index():
        return send_file(BASE_DIR.parent / 'frontend' / 'index.html')

    def cors(resp):
        resp.headers['Access-Control-Allow-Origin']  = '*'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        resp.headers['Access-Control-Allow-Methods'] = 'GET,POST,OPTIONS'
        return resp

    @app.after_request
    def after(resp):
        return cors(resp)

    # ── REST endpoints ──────────────────────────────────────────────────────

    @app.route('/problems', methods=['GET', 'OPTIONS'])
    def get_problems():
        return jsonify({'problems': PROBLEMS_META})

    @app.route('/history', methods=['GET', 'OPTIONS'])
    def get_history():
        history = _load_history()
        limit = min(int(request.args.get('limit', 50)), 200)
        pid = request.args.get('problem_id')
        if pid:
            history = [h for h in history if h.get('problem_id') == pid]
        return jsonify({'submissions': history[:limit]})

    @app.route('/run', methods=['POST', 'OPTIONS'])
    def run_free():
        if request.method == 'OPTIONS':
            return jsonify({})
        ip = request.remote_addr or '127.0.0.1'
        if not _check_rate_limit(ip):
            return jsonify({'error': 'Rate limit exceeded'}), 429
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
        _save_submission({'id': str(uuid.uuid4()),
                          'timestamp': datetime.now(timezone.utc).isoformat(),
                          'problem_id': '__free_run__', 'problem_title': 'Free Run',
                          'language': language, 'status': status,
                          'passed': 0, 'total': 0, 'source_code': source_code})
        return jsonify({'status': status, 'output': output, 'stderr': stderr, 'time_ms': elapsed})

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
        try:
            n = sync_from_zip(zip_file.read(), problem_id)
        except (zipfile.BadZipFile, ValueError) as e:
            return jsonify({'error': str(e)}), 400
        return jsonify({'message': f'Problem "{problem_id}" uploaded with {n} test cases.'})

    # ── WebSocket: interactive terminal ────────────────────────────────────

    @socketio.on('run_interactive')
    def handle_run_interactive(data):
        sid = request.sid
        language    = data.get('language', 'python3').lower().strip()
        source_code = data.get('source_code', '').strip()

        if not source_code:
            emit('terminal_output', {'type': 'error', 'text': 'No source code provided.\r\n'})
            emit('terminal_done', {'status': 'ERROR'})
            return

        work_dir = tempfile.mkdtemp(prefix='exec_')
        cmd, err, env = _compile(language, source_code, work_dir)

        if err:
            emit('terminal_output', {'type': 'stderr', 'text': err + '\r\n'})
            emit('terminal_done', {'status': 'COMPILATION_ERROR'})
            shutil.rmtree(work_dir, ignore_errors=True)
            return

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
        except Exception as e:
            emit('terminal_output', {'type': 'error', 'text': str(e) + '\r\n'})
            emit('terminal_done', {'status': 'ERROR'})
            shutil.rmtree(work_dir, ignore_errors=True)
            return

        with _procs_lock:
            _running_procs[sid] = (proc, work_dir)

        start_time = time.time()

        def stream(pipe, pipe_type):
            try:
                while True:
                    chunk = pipe.read(128)
                    if not chunk:
                        break
                    text = chunk.decode('utf-8', errors='replace')
                    socketio.emit('terminal_output', {'type': pipe_type, 'text': text}, to=sid)
            except Exception:
                pass

        t_out = threading.Thread(target=stream, args=(proc.stdout, 'stdout'), daemon=True)
        t_err = threading.Thread(target=stream, args=(proc.stderr, 'stderr'), daemon=True)
        t_out.start()
        t_err.start()

        def wait_proc():
            try:
                proc.wait(timeout=EXECUTION_TIMEOUT)
                status = 'OK' if proc.returncode == 0 else 'RUNTIME_ERROR'
            except subprocess.TimeoutExpired:
                proc.kill()
                status = 'TIME_LIMIT_EXCEEDED'
                socketio.emit('terminal_output',
                              {'type': 'error', 'text': '\r\n[Time limit exceeded (30s)]\r\n'}, to=sid)
            t_out.join(timeout=2)
            t_err.join(timeout=2)
            elapsed = round((time.time() - start_time) * 1000)
            socketio.emit('terminal_done', {'status': status, 'time_ms': elapsed}, to=sid)
            with _procs_lock:
                _running_procs.pop(sid, None)
            shutil.rmtree(work_dir, ignore_errors=True)
            _save_submission({'id': str(uuid.uuid4()),
                              'timestamp': datetime.now(timezone.utc).isoformat(),
                              'problem_id': '__interactive__', 'problem_title': 'Interactive Run',
                              'language': language, 'status': status,
                              'passed': 0, 'total': 0, 'source_code': source_code})

        threading.Thread(target=wait_proc, daemon=True).start()

    @socketio.on('terminal_stdin')
    def handle_stdin(data):
        sid = request.sid
        with _procs_lock:
            entry = _running_procs.get(sid)
        if entry:
            proc, _ = entry
            if proc.poll() is None:
                try:
                    line = data.get('text', '') + '\n'
                    proc.stdin.write(line.encode('utf-8'))
                    proc.stdin.flush()
                    # Echo input back so user sees what they typed
                    socketio.emit('terminal_output',
                                  {'type': 'echo', 'text': line.replace('\n', '\r\n')}, to=sid)
                except Exception:
                    pass

    @socketio.on('terminal_kill')
    def handle_kill():
        sid = request.sid
        with _procs_lock:
            entry = _running_procs.pop(sid, None)
        if entry:
            proc, work_dir = entry
            proc.kill()
            socketio.emit('terminal_output',
                          {'type': 'error', 'text': '\r\n[Process killed]\r\n'}, to=sid)
            socketio.emit('terminal_done', {'status': 'KILLED'}, to=sid)
            shutil.rmtree(work_dir, ignore_errors=True)

    @socketio.on('disconnect')
    def handle_disconnect():
        sid = request.sid
        with _procs_lock:
            entry = _running_procs.pop(sid, None)
        if entry:
            proc, work_dir = entry
            proc.kill()
            shutil.rmtree(work_dir, ignore_errors=True)

    return app, socketio


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'setup':
        setup_problems()
    else:
        if not EFS_DIR.exists() or not any(EFS_DIR.glob('*.enc')):
            logger.info('First run — setting up encrypted problems...')
            setup_problems()
        import webbrowser
        app, socketio = make_app()
        logger.info('━' * 45)
        logger.info('  Server ready →  http://localhost:5000')
        logger.info('━' * 45)
        threading.Timer(1.2, lambda: webbrowser.open('http://localhost:5000')).start()
        socketio.run(app, host='0.0.0.0', port=5000, debug=False)
