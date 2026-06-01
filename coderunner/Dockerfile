FROM public.ecr.aws/lambda/python:3.11

# Install g++ and Java (Amazon Linux 2023 uses dnf)
RUN dnf install -y gcc-c++ java-17-amazon-corretto-headless && dnf clean all

# Install Python dependencies
COPY requirements.txt .
RUN pip install -r requirements.txt --no-cache-dir

COPY app.py .
CMD ["app.lambda_handler"]
