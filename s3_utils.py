import boto3
from botocore.config import Config

S3_PROVIDER_ENDPOINTS = {
    "aws": None,  # boto3 handles AWS endpoints automatically
    "r2": "https://{account_id}.r2.cloudflarestorage.com",
    "gcs": "https://storage.googleapis.com",
    "runpod": None,  # TODO: confirm RunPod S3 endpoint format
    "custom": None,
}


def get_s3_client(settings: dict):
    endpoint_url = settings.get("s3_endpoint") or None
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=settings["s3_access_key"],
        aws_secret_access_key=settings["s3_secret_key"],
        config=Config(signature_version="s3v4"),
    )


def generate_presigned_get(client, bucket: str, key: str, expiry: int = 3600) -> str:
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=expiry,
    )


def generate_presigned_put(client, bucket: str, key: str, expiry: int = 3600) -> str:
    return client.generate_presigned_url(
        "put_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=expiry,
    )


def upload_file(client, bucket: str, key: str, file_path: str):
    client.upload_file(file_path, bucket, key)
