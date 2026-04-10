import os
import shutil
import time
import uuid

import requests
import runpod

COMFY_URL = "http://127.0.0.1:8188"
COMFY_INPUT_DIR = "/comfyui/input"
COMFY_OUTPUT_DIR = "/comfyui/output"
VOLUME_DIR = "/runpod-volume"
VOLUME_INPUTS_DIR = os.path.join(VOLUME_DIR, "inputs")
VOLUME_OUTPUTS_DIR = os.path.join(VOLUME_DIR, "outputs")


def copy_inputs(input_files: dict):
    """Copy input files from network volume to ComfyUI's input directory."""
    for filename, s3_key in input_files.items():
        src = os.path.join(VOLUME_DIR, s3_key)
        dest = os.path.join(COMFY_INPUT_DIR, filename)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        print(f"[RunOnRunpod] Copying input: {src} -> {dest}")
        shutil.copy2(src, dest)


def queue_workflow(workflow: dict) -> str:
    """Submit a workflow to ComfyUI and return the prompt_id."""
    resp = requests.post(
        f"{COMFY_URL}/prompt",
        json={"prompt": workflow},
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"ComfyUI rejected workflow: {data['error']}")
    return data["prompt_id"]


def poll_completion(prompt_id: str, timeout: int = 600) -> dict:
    """Poll ComfyUI's history endpoint until the prompt completes."""
    start = time.time()
    while time.time() - start < timeout:
        resp = requests.get(f"{COMFY_URL}/history/{prompt_id}")
        resp.raise_for_status()
        history = resp.json()
        if prompt_id in history:
            return history[prompt_id]
        time.sleep(1)
    raise TimeoutError(f"Workflow did not complete within {timeout}s")


def collect_outputs(history_entry: dict) -> list[str]:
    """Extract output file paths from a ComfyUI history entry."""
    files = []
    outputs = history_entry.get("outputs", {})
    for _node_id, node_output in outputs.items():
        for key in ("images", "gifs", "audio"):
            for item in node_output.get(key, []):
                subfolder = item.get("subfolder", "")
                filename = item["filename"]
                path = os.path.join(COMFY_OUTPUT_DIR, subfolder, filename)
                if os.path.exists(path):
                    files.append(path)
    return files


def save_outputs(output_files: list[str], job_prefix: str) -> list[str]:
    """Copy output files to the network volume outputs directory.

    Returns list of output filenames on the volume.
    """
    job_dir = os.path.join(VOLUME_OUTPUTS_DIR, job_prefix)
    os.makedirs(job_dir, exist_ok=True)
    saved = []
    for file_path in output_files:
        filename = os.path.basename(file_path)
        dest = os.path.join(job_dir, filename)
        print(f"[RunOnRunpod] Saving output: {file_path} -> {dest}")
        shutil.copy2(file_path, dest)
        saved.append(f"{job_prefix}/{filename}")
    return saved


def handler(job):
    """RunPod serverless handler."""
    try:
        job_input = job["input"]
        workflow = job_input["workflow"]
        input_files = job_input.get("input_files", {})
        job_prefix = job_input.get("job_prefix", uuid.uuid4().hex[:8])

        # Copy input files from network volume
        if input_files:
            copy_inputs(input_files)

        # Submit workflow to local ComfyUI
        print("[RunOnRunpod] Submitting workflow to ComfyUI...")
        prompt_id = queue_workflow(workflow)
        print(f"[RunOnRunpod] Prompt ID: {prompt_id}")

        # Poll for completion
        print("[RunOnRunpod] Waiting for completion...")
        result = poll_completion(prompt_id)

        # Check for errors in execution
        status_data = result.get("status", {})
        if status_data.get("status_str") == "error":
            messages = status_data.get("messages", [])
            error_msg = str(messages) if messages else "Workflow execution failed"
            return {"error": error_msg}

        # Collect and save outputs to network volume
        output_files = collect_outputs(result)
        print(f"[RunOnRunpod] Found {len(output_files)} output file(s)")

        saved_files = save_outputs(output_files, job_prefix)

        return {
            "status": "success",
            "output_count": len(saved_files),
            "output_files": saved_files,
        }

    except Exception as e:
        print(f"[RunOnRunpod] Error: {e}")
        return {"error": str(e)}


if __name__ == "__main__":
    runpod.serverless.start({"handler": handler})
