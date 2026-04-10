import json
import os
import uuid

import aiohttp
from aiohttp import web
from server import PromptServer

from .s3_utils import (
    generate_presigned_get,
    generate_presigned_put,
    get_s3_client,
    upload_file,
)

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


def _get_settings() -> dict:
    """Read RunOnRunpod settings from ComfyUI's user settings file."""
    user_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "..",
        "user",
        "default",
    )
    settings_path = os.path.join(user_dir, "comfy.settings.json")
    if not os.path.exists(settings_path):
        return {}
    with open(settings_path, "r") as f:
        all_settings = json.load(f)
    prefix = "RunOnRunpod."
    return {
        k.removeprefix(prefix): v
        for k, v in all_settings.items()
        if k.startswith(prefix)
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
    """Scan workflow for nodes that reference local input files.

    Returns {filename: field_info} for files that need uploading.
    """
    files = {}
    for node_id, node in workflow.items():
        class_type = node.get("class_type", "")
        field_name = INPUT_NODE_FIELDS.get(class_type)
        if field_name and field_name in node.get("inputs", {}):
            filename = node["inputs"][field_name]
            if isinstance(filename, str) and not filename.startswith("http"):
                files[filename] = {"node_id": node_id, "field": field_name}
    return files


# --- Routes ---


@routes.get("/RunOnRunpod/settings")
async def get_settings(request):
    return web.json_response(_get_settings())


@routes.post("/RunOnRunpod/submit")
async def submit_job(request):
    global _active_job

    settings = _get_settings()
    api_key = settings.get("RunPod.apiKey", "")
    endpoint_id = settings.get("RunPod.endpointId", "")

    if not api_key or not endpoint_id:
        return web.json_response(
            {"error": "RunPod API Key and Endpoint ID are required"}, status=400
        )

    data = await request.json()
    workflow = data.get("workflow", {})

    job_prefix = str(uuid.uuid4())[:8]

    # Upload input files to S3 and build input_files mapping
    input_files = {}
    input_file_refs = _scan_input_files(workflow)

    if input_file_refs:
        s3_settings = {
            "s3_endpoint": settings.get("S3.endpoint"),
            "s3_access_key": settings.get("S3.accessKey"),
            "s3_secret_key": settings.get("S3.secretKey"),
        }
        bucket = settings.get("S3.bucket", "")

        if not all([s3_settings["s3_access_key"], s3_settings["s3_secret_key"], bucket]):
            return web.json_response(
                {"error": "S3 credentials required for workflows with input files"},
                status=400,
            )

        client = get_s3_client(s3_settings)
        input_dir = _get_input_directory()

        for filename in input_file_refs:
            file_path = os.path.join(input_dir, filename)
            if not os.path.exists(file_path):
                return web.json_response(
                    {"error": f"Input file not found: {filename}"}, status=400
                )
            s3_key = f"inputs/{job_prefix}/{filename}"
            upload_file(client, bucket, s3_key, file_path)
            input_files[filename] = generate_presigned_get(client, bucket, s3_key)

    # Generate presigned PUT URLs for outputs
    output_urls = {}
    max_outputs = int(settings.get("S3.maxOutputUrls", 5))

    s3_settings = {
        "s3_endpoint": settings.get("S3.endpoint"),
        "s3_access_key": settings.get("S3.accessKey"),
        "s3_secret_key": settings.get("S3.secretKey"),
    }
    bucket = settings.get("S3.bucket", "")

    if all([s3_settings.get("s3_access_key"), s3_settings.get("s3_secret_key"), bucket]):
        client = get_s3_client(s3_settings)
        for i in range(max_outputs):
            s3_key = f"outputs/{job_prefix}/output_{i}"
            output_urls[str(i)] = {
                "put": generate_presigned_put(client, bucket, s3_key),
                "get": generate_presigned_get(client, bucket, s3_key),
            }

    # Submit to RunPod
    payload = {
        "input": {
            "workflow": workflow,
            "input_files": input_files,
            "output_urls": {k: v["put"] for k, v in output_urls.items()},
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
    _active_job = {
        "job_id": job_id,
        "output_urls": {k: v["get"] for k, v in output_urls.items()},
    }

    return web.json_response({
        "job_id": job_id,
        "status": result.get("status", "IN_QUEUE"),
        "output_get_urls": {k: v["get"] for k, v in output_urls.items()},
    })


@routes.get("/RunOnRunpod/status/{job_id}")
async def get_status(request):
    job_id = request.match_info["job_id"]
    settings = _get_settings()
    api_key = settings.get("RunPod.apiKey", "")
    endpoint_id = settings.get("RunPod.endpointId", "")

    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"https://api.runpod.ai/v2/{endpoint_id}/status/{job_id}",
            headers={"Authorization": f"Bearer {api_key}"},
        ) as resp:
            result = await resp.json()

    return web.json_response({
        "status": result.get("status", "UNKNOWN"),
        "output": result.get("output"),
        "error": result.get("error"),
    })


@routes.post("/RunOnRunpod/cancel/{job_id}")
async def cancel_job(request):
    global _active_job
    job_id = request.match_info["job_id"]
    settings = _get_settings()
    api_key = settings.get("RunPod.apiKey", "")
    endpoint_id = settings.get("RunPod.endpointId", "")

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
