import asyncio
import json
import os

import aiohttp
from aiohttp import web
from server import PromptServer

from .s3_utils import get_s3_client, upload_file, upload_file_dedup, download_file, delete_objects, list_objects, key_exists
from .model_lookup import lookup_model
from .latency import check_all_regions

_PREFIX = "[RunOnRunpod]"

routes = PromptServer.instance.routes


# In-memory state for active jobs: {job_id: asyncio.Task}
_active_tasks = {}

# Set of prep_ids whose submit/prep phase has been cancelled. submit_job
# checks membership at each cancellation-aware point; entries are discarded
# once submit_job returns (successful or not).
_cancelled_preps: set[str] = set()


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


async def _runpod_action(endpoint_id: str, api_key: str, action: str, payload: dict | None = None) -> dict:
    """Submit an action to the RunPod worker via /run and poll until complete.

    Returns the job output dict on success, raises RuntimeError on failure.
    """
    headers = {"Authorization": f"Bearer {api_key}"}
    body: dict = {"action": action}
    if payload:
        body.update(payload)

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"https://api.runpod.ai/v2/{endpoint_id}/run",
            headers=headers,
            json={"input": body},
        ) as resp:
            result = await resp.json()

    job_id = result.get("id")
    if not job_id:
        raise RuntimeError(result.get("error", f"Failed to submit {action}"))

    async with aiohttp.ClientSession() as session:
        while True:
            await asyncio.sleep(2)
            async with session.get(
                f"https://api.runpod.ai/v2/{endpoint_id}/status/{job_id}",
                headers=headers,
            ) as resp:
                status_result = await resp.json()

            status = status_result.get("status", "UNKNOWN")
            if status == "COMPLETED":
                return status_result.get("output", {})
            elif status in ("FAILED", "CANCELLED", "TIMED_OUT"):
                error = status_result.get("error") or status_result.get("output", {}).get("error") or status
                raise RuntimeError(f"Worker error: {error}")


async def _runpod_streaming_action(
    endpoint_id: str,
    api_key: str,
    action: str,
    payload: dict,
    on_progress,
) -> dict:
    """Submit an action and poll /status, invoking on_progress(output) each
    time the IN_PROGRESS output changes (driven by the worker calling
    runpod.serverless.progress_update). Returns the final COMPLETED output.
    """
    headers = {"Authorization": f"Bearer {api_key}"}
    body: dict = {"action": action}
    body.update(payload)

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"https://api.runpod.ai/v2/{endpoint_id}/run",
            headers=headers,
            json={"input": body},
        ) as resp:
            result = await resp.json()

    job_id = result.get("id")
    if not job_id:
        raise RuntimeError(result.get("error", f"Failed to submit {action}"))

    last_progress = None
    async with aiohttp.ClientSession() as session:
        while True:
            await asyncio.sleep(2)
            async with session.get(
                f"https://api.runpod.ai/v2/{endpoint_id}/status/{job_id}",
                headers=headers,
            ) as resp:
                status_result = await resp.json()

            status = status_result.get("status", "UNKNOWN")
            output = status_result.get("output")

            if status == "IN_PROGRESS":
                # progress_update sets output to the current progress payload.
                if output and output != last_progress:
                    last_progress = output
                    try:
                        on_progress(output)
                    except Exception as cb_exc:
                        print(_PREFIX, f"streaming on_progress error: {cb_exc}")
                continue

            if status == "COMPLETED":
                return output if isinstance(output, dict) else {}
            if status in ("FAILED", "CANCELLED", "TIMED_OUT"):
                error = status_result.get("error") or (isinstance(output, dict) and output.get("error")) or status
                raise RuntimeError(f"Worker error: {error}")


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
    try:
        return await _do_submit(data)
    finally:
        # Always discard so a cancelled prep doesn't leak its flag into
        # a future submit that happens to pick the same prep_id.
        _cancelled_preps.discard(prep_id)


async def _do_submit(data: dict):
    settings = data.get("settings", {})
    workflow = data.get("workflow", {})
    prep_id = data.get("prep_id", "")

    api_key = settings.get("apiKey", "")
    endpoint_id = settings.get("endpointId", "")

    if not api_key or not endpoint_id:
        print(_PREFIX,"RunPod API Key and Endpoint ID are required")
        return web.json_response(
            {"error": "RunPod API Key and Endpoint ID are required"}, status=400
        )

    bucket = settings.get("bucketName", "")
    s3_access = settings.get("s3AccessKey", "")
    s3_secret = settings.get("s3SecretKey", "")
    endpoint_url = settings.get("endpointUrl", "")

    if not bucket or not s3_access or not s3_secret or not endpoint_url:
        print(_PREFIX,"S3 credentials, endpoint URL, and bucket name are required")
        return web.json_response(
            {"error": "S3 credentials, endpoint URL, and bucket name are required"}, status=400
        )

    _send_event("progress", {"prep_id": prep_id, "message": "Validating credentials..."})

    # Validate RunPod API
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://api.runpod.ai/v2/{endpoint_id}/health",
                headers={"Authorization": f"Bearer {api_key}"},
            ) as resp:
                if resp.status != 200:
                    print(_PREFIX,f"RunPod API health check failed: {resp.status}")
                    return web.json_response(
                        {"error": f"RunPod API health check failed (status {resp.status})"}, status=400
                    )
    except Exception as e:
        print(_PREFIX,f"RunPod API health check error: {e}")
        return web.json_response(
            {"error": f"RunPod API error: {e}"}, status=400
        )

    # Validate S3 access
    try:
        client = _make_s3_client(settings)
        await asyncio.to_thread(client.head_bucket, Bucket=bucket)
    except Exception as e:
        print(_PREFIX,f"S3 storage validation failed: {e}")
        return web.json_response(
            {"error": f"S3 storage error: {e}"}, status=400
        )

    # Wait for worker to be available
    _send_event("progress", {"prep_id": prep_id, "message": "Waiting for worker..."})
    try:
        await _runpod_action(endpoint_id, api_key, "ping")
    except Exception as e:
        print(_PREFIX, f"Worker ping failed: {e}")
        return web.json_response({"error": f"Worker not available: {e}"}, status=500)

    if prep_id in _cancelled_preps:
        print(_PREFIX, "Submit cancelled during worker ping")
        return web.json_response({"error": "Cancelled"}, status=499)

    # Check node compatibility with the worker
    _send_event("progress", {"prep_id": prep_id, "message": "Checking custom nodes..."})
    try:
        output = await _runpod_action(endpoint_id, api_key, "node_list")
        worker_nodes = set(output.get("node_list", []))
        workflow_nodes = {node.get("class_type") for node in workflow.values() if node.get("class_type")}
        missing_nodes = sorted(workflow_nodes - worker_nodes)
        if missing_nodes:
            msg = f"Missing custom nodes on worker: {', '.join(missing_nodes)}"
            print(_PREFIX, msg)
            return web.json_response({"error": msg}, status=400)
    except Exception as e:
        print(_PREFIX, f"Node list check failed: {e}")
        return web.json_response({"error": f"Node check failed: {e}"}, status=500)

    if prep_id in _cancelled_preps:
        print(_PREFIX, "Submit cancelled during node check")
        return web.json_response({"error": "Cancelled"}, status=499)

    # Upload input files to network volume via S3
    input_files = {}
    input_file_refs = _scan_input_files(workflow)

    if input_file_refs:
        input_dir = _get_input_directory()

        for filename in input_file_refs:
            if prep_id in _cancelled_preps:
                print(_PREFIX, "Submit cancelled during input upload")
                return web.json_response({"error": "Cancelled"}, status=499)
            file_path = os.path.join(input_dir, filename)
            if not os.path.exists(file_path):
                return web.json_response(
                    {"error": f"Input file not found: {filename}"}, status=400
                )
            _send_event("progress", {"prep_id": prep_id, "message": f"Uploading input: {filename}"})
            s3_key = await asyncio.to_thread(upload_file_dedup, _s3_settings(settings), bucket, file_path)
            input_files[filename] = s3_key

    # Upload missing models to network volume
    if settings.get("uploadMissingModels", True):
        model_refs = _scan_model_files(workflow)
        if model_refs:
            # Pass 1: identify which models are actually missing and, if the
            # "Download from the source" option is enabled, try to look up a
            # remote source for each one. Models with a hit go to the worker's
            # fetch_models action; models without a hit go to the local upload
            # path as before.
            missing: list[tuple[str, str, str | None]] = []  # (subdir, filename, local_path)
            for (subdir, filename) in model_refs:
                if prep_id in _cancelled_preps:
                    print(_PREFIX, "Submit cancelled during model scan")
                    return web.json_response({"error": "Cancelled"}, status=499)
                s3_key = f"models/{subdir}/{filename}"
                exists = await asyncio.to_thread(key_exists, client, bucket, s3_key)
                if exists:
                    print(_PREFIX, f"Model already on volume: {s3_key}")
                    continue
                local_path = _find_model_file(subdir, filename)
                missing.append((subdir, filename, local_path))

            use_source = settings.get("downloadFromTheSource", False)
            worker_downloads: list[dict] = []
            upload_queue: list[tuple[str, str, str]] = []  # (subdir, filename, local_path)
            # filename -> (subdir, filename, local_path) so worker failures
            # can fall back to the original local file for an upload retry.
            worker_fallbacks: dict[str, tuple[str, str, str]] = {}

            if use_source:
                civitai_key = settings.get("civitaiApiKey") or None
                _send_event("progress", {
                    "prep_id": prep_id,
                    "message": f"Looking up sources for {len(missing)} model(s)...",
                })
                for subdir, filename, local_path in missing:
                    if prep_id in _cancelled_preps:
                        print(_PREFIX, "Submit cancelled during source lookup")
                        return web.json_response({"error": "Cancelled"}, status=499)
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
            else:
                for subdir, filename, local_path in missing:
                    if local_path:
                        upload_queue.append((subdir, filename, local_path))
                    else:
                        print(_PREFIX, f"Model not found locally: {subdir}/{filename}")

            # Unified model status tracking — all worker fetches and local
            # uploads share a single ordered list so the job card can show
            # every model with a per-file status icon.
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

            def _emit_model_progress(label: str, _pid=prep_id):
                ordered = [model_status[f] for f in planned_order]
                done = sum(1 for r in ordered if r.get("status") == "done")
                _send_event("fetch_progress", {
                    "prep_id": _pid,
                    "message": f"{label} {done}/{len(ordered)}",
                    "done": done,
                    "total": len(ordered),
                    "results": ordered,
                })

            # Worker-side downloads.
            if worker_downloads:
                def _fetch_progress(output: dict):
                    by_name = {r.get("filename"): r for r in (output.get("results") or [])}
                    current = output.get("current_filename") or ""
                    for fname in [os.path.basename(d["dest_path"]) for d in worker_downloads]:
                        existing = by_name.get(fname)
                        if existing:
                            model_status[fname] = existing
                        elif fname == current and model_status[fname].get("status") == "pending":
                            model_status[fname] = {"filename": fname, "status": "downloading"}
                    label = "Fetching models"
                    if current:
                        label = f"Fetching {current} —"
                    _emit_model_progress(label)

                _emit_model_progress("Fetching models")

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
                        _fetch_progress,
                    )
                except Exception as e:
                    print(_PREFIX, f"fetch_models action failed: {e}")
                    # Full failure of the action — fall back to uploading
                    # everything we had planned for worker download.
                    fetch_output = {"results": [
                        {"filename": os.path.basename(d["dest_path"]), "status": "failed", "error": str(e)}
                        for d in worker_downloads
                    ]}

                for result in (fetch_output.get("results") or []):
                    if result.get("status") != "done":
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

            # Local upload path (for misses + worker failures).
            if upload_queue:
                _emit_model_progress("Uploading models")
            for subdir, filename, local_path in upload_queue:
                if prep_id in _cancelled_preps:
                    print(_PREFIX, "Submit cancelled during model upload")
                    return web.json_response({"error": "Cancelled"}, status=499)
                s3_key = f"models/{subdir}/{filename}"
                model_status[filename] = {"filename": filename, "status": "uploading"}
                _emit_model_progress(f"Uploading {filename} —")
                print(_PREFIX, f"Uploading missing model: {local_path} -> {s3_key}")

                def _model_progress(uploaded, total, _fn=filename, _pid=prep_id):
                    pct = int(uploaded / total * 100) if total else 100
                    mb_done = uploaded / (1024 * 1024)
                    mb_total = total / (1024 * 1024)
                    _send_event("upload_progress", {
                        "prep_id": _pid,
                        "message": f"Uploading model: {_fn}",
                        "percent": pct,
                        "uploaded_mb": round(mb_done, 1),
                        "total_mb": round(mb_total, 1),
                    })

                try:
                    await asyncio.to_thread(upload_file, _s3_settings(settings), bucket, s3_key, local_path, _model_progress)
                    model_status[filename] = {"filename": filename, "status": "done"}
                except Exception as e:
                    print(_PREFIX, f"Upload failed for {filename}: {e}")
                    model_status[filename] = {"filename": filename, "status": "failed", "error": str(e)}
                    _emit_model_progress("Uploading models")
                    raise
                _emit_model_progress("Uploading models")

    _send_event("progress", {"prep_id": prep_id, "message": "Submitting to RunPod..."})

    # Submit to RunPod
    payload = {
        "input": {
            "workflow": workflow,
            "input_files": input_files,
        }
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"https://api.runpod.ai/v2/{endpoint_id}/run",
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
        ) as resp:
            result = await resp.json()

    if "id" not in result:
        return web.json_response(
            {"error": result.get("error", "Failed to submit job")}, status=500
        )

    job_id = result["id"]
    print(_PREFIX, f"Job submitted: {job_id}")
    _send_event("queued", {"job_id": job_id, "prep_id": prep_id})

    # Start background task to poll RunPod and handle completion
    task = asyncio.create_task(_poll_and_finish(job_id, settings, input_files))
    _active_tasks[job_id] = task

    return web.json_response({
        "job_id": job_id,
        "status": result.get("status", "IN_QUEUE"),
    })


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
                error = result.get("error") or result.get("output", {}).get("error") or "Job failed"
                print(_PREFIX, f"Job {job_id}: FAILED: {error}")
                _send_event("failed", {"job_id": job_id, "error": str(error)})
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
