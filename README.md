# Serverless Code Execution Engine

> Bài tập lớn môn **Điện toán Đám mây** — Thiết kế và hiện thực nền tảng thực thi mã nguồn trực tuyến theo kiến trúc Serverless.

---

## Giới thiệu đề tài

Hệ thống cho phép người dùng nộp mã nguồn (Python, C++, Java) để chạy và chấm điểm tự động — tương tự các nền tảng Online Judge như Codeforces hay LeetCode — nhưng được xây dựng hoàn toàn trên kiến trúc **Serverless** của AWS.

### Kiến trúc hệ thống

```
Internet
    │
    ▼
┌─────────────────────────────────────────────────────┐
│  API Gateway  (HTTPS, CORS, rate limiting tự động)  │
└───────────────────────┬─────────────────────────────┘
                        │ POST /submit
                        ▼
┌──────────────────────────────────────────────────────┐
│  Bouncer Lambda  (ngoài VPC)                         │
│  • Xác thực request                                  │
│  • Lấy encryption key từ SSM Parameter Store         │
│  • Rate limiting (10 req/min/IP)                     │
│  • Ghi lịch sử vào DynamoDB                          │
│  • Invoke CodeRunner Lambda                          │
└───────────────────────┬──────────────────────────────┘
                        │ invoke (synchronous)
                        ▼
┌──────────────────────────────────────────────────────┐
│  VPC — Dark Subnet (không có Internet Gateway)       │
│  ┌────────────────────────────────────────────────┐  │
│  │  CodeRunner Lambda  (Container Image)          │  │
│  │  • Tải test cases từ S3 qua Gateway Endpoint   │  │
│  │  • Giải mã Fernet vào RAM (không ghi disk)     │  │
│  │  • Biên dịch + chạy code (g++, javac, python)  │  │
│  │  • Timeout 5s → SIGKILL                        │  │
│  │  • Cleanup /tmp sau mỗi lần chạy               │  │
│  └────────────────────────────────────────────────┘  │
│              │ S3 Gateway Endpoint (free)             │
└──────────────┼───────────────────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────────────────┐
│  S3 Bucket                                           │
│  • problems/{id}.zip  ← giảng viên upload           │
│  • testcases/{id}.enc ← Sync Lambda tạo ra          │
└──────────────────────────────────────────────────────┘
               ▲
               │ S3 Event Trigger
┌──────────────────────────────────────────────────────┐
│  Sync Lambda  (ngoài VPC)                            │
│  • Nhận trigger khi zip mới được upload              │
│  • Parse .in/.out pairs                              │
│  • Mã hóa Fernet + nén gzip                         │
│  • Lưu lại S3 testcases/                            │
└──────────────────────────────────────────────────────┘
```

### Tính năng bảo mật nổi bật

| Cơ chế | Mô tả |
|--------|-------|
| **Dark Subnet** | CodeRunner nằm trong VPC không có Internet Gateway — mã độc không thể gọi ra ngoài |
| **Fernet Encryption** | Test cases mã hóa AES trên S3, chỉ giải mã vào RAM khi chạy |
| **Subprocess Timeout** | Vòng lặp vô hạn bị SIGKILL sau 5 giây |
| **`/tmp` Cleanup** | Dọn sạch sau mỗi invocation, tránh rò rỉ dữ liệu giữa warm starts |
| **Rate Limiting** | 10 submissions/phút/IP tại Bouncer |
| **IAM Least Privilege** | Mỗi Lambda chỉ có đúng quyền cần thiết |

### Stack công nghệ (AWS Free Tier)

| Thành phần | Dịch vụ AWS | Chi phí |
|-----------|-------------|---------|
| API Gateway | Amazon API Gateway | Free (1M req/tháng) |
| Bouncer + Sync | AWS Lambda (Python 3.11) | Free (1M invocations) |
| CodeRunner | AWS Lambda Container Image (g++, javac, python3) | Free |
| Test cases | Amazon S3 | Free (5GB) |
| Encryption key | SSM Parameter Store Standard | Free |
| Submission history | Amazon DynamoDB | Free (25GB) |
| Network isolation | VPC + S3 Gateway Endpoint | Free |

---

## Cài đặt và Chạy

### Yêu cầu

- Python 3.10+
- `pip install flask cryptography`
- g++ (MinGW-w64 trên Windows, hoặc `apt install g++` trên Linux)
- Java JDK 17+ (tùy chọn, để test Java)

### Chạy demo local (không cần AWS)

```bash
# 1. Clone repo
git clone https://github.com/<your-username>/BTL-Cloud-Computing.git
cd BTL-Cloud-Computing

# 2. Cài dependencies
pip install flask cryptography

# 3. Khởi động server (tự động setup lần đầu)
python local/server.py

# 4. Mở giao diện web
#    Mở file frontend/index.html trong trình duyệt
#    API URL mặc định: http://localhost:5000
```

Server tự động:
- Tạo Fernet key ngẫu nhiên → lưu `local/secrets.json`
- Mã hóa 6 bài toán mẫu → lưu `local/efs/*.enc`
- Khởi động Flask API tại `http://localhost:5000`

### Deploy lên AWS (Free Tier)

Xem hướng dẫn chi tiết: **[AWS_SETUP.md](AWS_SETUP.md)**

```bash
# Sau khi cấu hình AWS credentials:
pip install aws-sam-cli
python scripts/deploy.py

# Dọn dẹp sau demo:
python scripts/teardown.py
```

---

## Bài toán mẫu

| ID | Tên | Độ khó | Test cases |
|----|-----|--------|-----------|
| `hello_world` | Hello World | Easy | 1 |
| `sum_two` | Sum of Two Numbers | Easy | 3 |
| `fibonacci` | Fibonacci Number | Easy | 3 |
| `prime_check` | Prime Check | Medium | 3 |
| `reverse_string` | Reverse String | Easy | 2 |
| `sort_array` | Sort Array | Medium | 3 |

### Ngôn ngữ hỗ trợ

| Ngôn ngữ | Runtime | Compiler |
|----------|---------|----------|
| Python 3 | CPython 3.11 | — |
| C++ | — | g++ -O2 -std=c++17 |
| Java | JVM 17 | javac |

---

## Kết quả có thể trả về

| Status | Mô tả |
|--------|-------|
| `ACCEPTED` | Tất cả test cases pass |
| `WRONG_ANSWER` | Output không khớp expected |
| `TIME_LIMIT_EXCEEDED` | Vượt quá 5 giây |
| `RUNTIME_ERROR` | Crash / non-zero exit code |
| `COMPILATION_ERROR` | Lỗi biên dịch (C++/Java) |

---

## API Reference

Base URL: `http://localhost:5000` (local) hoặc API Gateway URL (AWS)

| Method | Endpoint | Mô tả |
|--------|----------|-------|
| `GET` | `/problems` | Danh sách bài toán |
| `POST` | `/submit` | Nộp bài |
| `GET` | `/history` | Lịch sử nộp bài |
| `GET` | `/stats` | Thống kê theo bài |
| `POST` | `/admin/upload` | Upload bài toán mới (zip) |

### POST /submit

```json
{
  "language": "python3",
  "problem_id": "sum_two",
  "source_code": "a, b = map(int, input().split())\nprint(a + b)"
}
```

Response:
```json
{
  "status": "ACCEPTED",
  "passed": 3,
  "total": 3,
  "results": [
    {"test_case": 1, "status": "ACCEPTED", "expected": null, "actual": null},
    {"test_case": 2, "status": "ACCEPTED", "expected": null, "actual": null},
    {"test_case": 3, "status": "ACCEPTED", "expected": null, "actual": null}
  ]
}
```

---

## Chạy test suite

```bash
pip install pytest
pytest tests/ -v
# Expected: 26 passed
```

---

## Cấu trúc thư mục

```
BTL-Cloud-Computing/
├── template.yaml          # AWS SAM / CloudFormation
├── samconfig.toml         # SAM config (region: ap-southeast-1)
├── bouncer/               # Lambda 1: API Gateway + điều phối
│   ├── app.py
│   └── requirements.txt
├── coderunner/            # Lambda 2: sandbox thực thi (Container Image)
│   ├── app.py
│   ├── Dockerfile         # g++ + javac + python3
│   └── requirements.txt
├── sync/                  # Lambda 3: S3 trigger → mã hóa → S3
│   ├── app.py
│   └── requirements.txt
├── frontend/
│   └── index.html         # Web UI (4 tabs: Judge / History / Stats / Admin)
├── local/
│   ├── server.py          # Flask demo server (không cần AWS)
│   └── requirements.txt
├── problems/              # Test cases thô
│   ├── hello_world/
│   ├── sum_two/
│   ├── fibonacci/
│   ├── prime_check/
│   ├── reverse_string/
│   └── sort_array/
├── scripts/
│   ├── deploy.py          # One-command AWS deployment
│   └── teardown.py        # Dọn dẹp sau demo
├── tests/
│   └── test_engine.py     # 26 unit tests
└── AWS_SETUP.md           # Hướng dẫn tạo tài khoản AWS
```
