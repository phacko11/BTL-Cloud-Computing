# Serverless Code Execution Engine

A cloud-native online judge platform that compiles and evaluates user-submitted code against hidden test cases — built entirely on AWS serverless infrastructure.

Supports **Python 3**, **C++**, and **Java** with strict sandboxing: no internet access from the execution environment, encrypted test cases, per-invocation cleanup, and rate limiting.

---

## Architecture

```
Internet
    │
    ▼
┌─────────────────────────────────────────────┐
│           API Gateway  (HTTPS, CORS)         │
└─────────────────────┬───────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────┐
│  Bouncer  (Lambda — Python 3.11)            │
│  · Rate limiting  10 req / min / IP         │
│  · Fetches encryption key from SSM          │
│  · Writes submission record to DynamoDB     │
│  · Invokes CodeRunner synchronously         │
└─────────────────────┬───────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────┐
│  VPC — Private Subnet (no Internet Gateway) │
│  ┌─────────────────────────────────────────┐│
│  │  CodeRunner  (Lambda Container Image)   ││
│  │  · Downloads test cases via S3 Endpoint ││
│  │  · Decrypts Fernet payload into RAM     ││
│  │  · Compiles & runs: g++ / javac / py    ││
│  │  · 5 s timeout → SIGKILL               ││
│  │  · Wipes /tmp after every invocation   ││
│  └─────────────────────────────────────────┘│
│               │  S3 Gateway Endpoint (free)  │
└───────────────┼─────────────────────────────┘
                ▼
┌─────────────────────────────────────────────┐
│  S3 Bucket                                  │
│  · problems/{id}.zip   ← admin upload       │
│  · testcases/{id}.enc  ← Sync Lambda writes │
└─────────────────────────────────────────────┘
                ▲
                │  ObjectCreated trigger
┌─────────────────────────────────────────────┐
│  Sync  (Lambda — Python 3.11)               │
│  · Unzips problem archive                   │
│  · Pairs .in / .out files                   │
│  · Encrypts with Fernet + gzip              │
│  · Uploads to testcases/                    │
└─────────────────────────────────────────────┘
```

---

## Security

| Mechanism | Detail |
|-----------|--------|
| **Dark subnet** | CodeRunner VPC has no Internet Gateway — submitted code cannot reach the internet |
| **Fernet encryption** | Test cases stored AES-encrypted on S3; decrypted into RAM only at runtime |
| **Process timeout** | 5-second hard limit enforced via `subprocess.run(timeout=…)` + SIGKILL |
| **`/tmp` wipe** | Full cleanup after every invocation; cold start forced on cleanup failure |
| **Rate limiting** | 10 submissions / minute / IP at the Bouncer layer |
| **IAM least privilege** | Each Lambda carries only the permissions it requires |

---

## AWS Stack (all Free Tier)

| Component | Service |
|-----------|---------|
| HTTP API | Amazon API Gateway |
| Bouncer · Sync | AWS Lambda (Python 3.11) |
| CodeRunner sandbox | AWS Lambda Container Image (g++ · javac · python3) |
| Test case storage | Amazon S3 |
| Encryption key | SSM Parameter Store Standard |
| Submission history | Amazon DynamoDB (on-demand) |
| Network isolation | VPC + S3 Gateway Endpoint |

---

## Getting Started

### Local demo (no AWS required)

```bash
git clone https://github.com/phacko11/BTL-Cloud-Computing.git
cd BTL-Cloud-Computing

pip install flask cryptography
python local/server.py
```

Open `frontend/index.html` in a browser. Default API URL: `http://localhost:5000`.

On first run the server generates a Fernet key, encrypts the sample problems, and starts serving immediately.

### Deploy to AWS

See **[AWS_SETUP.md](AWS_SETUP.md)** for full instructions.

```bash
pip install aws-sam-cli
python scripts/deploy.py      # build → deploy → upload problems → print API URL

# Tear down when done:
python scripts/teardown.py
```

---

## Sample Problems

| ID | Title | Difficulty | Test cases |
|----|-------|-----------|-----------|
| `hello_world` | Hello World | Easy | 1 |
| `sum_two` | Sum of Two Numbers | Easy | 3 |
| `fibonacci` | Fibonacci Number | Easy | 3 |
| `prime_check` | Prime Check | Medium | 3 |
| `reverse_string` | Reverse String | Easy | 2 |
| `sort_array` | Sort Array | Medium | 3 |

---

## API

Base URL: `http://localhost:5000` (local) · API Gateway URL (AWS)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/problems` | List all problems |
| `POST` | `/submit` | Submit code for evaluation |
| `GET` | `/history` | Submission history |
| `GET` | `/stats` | Per-problem statistics |
| `POST` | `/admin/upload` | Upload a new problem zip |

**POST /submit**

```json
{
  "language": "python3",
  "problem_id": "sum_two",
  "source_code": "a, b = map(int, input().split())\nprint(a + b)"
}
```

```json
{
  "status": "ACCEPTED",
  "passed": 3,
  "total": 3,
  "results": [
    {"test_case": 1, "status": "ACCEPTED"},
    {"test_case": 2, "status": "ACCEPTED"},
    {"test_case": 3, "status": "ACCEPTED"}
  ]
}
```

**Verdict codes:** `ACCEPTED` · `WRONG_ANSWER` · `TIME_LIMIT_EXCEEDED` · `RUNTIME_ERROR` · `COMPILATION_ERROR`

---

## Tests

```bash
pip install pytest
pytest tests/ -v
# 26 passed
```

Covers: Python / C++ / Java execution, encryption roundtrip, admin upload, rate limiter.

---

## Project Structure

```
.
├── template.yaml          # AWS SAM / CloudFormation
├── samconfig.toml
├── bouncer/               # Lambda 1 — routing, rate limit, DynamoDB
├── coderunner/            # Lambda 2 — sandboxed execution (Container Image)
│   └── Dockerfile         # python3.11 + g++ + javac
├── sync/                  # Lambda 3 — S3 trigger → encrypt → S3
├── frontend/
│   └── index.html         # Web UI: Judge · History · Stats · Admin
├── local/
│   └── server.py          # Flask server for local demo
├── problems/              # Raw test cases (.in / .out)
├── scripts/
│   ├── deploy.py          # One-command AWS deployment
│   └── teardown.py        # Stack cleanup
└── tests/
    └── test_engine.py
```
