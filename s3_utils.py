import hashlib
import os

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


def get_s3_client(settings: dict):
    endpoint_url = settings.get("s3_endpoint") or None
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=settings["s3_access_key"],
        aws_secret_access_key=settings["s3_secret_key"],
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


def upload_file(client, bucket: str, key: str, file_path: str):
    client.upload_file(file_path, bucket, key)


def download_file(client, bucket: str, key: str, dest: str):
    """Download an object from S3 to a local path."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    client.download_file(bucket, key, dest)


def delete_objects(client, bucket: str, keys: list[str]):
    """Delete a list of S3 keys from the bucket."""
    if not keys:
        return
    objects = [{"Key": k} for k in keys]
    client.delete_objects(Bucket=bucket, Delete={"Objects": objects})


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
