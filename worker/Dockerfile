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

# Install ComfyUI at a specific tag (changes with new releases)
ARG COMFYUI_TAG=latest
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

# Copy handler and start script last (changes often, small layer)
COPY handler.py /handler.py
COPY start.sh /start.sh
RUN chmod +x /start.sh

CMD ["/start.sh"]
