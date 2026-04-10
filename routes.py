import os
import uuid

import aiohttp
from aiohttp import web
from server import PromptServer

from .s3_utils import get_s3_client, upload_file

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
        "s3_endpoint": settings.get("s3Endpoint"),
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
    s3_endpoint = settings.get("s3Endpoint", "")
    if not bucket or not s3_access or not s3_secret or not s3_endpoint:
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
    s3_endpoint = settings.get("s3Endpoint", "")

    if not bucket or not s3_access or not s3_secret or not s3_endpoint:
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
            s3_key = f"inputs/{filename}"
            upload_file(client, bucket, s3_key, file_path)
            input_files[filename] = s3_key

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

    if status == "FAILED":
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
