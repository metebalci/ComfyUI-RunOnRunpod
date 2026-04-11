import os
import uuid

import aiohttp
from aiohttp import web
from server import PromptServer

from .s3_utils import get_s3_client, upload_file, upload_file_dedup, download_file, delete_objects, key_exists

_PREFIX = "[RunOnRunpod]"

routes = PromptServer.instance.routes

# In-memory job state for the current session
_active_job = {}

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


def _get_models_directory() -> str:
    """Return ComfyUI's models directory path."""
    try:
        import folder_paths
        return folder_paths.models_dir
    except (ImportError, AttributeError):
        return os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "..",
            "models",
        )


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


def _make_s3_client(settings: dict):
    """Create S3 client from settings."""
    return get_s3_client({
        "endpoint_url": settings.get("endpointUrl"),
        "region": settings.get("region"),
        "s3_access_key": settings.get("s3AccessKey"),
        "s3_secret_key": settings.get("s3SecretKey"),
    })


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


@routes.post("/RunOnRunpod/submit")
async def submit_job(request):
    global _active_job

    data = await request.json()
    settings = data.get("settings", {})
    workflow = data.get("workflow", {})

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
        client.head_bucket(Bucket=bucket)
    except Exception as e:
        print(_PREFIX,f"S3 storage validation failed: {e}")
        return web.json_response(
            {"error": f"S3 storage error: {e}"}, status=400
        )

    job_prefix = str(uuid.uuid4())[:8]

    # Upload input files to network volume via S3
    input_files = {}
    input_file_refs = _scan_input_files(workflow)

    if input_file_refs:
        input_dir = _get_input_directory()

        for filename in input_file_refs:
            file_path = os.path.join(input_dir, filename)
            if not os.path.exists(file_path):
                return web.json_response(
                    {"error": f"Input file not found: {filename}"}, status=400
                )
            s3_key = upload_file_dedup(client, bucket, file_path)
            input_files[filename] = s3_key

    # Upload missing models to network volume
    if settings.get("uploadMissingModels", True):
        model_refs = _scan_model_files(workflow)
        if model_refs:
            models_dir = _get_models_directory()
            for (subdir, filename) in model_refs:
                s3_key = f"models/{subdir}/{filename}"
                if not key_exists(client, bucket, s3_key):
                    local_path = os.path.join(models_dir, subdir, filename)
                    if os.path.exists(local_path):
                        print(_PREFIX, f"Uploading missing model: {local_path} -> {s3_key}")
                        upload_file(client, bucket, s3_key, local_path)
                    else:
                        print(_PREFIX, f"Model not found locally: {local_path}")
                else:
                    print(_PREFIX, f"Model already on volume: {s3_key}")

    # Submit to RunPod
    payload = {
        "input": {
            "workflow": workflow,
            "input_files": input_files,
            "job_prefix": job_prefix,
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
    _active_job = {"job_id": job_id}
    print(_PREFIX,f"Job submitted: {job_id}, polling every 2s")

    return web.json_response({
        "job_id": job_id,
        "status": result.get("status", "IN_QUEUE"),
        "input_files": input_files,
    })


@routes.post("/RunOnRunpod/status")
async def get_status(request):
    data = await request.json()
    settings = data.get("settings", {})
    job_id = data.get("job_id", "")

    api_key = settings.get("apiKey", "")
    endpoint_id = settings.get("endpointId", "")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://api.runpod.ai/v2/{endpoint_id}/status/{job_id}",
                headers={"Authorization": f"Bearer {api_key}"},
            ) as resp:
                if resp.status == 404:
                    print(_PREFIX,f"Job {job_id}: not found (404)")
                    return web.json_response({
                        "status": "UNKNOWN",
                        "output": None,
                        "error": "Job not found",
                    })
                result = await resp.json()
    except Exception as e:
        print(_PREFIX,f"Job {job_id}: status check failed: {e}")
        return web.json_response({
            "status": "UNKNOWN",
            "output": None,
            "error": str(e),
        })

    status = result.get("status", "UNKNOWN")
    error = result.get("error")
    output = result.get("output")

    if status == "COMPLETED":
        print(_PREFIX,f"Job {job_id}: COMPLETED, output: {output}")
    elif status == "FAILED":
        print(_PREFIX,f"Job {job_id} FAILED: {error or output}")
    elif status in ("CANCELLED", "TIMED_OUT"):
        print(_PREFIX,f"Job {job_id}: {status}")
    else:
        print(_PREFIX,f"Job {job_id}: {status}")

    return web.json_response({
        "status": status,
        "output": output,
        "error": error,
    })


@routes.post("/RunOnRunpod/download")
async def download_outputs(request):
    """Download output files from S3 to local ComfyUI and optionally clean up."""
    data = await request.json()
    settings = data.get("settings", {})
    output_files = data.get("output_files", [])
    input_files = data.get("input_files", {})
    delete_inputs = data.get("delete_inputs", False)
    delete_outputs = data.get("delete_outputs", False)

    bucket = settings.get("bucketName", "")
    try:
        client = _make_s3_client(settings)
    except Exception as e:
        print(_PREFIX, f"S3 client error: {e}")
        return web.json_response({"error": str(e)}, status=400)

    # Download output files to local ComfyUI output directory
    downloaded = []
    if output_files:
        output_dir = _get_output_directory()
        for rel_path in output_files:
            s3_key = f"outputs/{rel_path}"
            job_prefix = rel_path.split("/")[0]
            basename = os.path.basename(rel_path)
            filename = f"{job_prefix}_{basename}"
            dest = os.path.join(output_dir, filename)
            try:
                print(_PREFIX, f"Downloading {s3_key} -> {dest}")
                download_file(client, bucket, s3_key, dest)
                downloaded.append(filename)
            except Exception as e:
                print(_PREFIX, f"Failed to download {s3_key}: {e}")

    # Clean up remote outputs
    if delete_outputs and output_files:
        try:
            s3_keys = [f"outputs/{rel_path}" for rel_path in output_files]
            print(_PREFIX, f"Deleting {len(s3_keys)} output(s) from S3")
            delete_objects(client, bucket, s3_keys)
        except Exception as e:
            print(_PREFIX, f"Failed to delete outputs: {e}")

    # Clean up remote inputs
    if delete_inputs and input_files:
        try:
            s3_keys = list(input_files.values())
            print(_PREFIX, f"Deleting {len(s3_keys)} input(s) from S3")
            delete_objects(client, bucket, s3_keys)
        except Exception as e:
            print(_PREFIX, f"Failed to delete inputs: {e}")

    return web.json_response({
        "downloaded": downloaded,
    })


@routes.post("/RunOnRunpod/cancel")
async def cancel_job(request):
    global _active_job

    data = await request.json()
    settings = data.get("settings", {})
    job_id = data.get("job_id", "")

    api_key = settings.get("apiKey", "")
    endpoint_id = settings.get("endpointId", "")

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"https://api.runpod.ai/v2/{endpoint_id}/cancel/{job_id}",
            headers={"Authorization": f"Bearer {api_key}"},
        ) as resp:
            result = await resp.json()

    _active_job = {}

    return web.json_response({
        "status": result.get("status", "CANCELLED"),
    })
