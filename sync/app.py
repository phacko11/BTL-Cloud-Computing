import json
import os
import zipfile
import io
import gzip
import logging
from pathlib import Path
import boto3
from cryptography.fernet import Fernet

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3  = boto3.client('s3')
ssm = boto3.client('ssm')

SSM_KEY_PARAM = os.environ['SSM_KEY_PARAM']

_cached_key: str | None = None


def lambda_handler(event, context):
    for record in event.get('Records', []):
        bucket = record['s3']['bucket']['name']
        key    = record['s3']['object']['key']
        logger.info('Processing s3://%s/%s', bucket, key)
        try:
            _process(bucket, key)
        except Exception as e:
            logger.error('Failed to process %s: %s', key, e)
            raise
    return {'status': 'ok'}


def _process(bucket: str, key: str):
    problem_id = Path(key).stem          # problems/hello_world.zip → hello_world

    # Fetch encryption key from SSM (cached across warm invocations)
    global _cached_key
    if not _cached_key:
        resp = ssm.get_parameter(Name=SSM_KEY_PARAM, WithDecryption=True)
        _cached_key = resp['Parameter']['Value']

    # Download zip from S3 into memory
    zip_bytes  = s3.get_object(Bucket=bucket, Key=key)['Body'].read()
    test_cases = _parse_zip(zip_bytes)

    if not test_cases:
        raise ValueError(f'No valid .in/.out pairs in {key}')

    logger.info('Found %d test cases for "%s"', len(test_cases), problem_id)

    # Serialize → gzip → Fernet encrypt (all in RAM)
    raw       = json.dumps(test_cases).encode('utf-8')
    compressed = gzip.compress(raw)
    encrypted  = Fernet(_cached_key.encode()).encrypt(compressed)

    # Store encrypted test cases back in S3 under testcases/ prefix
    out_key = f'testcases/{problem_id}.enc'
    s3.put_object(Bucket=bucket, Key=out_key, Body=encrypted,
                  ContentType='application/octet-stream')
    logger.info('Uploaded %d bytes → s3://%s/%s', len(encrypted), bucket, out_key)


def _parse_zip(zip_bytes: bytes) -> list:
    test_cases = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = set(zf.namelist())
        for inp in sorted(n for n in names if n.endswith('.in')):
            out = inp[:-3] + '.out'
            if out in names:
                test_cases.append({
                    'input':  zf.read(inp).decode('utf-8'),
                    'output': zf.read(out).decode('utf-8').strip(),
                })
    return test_cases
