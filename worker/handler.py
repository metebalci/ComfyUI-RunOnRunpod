import os
import time
import urllib.request

import requests
import runpod

COMFY_URL = "http://127.0.0.1:8188"
COMFY_INPUT_DIR = "/comfyui/input"
COMFY_OUTPUT_DIR = "/comfyui/output"


def download_inputs(input_files: dict):
    """Download input files from presigned GET URLs to ComfyUI's input directory."""
    for filename, url in input_files.items():
        dest = os.path.join(COMFY_INPUT_DIR, filename)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        print(f"[RunOnRunpod] Downloading input: {filename}")
        urllib.request.urlretrieve(url, dest)


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
        # Images
        for image in node_output.get("images", []):
            subfolder = image.get("subfolder", "")
            filename = image["filename"]
            path = os.path.join(COMFY_OUTPUT_DIR, subfolder, filename)
            if os.path.exists(path):
                files.append(path)
        # Video/GIFs (VHS and similar)
        for gif in node_output.get("gifs", []):
            subfolder = gif.get("subfolder", "")
            filename = gif["filename"]
            path = os.path.join(COMFY_OUTPUT_DIR, subfolder, filename)
            if os.path.exists(path):
                files.append(path)
        # Audio
        for audio in node_output.get("audio", []):
            subfolder = audio.get("subfolder", "")
            filename = audio["filename"]
            path = os.path.join(COMFY_OUTPUT_DIR, subfolder, filename)
            if os.path.exists(path):
                files.append(path)
    return files


def upload_outputs(files: list[str], output_urls: dict) -> list[int]:
    """Upload output files to S3 via presigned PUT URLs.

    Returns list of indices that were used.
    """
    used_indices = []
    url_keys = sorted(output_urls.keys(), key=int)

    for i, file_path in enumerate(files):
        if i >= len(url_keys):
            print(f"[RunOnRunpod] Warning: more outputs ({len(files)}) than presigned URLs ({len(url_keys)}), skipping remaining")
            break

        key = url_keys[i]
        put_url = output_urls[key]

        with open(file_path, "rb") as f:
            data = f.read()

        print(f"[RunOnRunpod] Uploading output: {os.path.basename(file_path)} ({len(data)} bytes)")
        resp = requests.put(put_url, data=data)
        resp.raise_for_status()
        used_indices.append(int(key))

    return used_indices


def handler(job):
    """RunPod serverless handler."""
    try:
        job_input = job["input"]
        workflow = job_input["workflow"]
        input_files = job_input.get("input_files", {})
        output_urls = job_input.get("output_urls", {})

        # Download input files
        if input_files:
            download_inputs(input_files)

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

        # Collect and upload outputs
        output_files = collect_outputs(result)
        print(f"[RunOnRunpod] Found {len(output_files)} output file(s)")

        used_indices = []
        if output_urls and output_files:
            used_indices = upload_outputs(output_files, output_urls)

        return {
            "status": "success",
            "used_indices": used_indices,
            "output_count": len(output_files),
        }

    except Exception as e:
        print(f"[RunOnRunpod] Error: {e}")
        return {"error": str(e)}


if __name__ == "__main__":
    runpod.serverless.start({"handler": handler})
