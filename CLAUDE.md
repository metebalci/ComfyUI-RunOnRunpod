# ComfyUI Serverless

## Project Goal

Build two things:

1. **A RunPod Serverless worker** — A Dockerized ComfyUI instance that receives workflow JSON, executes it, and returns results (base64 images or S3 URLs for large outputs like video).
2. **A ComfyUI frontend plugin** — Installed on the user's local ComfyUI, adds a "Run on RunPod" button that submits the current workflow to the RunPod Serverless endpoint and displays results back in the UI.

This fills a gap: no existing plugin bridges local ComfyUI to RunPod Serverless. Existing solutions (ComfyUI_NetDist, ComfyUI-Distributed) only do ComfyUI-to-ComfyUI. RunPod's comfy.getrunpod.io deploys workflows as external APIs but has no ComfyUI integration.

## Architecture Decisions

- **Async execution**: Use RunPod's `/run` (async) endpoint, poll `/status/{job_id}` or use webhooks. Video workflows can run for a long time (RunPod supports up to 24h timeout).
- **Large outputs**: Upload video/large files to S3/R2 via presigned URLs rather than returning base64 (RunPod has ~20MB payload limit).
- **Custom nodes strategy (Approach 4)**: Ship a base Docker image with ~20 popular custom nodes baked in. Users can extend the Dockerfile to add more. The plugin should detect if a workflow uses unsupported nodes and warn before submitting.
- **The Dockerfile repo is public.**
- **User currently uses**: ComfyUI_essentials

## Components

### RunPod Worker (Docker image)
- Installs ComfyUI + common custom nodes
- Python handler that:
  - Starts ComfyUI subprocess
  - Receives workflow JSON via RunPod job input
  - Submits to local ComfyUI API (`POST /prompt`)
  - Polls `/history/{prompt_id}` for completion
  - Collects outputs (images/video)
  - Returns base64 or uploads to S3
- User-customizable Dockerfile for adding custom nodes/models

### ComfyUI Plugin (frontend extension)
- Adds "Run on RunPod" button to the UI
- Settings: RunPod API key, endpoint ID
- Sends current workflow (API format JSON) to RunPod
- Async polling with progress updates
- Displays results back in ComfyUI gallery
- Node compatibility check: warns if workflow uses nodes not in the worker image
