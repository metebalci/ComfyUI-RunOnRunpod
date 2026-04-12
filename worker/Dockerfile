FROM nvidia/cuda:13.0.2-runtime-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive

# Minimal packages for Python/PyTorch
RUN apt-get update && apt-get install -y \
    python3.12 python3.12-venv python3-pip \
    && rm -rf /var/lib/apt/lists/*

# Create venv
RUN python3.12 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install PyTorch (stable layer — rarely changes)
RUN pip install torch==2.11.0 torchvision torchaudio --index-url https://download.pytorch.org/whl/cu130

# Install Flash Attention 2 (prebuilt wheel for CUDA 13.0 + torch 2.11)
RUN pip install https://github.com/mjun0812/flash-attention-prebuild-wheels/releases/download/v0.9.4/flash_attn-2.8.3+cu130torch2.11-cp312-cp312-linux_x86_64.whl

# Additional system packages
RUN apt-get update && apt-get install -y \
    git curl \
    && rm -rf /var/lib/apt/lists/*

# Install RunPod SDK and handler dependencies (stable layer)
RUN pip install runpod requests

# Install ComfyUI at a specific tag (changes with new releases).
# Defaults to `master` so a bare `docker build ./worker` clones the
# latest ComfyUI main branch. Override with --build-arg COMFYUI_TAG=v0.x.y
# to pin to a specific release.
ARG COMFYUI_TAG=master
ENV COMFYUI_VERSION=${COMFYUI_TAG}
RUN git clone --branch "${COMFYUI_TAG}" --depth 1 https://github.com/comfyanonymous/ComfyUI.git /comfyui
WORKDIR /comfyui
RUN pip install -r requirements.txt

# Install custom nodes from list (changes when user edits custom_nodes.txt)
COPY custom_nodes.txt /tmp/custom_nodes.txt
RUN while IFS= read -r repo || [ -n "$repo" ]; do \
        [ -z "$repo" ] && continue; \
        case "$repo" in \#*) continue ;; esac; \
        name=$(basename "$repo" .git); \
        echo "[RunOnRunpod] Installing custom node: $name"; \
        git clone "$repo" "/comfyui/custom_nodes/$name"; \
        if [ -f "/comfyui/custom_nodes/$name/requirements.txt" ]; then \
            pip install -r "/comfyui/custom_nodes/$name/requirements.txt"; \
        fi; \
    done < /tmp/custom_nodes.txt

# Worker semver — bump in this file when anything in the worker image
# changes deliberately (CUDA/torch bump, custom_nodes edit, handler
# code change, Dockerfile restructure). Daily ComfyUI rebuilds do NOT
# bump this; they only change the comfyui part of the image tag.
ARG WORKER_VERSION=0.1.0
ENV WORKER_VERSION=${WORKER_VERSION}

# Wire-protocol version baked into the env so the worker can report
# it back via the version action. The plugin uses it for a strict
# equality check; mismatch is a hard error. MUST be kept in sync with
# `PROTOCOL_VERSION` in the plugin's routes.py.
ARG PROTOCOL_VERSION=1
ENV PROTOCOL_VERSION=${PROTOCOL_VERSION}

# Copy handler and start script last (changes often, small layer)
COPY handler.py /handler.py
COPY model_fetcher.py /model_fetcher.py
COPY start.sh /start.sh
RUN chmod +x /start.sh

CMD ["/start.sh"]
