#!/bin/bash

# Link network volume models if available
if [ -d "/runpod-volume/models" ]; then
    rm -rf /comfyui/models
    ln -s /runpod-volume/models /comfyui/models
    echo "[RunOnRunpod] Linked network volume models"
fi

# Install extra custom nodes from URL if provided
if [ -n "$EXTRA_CUSTOM_NODES_URL" ]; then
    echo "[RunOnRunpod] Downloading extra custom nodes list from $EXTRA_CUSTOM_NODES_URL"
    curl -sL "$EXTRA_CUSTOM_NODES_URL" -o /tmp/extra_custom_nodes.txt
    while IFS= read -r repo || [ -n "$repo" ]; do
        [ -z "$repo" ] && continue
        case "$repo" in \#*) continue ;; esac
        name=$(basename "$repo" .git)
        if [ -d "/comfyui/custom_nodes/$name" ]; then
            echo "[RunOnRunpod] Custom node already installed: $name"
            continue
        fi
        echo "[RunOnRunpod] Installing extra custom node: $name"
        git clone "$repo" "/comfyui/custom_nodes/$name"
        if [ -f "/comfyui/custom_nodes/$name/requirements.txt" ]; then
            pip install -r "/comfyui/custom_nodes/$name/requirements.txt"
        fi
    done < /tmp/extra_custom_nodes.txt
fi

# Start ComfyUI in background
echo "[RunOnRunpod] Starting ComfyUI..."
python3 /comfyui/main.py --listen 127.0.0.1 --port 8188 --disable-auto-launch --use-flash-attention &

# Wait for ComfyUI to be ready
echo "[RunOnRunpod] Waiting for ComfyUI to be ready..."
until curl -s http://127.0.0.1:8188/system_stats > /dev/null 2>&1; do
    sleep 1
done
echo "[RunOnRunpod] ComfyUI is ready"

# Start RunPod handler
echo "[RunOnRunpod] Starting handler..."
python3 -u /handler.py
