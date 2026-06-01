"""
Clean up all AWS resources after the demo — avoid any lingering costs.

Usage:
    python scripts/teardown.py
"""

import subprocess
import boto3
from pathlib import Path

STACK_NAME = 'code-exec-engine'
REGION     = 'ap-southeast-1'


def empty_bucket(bucket_name: str):
    """S3 bucket must be empty before stack deletion."""
    s3 = boto3.resource('s3', region_name=REGION)
    try:
        bucket = s3.Bucket(bucket_name)
        print(f'  Emptying s3://{bucket_name}...')
        bucket.objects.all().delete()
        bucket.object_versions.all().delete()
        print(f'  Emptied s3://{bucket_name}')
    except Exception as e:
        print(f'  Warning: {e}')


def main():
    print('Tearing down stack:', STACK_NAME)

    cf = boto3.client('cloudformation', region_name=REGION)
    try:
        outputs = {o['OutputKey']: o['OutputValue']
                   for o in cf.describe_stacks(StackName=STACK_NAME)['Stacks'][0]['Outputs']}
        bucket = outputs.get('ProblemsBucketName')
        if bucket:
            empty_bucket(bucket)
    except Exception:
        pass  # stack might not exist

    result = subprocess.run(
        f'sam delete --stack-name {STACK_NAME} --region {REGION} --no-prompts',
        shell=True,
    )
    if result.returncode == 0:
        print('Stack deleted successfully.')
    else:
        print('sam delete failed. Try manually in AWS Console → CloudFormation.')


if __name__ == '__main__':
    main()
