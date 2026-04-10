import json
import os
import uuid

import aiohttp
from aiohttp import web
from server import PromptServer

from .s3_utils import get_s3_client, upload_file

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

# RunPod S3 endpoint
RUNPOD_S3_ENDPOINT = "https://s3.runpod.io"


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
    prefix = "Run on RunPod."
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


def _get_s3_client_from_settings(settings: dict):
    """Create S3 client from settings for RunPod network volume."""
    return get_s3_client({
        "s3_endpoint": RUNPOD_S3_ENDPOINT,
        "s3_access_key": settings.get("RunPod.s3AccessKey"),
        "s3_secret_key": settings.get("RunPod.s3SecretKey"),
    })


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

    # Upload input files to network volume via S3
    input_files = {}
    input_file_refs = _scan_input_files(workflow)

    volume_id = settings.get("RunPod.volumeId", "")

    if input_file_refs:
        if not volume_id:
            return web.json_response(
                {"error": "Network Volume ID required for workflows with input files"},
                status=400,
            )

        client = _get_s3_client_from_settings(settings)
        input_dir = _get_input_directory()

        for filename in input_file_refs:
            file_path = os.path.join(input_dir, filename)
            if not os.path.exists(file_path):
                return web.json_response(
                    {"error": f"Input file not found: {filename}"}, status=400
                )
            s3_key = f"inputs/{filename}"
            upload_file(client, volume_id, s3_key, file_path)
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

    return web.json_response({
        "job_id": job_id,
        "status": result.get("status", "IN_QUEUE"),
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
