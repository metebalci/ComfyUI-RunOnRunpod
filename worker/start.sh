#!/bin/bash

# Link network volume models if available
if [ -d "/runpod-volume/models" ]; then
    rm -rf /comfyui/models
    ln -s /runpod-volume/models /comfyui/models
    echo "[RunOnRunpod] Linked network volume models"
fi

# Start ComfyUI in background
echo "[RunOnRunpod] Starting ComfyUI..."
python3 /comfyui/main.py --listen 127.0.0.1 --port 8188 --disable-auto-launch &

# Wait for ComfyUI to be ready
echo "[RunOnRunpod] Waiting for ComfyUI to be ready..."
until curl -s http://127.0.0.1:8188/system_stats > /dev/null 2>&1; do
    sleep 1
done
echo "[RunOnRunpod] ComfyUI is ready"

# Start RunPod handler
echo "[RunOnRunpod] Starting handler..."
python3 -u /handler.py
