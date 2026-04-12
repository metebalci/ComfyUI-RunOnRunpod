"""Worker-side downloader: given a list of model download descriptors from
the plugin, fetch each one onto the network volume with one retry on failure
and SHA-256 verification when an expected hash is provided.

Falls back on the client to re-upload any file this module cannot download.
"""

import hashlib
import os
import time
from typing import Optional

import requests

_PREFIX = "[RunOnRunpod]"

VOLUME_DIR = "/runpod-volume"
_CHUNK_SIZE = 4 * 1024 * 1024  # 4 MB, good balance on fast datacenter connections
_CONNECT_TIMEOUT = 30
_READ_TIMEOUT = 300  # per-chunk read timeout — some HF/CivitAI mirrors are slow


class FetchError(Exception):
    pass


def _auth_headers(auth: str, hf_token: Optional[str], civitai_key: Optional[str]) -> dict:
    headers = {"User-Agent": "ComfyUI-RunOnRunpod-Worker"}
    if auth == "hf" and hf_token:
        headers["Authorization"] = f"Bearer {hf_token}"
    elif auth == "civitai" and civitai_key:
        headers["Authorization"] = f"Bearer {civitai_key}"
    return headers


def _hash_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _stream_download(url: str, headers: dict, dest_path: str) -> None:
    """Stream a URL to a .part file, then atomically rename on success.

    Follows redirects (HF and CivitAI both issue 302s to CDN URLs).
    Removes the partial file on any failure so a retry starts clean.
    """
    part_path = dest_path + ".part"
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)

    with requests.get(
        url,
        headers=headers,
        stream=True,
        allow_redirects=True,
        timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
    ) as resp:
        if resp.status_code != 200:
            raise FetchError(f"HTTP {resp.status_code} from {url}")
        total = int(resp.headers.get("Content-Length") or 0)
        written = 0
        last_log = 0.0
        try:
            with open(part_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=_CHUNK_SIZE):
                    if not chunk:
                        continue
                    f.write(chunk)
                    written += len(chunk)
                    now = time.monotonic()
                    if now - last_log >= 2.0:
                        last_log = now
                        if total:
                            pct = 100.0 * written / total
                            mb_done = written / (1024 * 1024)
                            mb_total = total / (1024 * 1024)
                            print(f"{_PREFIX} {os.path.basename(dest_path)}: {mb_done:.0f}/{mb_total:.0f} MB ({pct:.1f}%)")
                        else:
                            mb_done = written / (1024 * 1024)
                            print(f"{_PREFIX} {os.path.basename(dest_path)}: {mb_done:.0f} MB")
        except Exception:
            if os.path.exists(part_path):
                os.remove(part_path)
            raise

    os.replace(part_path, dest_path)


def download_one(
    descriptor: dict,
    hf_token: Optional[str] = None,
    civitai_key: Optional[str] = None,
) -> None:
    """Download a single model and verify its hash if provided.

    One retry on any transport/HTTP error. Raises FetchError on final failure.
    """
    url = descriptor.get("url")
    dest_rel = descriptor.get("dest_path")
    expected_sha256 = descriptor.get("expected_sha256")
    auth = descriptor.get("auth", "none")
    if not url or not dest_rel:
        raise FetchError("descriptor missing url or dest_path")

    dest_abs = os.path.join(VOLUME_DIR, dest_rel)
    headers = _auth_headers(auth, hf_token, civitai_key)

    for attempt in (1, 2):
        try:
            print(f"{_PREFIX} Downloading (attempt {attempt}) {url} -> {dest_abs}")
            _stream_download(url, headers, dest_abs)
            break
        except Exception as e:
            print(f"{_PREFIX} Download attempt {attempt} failed: {e}")
            if attempt == 2:
                raise FetchError(f"download failed after 2 attempts: {e}") from e
            time.sleep(2)

    if expected_sha256:
        print(f"{_PREFIX} Verifying SHA-256 of {dest_abs}...")
        actual = _hash_file(dest_abs)
        if actual.lower() != expected_sha256.lower():
            try:
                os.remove(dest_abs)
            except OSError:
                pass
            raise FetchError(
                f"hash mismatch: expected {expected_sha256}, got {actual}"
            )
        print(f"{_PREFIX} SHA-256 OK")
