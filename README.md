# ComfyUI-RunOnRunpod

A ComfyUI plugin that lets you run workflows on [RunPod Serverless](https://www.runpod.io/product/serverless). Adds a "Run on RunPod" button to the UI that submits the current workflow to your RunPod endpoint and shows results.

## Components

### Plugin (ComfyUI custom node)

Installed in your local ComfyUI's `custom_nodes/` directory. Provides:

- **Run on RunPod button** with status indicator (white → yellow → blue → green/red)
- **Cancel button** to abort running jobs
- **Settings panel** for RunPod API key, endpoint ID, and S3 storage configuration
- **S3-based file I/O** — uploads input files, generates presigned URLs for outputs

### Worker (RunPod Serverless)

A Docker image that runs ComfyUI on RunPod. The worker:

- Receives workflow JSON + presigned URLs via RunPod job input
- Downloads input files, executes the workflow, uploads outputs
- Requires **zero S3 configuration** — all file transfers use presigned URLs
- Uses a RunPod **network volume** for models

## Setup

### 1. Prepare the worker

A pre-built image is available at `docker.io/metebalci/comfyui-runonrunpod:latest` with the custom nodes listed in `worker/custom_nodes.txt`.

To build your own image with different custom nodes:

1. Edit `worker/custom_nodes.txt` to list the custom nodes you need (one git URL per line)
2. Build the Docker image:
   ```bash
   cd worker
   docker build -t comfyui-runonrunpod .
   ```
3. Push to Docker Hub or another registry

Then create a RunPod Serverless endpoint using the image, with a network volume attached for models.

### 2. Install the plugin

Clone this repo into your ComfyUI custom nodes directory:

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/metebalci/ComfyUI-RunOnRunpod.git
pip install -r ComfyUI-RunOnRunpod/requirements.txt
```

Restart ComfyUI.

### 3. Configure

Open ComfyUI Settings and find the **RunOnRunpod** section:

**RunPod:**
- RunPod API Key
- Endpoint ID

**S3 Storage (for inputs/outputs):**
- S3 Provider (AWS / Cloudflare R2 / Google Cloud Storage / RunPod / Custom)
- S3 Endpoint (auto-populated based on provider)
- S3 Access Key
- S3 Secret Key
- S3 Bucket
- Max Output URLs per Job (default: 5)

### 4. Models

Models must be on a RunPod **network volume** mounted to the worker. Upload models to the network volume via:

- RunPod's S3-compatible API
- A temporary pod attached to the volume
- RunPod's cloud sync feature

The worker automatically symlinks the network volume's `models/` directory to ComfyUI's model path.

## Usage

1. Build your workflow in ComfyUI as usual
2. Click **Run on RunPod**
3. Watch the button color for status:
   - **Yellow** — queued
   - **Blue (pulsing)** — running
   - **Green** — completed
   - **Red** — failed
4. On completion, a notification appears with links to the output files in your S3 storage
5. Click **X** next to the button to cancel a running job

## Storage Architecture

- **Models** — RunPod network volume (fast local access on worker)
- **Inputs/Outputs** — Any S3-compatible storage (AWS S3, Cloudflare R2, Google Cloud Storage, RunPod, or custom)
- The plugin generates presigned URLs so the worker never needs S3 credentials

## License

GNU General Public License v3.0. See [LICENSE](LICENSE).
