import asyncio
import json
import os
import re

import aiohttp
from aiohttp import web
from server import PromptServer

from .s3_utils import get_s3_client, upload_file, upload_file_dedup, download_file, delete_objects, list_objects, key_exists
from .model_lookup import lookup_model
from .latency import check_all_regions

_PREFIX = "[RunOnRunpod]"

routes = PromptServer.instance.routes


def _read_plugin_version() -> str:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pyproject.toml")
    try:
        with open(path) as f:
            for line in f:
                m = re.match(r'^\s*version\s*=\s*"([^"]+)"', line)
                if m:
                    return m.group(1)
    except Exception as e:
        print(_PREFIX, f"Could not read plugin version from pyproject.toml: {e}")
    return "unknown"


PLUGIN_VERSION = _read_plugin_version()

# Wire-protocol version. Bump this whenever the plugin/worker action
# protocol changes (action set, input/output shapes, error format).
# MUST be kept in sync with `ARG PROTOCOL_VERSION` in worker/Dockerfile.
PROTOCOL_VERSION = 1


# In-memory state for active jobs: {job_id: asyncio.Task}
_active_tasks = {}

# Last worker version manifest seen via the version action. Populated
# on the first successful submit and reused by the sidebar /info
# endpoint so we can show worker info without forcing another cold
# start. Empty dict on startup.
_last_worker_info: dict = {}

# Set of prep_ids whose submit/prep phase has been cancelled. submit_job
# checks membership at each cancellation-aware point; entries are discarded
# once submit_job returns (successful or not).
_cancelled_preps: set[str] = set()

# Set of prep_ids currently being processed by a submit/prep call. Used by
# the recover-jobs endpoint after a page reload — if a persisted prep_id
# is in this set, the backend is still working on it and the frontend
# should keep the card. Otherwise the prep is dead (e.g., the ComfyUI
# process restarted) and the card can be dropped.
_active_preps: set[str] = set()


def _send_event(event: str, data: dict = {}):
    """Push an event to the frontend via WebSocket."""
    PromptServer.instance.send_sync("runonrunpod", {"event": event, **data})

# Known input node types and the field that holds the filename
INPUT_NODE_FIELDS = {
    "LoadImage": "image",
    "LoadVideo": "video",
    "LoadAudio": "audio",
    "VHS_LoadVideo": "video",
}

# Model loader node types: class_type -> (field_name, model_subdirectory)
MODEL_NODE_FIELDS = {
    "CheckpointLoaderSimple": ("ckpt_name", "checkpoints"),
    "CheckpointLoader": ("ckpt_name", "checkpoints"),
    "LoraLoader": ("lora_name", "loras"),
    "LoraLoaderModelOnly": ("lora_name", "loras"),
    "VAELoader": ("vae_name", "vae"),
    "CLIPLoader": ("clip_name", "text_encoders"),
    "DualCLIPLoader": [("clip_name1", "text_encoders"), ("clip_name2", "text_encoders")],
    "TripleCLIPLoader": [("clip_name1", "text_encoders"), ("clip_name2", "text_encoders"), ("clip_name3", "text_encoders")],
    "UNETLoader": ("unet_name", "diffusion_models"),
    "ControlNetLoader": ("control_net_name", "controlnet"),
    "CLIPVisionLoader": ("clip_name", "clip_vision"),
    "UpscaleModelLoader": ("model_name", "upscale_models"),
}



def _get_input_directory() -> str:
    """Return ComfyUI's input directory path."""
    try:
        import folder_paths
        return folder_paths.get_input_directory()
    except ImportError:
        return os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "..",
            "input",
        )


def _get_output_directory() -> str:
    """Return ComfyUI's output directory path."""
    try:
        import folder_paths
        return folder_paths.get_output_directory()
    except ImportError:
        return os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "..",
            "output",
        )


def _find_model_file(subdir: str, filename: str) -> str | None:
    """Find a model file across all ComfyUI search paths for the given model type."""
    try:
        import folder_paths
        paths = folder_paths.get_folder_paths(subdir)
        for base in paths:
            full_path = os.path.join(base, filename)
            if os.path.exists(full_path):
                return full_path
    except (ImportError, AttributeError):
        fallback = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "..", "models", subdir, filename,
        )
        if os.path.exists(fallback):
            return fallback
    return None


def _scan_model_files(workflow: dict) -> dict:
    """Scan workflow for nodes that reference model files.

    Returns dict: {(subdir, filename): node_id}
    """
    files = {}
    for node_id, node in workflow.items():
        class_type = node.get("class_type", "")
        fields = MODEL_NODE_FIELDS.get(class_type)
        if fields is None:
            continue
        # Normalize to list of (field, subdir) tuples
        if isinstance(fields, tuple):
            fields = [fields]
        inputs = node.get("inputs", {})
        for field_name, subdir in fields:
            filename = inputs.get(field_name)
            if isinstance(filename, str) and filename:
                files[(subdir, filename)] = node_id
    return files


def _scan_input_files(workflow: dict) -> dict:
    """Scan workflow for nodes that reference local input files."""
    files = {}
    for node_id, node in workflow.items():
        class_type = node.get("class_type", "")
        field_name = INPUT_NODE_FIELDS.get(class_type)
        if field_name and field_name in node.get("inputs", {}):
            filename = node["inputs"][field_name]
            if isinstance(filename, str) and not filename.startswith("http"):
                files[filename] = {"node_id": node_id, "field": field_name}
    return files


def _s3_settings(settings: dict) -> dict:
    """Normalize frontend camelCase settings to the s3_utils key layout."""
    return {
        "endpoint_url": settings.get("endpointUrl"),
        "region": settings.get("region"),
        "s3_access_key": settings.get("s3AccessKey"),
        "s3_secret_key": settings.get("s3SecretKey"),
    }


def _make_s3_client(settings: dict):
    """Create S3 client from settings."""
    return get_s3_client(_s3_settings(settings))


# --- Routes ---
# All routes receive settings from the frontend in the request body.


@routes.post("/RunOnRunpod/verify")
async def verify_settings(request):
    """Verify RunPod API and S3 credentials."""
    data = await request.json()
    settings = data.get("settings", {})
    results = {"runpod_api": False, "s3_storage": False, "errors": []}

    # Check RunPod API key + endpoint
    api_key = settings.get("apiKey", "")
    endpoint_id = settings.get("endpointId", "")
    if not api_key or not endpoint_id:
        results["errors"].append("API Key and Endpoint ID are required")
    else:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"https://api.runpod.ai/v2/{endpoint_id}/health",
                    headers={"Authorization": f"Bearer {api_key}"},
                ) as resp:
                    if resp.status == 200:
                        results["runpod_api"] = True
                    else:
                        results["errors"].append(
                            f"RunPod API returned status {resp.status}"
                        )
        except Exception as e:
            results["errors"].append(f"RunPod API error: {e}")

    # Check S3 credentials + bucket
    bucket = settings.get("bucketName", "")
    s3_access = settings.get("s3AccessKey", "")
    s3_secret = settings.get("s3SecretKey", "")
    endpoint_url = settings.get("endpointUrl", "")
    if not bucket or not s3_access or not s3_secret or not endpoint_url:
        results["errors"].append("S3 credentials, endpoint URL, and bucket name are required")
    else:
        try:
            client = _make_s3_client(settings)
            client.head_bucket(Bucket=bucket)
            results["s3_storage"] = True
        except Exception as e:
            results["errors"].append(f"S3 storage error: {e}")

    if results["errors"]:
        print(_PREFIX,f"Verify failed: {results['errors']}")
    return web.json_response(results)


def _extract_error(result: dict, default: str = "Unknown error") -> str:
    """Pull an error string out of a RunPod status response.

    RunPod may surface the error at the top level, inside ``output`` when
    the worker returns ``{"error": ...}``, or not at all. ``output`` may
    legitimately be ``None`` (FAILED jobs with no worker output), so a
    straight ``.get("output", {}).get(...)`` chain is unsafe.
    """
    top = result.get("error")
    if top:
        return str(top)
    output = result.get("output")
    if isinstance(output, dict):
        nested = output.get("error")
        if nested:
            return str(nested)
    return default


async def _submit_runpod_job(
    session: aiohttp.ClientSession,
    endpoint_id: str,
    api_key: str,
    action: str,
    payload: dict | None,
) -> str:
    """POST /run to start a worker action; return the job id."""
    body: dict = {"action": action}
    if payload:
        body.update(payload)
    async with session.post(
        f"https://api.runpod.ai/v2/{endpoint_id}/run",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"input": body},
    ) as resp:
        result = await resp.json()
    job_id = result.get("id")
    if not job_id:
        raise RuntimeError(_extract_error(result, f"Failed to submit {action}"))
    return job_id


async def _poll_runpod_job(
    session: aiohttp.ClientSession,
    endpoint_id: str,
    api_key: str,
    job_id: str,
    on_progress=None,
) -> dict:
    """Poll /status until the job reaches a terminal state. On success
    return the COMPLETED output dict; on failure raise RuntimeError.

    If ``on_progress`` is given, invoke it with the IN_PROGRESS output
    payload each time it changes, and also once with the final COMPLETED
    output so callers that race past IN_PROGRESS still see the last state.
    """
    headers = {"Authorization": f"Bearer {api_key}"}
    last_progress = None
    delay = 0.2
    while True:
        async with session.get(
            f"https://api.runpod.ai/v2/{endpoint_id}/status/{job_id}",
            headers=headers,
        ) as resp:
            status_result = await resp.json()

        status = status_result.get("status", "UNKNOWN")
        output = status_result.get("output")

        if status == "IN_PROGRESS":
            if on_progress and output and output != last_progress:
                last_progress = output
                try:
                    on_progress(output)
                except Exception as cb_exc:
                    print(_PREFIX, f"streaming on_progress error: {cb_exc}")
        elif status == "COMPLETED":
            if on_progress and isinstance(output, dict) and output != last_progress:
                try:
                    on_progress(output)
                except Exception as cb_exc:
                    print(_PREFIX, f"streaming on_progress error: {cb_exc}")
            return output if isinstance(output, dict) else {}
        elif status in ("FAILED", "CANCELLED", "TIMED_OUT"):
            raise RuntimeError(f"Worker error: {_extract_error(status_result, status)}")

        await asyncio.sleep(delay)
        delay = min(delay * 1.5, 2.0)


async def _runpod_action(endpoint_id: str, api_key: str, action: str, payload: dict | None = None) -> dict:
    """Submit an action to the worker and return its COMPLETED output."""
    async with aiohttp.ClientSession() as session:
        job_id = await _submit_runpod_job(session, endpoint_id, api_key, action, payload)
        return await _poll_runpod_job(session, endpoint_id, api_key, job_id)


async def _runpod_streaming_action(
    endpoint_id: str,
    api_key: str,
    action: str,
    payload: dict,
    on_progress,
) -> dict:
    """Like ``_runpod_action`` but pipes IN_PROGRESS updates to ``on_progress``."""
    async with aiohttp.ClientSession() as session:
        job_id = await _submit_runpod_job(session, endpoint_id, api_key, action, payload)
        return await _poll_runpod_job(session, endpoint_id, api_key, job_id, on_progress=on_progress)


@routes.post("/RunOnRunpod/cancel-prepare")
async def cancel_prepare(request):
    """Cancel a specific in-flight submit/prep phase by prep_id."""
    data = await request.json()
    prep_id = data.get("prep_id", "")
    if prep_id:
        _cancelled_preps.add(prep_id)
    return web.json_response({"status": "cancelling"})


@routes.post("/RunOnRunpod/submit")
async def submit_job(request):
    data = await request.json()
    prep_id = data.get("prep_id", "")
    if prep_id:
        _active_preps.add(prep_id)
    try:
        return await _do_submit(data)
    finally:
        # Always discard so a cancelled prep doesn't leak its flag into
        # a future submit that happens to pick the same prep_id.
        _active_preps.discard(prep_id)
        _cancelled_preps.discard(prep_id)


class _SubmitError(Exception):
    """Raised by submit-phase helpers to abort with a JSON error response.

    Carries the status code and the user-facing message; the orchestrator
    catches it, logs, and turns it into a ``web.json_response``.
    """

    def __init__(self, message: str, status: int = 400, *, log: str | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.log = log  # separate verbose line for the server log


def _raise_if_cancelled(prep_id: str, stage: str) -> None:
    if prep_id in _cancelled_preps:
        raise _SubmitError("Cancelled", 499, log=f"Submit cancelled during {stage}")


def _validate_settings(settings: dict) -> None:
    """Ensure the user filled in RunPod + S3 credentials."""
    if not settings.get("apiKey") or not settings.get("endpointId"):
        raise _SubmitError("RunPod API Key and Endpoint ID are required")
    if not all(settings.get(k) for k in ("bucketName", "s3AccessKey", "s3SecretKey", "endpointUrl")):
        raise _SubmitError("S3 credentials, endpoint URL, and bucket name are required")


async def _validate_runpod_health(endpoint_id: str, api_key: str) -> None:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://api.runpod.ai/v2/{endpoint_id}/health",
                headers={"Authorization": f"Bearer {api_key}"},
            ) as resp:
                if resp.status != 200:
                    raise _SubmitError(
                        f"RunPod API health check failed (status {resp.status})",
                        log=f"RunPod API health check failed: {resp.status}",
                    )
    except _SubmitError:
        raise
    except Exception as e:
        raise _SubmitError(f"RunPod API error: {e}", log=f"RunPod API health check error: {e}")


async def _validate_s3(settings: dict, bucket: str):
    """Head-check the S3 bucket and return the ready-to-use client."""
    try:
        client = _make_s3_client(settings)
        await asyncio.to_thread(client.head_bucket, Bucket=bucket)
    except Exception as e:
        raise _SubmitError(f"S3 storage error: {e}", log=f"S3 storage validation failed: {e}")
    return client


async def _fetch_and_check_worker_version(endpoint_id: str, api_key: str) -> dict:
    """Ping the worker and enforce strict protocol version equality.

    Also caches the result in ``_last_worker_info`` and pushes a
    ``worker_info`` event so the sidebar renders immediately.
    """
    try:
        version_output = await _runpod_action(endpoint_id, api_key, "version")
    except Exception as e:
        raise _SubmitError(f"Worker not available: {e}", 500, log=f"Worker version request failed: {e}")

    if not isinstance(version_output, dict):
        raise _SubmitError("Worker returned invalid version response.", 500)

    worker_protocol = version_output.get("protocol_version")
    if not isinstance(worker_protocol, int) or worker_protocol == 0:
        raise _SubmitError(
            "Worker doesn't report a protocol version. Update your worker image.",
            log="ERROR: Worker doesn't report a protocol version",
        )
    if worker_protocol != PROTOCOL_VERSION:
        direction = "worker image" if worker_protocol < PROTOCOL_VERSION else "plugin"
        msg = (
            f"Plugin/worker protocol mismatch (plugin={PROTOCOL_VERSION}, "
            f"worker={worker_protocol}). Update your {direction}."
        )
        raise _SubmitError(msg, log=f"ERROR: {msg}")

    _last_worker_info.update({
        "worker_version": version_output.get("worker_version", "unknown"),
        "protocol_version": worker_protocol,
        "cuda_version": version_output.get("cuda_version", ""),
        "pytorch_version": version_output.get("pytorch_version", ""),
        "comfyui_version": version_output.get("comfyui_version", "unknown"),
    })
    _send_event("worker_info", _last_worker_info)
    return version_output


async def _check_node_compatibility(endpoint_id: str, api_key: str, workflow: dict) -> None:
    try:
        output = await _runpod_action(endpoint_id, api_key, "node_list")
    except Exception as e:
        raise _SubmitError(f"Node check failed: {e}", 500, log=f"Node list check failed: {e}")
    worker_nodes = set(output.get("node_list", []))
    workflow_nodes = {node.get("class_type") for node in workflow.values() if node.get("class_type")}
    missing_nodes = sorted(workflow_nodes - worker_nodes)
    if missing_nodes:
        msg = f"Missing custom nodes on worker: {', '.join(missing_nodes)}"
        raise _SubmitError(msg, log=msg)


async def _upload_input_files(settings: dict, bucket: str, workflow: dict, prep_id: str) -> dict:
    """Upload every LoadImage/LoadVideo/etc. referenced by the workflow.

    Returns a ``{filename: s3_key}`` mapping the worker can read back.
    """
    input_files: dict[str, str] = {}
    input_file_refs = _scan_input_files(workflow)
    if not input_file_refs:
        return input_files

    input_dir = _get_input_directory()
    for filename in input_file_refs:
        _raise_if_cancelled(prep_id, "input upload")
        file_path = os.path.join(input_dir, filename)
        if not os.path.exists(file_path):
            raise _SubmitError(f"Input file not found: {filename}")
        _send_event("progress", {"prep_id": prep_id, "message": f"Uploading input: {filename}"})
        s3_key = await asyncio.to_thread(upload_file_dedup, _s3_settings(settings), bucket, file_path)
        input_files[filename] = s3_key
    return input_files


async def _identify_missing_models(
    client,
    bucket: str,
    model_refs: dict,
    prep_id: str,
) -> list[tuple[str, str, str | None]]:
    """Return ``[(subdir, filename, local_path_or_None)]`` for every model
    that isn't already on the network volume.
    """
    missing: list[tuple[str, str, str | None]] = []
    for (subdir, filename) in model_refs:
        _raise_if_cancelled(prep_id, "model scan")
        s3_key = f"models/{subdir}/{filename}"
        if await asyncio.to_thread(key_exists, client, bucket, s3_key):
            print(_PREFIX, f"Model already on volume: {s3_key}")
            continue
        local_path = _find_model_file(subdir, filename)
        missing.append((subdir, filename, local_path))
    return missing


def _workflow_metadata_descriptor(subdir: str, filename: str, wm: dict | None) -> dict | None:
    """Build a worker download descriptor from workflow-author metadata.

    Returns None if the metadata doesn't provide a usable URL. Workflow
    metadata is authoritative — it's tried before any third-party lookup.
    """
    if not wm or not wm.get("url"):
        return None
    url = wm["url"]
    return {
        "source": "workflow",
        "url": url,
        "dest_path": f"models/{subdir}/{filename}",
        "expected_sha256": wm.get("hash") or wm.get("sha256"),
        "auth": "hf" if "huggingface.co" in url else "none",
    }


async def _resolve_model_sources(
    missing: list[tuple[str, str, str | None]],
    workflow_models_by_name: dict[str, dict],
    settings: dict,
    prep_id: str,
) -> tuple[list[dict], list[tuple[str, str, str]], dict[str, tuple[str, str, str]]]:
    """Split every missing model into worker-fetch vs local-upload buckets.

    Preference order: workflow metadata → opt-in lookup chain (Manager /
    HF cache / CivitAI) → local upload. A model that can be fetched by
    the worker AND has a local copy gets recorded in ``worker_fallbacks``
    so a worker failure can fall back to a local upload.
    """
    use_source = settings.get("downloadModelsFromTheSource", False)
    civitai_key = settings.get("civitaiApiKey") or None

    if missing:
        _send_event("progress", {
            "prep_id": prep_id,
            "message": f"Resolving sources for {len(missing)} model(s)...",
        })

    worker_downloads: list[dict] = []
    upload_queue: list[tuple[str, str, str]] = []
    worker_fallbacks: dict[str, tuple[str, str, str]] = {}

    for subdir, filename, local_path in missing:
        _raise_if_cancelled(prep_id, "source lookup")

        descriptor = _workflow_metadata_descriptor(subdir, filename, workflow_models_by_name.get(filename))
        if descriptor:
            print(_PREFIX, f"Workflow metadata hit: {filename} -> {descriptor['url']}")

        if descriptor is None and use_source:
            descriptor = await asyncio.to_thread(
                lookup_model, subdir, filename, local_path, civitai_key
            )

        if descriptor:
            worker_downloads.append(dict(descriptor))
            if local_path:
                worker_fallbacks[filename] = (subdir, filename, local_path)
        elif local_path:
            upload_queue.append((subdir, filename, local_path))
        else:
            print(_PREFIX, f"Model not found locally and no source: {subdir}/{filename}")

    return worker_downloads, upload_queue, worker_fallbacks


def _build_model_status(
    worker_downloads: list[dict],
    upload_queue: list[tuple[str, str, str]],
) -> tuple[list[str], dict[str, dict]]:
    """Seed a single ordered list + status map covering every planned
    worker fetch and local upload, so the job card can show one unified
    per-file list with icons.
    """
    planned_order: list[str] = []
    model_status: dict[str, dict] = {}
    for d in worker_downloads:
        fname = os.path.basename(d["dest_path"])
        if fname not in model_status:
            planned_order.append(fname)
            model_status[fname] = {"filename": fname, "status": "pending"}
    for (_subdir, filename, _local_path) in upload_queue:
        if filename not in model_status:
            planned_order.append(filename)
            model_status[filename] = {"filename": filename, "status": "pending"}
    return planned_order, model_status


def _make_progress_emitter(prep_id: str, planned_order: list[str], model_status: dict[str, dict]):
    """Return an ``emit(label)`` closure that sends a ``fetch_progress``
    event reflecting the current state of ``model_status``.
    """
    def emit(label: str) -> None:
        ordered = [model_status[f] for f in planned_order]
        done = sum(1 for r in ordered if r.get("status") == "done")
        _send_event("fetch_progress", {
            "prep_id": prep_id,
            "message": f"{label} {done}/{len(ordered)}",
            "done": done,
            "total": len(ordered),
            "results": ordered,
        })
    return emit


async def _run_worker_fetches(
    endpoint_id: str,
    api_key: str,
    settings: dict,
    worker_downloads: list[dict],
    worker_fallbacks: dict[str, tuple[str, str, str]],
    upload_queue: list[tuple[str, str, str]],
    planned_order: list[str],
    model_status: dict[str, dict],
    emit_progress,
) -> None:
    """Drive the worker's ``fetch_models`` action and, for any files the
    worker couldn't pull, append a local-upload fallback to ``upload_queue``.
    """
    def _on_fetch_progress(output: dict) -> None:
        by_name = {r.get("filename"): r for r in (output.get("results") or [])}
        current = output.get("current_filename") or ""
        for d in worker_downloads:
            fname = os.path.basename(d["dest_path"])
            existing = by_name.get(fname)
            if existing:
                model_status[fname] = existing
            elif fname == current and model_status[fname].get("status") == "pending":
                model_status[fname] = {"filename": fname, "status": "downloading"}
        emit_progress(f"Fetching {current} —" if current else "Fetching models")

    emit_progress("Fetching models")

    try:
        fetch_output = await _runpod_streaming_action(
            endpoint_id,
            api_key,
            "fetch_models",
            {
                "downloads": worker_downloads,
                "hf_token": settings.get("hfToken") or "",
                "civitai_key": settings.get("civitaiApiKey") or "",
            },
            _on_fetch_progress,
        )
    except Exception as e:
        print(_PREFIX, f"fetch_models action failed: {e}")
        # Full action failure — mark every planned download as failed
        # so the fallback loop below re-routes them to local upload.
        fetch_output = {"results": [
            {"filename": os.path.basename(d["dest_path"]), "status": "failed", "error": str(e)}
            for d in worker_downloads
        ]}

    for result in (fetch_output.get("results") or []):
        if result.get("status") == "done":
            continue
        fname = result.get("filename", "")
        fallback = worker_fallbacks.get(fname)
        if fallback:
            print(_PREFIX, f"Worker failed {fname} ({result.get('error')}); falling back to local upload")
            upload_queue.append(fallback)
            if fname not in model_status:
                planned_order.append(fname)
            model_status[fname] = {"filename": fname, "status": "pending"}
        else:
            print(_PREFIX, f"Worker failed {fname} with no local fallback available")


async def _upload_local_models(
    settings: dict,
    bucket: str,
    upload_queue: list[tuple[str, str, str]],
    prep_id: str,
    model_status: dict[str, dict],
    emit_progress,
) -> None:
    """Upload every (subdir, filename, local_path) in the queue to S3.

    Updates ``model_status`` in place and emits a per-file progress
    event while each upload is in flight.
    """
    if not upload_queue:
        return
    emit_progress("Uploading models")

    for subdir, filename, local_path in upload_queue:
        _raise_if_cancelled(prep_id, "model upload")
        s3_key = f"models/{subdir}/{filename}"
        model_status[filename] = {"filename": filename, "status": "uploading"}
        emit_progress(f"Uploading {filename} —")
        print(_PREFIX, f"Uploading missing model: {local_path} -> {s3_key}")

        def _model_progress(uploaded, total, _fn=filename, _pid=prep_id):
            pct = int(uploaded / total * 100) if total else 100
            _send_event("upload_progress", {
                "prep_id": _pid,
                "message": f"Uploading model: {_fn}",
                "percent": pct,
                "uploaded_mb": round(uploaded / (1024 * 1024), 1),
                "total_mb": round(total / (1024 * 1024), 1),
            })

        try:
            await asyncio.to_thread(
                upload_file, _s3_settings(settings), bucket, s3_key, local_path, _model_progress
            )
            model_status[filename] = {"filename": filename, "status": "done"}
        except Exception as e:
            print(_PREFIX, f"Upload failed for {filename}: {e}")
            model_status[filename] = {"filename": filename, "status": "failed", "error": str(e)}
            emit_progress("Uploading models")
            raise
        emit_progress("Uploading models")


async def _prepare_models(
    settings: dict,
    bucket: str,
    client,
    workflow: dict,
    workflow_models_by_name: dict[str, dict],
    endpoint_id: str,
    api_key: str,
    prep_id: str,
) -> None:
    """Orchestrate the full model-preparation phase:
    scan → resolve sources → worker fetches → local uploads.
    """
    model_refs = _scan_model_files(workflow)
    if not model_refs:
        return

    missing = await _identify_missing_models(client, bucket, model_refs, prep_id)
    worker_downloads, upload_queue, worker_fallbacks = await _resolve_model_sources(
        missing, workflow_models_by_name, settings, prep_id,
    )
    planned_order, model_status = _build_model_status(worker_downloads, upload_queue)
    emit_progress = _make_progress_emitter(prep_id, planned_order, model_status)

    if worker_downloads:
        await _run_worker_fetches(
            endpoint_id, api_key, settings,
            worker_downloads, worker_fallbacks, upload_queue,
            planned_order, model_status, emit_progress,
        )

    await _upload_local_models(
        settings, bucket, upload_queue, prep_id, model_status, emit_progress,
    )


async def _submit_workflow_to_runpod(
    endpoint_id: str,
    api_key: str,
    workflow: dict,
    input_files: dict,
    settings: dict,
    prep_id: str,
) -> web.Response:
    """POST /run with the workflow payload and start the background poller."""
    payload = {"input": {"workflow": workflow, "input_files": input_files}}
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"https://api.runpod.ai/v2/{endpoint_id}/run",
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
        ) as resp:
            result = await resp.json()

    if "id" not in result:
        raise _SubmitError(_extract_error(result, "Failed to submit job"), 500)

    job_id = result["id"]
    print(_PREFIX, f"Job submitted: {job_id}")
    _send_event("queued", {"job_id": job_id, "prep_id": prep_id})

    task = asyncio.create_task(_poll_and_finish(job_id, settings, input_files))
    _active_tasks[job_id] = task

    return web.json_response({
        "job_id": job_id,
        "status": result.get("status", "IN_QUEUE"),
    })


def _workflow_models_by_name(data: dict) -> dict[str, dict]:
    """Index the optional ``workflow_models`` metadata payload by filename."""
    indexed: dict[str, dict] = {}
    for entry in data.get("workflow_models") or []:
        if isinstance(entry, dict) and entry.get("name") and entry.get("url"):
            indexed[entry["name"]] = entry
    return indexed


async def _do_submit(data: dict):
    settings = data.get("settings", {})
    workflow = data.get("workflow", {})
    prep_id = data.get("prep_id", "")
    workflow_models_by_name = _workflow_models_by_name(data)

    api_key = settings.get("apiKey", "")
    endpoint_id = settings.get("endpointId", "")
    bucket = settings.get("bucketName", "")

    try:
        _validate_settings(settings)

        _send_event("progress", {"prep_id": prep_id, "message": "Validating credentials..."})
        await _validate_runpod_health(endpoint_id, api_key)
        client = await _validate_s3(settings, bucket)

        _send_event("progress", {"prep_id": prep_id, "message": "Waiting for worker..."})
        await _fetch_and_check_worker_version(endpoint_id, api_key)
        _raise_if_cancelled(prep_id, "worker ping")

        _send_event("progress", {"prep_id": prep_id, "message": "Checking custom nodes..."})
        await _check_node_compatibility(endpoint_id, api_key, workflow)
        _raise_if_cancelled(prep_id, "node check")

        input_files = await _upload_input_files(settings, bucket, workflow, prep_id)

        if settings.get("uploadMissingModels", True):
            await _prepare_models(
                settings, bucket, client, workflow, workflow_models_by_name,
                endpoint_id, api_key, prep_id,
            )

        _send_event("progress", {"prep_id": prep_id, "message": "Submitting to RunPod..."})
        return await _submit_workflow_to_runpod(
            endpoint_id, api_key, workflow, input_files, settings, prep_id,
        )
    except _SubmitError as e:
        print(_PREFIX, e.log or e.message)
        return web.json_response({"error": e.message}, status=e.status)


async def _poll_and_finish(job_id: str, settings: dict, input_files: dict):
    """Background task: poll RunPod for job status, download outputs on completion."""
    api_key = settings.get("apiKey", "")
    endpoint_id = settings.get("endpointId", "")

    try:
        while True:
            await asyncio.sleep(2)
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"https://api.runpod.ai/v2/{endpoint_id}/status/{job_id}",
                        headers={"Authorization": f"Bearer {api_key}"},
                    ) as resp:
                        if resp.status == 404:
                            print(_PREFIX, f"Job {job_id}: not found (404)")
                            _send_event("failed", {"job_id": job_id, "error": "Job not found"})
                            return
                        result = await resp.json()
            except Exception as e:
                print(_PREFIX, f"Job {job_id}: status check failed: {e}")
                continue

            status = result.get("status", "UNKNOWN")

            if status == "IN_PROGRESS":
                _send_event("running", {"job_id": job_id})
            elif status == "COMPLETED":
                output = result.get("output", {})
                print(_PREFIX, f"Job {job_id}: COMPLETED, output:\n{json.dumps(output, indent=2)}")
                output_files = output.get("output_files", [])
                downloaded = await _download_and_cleanup(settings, output_files, input_files)
                _send_event("completed", {"job_id": job_id, "files": downloaded})
                return
            elif status == "FAILED":
                error = _extract_error(result, "Job failed")
                print(_PREFIX, f"Job {job_id}: FAILED: {error}")
                _send_event("failed", {"job_id": job_id, "error": error})
                return
            elif status == "CANCELLED":
                print(_PREFIX, f"Job {job_id}: CANCELLED")
                _send_event("cancelled", {"job_id": job_id})
                return
            elif status == "TIMED_OUT":
                print(_PREFIX, f"Job {job_id}: TIMED_OUT")
                _send_event("timed_out", {
                    "job_id": job_id,
                    "error": "Job timed out — worker did not start or stopped reporting before the endpoint timeout",
                })
                return

    except asyncio.CancelledError:
        print(_PREFIX, f"Job {job_id}: polling cancelled")
    finally:
        _active_tasks.pop(job_id, None)


async def _download_and_cleanup(settings: dict, output_files: list, input_files: dict):
    """Download output files from S3 and optionally clean up."""
    bucket = settings.get("bucketName", "")
    delete_inputs = settings.get("deleteInputsAfterJob", False)
    delete_outputs = settings.get("deleteOutputsAfterJob", True)

    try:
        client = _make_s3_client(settings)
    except Exception as e:
        print(_PREFIX, f"S3 client error: {e}")
        return []

    downloaded = []
    if output_files:
        output_dir = _get_output_directory()
        for rel_path in output_files:
            s3_key = f"outputs/{rel_path}"
            dest = os.path.join(output_dir, rel_path)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            try:
                _send_event("progress", {"message": f"Downloading: {os.path.basename(rel_path)}"})
                print(_PREFIX, f"Downloading {s3_key} -> {dest}")
                download_file(client, bucket, s3_key, dest)
                downloaded.append(rel_path)
            except Exception as e:
                print(_PREFIX, f"Failed to download {s3_key}: {e}")

    if delete_outputs and output_files:
        try:
            s3_keys = [f"outputs/{rel_path}" for rel_path in output_files]
            print(_PREFIX, f"Deleting {len(s3_keys)} output(s) from S3")
            delete_objects(client, bucket, s3_keys)
        except Exception as e:
            print(_PREFIX, f"Failed to delete outputs: {e}")

    if delete_inputs and input_files:
        try:
            s3_keys = list(input_files.values())
            print(_PREFIX, f"Deleting {len(s3_keys)} input(s) from S3")
            delete_objects(client, bucket, s3_keys)
        except Exception as e:
            print(_PREFIX, f"Failed to delete inputs: {e}")

    return downloaded


@routes.post("/RunOnRunpod/cancel")
async def cancel_job(request):
    data = await request.json()
    settings = data.get("settings", {})
    job_id = data.get("job_id", "")

    # Cancel the background polling task for this job
    task = _active_tasks.get(job_id)
    if task and not task.done():
        task.cancel()

    api_key = settings.get("apiKey", "")
    endpoint_id = settings.get("endpointId", "")

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"https://api.runpod.ai/v2/{endpoint_id}/cancel/{job_id}",
            headers={"Authorization": f"Bearer {api_key}"},
        ) as resp:
            result = await resp.json()

    return web.json_response({
        "status": result.get("status", "CANCELLED"),
    })


@routes.post("/RunOnRunpod/purge-queue")
async def purge_queue(request):
    """Cancel every active job and empty the endpoint queue atomically.

    - Marks every tracked prep_id as cancelled so any in-progress submit
      stops after its current upload.
    - Calls RunPod's endpoint-wide /purge-queue to clear all queued jobs
      (note: this affects ALL jobs on the endpoint, not just ours).
    - Calls /cancel/{job_id} for every running job we track, since
      purge-queue doesn't interrupt jobs already running on a worker.
    - Cancels all background polling tasks.
    """
    data = await request.json()
    settings = data.get("settings", {})
    api_key = settings.get("apiKey", "")
    endpoint_id = settings.get("endpointId", "")

    if not api_key or not endpoint_id:
        return web.json_response({"error": "API key and endpoint ID required"}, status=400)

    # Mark every known prep as cancelled. Callers send in-progress prep_ids
    # in the body so we can also cancel preps we haven't seen yet via the
    # submit route (rare race condition).
    prep_ids = data.get("prep_ids", []) or []
    for pid in prep_ids:
        if pid:
            _cancelled_preps.add(pid)

    headers = {"Authorization": f"Bearer {api_key}"}
    tracked_job_ids = list(_active_tasks.keys())

    async with aiohttp.ClientSession() as session:
        # Cancel tracked running/queued jobs individually. purge-queue only
        # touches IN_QUEUE jobs, so this is the only way to stop anything
        # already running on a worker.
        for jid in tracked_job_ids:
            try:
                async with session.post(
                    f"https://api.runpod.ai/v2/{endpoint_id}/cancel/{jid}",
                    headers=headers,
                ) as resp:
                    await resp.read()
            except Exception as e:
                print(_PREFIX, f"purge-queue: cancel {jid} failed: {e}")

        # Endpoint-wide queue purge.
        try:
            async with session.post(
                f"https://api.runpod.ai/v2/{endpoint_id}/purge-queue",
                headers=headers,
            ) as resp:
                purge_result = await resp.json()
        except Exception as e:
            print(_PREFIX, f"purge-queue: purge failed: {e}")
            purge_result = {"error": str(e)}

    # Cancel in-process polling tasks so they stop consuming API calls.
    for jid, task in list(_active_tasks.items()):
        if not task.done():
            task.cancel()

    return web.json_response({
        "cancelled_jobs": len(tracked_job_ids),
        "cancelled_preps": len(prep_ids),
        "purge_result": purge_result,
    })


@routes.post("/RunOnRunpod/check-latency")
async def check_latency(request):
    """Measure TCP connect latency to every Runpod S3 datacenter. Streams
    per-region progress to the frontend via WebSocket events so the modal
    can fill in its table as results arrive, rather than making the user
    stare at a spinner for the whole run.
    """
    def _on_start(total: int):
        _send_event("latency_start", {"total": total})

    def _on_progress(result: dict):
        _send_event("latency_progress", {"result": result})

    try:
        results = await check_all_regions(on_progress=_on_progress, on_start=_on_start)
    except Exception as e:
        print(_PREFIX, f"check-latency failed: {e}")
        _send_event("latency_error", {"error": str(e)})
        return web.json_response({"error": str(e)}, status=500)
    _send_event("latency_done", {"results": results})
    return web.json_response({"results": results})


@routes.get("/RunOnRunpod/info")
async def get_info(_request):
    """Return plugin metadata + last seen worker info for the sidebar."""
    return web.json_response({
        "plugin_version": PLUGIN_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "worker_info": _last_worker_info or None,
    })


@routes.post("/RunOnRunpod/recover-jobs")
async def recover_jobs(request):
    """Re-attach to in-flight jobs after a page reload or ComfyUI restart.

    For each persisted job_id the frontend hands us, query RunPod for
    its current status. If the job is still IN_QUEUE/IN_PROGRESS and
    we don't already have a polling task for it, start a fresh one.
    Recovered jobs run with empty input_files since the original
    upload context is gone — that means input cleanup won't happen
    automatically for them, but the workflow output still gets
    downloaded normally.
    """
    data = await request.json()
    settings = data.get("settings", {})
    job_ids = data.get("job_ids", []) or []
    prep_ids = data.get("prep_ids", []) or []
    api_key = settings.get("apiKey", "")
    endpoint_id = settings.get("endpointId", "")

    # Resolve prep_ids against the in-memory set of preps the backend
    # is currently working on. If the prep is still active, the
    # frontend keeps the card and the still-running prep task will
    # push websocket events to the new page session normally. If the
    # prep is gone (e.g., ComfyUI restarted), it can never complete,
    # so the card is dropped.
    preps: list[dict] = []
    for prep_id in prep_ids:
        if not isinstance(prep_id, str) or not prep_id:
            continue
        if prep_id in _active_preps:
            preps.append({"prep_id": prep_id, "state": "preparing"})
        else:
            preps.append({"prep_id": prep_id, "state": "lost"})

    if not api_key or not endpoint_id:
        # Without RunPod credentials we can still report prep state.
        return web.json_response({"recovered": [], "preps": preps})

    recovered: list[dict] = []

    async with aiohttp.ClientSession() as session:
        for job_id in job_ids:
            if not isinstance(job_id, str) or not job_id:
                continue

            # If we already have a polling task for this job, the
            # frontend will receive its events normally — just report
            # the current state without re-attaching.
            existing_task = _active_tasks.get(job_id)
            already_polling = existing_task is not None and not existing_task.done()

            try:
                async with session.get(
                    f"https://api.runpod.ai/v2/{endpoint_id}/status/{job_id}",
                    headers={"Authorization": f"Bearer {api_key}"},
                ) as resp:
                    if resp.status == 404:
                        recovered.append({"job_id": job_id, "state": "lost"})
                        continue
                    result = await resp.json()
            except Exception as e:
                print(_PREFIX, f"recover-jobs: status check failed for {job_id}: {e}")
                recovered.append({"job_id": job_id, "state": "lost"})
                continue

            status = result.get("status", "UNKNOWN")
            entry: dict = {"job_id": job_id}

            if status == "COMPLETED":
                entry["state"] = "completed"
                output = result.get("output", {}) or {}
                output_files = output.get("output_files", []) if isinstance(output, dict) else []
                # Download outputs to local; pass empty input_files
                # because the original upload context is gone.
                downloaded = await _download_and_cleanup(settings, output_files, {})
                entry["files"] = downloaded
            elif status in ("IN_QUEUE", "IN_PROGRESS"):
                entry["state"] = "running" if status == "IN_PROGRESS" else "queued"
                if not already_polling:
                    task = asyncio.create_task(_poll_and_finish(job_id, settings, {}))
                    _active_tasks[job_id] = task
                    print(_PREFIX, f"recover-jobs: re-attached polling for {job_id}")
            elif status == "FAILED":
                entry["state"] = "failed"
                entry["error"] = _extract_error(result, "Job failed")
            elif status == "CANCELLED":
                entry["state"] = "cancelled"
            elif status == "TIMED_OUT":
                entry["state"] = "timed_out"
            else:
                entry["state"] = "lost"

            recovered.append(entry)

    return web.json_response({"recovered": recovered, "preps": preps})


@routes.post("/RunOnRunpod/check-local-outputs")
async def check_local_outputs(request):
    """Return which of the given relative output paths still exist on disk.

    Used by the frontend on page load to filter persisted job cards whose
    files have since been deleted by the user.
    """
    data = await request.json()
    rel_paths = data.get("files") or []

    output_dir = os.path.realpath(_get_output_directory())
    existing: list[str] = []

    for rel_path in rel_paths:
        if not isinstance(rel_path, str) or not rel_path:
            continue
        candidate = os.path.realpath(os.path.join(output_dir, rel_path))
        if not (candidate == output_dir or candidate.startswith(output_dir + os.sep)):
            continue
        if os.path.isfile(candidate):
            existing.append(rel_path)

    return web.json_response({"existing": existing})


@routes.post("/RunOnRunpod/delete-local-outputs")
async def delete_local_outputs(request):
    """Delete files under ComfyUI's output directory by relative path.

    Paths are clamped to stay within the output directory so a malformed
    request cannot escape it. Missing files are silently ignored.
    """
    data = await request.json()
    rel_paths = data.get("files") or []

    output_dir = os.path.realpath(_get_output_directory())
    deleted: list[str] = []
    errors: list[dict] = []
    parent_dirs: set[str] = set()

    for rel_path in rel_paths:
        if not isinstance(rel_path, str) or not rel_path:
            continue
        candidate = os.path.realpath(os.path.join(output_dir, rel_path))
        if not (candidate == output_dir or candidate.startswith(output_dir + os.sep)):
            errors.append({"file": rel_path, "error": "outside output dir"})
            continue
        try:
            if os.path.isfile(candidate):
                os.remove(candidate)
                deleted.append(rel_path)
                parent_dirs.add(os.path.dirname(candidate))
                print(_PREFIX, f"Deleted local output: {candidate}")
        except Exception as e:
            print(_PREFIX, f"Failed to delete local output {candidate}: {e}")
            errors.append({"file": rel_path, "error": str(e)})

    # Walk up each parent and rmdir while empty, stopping at output_dir.
    # os.rmdir only succeeds on empty dirs, so unrelated files are safe.
    for start in parent_dirs:
        current = start
        while current != output_dir and current.startswith(output_dir + os.sep):
            try:
                os.rmdir(current)
                print(_PREFIX, f"Removed empty output folder: {current}")
            except OSError:
                break
            current = os.path.dirname(current)

    return web.json_response({"deleted": deleted, "errors": errors})


@routes.post("/RunOnRunpod/clean")
async def clean_storage(request):
    """Delete all objects under one or more prefixes on the network volume.

    ``folder`` can be ``inputs``, ``outputs``, or ``all``. ``all`` deletes
    inputs, outputs, **and models** — it's the nuke button.
    """
    data = await request.json()
    settings = data.get("settings", {})
    folder = data.get("folder", "")

    if folder == "all":
        prefixes = ["inputs/", "outputs/", "models/"]
    elif folder in ("inputs", "outputs"):
        prefixes = [f"{folder}/"]
    else:
        return web.json_response({"error": "Invalid folder"}, status=400)

    try:
        client = _make_s3_client(settings)
        bucket = settings.get("bucketName", "")
        total_deleted = 0
        per_prefix: dict[str, int] = {}
        for prefix in prefixes:
            keys = await asyncio.to_thread(list_objects, client, bucket, prefix)
            if keys:
                await asyncio.to_thread(delete_objects, client, bucket, keys)
                total_deleted += len(keys)
                per_prefix[prefix] = len(keys)
                print(_PREFIX, f"Cleaned {len(keys)} object(s) from {prefix}")
            else:
                per_prefix[prefix] = 0
        return web.json_response({"deleted": total_deleted, "per_prefix": per_prefix})
    except Exception as e:
        print(_PREFIX, f"Clean error: {e}")
        return web.json_response({"error": str(e)}, status=400)
