# ComfyUI-RunOnRunpod

A ComfyUI plugin that lets you run workflows on [RunPod Serverless](https://www.runpod.io/product/serverless). Adds a "Run on RunPod" button to the UI that submits the current workflow to your RunPod endpoint and shows results.

## Components

### Plugin (ComfyUI custom node)

Installed in your local ComfyUI's `custom_nodes/` directory. Provides:

- **Run on RunPod button** with status indicator (green → yellow → blue → green/red)
- Click the button to cancel a running job
- **Settings panel** for RunPod and storage configuration
- Uploads input files (images, video, audio) to the network volume before submitting

### Worker (RunPod Serverless)

A Docker image that runs ComfyUI on RunPod. The worker:

- Receives workflow JSON via RunPod job input
- Reads input files and writes output files directly on the mounted network volume
- Requires **zero configuration** — no S3 credentials or environment variables needed
- Uses a RunPod **network volume** for models, inputs, and outputs

## Setup

### 1. Prepare the network volume

Create a RunPod network volume and set up the following directory structure:

```
/models/         # ComfyUI models (checkpoints, loras, etc.)
/inputs/         # Input files (uploaded by the plugin)
/outputs/        # Output files (written by the worker)
```

Upload models to the network volume using AWS CLI or any S3-compatible client with RunPod's S3 API credentials.

### 2. Prepare the worker

ComfyUI and custom nodes are bundled into the Docker image to minimize cold start times on RunPod Serverless. Without bundling, each cold start would need to install dependencies, adding minutes of delay.

A pre-built image is available at `docker.io/metebalci/comfyui-runonrunpod:latest` with the custom nodes listed in `worker/custom_nodes.txt`.

To build your own image with different custom nodes:

1. Edit `worker/custom_nodes.txt` to list the custom nodes you need (one git URL per line)
2. Build and push:
   ```bash
   cd worker
   ./build-docker.sh
   ```

Create a RunPod Serverless endpoint using the image, with the network volume attached.

### 3. Install the plugin

Clone this repo into your ComfyUI custom nodes directory:

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/metebalci/ComfyUI-RunOnRunpod.git
pip install -r ComfyUI-RunOnRunpod/requirements.txt
```

Restart ComfyUI.

**Note:** This plugin requires the new menu (Settings -> "Use new menu" -> "Top"). The legacy menu is not supported.

### 4. Configure

Open ComfyUI Settings and find the **Run on Runpod** section:

- API Key — RunPod API key
- S3 Access Key — from RunPod S3 API keys
- S3 Secret Key — from RunPod S3 API keys
- Endpoint ID — your RunPod Serverless endpoint ID
- Bucket Name — your network volume ID
- S3 Endpoint URL — RunPod S3 endpoint (region-specific, shown on RunPod dashboard)

## Usage

1. Build your workflow in ComfyUI as usual
2. Click **Run on RunPod**
3. Watch the button color for status:
   - **Yellow** — queued
   - **Blue (pulsing)** — running
   - **Green** — completed
   - **Red** — failed
4. On completion, a notification appears with the output count. Outputs are saved to the `/outputs/` directory on the network volume.
5. Click the button while a job is running to cancel it

## Storage Architecture

Everything lives on the RunPod network volume:

- **Models** — `/models/` (symlinked to ComfyUI's model path)
- **Inputs** — `/inputs/` (plugin uploads via RunPod S3 API, worker reads as local files)
- **Outputs** — `/outputs/` (worker writes as local files, accessible via RunPod S3 API)

The worker has zero storage configuration — the network volume is mounted locally and it just reads/writes files.

## License

GNU General Public License v3.0. See [LICENSE](LICENSE).
