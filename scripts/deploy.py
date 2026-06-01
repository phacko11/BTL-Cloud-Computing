"""
One-command AWS deployment script.

Usage:
    pip install boto3 cryptography
    python scripts/deploy.py

Prerequisites:
    aws configure   (hoặc set AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY)
    docker          (để build container image cho CodeRunner)
    sam             (pip install aws-sam-cli)
"""

import subprocess
import sys
import json
import os
from pathlib import Path

BASE_DIR   = Path(__file__).parent.parent
STACK_NAME = 'code-exec-engine'
REGION     = 'ap-southeast-1'   # Singapore — gần VN nhất


def run(cmd: str, check=True):
    print(f'\n$ {cmd}')
    result = subprocess.run(cmd, shell=True, check=check)
    return result.returncode


def main():
    print('=' * 60)
    print('  Serverless Code Execution Engine — AWS Deployment')
    print('=' * 60)

    # 1. Generate Fernet key
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    print(f'\n[1/5] Generated encryption key: {key[:8]}...')

    # 2. Build container image + SAM build
    print('\n[2/5] SAM build (includes Docker build for CodeRunner)...')
    run(f'sam build --region {REGION}', check=True)

    # 3. Deploy stack
    print('\n[3/5] SAM deploy...')
    run(
        f'sam deploy '
        f'--stack-name {STACK_NAME} '
        f'--region {REGION} '
        f'--capabilities CAPABILITY_IAM '
        f'--resolve-s3 '
        f'--parameter-overrides EncryptionKey={key} '
        f'--no-confirm-changeset',
        check=True
    )

    # 4. Upload problems to S3
    print('\n[4/5] Uploading problems to S3...')
    import boto3
    cf = boto3.client('cloudformation', region_name=REGION)
    outputs = {o['OutputKey']: o['OutputValue']
               for o in cf.describe_stacks(StackName=STACK_NAME)['Stacks'][0]['Outputs']}
    bucket = outputs['ProblemsBucketName']
    api    = outputs['ApiEndpoint']

    s3 = boto3.client('s3', region_name=REGION)
    import zipfile, io
    problems_dir = BASE_DIR / 'problems'
    for problem_dir in sorted(problems_dir.iterdir()):
        if not problem_dir.is_dir():
            continue
        problem_id = problem_dir.name
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as zf:
            for f in sorted(problem_dir.iterdir()):
                if f.suffix in ('.in', '.out'):
                    zf.write(f, f.name)
        buf.seek(0)
        s3_key = f'problems/{problem_id}.zip'
        s3.put_object(Bucket=bucket, Key=s3_key, Body=buf.read())
        print(f'   Uploaded: {s3_key}')

    # 5. Print summary
    print('\n[5/5] Deployment complete!')
    print('=' * 60)
    print(f'  API URL:   {api}')
    print(f'  S3 Bucket: {bucket}')
    print(f'  Region:    {REGION}')
    print('=' * 60)
    print('\nNext: open frontend/index.html and set API URL to:')
    print(f'  {api}')
    print('\nTo tear down (avoid charges):')
    print(f'  sam delete --stack-name {STACK_NAME} --region {REGION}')


if __name__ == '__main__':
    os.chdir(BASE_DIR)
    main()
