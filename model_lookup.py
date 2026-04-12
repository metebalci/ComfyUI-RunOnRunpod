"""Client-side lookup: given a local model file, try to find a remote source
(ComfyUI-Manager DB, HuggingFace cache, CivitAI) so the worker can download it
directly onto the network volume instead of having the user upload it.

Lookup order (cheapest / most private first):
  1. ComfyUI-Manager model-list.json — filename match, no external call per model
  2. HuggingFace cache reverse-lookup — local filesystem, no network call
  3. CivitAI by-hash — sends the file SHA-256 externally, so it's last

All lookups fall back to None; the caller uploads the file locally as a last
resort. This module is opt-in and only called when the user enables the
"Download from the source when possible" setting.
"""

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from typing import Optional, TypedDict

_PREFIX = "[RunOnRunpod]"

# Manager's curated model database, fetched once per 24h.
_MANAGER_DB_URL = "https://raw.githubusercontent.com/ltdrdata/ComfyUI-Manager/main/model-list.json"
_MANAGER_DB_TTL = 24 * 60 * 60  # 24 hours

# CivitAI by-hash API.
_CIVITAI_HASH_URL = "https://civitai.com/api/v1/model-versions/by-hash/{sha256}"


class Descriptor(TypedDict, total=False):
    """Describes how a model can be fetched from its source."""
    source: str  # "manager" | "hf" | "civitai"
    url: str
    dest_path: str  # models/{subdir}/{filename} on the network volume
    expected_sha256: Optional[str]
    auth: str  # "hf" | "civitai" | "none"


def _cache_dir() -> str:
    """Return the plugin-local cache directory, creating it if needed."""
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
    os.makedirs(d, exist_ok=True)
    return d


# -----------------------------------------------------------------------------
# Hash cache — SHA-256 on a 12GB file takes minutes, so memoize by stat key.
# -----------------------------------------------------------------------------
def _hash_cache_path() -> str:
    return os.path.join(_cache_dir(), "hash-cache.json")


def _load_hash_cache() -> dict:
    try:
        with open(_hash_cache_path(), "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_hash_cache(cache: dict) -> None:
    try:
        with open(_hash_cache_path(), "w") as f:
            json.dump(cache, f)
    except OSError as e:
        print(f"{_PREFIX} Failed to save hash cache: {e}")


def _stat_key(path: str) -> str:
    """Stable identity key for a file: realpath + size + mtime."""
    try:
        st = os.stat(path)  # follows symlinks
        real = os.path.realpath(path)
        return f"{real}|{st.st_size}|{int(st.st_mtime)}"
    except OSError:
        return path


def file_sha256(path: str) -> Optional[str]:
    """Return the SHA-256 of a file, cached by (realpath, size, mtime)."""
    if not os.path.exists(path):
        return None
    cache = _load_hash_cache()
    key = _stat_key(path)
    if key in cache:
        return cache[key]

    print(f"{_PREFIX} Hashing {path} (this can take a while for large files)...")
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
    except OSError as e:
        print(f"{_PREFIX} Hash failed for {path}: {e}")
        return None
    digest = h.hexdigest()

    cache[key] = digest
    _save_hash_cache(cache)
    return digest


# -----------------------------------------------------------------------------
# ComfyUI-Manager model database
# -----------------------------------------------------------------------------
def _manager_db_cache_path() -> str:
    return os.path.join(_cache_dir(), "manager-db.json")


def _fetch_url_json(url: str, timeout: int = 30) -> Optional[dict]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ComfyUI-RunOnRunpod"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, TimeoutError) as e:
        print(f"{_PREFIX} Failed to fetch {url}: {e}")
        return None


def fetch_manager_db() -> Optional[dict]:
    """Fetch ComfyUI-Manager's model-list.json, cached for 24h."""
    cache_path = _manager_db_cache_path()
    if os.path.exists(cache_path):
        age = time.time() - os.path.getmtime(cache_path)
        if age < _MANAGER_DB_TTL:
            try:
                with open(cache_path, "r") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass  # fall through and re-fetch

    print(f"{_PREFIX} Fetching ComfyUI-Manager model database...")
    data = _fetch_url_json(_MANAGER_DB_URL)
    if data is None:
        # If fetch failed but a stale cache exists, use it rather than nothing.
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r") as f:
                    print(f"{_PREFIX} Using stale Manager DB cache")
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                return None
        return None

    try:
        with open(cache_path, "w") as f:
            json.dump(data, f)
    except OSError as e:
        print(f"{_PREFIX} Failed to cache Manager DB: {e}")
    return data


def lookup_manager(subdir: str, filename: str) -> Optional[Descriptor]:
    """Look up a model by filename in ComfyUI-Manager's model database.

    Manager's schema uses entries with ``filename``, ``url``, and ``save_path``
    fields. We match on filename only — Manager's filenames are unique in
    practice for most models.
    """
    db = fetch_manager_db()
    if db is None:
        return None
    entries = db.get("models", [])
    for entry in entries:
        if entry.get("filename") == filename:
            url = entry.get("url")
            if not url:
                continue
            return Descriptor(
                source="manager",
                url=url,
                dest_path=f"models/{subdir}/{filename}",
                expected_sha256=None,  # Manager DB doesn't ship hashes
                auth="hf" if _looks_like_hf_url(url) else "none",
            )
    return None


def _looks_like_hf_url(url: str) -> bool:
    return "huggingface.co" in url


# -----------------------------------------------------------------------------
# HuggingFace cache reverse lookup
# -----------------------------------------------------------------------------
def _hf_cache_root() -> str:
    """Return the path to the HuggingFace cache hub directory."""
    # Honor HF_HOME / HUGGINGFACE_HUB_CACHE if set; fall back to ~/.cache.
    hub = os.environ.get("HUGGINGFACE_HUB_CACHE")
    if hub:
        return hub
    hf_home = os.environ.get("HF_HOME")
    if hf_home:
        return os.path.join(hf_home, "hub")
    return os.path.expanduser("~/.cache/huggingface/hub")


def lookup_hf_cache(local_path: str, subdir: str, filename: str) -> Optional[Descriptor]:
    """If ``local_path`` is (or resolves to) a file inside the HF hub cache,
    recover the repo ID and relative filename so the worker can re-download it.

    HF cache layout: ``hub/models--{org}--{name}/snapshots/{commit}/{relpath}``
    Files under ``snapshots/`` are typically symlinks into ``blobs/`` but we
    walk up from the snapshots dir to recover the repo name.
    """
    if not os.path.exists(local_path):
        return None
    real = os.path.realpath(local_path)
    hub = _hf_cache_root()
    if not real.startswith(os.path.abspath(hub)):
        # Not in HF cache — may still be a plain copy; not handled here.
        return None

    # Walk parents to find the "models--{org}--{name}" dir.
    parts = os.path.relpath(real, hub).split(os.sep)
    # Expected layout: ["models--org--name", "blobs", "<hash>"] for symlinks
    # resolving to blobs, or ["models--org--name", "snapshots", "<commit>", ...]
    # if the caller passed a snapshots path directly.
    repo_dir = None
    for p in parts:
        if p.startswith("models--"):
            repo_dir = p
            break
    if not repo_dir:
        return None

    # Strip "models--" prefix and convert double-dash back to slash.
    repo_id = repo_dir[len("models--"):].replace("--", "/", 1)

    # Try to find a snapshots path for this blob to recover the in-repo filename.
    # Scan snapshots/*/ for any entry whose realpath matches `real`.
    snapshots_root = os.path.join(hub, repo_dir, "snapshots")
    in_repo_path: Optional[str] = None
    if os.path.isdir(snapshots_root):
        for commit in os.listdir(snapshots_root):
            commit_dir = os.path.join(snapshots_root, commit)
            if not os.path.isdir(commit_dir):
                continue
            for root, _dirs, files in os.walk(commit_dir):
                for f in files:
                    entry = os.path.join(root, f)
                    try:
                        if os.path.realpath(entry) == real:
                            in_repo_path = os.path.relpath(entry, commit_dir)
                            break
                    except OSError:
                        continue
                if in_repo_path:
                    break
            if in_repo_path:
                break

    # Fall back to the provided filename if we can't recover the in-repo path.
    if not in_repo_path:
        in_repo_path = filename

    url = f"https://huggingface.co/{repo_id}/resolve/main/{in_repo_path}"
    return Descriptor(
        source="hf",
        url=url,
        dest_path=f"models/{subdir}/{filename}",
        expected_sha256=None,
        auth="hf",
    )


# -----------------------------------------------------------------------------
# CivitAI by-hash lookup
# -----------------------------------------------------------------------------
def lookup_civitai(sha256: str, subdir: str, filename: str, api_key: Optional[str]) -> Optional[Descriptor]:
    """Query CivitAI's by-hash API. The file's SHA-256 is sent externally."""
    if not sha256:
        return None
    url = _CIVITAI_HASH_URL.format(sha256=sha256)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ComfyUI-RunOnRunpod"})
        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None  # not a CivitAI-tracked model
        print(f"{_PREFIX} CivitAI lookup failed ({e.code}): {e}")
        return None
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
        print(f"{_PREFIX} CivitAI lookup error: {e}")
        return None

    # The response has `files: [{ downloadUrl, hashes: { SHA256 }, ... }]` —
    # we want the file matching our hash.
    download_url: Optional[str] = None
    for file_entry in data.get("files", []):
        hashes = {k.lower(): v.lower() for k, v in (file_entry.get("hashes") or {}).items()}
        if hashes.get("sha256") == sha256.lower():
            download_url = file_entry.get("downloadUrl")
            break
    if not download_url:
        download_url = data.get("downloadUrl")
    if not download_url:
        return None

    return Descriptor(
        source="civitai",
        url=download_url,
        dest_path=f"models/{subdir}/{filename}",
        expected_sha256=sha256,
        auth="civitai",
    )


# -----------------------------------------------------------------------------
# Driver
# -----------------------------------------------------------------------------
def lookup_model(
    subdir: str,
    filename: str,
    local_path: Optional[str],
    civitai_api_key: Optional[str] = None,
) -> Optional[Descriptor]:
    """Try the full lookup chain for a model file.

    Returns a descriptor on the first hit, or None if nothing matches.
    CivitAI is only attempted if we have a local file to hash.
    """
    # 1. Manager DB — filename-only match, no external calls per model.
    d = lookup_manager(subdir, filename)
    if d is not None:
        print(f"{_PREFIX} Manager DB hit: {filename} -> {d.get('url')}")
        return d

    if not local_path or not os.path.exists(local_path):
        return None

    # 2. HF cache reverse-lookup — local filesystem only.
    d = lookup_hf_cache(local_path, subdir, filename)
    if d is not None:
        print(f"{_PREFIX} HF cache hit: {filename} -> {d.get('url')}")
        return d

    # 3. CivitAI by hash — sends the SHA-256 externally.
    sha256 = file_sha256(local_path)
    if sha256:
        d = lookup_civitai(sha256, subdir, filename, civitai_api_key)
        if d is not None:
            print(f"{_PREFIX} CivitAI hit: {filename} -> {d.get('url')}")
            return d

    return None
