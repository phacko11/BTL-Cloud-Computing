# Hướng dẫn Tạo tài khoản AWS và Deploy hệ thống

## Bước 1 — Tạo tài khoản AWS Free Tier

1. Truy cập [https://aws.amazon.com/free](https://aws.amazon.com/free) → **Create a Free Account**
2. Nhập email, đặt mật khẩu, chọn tên tài khoản
3. Điền thông tin thanh toán (thẻ Visa/Mastercard) — **không bị tính phí** nếu dùng trong free tier
4. Xác minh số điện thoại (OTP)
5. Chọn plan: **Basic Support (Free)**
6. Chờ 5–10 phút để tài khoản được kích hoạt

> **Lưu ý thẻ ngân hàng:** AWS sẽ tạm giữ $1 để xác minh thẻ, sẽ hoàn lại sau 3–5 ngày. Đảm bảo thẻ có ít nhất $1 khả dụng.

---

## Bước 2 — Tạo IAM User (không dùng root account)

Sau khi đăng nhập bằng root account:

1. Vào **IAM** → **Users** → **Create user**
2. Tên user: `code-exec-deployer`
3. Chọn **Attach policies directly** → tìm và tích:
   - `AdministratorAccess` *(đủ quyền để deploy stack trong demo)*
4. **Create user** → vào user vừa tạo → tab **Security credentials**
5. **Create access key** → chọn **CLI** → tải file `.csv`

> File CSV chứa `Access Key ID` và `Secret Access Key` — **lưu cẩn thận, không chia sẻ**.

---

## Bước 3 — Cài công cụ

```bash
# AWS CLI
pip install awscli

# AWS SAM CLI (Serverless Application Model)
pip install aws-sam-cli

# Docker Desktop (cần để build CodeRunner container image)
# Tải tại: https://www.docker.com/products/docker-desktop/
```

Kiểm tra:
```bash
aws --version      # aws-cli/2.x.x
sam --version      # SAM CLI, version 1.x.x
docker --version   # Docker version 24.x.x
```

---

## Bước 4 — Cấu hình AWS Credentials

```bash
aws configure
```

Nhập lần lượt:
```
AWS Access Key ID:     AKIA...  (từ file CSV ở Bước 2)
AWS Secret Access Key: ...      (từ file CSV)
Default region name:  ap-southeast-1
Default output format: json
```

Kiểm tra kết nối:
```bash
aws sts get-caller-identity
# Hiện AccountId, UserId, Arn → thành công
```

---

## Bước 5 — Deploy hệ thống

```bash
# Clone repo (nếu chưa có)
git clone https://github.com/<your-username>/BTL-Cloud-Computing.git
cd BTL-Cloud-Computing

# Deploy (khoảng 5–10 phút lần đầu do build Docker image)
python scripts/deploy.py
```

Script tự động:
1. Sinh Fernet encryption key ngẫu nhiên
2. `sam build` — build container image CodeRunner (g++, javac, python3)
3. `sam deploy` — tạo toàn bộ stack trên AWS:
   - VPC + Private Subnets (Dark Subnet)
   - S3 Bucket (lưu test cases)
   - DynamoDB Table (lịch sử nộp bài)
   - SSM Parameter (encryption key)
   - 3 Lambda functions + API Gateway
   - S3 Gateway Endpoint (free, cho CodeRunner truy cập S3 trong VPC)
4. Upload 6 bài toán mẫu lên S3
5. In ra **API URL**

Output mẫu:
```
=============================================================
  API URL:   https://abc123xyz.execute-api.ap-southeast-1.amazonaws.com/Prod
  S3 Bucket: code-exec-123456789012
  Region:    ap-southeast-1
=============================================================
```

---

## Bước 6 — Kết nối Frontend

1. Mở `frontend/index.html` trong trình duyệt
2. Ở góc phải dưới, thay **API URL** thành URL từ bước trên:
   ```
   https://abc123xyz.execute-api.ap-southeast-1.amazonaws.com/Prod
   ```
3. Đèn trạng thái chuyển **xanh** → hệ thống đã kết nối

---

## Kiểm tra hệ thống

```bash
# Thay <API_URL> bằng URL thật
API=https://abc123xyz.execute-api.ap-southeast-1.amazonaws.com/Prod

# Lấy danh sách bài
curl $API/problems

# Nộp bài Python
curl -X POST $API/submit \
  -H "Content-Type: application/json" \
  -d '{"language":"python3","problem_id":"hello_world","source_code":"print(\"Hello, World!\")"}'

# Nộp bài C++
curl -X POST $API/submit \
  -H "Content-Type: application/json" \
  -d '{"language":"cpp","problem_id":"sum_two","source_code":"#include<iostream>\nusing namespace std;\nint main(){long long a,b;cin>>a>>b;cout<<a+b<<endl;}"}'
```

---

## Upload bài toán mới

Tạo file zip chứa các cặp `.in` / `.out`:

```
my_problem.zip
├── 1.in
├── 1.out
├── 2.in
└── 2.out
```

Upload lên S3:
```bash
aws s3 cp my_problem.zip s3://<bucket>/problems/my_problem.zip
```

Sync Lambda tự động trigger, mã hóa và lưu vào `testcases/my_problem.enc` trong vài giây.

Hoặc dùng tab **Admin** trong giao diện web để upload trực tiếp.

---

## Theo dõi logs (CloudWatch)

```bash
# Xem logs của Bouncer Lambda
sam logs -n code-exec-bouncer-code-exec-engine --region ap-southeast-1 --tail

# Xem logs của CodeRunner Lambda
sam logs -n code-exec-runner-code-exec-engine --region ap-southeast-1 --tail
```

Hoặc vào **AWS Console → CloudWatch → Log groups**.

---

## Xóa tài nguyên sau demo (quan trọng!)

```bash
python scripts/teardown.py
```

Script sẽ:
1. Xóa toàn bộ objects trong S3 bucket
2. Chạy `sam delete` → xóa stack và tất cả tài nguyên liên quan

Sau khi xóa, kiểm tra lại:
- **CloudFormation** → không còn stack `code-exec-engine`
- **ECR** → xóa thủ công repository `code-exec-runner` nếu còn
- **S3** → không còn bucket `code-exec-*`

---

## Chi phí ước tính

Toàn bộ hệ thống này **nằm trong AWS Free Tier**:

| Dịch vụ | Free Tier | Sử dụng thực tế |
|---------|-----------|----------------|
| Lambda | 1M invocations / 400K GB-s / tháng | ~vài nghìn invocations |
| API Gateway | 1M calls / tháng (12 tháng đầu) | ~vài nghìn calls |
| S3 | 5GB / 20K GET / 2K PUT | < 1MB |
| DynamoDB | 25GB / 25 WCU / 25 RCU | < 1MB |
| SSM Parameter Store | Standard parameters: **miễn phí mãi mãi** | 1 parameter |
| VPC / Subnets | Miễn phí | — |
| S3 Gateway Endpoint | **Miễn phí mãi mãi** | — |
| ECR | 500MB / tháng (12 tháng đầu) | ~300MB image |

> **Chi phí thực tế cho demo 1–2 ngày: $0.00**

---

## Xử lý sự cố thường gặp

| Lỗi | Nguyên nhân | Cách xử lý |
|-----|-------------|-----------|
| `docker: command not found` | Docker chưa chạy | Mở Docker Desktop trước khi deploy |
| `ExpiredTokenException` | Credentials hết hạn | Chạy lại `aws configure` |
| `Access Denied` trên S3 | IAM role thiếu quyền | Kiểm tra stack deploy thành công chưa |
| Frontend không kết nối | Sai API URL | Copy chính xác URL từ output deploy |
| CodeRunner timeout | Container cold start lần đầu | Chờ 30s, submit lại — warm start sẽ nhanh hơn |
