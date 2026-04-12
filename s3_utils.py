import hashlib
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

import boto3
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectTimeoutError,
    ReadTimeoutError,
)

# The multipart upload path below closely mirrors the reference script
# runpod/runpod-s3-examples/upload_large_file.py. RunPod network volumes are
# POSIX-backed, so CompleteMultipartUpload and checksumming can be very slow
# for large files and cause proxy-layer 524 timeouts even when the server is
# still working. The reliability features (524 retry, timeout-tolerant
# complete, HeadObject merge verification, final size check) exist to handle
# those conditions.

_PREFIX = "[RunOnRunpod]"

_DEFAULT_PART_SIZE = 50 * 1024 * 1024  # 50 MB, as in reference implementation
_DEFAULT_MAX_RETRIES = 5
_DEFAULT_MAX_WORKERS = 4
# Files at or below this size use a single PutObject. S3 multipart requires
# non-final parts to be >= 5 MB, and for small files multipart is pure
# overhead.
_MULTIPART_THRESHOLD = _DEFAULT_PART_SIZE


def get_s3_client(settings: dict):
    endpoint_url = settings.get("endpoint_url") or None
    region = settings.get("region") or "us-east-1"
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=settings["s3_access_key"],
        aws_secret_access_key=settings["s3_secret_key"],
        region_name=region,
        config=Config(
            signature_version="s3v4",
            retries={"max_attempts": _DEFAULT_MAX_RETRIES, "mode": "standard"},
        ),
    )


def file_hash(file_path: str) -> str:
    """Return the SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def key_exists(client, bucket: str, key: str) -> bool:
    """Check whether an object already exists in the bucket."""
    try:
        client.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as exc:
        # Only treat a genuine 404 as "doesn't exist"; surface 403/500/etc.
        code = exc.response.get("Error", {}).get("Code")
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code in ("404", "NoSuchKey", "NotFound") or status == 404:
            return False
        raise


# -----------------------------------------------------------------------------
# Large-file multipart uploader
# -----------------------------------------------------------------------------
class LargeMultipartUploader:
    """Upload a large file using robust multipart uploads.

    Closely follows runpod-s3-examples/upload_large_file.py. Callers should
    normally go through ``upload_file`` rather than instantiating this class
    directly.
    """

    def __init__(
        self,
        *,
        file_path: str,
        bucket: str,
        key: str,
        settings: dict,
        part_size: int = _DEFAULT_PART_SIZE,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        max_workers: int = _DEFAULT_MAX_WORKERS,
        progress_fn=None,
    ) -> None:
        self.file_path = file_path
        self.bucket = bucket
        self.key = key
        self.part_size = part_size
        self.max_retries = max_retries
        self.max_workers = max_workers
        self.progress_fn = progress_fn

        self.endpoint = settings.get("endpoint_url") or None
        self.region = settings.get("region") or "us-east-1"

        self.progress_lock = Lock()
        self.parts_completed = 0
        self.bytes_uploaded = 0
        self._last_progress_log = 0.0

        # Build our own session/client so we can rebuild the client with a
        # different read/connect timeout during complete_multipart_upload
        # retries (see complete_with_timeout_retry). As in reference
        # implementation.
        self.session = boto3.session.Session(
            aws_access_key_id=settings["s3_access_key"],
            aws_secret_access_key=settings["s3_secret_key"],
            region_name=self.region,
        )
        self.botocore_cfg = Config(
            signature_version="s3v4",
            region_name=self.region,
            retries={"max_attempts": self.max_retries, "mode": "standard"},
        )
        self.s3 = self.session.client(
            "s3", config=self.botocore_cfg, endpoint_url=self.endpoint
        )
        self.upload_id: str | None = None

    # ------------------------------------------------------------------
    # Error classifiers. As in reference implementation.
    # ------------------------------------------------------------------
    @staticmethod
    def is_insufficient_storage_error(exc: Exception) -> bool:
        """Return True if the exception wraps a 507 Insufficient Storage response."""
        if isinstance(exc, ClientError):
            meta = exc.response.get("ResponseMetadata", {})
            return meta.get("HTTPStatusCode") == 507
        return False

    @staticmethod
    def is_524_error(exc: Exception) -> bool:
        """Return True if the exception wraps a 524 timeout response."""
        if isinstance(exc, ClientError):
            meta = exc.response.get("ResponseMetadata", {})
            return meta.get("HTTPStatusCode") == 524
        return False

    @staticmethod
    def is_no_such_upload_error(exc: Exception) -> bool:
        """Return True if the exception reports a missing multipart upload."""
        if isinstance(exc, ClientError):
            err = exc.response.get("Error", {})
            return err.get("Code") == "NoSuchUpload"
        return False

    # ------------------------------------------------------------------
    # Retry helpers. As in reference implementation.
    # ------------------------------------------------------------------
    def call_with_524_retry(self, description: str, func):
        """Call ``func`` retrying on HTTP 524 or transport timeout errors.

        Proxies between the client and RunPod's S3 frontend terminate slow
        connections with 524; default boto3 retry mode does not retry these.
        """
        for attempt in range(1, self.max_retries + 1):
            try:
                return func()
            except ClientError as exc:
                if self.is_524_error(exc):
                    print(f"{_PREFIX} {description}: received 524 response (attempt {attempt})")
                    if attempt == self.max_retries:
                        print(f"{_PREFIX} {description}: exceeded max_retries for 524")
                        raise
                    backoff = 2 ** attempt
                    print(f"{_PREFIX} {description}: retrying in {backoff}s...")
                    time.sleep(backoff)
                    continue
                raise
            except (ReadTimeoutError, ConnectTimeoutError) as exc:
                print(f"{_PREFIX} {description}: request timed out (attempt {attempt}): {exc}")
                if attempt == self.max_retries:
                    print(f"{_PREFIX} {description}: exceeded max_retries for timeout")
                    raise
                backoff = 2 ** attempt
                print(f"{_PREFIX} {description}: retrying in {backoff}s...")
                time.sleep(backoff)

    def complete_with_timeout_retry(
        self,
        *,
        parts_sorted: list,
        initial_timeout: int,
        expected_size: int,
    ):
        """Complete the multipart upload, doubling timeout on client timeouts.

        As in reference implementation: if CompleteMultipartUpload times out
        client-side the server-side merge may still be in progress. Wait
        ``timeout`` seconds, then HeadObject — if the size matches the local
        file the merge finished and we treat the complete as successful.
        Otherwise retry complete_multipart_upload with doubled timeout.
        """
        if self.upload_id is None:
            raise RuntimeError("upload_id not set")

        timeout = initial_timeout
        cfg = self.botocore_cfg
        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            cfg = cfg.merge(Config(read_timeout=timeout, connect_timeout=timeout))
            client = self.session.client("s3", config=cfg, endpoint_url=self.endpoint)
            try:
                client.complete_multipart_upload(
                    Bucket=self.bucket,
                    Key=self.key,
                    UploadId=self.upload_id,
                    MultipartUpload={"Parts": parts_sorted},
                )
                self.s3 = client
                self.botocore_cfg = cfg
                return
            except (ReadTimeoutError, ConnectTimeoutError) as exc:
                last_exc = exc
                no_such_upload = False
                print(f"{_PREFIX} complete_multipart_upload timed out after {timeout}s: {exc}")
            except (ClientError, BotoCoreError) as exc:
                last_exc = exc
                no_such_upload = self.is_no_such_upload_error(exc)
                print(f"{_PREFIX} complete_multipart_upload failed (attempt {attempt}): {exc}")

            if no_such_upload:
                # A missing upload session can mean the server already merged
                # and cleaned up; check immediately instead of waiting.
                print(f"{_PREFIX} Upload session missing; checking object state immediately")
            else:
                print(
                    f"{_PREFIX} Waiting {timeout}s before checking object state "
                    f"to see if merge has completed"
                )
                time.sleep(timeout)

            try:
                head = self.call_with_524_retry(
                    "head_object",
                    lambda c=client: c.head_object(Bucket=self.bucket, Key=self.key),
                )
                uploaded_size = head.get("ContentLength")
                if uploaded_size == expected_size:
                    print(f"{_PREFIX} HeadObject confirms multipart upload merge has completed")
                    self.s3 = client
                    self.botocore_cfg = cfg
                    return
                print(
                    f"{_PREFIX} HeadObject size mismatch after timeout; "
                    f"will retry complete_multipart_upload"
                )
            except Exception as head_exc:
                print(f"{_PREFIX} head_object failed after error: {head_exc}")

            if attempt == self.max_retries:
                raise (
                    last_exc
                    if last_exc
                    else RuntimeError("Exceeded max_retries without completing multipart upload")
                )

            timeout *= 2
            print(f"{_PREFIX} Increasing timeout to {timeout}s and retrying")

    # ------------------------------------------------------------------
    # Part upload. As in reference implementation, with an added progress
    # callback so the plugin can report bytes uploaded to the UI.
    # ------------------------------------------------------------------
    def upload_part(
        self,
        *,
        part_number: int,
        offset: int,
        bytes_to_read: int,
        total_parts: int,
        file_size: int,
        start_time: float,
    ) -> dict:
        """Upload a single part with exponential-backoff retries."""
        if self.upload_id is None:
            raise RuntimeError("upload_id not set")

        for attempt in range(1, self.max_retries + 1):
            try:
                with open(self.file_path, "rb") as f:
                    f.seek(offset)
                    data = f.read(bytes_to_read)
                resp = self.s3.upload_part(
                    Bucket=self.bucket,
                    Key=self.key,
                    PartNumber=part_number,
                    UploadId=self.upload_id,
                    Body=data,
                )
                etag = resp["ETag"]

                with self.progress_lock:
                    self.parts_completed += 1
                    self.bytes_uploaded += bytes_to_read
                    parts_pct = 100.0 * self.parts_completed / total_parts
                    now = time.monotonic()
                    should_log = now - self._last_progress_log >= 2.0
                    if should_log:
                        self._last_progress_log = now
                    uploaded_snapshot = self.bytes_uploaded

                if should_log:
                    mb_done = uploaded_snapshot / (1024 * 1024)
                    mb_total = file_size / (1024 * 1024)
                    elapsed = time.time() - start_time
                    speed = (uploaded_snapshot / (1024 * 1024)) / elapsed if elapsed > 0 else 0
                    print(
                        f"{_PREFIX} Uploading {self.key}: {mb_done:.0f}/{mb_total:.0f} MB "
                        f"({parts_pct:.1f}%, {speed:.1f} MB/s)"
                    )
                    if self.progress_fn:
                        try:
                            self.progress_fn(uploaded_snapshot, file_size)
                        except Exception as cb_exc:
                            print(f"{_PREFIX} progress_fn raised: {cb_exc}")
                return {"PartNumber": part_number, "ETag": etag}
            except (BotoCoreError, ClientError) as exc:
                if self.is_insufficient_storage_error(exc):
                    print(f"{_PREFIX} Part {part_number}: received 507 Insufficient Storage; aborting")
                    raise RuntimeError("Server reported insufficient storage") from exc
                if self.is_524_error(exc):
                    print(f"{_PREFIX} Part {part_number}: received 524 response (attempt {attempt})")
                else:
                    print(f"{_PREFIX} Part {part_number}: attempt {attempt} failed: {exc}")
                if attempt == self.max_retries:
                    print(f"{_PREFIX} Part {part_number}: exceeded max_retries ({self.max_retries})")
                    raise
                backoff = 2 ** attempt
                print(f"{_PREFIX} Part {part_number}: retrying in {backoff}s...")
                time.sleep(backoff)

    # ------------------------------------------------------------------
    # Main driver. As in reference implementation.
    # ------------------------------------------------------------------
    def upload(self) -> None:
        file_size = os.path.getsize(self.file_path)
        total_parts = math.ceil(file_size / self.part_size)
        print(
            f"{_PREFIX} Uploading {self.file_path} -> {self.key} "
            f"({file_size} bytes, {total_parts} parts of up to {self.part_size} bytes)"
        )

        start_time = time.time()

        # Scale the initial CompleteMultipartUpload timeout with file size,
        # since POSIX-backed merges are roughly linear in total bytes.
        # Formula from reference implementation.
        file_gb = file_size / float(1024 ** 3)
        completion_timeout = max(60, int(math.ceil(file_gb) * 5))

        resp = self.call_with_524_retry(
            "create_multipart_upload",
            lambda: self.s3.create_multipart_upload(Bucket=self.bucket, Key=self.key),
        )
        self.upload_id = resp["UploadId"]
        print(f"{_PREFIX} Initiated multipart upload: UploadId={self.upload_id}")

        parts: list[dict] = []
        try:
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                futures = {}
                for part_num in range(1, total_parts + 1):
                    offset = (part_num - 1) * self.part_size
                    chunk_size = min(self.part_size, file_size - offset)
                    futures[
                        executor.submit(
                            self.upload_part,
                            part_number=part_num,
                            offset=offset,
                            bytes_to_read=chunk_size,
                            total_parts=total_parts,
                            file_size=file_size,
                            start_time=start_time,
                        )
                    ] = part_num

                for fut in as_completed(futures):
                    parts.append(fut.result())

            # Server-side verification that every part actually landed before
            # we call complete. As in reference implementation.
            def fetch_parts():
                paginator = self.s3.get_paginator("list_parts")
                found = []
                for page in paginator.paginate(
                    Bucket=self.bucket, Key=self.key, UploadId=self.upload_id
                ):
                    found.extend(page.get("Parts", []))
                return found

            seen = self.call_with_524_retry("list_parts", fetch_parts)
            print(f"{_PREFIX} Verified {len(seen)} of {total_parts} parts uploaded")
            if len(seen) != total_parts:
                raise RuntimeError(f"Expected {total_parts} parts but saw {len(seen)}")

            parts_sorted = sorted(parts, key=lambda x: x["PartNumber"])
            print(
                f"{_PREFIX} Sending complete_multipart_upload request "
                f"(initial timeout={completion_timeout}s)"
            )
            self.complete_with_timeout_retry(
                parts_sorted=parts_sorted,
                initial_timeout=completion_timeout,
                expected_size=file_size,
            )

            # Final size verification. As in reference implementation.
            head = self.call_with_524_retry(
                "head_object",
                lambda: self.s3.head_object(Bucket=self.bucket, Key=self.key),
            )
            uploaded_size = head.get("ContentLength")
            if uploaded_size != file_size:
                raise RuntimeError(
                    f"Multipart upload verification failed: remote object is "
                    f"{uploaded_size} bytes, but local file is {file_size} bytes"
                )
            print(f"{_PREFIX} Verified upload: {uploaded_size} bytes match local file")
        except Exception as exc:
            print(f"{_PREFIX} Upload interrupted: {exc}")
            if self.upload_id:
                # As in reference implementation: leave the UploadId open so
                # it can be resumed manually instead of silently aborting.
                print(f"{_PREFIX} UploadId {self.upload_id} left open for resumption")
            raise

        elapsed = time.time() - start_time
        speed_mbps = (file_size / (1024 * 1024)) / elapsed if elapsed > 0 else float("inf")
        print(f"{_PREFIX} Upload done: {speed_mbps:.2f} MB/s over {elapsed:.1f}s")


def upload_file(settings: dict, bucket: str, key: str, file_path: str, progress_fn=None):
    """Upload a local file to S3.

    Small files go through a single PutObject; large files go through
    ``LargeMultipartUploader`` with the reliability behaviors described there.
    """
    file_size = os.path.getsize(file_path)

    if file_size <= _MULTIPART_THRESHOLD:
        client = get_s3_client(settings)
        print(f"{_PREFIX} Uploading {file_path} -> {key} ({file_size} bytes, single-part)")
        with open(file_path, "rb") as f:
            client.put_object(Bucket=bucket, Key=key, Body=f)
        if progress_fn:
            try:
                progress_fn(file_size, file_size)
            except Exception:
                pass
        return

    uploader = LargeMultipartUploader(
        file_path=file_path,
        bucket=bucket,
        key=key,
        settings=settings,
        progress_fn=progress_fn,
    )
    uploader.upload()


def download_file(client, bucket: str, key: str, dest: str):
    """Download an object from S3 to a local path."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    resp = client.get_object(Bucket=bucket, Key=key)
    with open(dest, "wb") as f:
        for chunk in resp["Body"].iter_chunks(1024 * 1024):
            f.write(chunk)


def delete_objects(client, bucket: str, keys: list[str], max_workers: int = 20):
    """Delete a list of S3 keys from the bucket using parallel single-object
    deletes.

    RunPod's S3 API doesn't support the batch DeleteObjects operation
    (it returns HTTP 307 Temporary Redirect on the POST /?delete request),
    so we fall back to per-object DeleteObject calls issued concurrently
    from a thread pool. 20 workers gives us ~20× the throughput of a
    sequential loop without tripping rate limits in practice.
    """
    if not keys:
        return

    errors: list[tuple[str, Exception]] = []

    def _delete_one(key: str) -> None:
        try:
            client.delete_object(Bucket=bucket, Key=key)
        except Exception as e:
            errors.append((key, e))

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(_delete_one, k) for k in keys]
        for _ in as_completed(futures):
            pass

    if errors:
        first_key, first_err = errors[0]
        print(
            f"{_PREFIX} {len(errors)}/{len(keys)} delete(s) failed; "
            f"first: {first_key}: {first_err}"
        )
        # Only raise if everything failed — otherwise the caller still
        # wants the partial progress to count.
        if len(errors) == len(keys):
            raise RuntimeError(f"All {len(keys)} deletes failed: {first_err}") from first_err


def list_objects(client, bucket: str, prefix: str) -> list[str]:
    """List all object keys under a prefix."""
    keys = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            keys.append(obj["Key"])
    return keys


def upload_file_dedup(settings: dict, bucket: str, file_path: str, progress_fn=None) -> str:
    """Upload a file using its content hash as the key, skipping if it already exists.

    Returns the S3 key used.
    """
    digest = file_hash(file_path)
    ext = os.path.splitext(file_path)[1]
    s3_key = f"inputs/{digest}{ext}"
    client = get_s3_client(settings)
    if key_exists(client, bucket, s3_key):
        print(f"{_PREFIX} Skipping upload, already exists: {s3_key}")
        return s3_key
    upload_file(settings, bucket, s3_key, file_path, progress_fn=progress_fn)
    return s3_key
