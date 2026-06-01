"""
pytest test suite for the Code Execution Engine.

Run:
    pip install pytest
    cd BTL
    pytest tests/ -v
"""

import sys
import json
import gzip
from pathlib import Path
from cryptography.fernet import Fernet

# Add local server to path
sys.path.insert(0, str(Path(__file__).parent.parent / 'local'))
from server import run_code, sync_from_zip, load_test_cases, _get_or_create_key, EFS_DIR

import pytest


# ── Helpers ────────────────────────────────────────────────────────────────

HELLO_WORLD_CASES  = [{'input': '', 'output': 'Hello, World!'}]
SUM_CASES          = [{'input': '3 7\n', 'output': '10'}, {'input': '-5 100\n', 'output': '95'}]
FIBONACCI_CASES    = [{'input': '6\n', 'output': '8'}, {'input': '10\n', 'output': '55'}]
PRIME_CASES        = [{'input': '17\n', 'output': 'YES'}, {'input': '4\n', 'output': 'NO'}]
SORT_CASES         = [{'input': '5\n3 1 4 1 5\n', 'output': '1 1 3 4 5'}]


# ── Python 3 ──────────────────────────────────────────────────────────────

class TestPython3:
    def test_hello_world_accepted(self):
        r = run_code('python3', 'print("Hello, World!")', HELLO_WORLD_CASES)
        assert r['status'] == 'ACCEPTED'
        assert r['passed'] == 1

    def test_wrong_answer(self):
        r = run_code('python3', 'print("wrong")', HELLO_WORLD_CASES)
        assert r['status'] == 'WRONG_ANSWER'
        assert r['passed'] == 0

    def test_time_limit_exceeded(self):
        r = run_code('python3', 'while True: pass', HELLO_WORLD_CASES)
        assert r['status'] == 'TIME_LIMIT_EXCEEDED'

    def test_runtime_error(self):
        r = run_code('python3', 'x = 1/0', HELLO_WORLD_CASES)
        assert r['status'] == 'RUNTIME_ERROR'

    def test_sum_multiple_test_cases(self):
        r = run_code('python3', 'a,b=map(int,input().split())\nprint(a+b)', SUM_CASES)
        assert r['status'] == 'ACCEPTED'
        assert r['passed'] == r['total'] == 2

    def test_fibonacci(self):
        code = 'n=int(input())\na,b=1,1\nfor _ in range(n-1):\n    a,b=b,a+b\nprint(a)'
        r = run_code('python3', code, FIBONACCI_CASES)
        assert r['status'] == 'ACCEPTED'

    def test_partial_pass(self):
        # Always prints 10 → only first test passes (3+7=10, -5+100≠10)
        r = run_code('python3', 'print(10)', SUM_CASES)
        assert r['status'] == 'WRONG_ANSWER'
        assert r['passed'] == 1

    def test_prime_check(self):
        code = ('import math\nn=int(input())\n'
                'print("YES" if n>=2 and all(n%i for i in range(2,int(math.sqrt(n))+1)) else "NO")')
        r = run_code('python3', code, PRIME_CASES)
        assert r['status'] == 'ACCEPTED'

    def test_sort_array(self):
        code = 'n=int(input())\nprint(*sorted(map(int,input().split())))'
        r = run_code('python3', code, SORT_CASES)
        assert r['status'] == 'ACCEPTED'

    def test_results_structure(self):
        r = run_code('python3', 'print("Hello, World!")', HELLO_WORLD_CASES)
        assert 'status' in r
        assert 'passed' in r
        assert 'total' in r
        assert 'results' in r
        assert isinstance(r['results'], list)


# ── C++ ───────────────────────────────────────────────────────────────────

class TestCpp:
    def test_hello_world_accepted(self):
        code = '#include<iostream>\nusing namespace std;\nint main(){cout<<"Hello, World!"<<endl;}'
        r = run_code('cpp', code, HELLO_WORLD_CASES)
        assert r['status'] == 'ACCEPTED'

    def test_sum_accepted(self):
        code = '#include<iostream>\nusing namespace std;\nint main(){long long a,b;cin>>a>>b;cout<<a+b<<endl;}'
        r = run_code('cpp', code, SUM_CASES)
        assert r['status'] == 'ACCEPTED'
        assert r['passed'] == 2

    def test_compilation_error(self):
        code = 'int main() { cout << "missing include"; }'
        r = run_code('cpp', code, HELLO_WORLD_CASES)
        assert r['status'] == 'COMPILATION_ERROR'
        assert 'error' in r

    def test_time_limit_exceeded(self):
        code = '#include<iostream>\nusing namespace std;\nint main(){while(1);}'
        r = run_code('cpp', code, HELLO_WORLD_CASES)
        assert r['status'] == 'TIME_LIMIT_EXCEEDED'

    def test_wrong_answer(self):
        code = '#include<iostream>\nusing namespace std;\nint main(){cout<<"wrong"<<endl;}'
        r = run_code('cpp', code, HELLO_WORLD_CASES)
        assert r['status'] == 'WRONG_ANSWER'


# ── Java ──────────────────────────────────────────────────────────────────

class TestJava:
    def test_hello_world_accepted(self):
        code = 'public class Solution{\npublic static void main(String[] a){\nSystem.out.println("Hello, World!");\n}}'
        r = run_code('java', code, HELLO_WORLD_CASES)
        assert r['status'] == 'ACCEPTED'

    def test_sum_accepted(self):
        code = ('import java.util.*;\npublic class Solution{\n'
                'public static void main(String[] a){\n'
                'Scanner sc=new Scanner(System.in);\n'
                'long x=sc.nextLong(),y=sc.nextLong();\n'
                'System.out.println(x+y);\n}}')
        r = run_code('java', code, SUM_CASES)
        assert r['status'] == 'ACCEPTED'

    def test_missing_solution_class(self):
        code = 'public class Main { public static void main(String[] a) {} }'
        r = run_code('java', code, HELLO_WORLD_CASES)
        assert r['status'] == 'COMPILATION_ERROR'

    def test_compilation_error(self):
        code = 'public class Solution { public static void main(String[] a) { bad syntax } }'
        r = run_code('java', code, HELLO_WORLD_CASES)
        assert r['status'] == 'COMPILATION_ERROR'


# ── Encryption / EFS ──────────────────────────────────────────────────────

class TestEncryption:
    def test_load_test_cases_correct(self):
        cases = load_test_cases('hello_world')
        assert isinstance(cases, list)
        assert len(cases) > 0
        assert 'input' in cases[0]
        assert 'output' in cases[0]

    def test_load_nonexistent_problem(self):
        import pytest
        with pytest.raises(FileNotFoundError):
            load_test_cases('nonexistent_problem_xyz')

    def test_encryption_roundtrip(self, tmp_path):
        key = Fernet.generate_key().decode()
        fernet = Fernet(key.encode())
        test_data = [{'input': 'a', 'output': 'b'}]
        raw = json.dumps(test_data).encode()
        encrypted = fernet.encrypt(gzip.compress(raw))
        (tmp_path / 'test.enc').write_bytes(encrypted)

        recovered = json.loads(gzip.decompress(fernet.decrypt(encrypted)).decode())
        assert recovered == test_data


# ── Admin upload ──────────────────────────────────────────────────────────

class TestAdminUpload:
    def test_sync_valid_zip(self, tmp_path):
        import zipfile, io
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as zf:
            zf.writestr('1.in', '2 3\n')
            zf.writestr('1.out', '5')
            zf.writestr('2.in', '10 20\n')
            zf.writestr('2.out', '30')
        n = sync_from_zip(buf.getvalue(), 'test_upload_temp')
        assert n == 2
        # Clean up
        (EFS_DIR / 'test_upload_temp.enc').unlink(missing_ok=True)

    def test_sync_empty_zip(self):
        import zipfile, io
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as zf:
            zf.writestr('readme.txt', 'no tests')
        with pytest.raises(ValueError, match='No valid'):
            sync_from_zip(buf.getvalue(), 'should_fail')


# ── Rate limiter ──────────────────────────────────────────────────────────

class TestRateLimiter:
    def test_rate_limit_allows_under_limit(self):
        from server import _check_rate_limit, _rate_limit_store, RATE_LIMIT_MAX
        test_ip = '192.0.2.255'
        _rate_limit_store.pop(test_ip, None)
        for _ in range(RATE_LIMIT_MAX):
            assert _check_rate_limit(test_ip) is True

    def test_rate_limit_blocks_over_limit(self):
        from server import _check_rate_limit, _rate_limit_store, RATE_LIMIT_MAX
        test_ip = '192.0.2.254'
        _rate_limit_store.pop(test_ip, None)
        for _ in range(RATE_LIMIT_MAX):
            _check_rate_limit(test_ip)
        assert _check_rate_limit(test_ip) is False
