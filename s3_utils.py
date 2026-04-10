import boto3
from botocore.config import Config


def get_s3_client(settings: dict):
    endpoint_url = settings.get("s3_endpoint") or None
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=settings["s3_access_key"],
        aws_secret_access_key=settings["s3_secret_key"],
        config=Config(signature_version="s3v4"),
    )


def upload_file(client, bucket: str, key: str, file_path: str):
    client.upload_file(file_path, bucket, key)
