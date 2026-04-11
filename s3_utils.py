import hashlib
import os
import time

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


def get_s3_client(settings: dict):
    endpoint_url = settings.get("endpoint_url") or None
    region = settings.get("region") or "us-east-1"
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=settings["s3_access_key"],
        aws_secret_access_key=settings["s3_secret_key"],
        region_name=region,
        config=Config(signature_version="s3v4"),
    )


def file_hash(file_path: str) -> str:
    """Return the SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def key_exists(client, bucket: str, key: str) -> bool:
    """Check whether an object already exists in the bucket."""
    try:
        client.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError:
        return False


class _UploadProgress:
    def __init__(self, file_path: str, key: str, interval: float = 5.0):
        self._total = os.path.getsize(file_path)
        self._key = key
        self._uploaded = 0
        self._last_log = 0.0
        self._interval = interval

    def __call__(self, bytes_amount):
        self._uploaded += bytes_amount
        now = time.monotonic()
        if now - self._last_log >= self._interval:
            pct = self._uploaded / self._total * 100 if self._total else 100
            mb_done = self._uploaded / (1024 * 1024)
            mb_total = self._total / (1024 * 1024)
            print(f"[RunOnRunpod] Uploading {self._key}: {mb_done:.0f}/{mb_total:.0f} MB ({pct:.0f}%)")
            self._last_log = now


def upload_file(client, bucket: str, key: str, file_path: str):
    callback = _UploadProgress(file_path, key)
    client.upload_file(file_path, bucket, key, Callback=callback)


def download_file(client, bucket: str, key: str, dest: str):
    """Download an object from S3 to a local path."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    resp = client.get_object(Bucket=bucket, Key=key)
    with open(dest, "wb") as f:
        for chunk in resp["Body"].iter_chunks(8192):
            f.write(chunk)


def delete_objects(client, bucket: str, keys: list[str]):
    """Delete a list of S3 keys from the bucket."""
    for key in keys:
        client.delete_object(Bucket=bucket, Key=key)


def list_objects(client, bucket: str, prefix: str) -> list[str]:
    """List all object keys under a prefix."""
    keys = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            keys.append(obj["Key"])
    return keys


def upload_file_dedup(client, bucket: str, file_path: str) -> str:
    """Upload a file using its content hash as the key, skipping if it already exists.

    Returns the S3 key used.
    """
    digest = file_hash(file_path)
    ext = os.path.splitext(file_path)[1]
    s3_key = f"inputs/{digest}{ext}"
    if key_exists(client, bucket, s3_key):
        print(f"[RunOnRunpod] Skipping upload, already exists: {s3_key}")
    else:
        print(f"[RunOnRunpod] Uploading {file_path} -> {s3_key}")
        client.upload_file(file_path, bucket, s3_key)
    return s3_key
