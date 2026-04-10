FROM nvidia/cuda:13.0.2-devel-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    python3 python3-pip python3-venv git wget curl \
    && rm -rf /var/lib/apt/lists/*

# Create venv
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install ComfyUI at a specific tag (passed via build arg)
ARG COMFYUI_TAG=latest
RUN git clone --branch "${COMFYUI_TAG}" --depth 1 https://github.com/comfyanonymous/ComfyUI.git /comfyui
WORKDIR /comfyui
RUN pip install -r requirements.txt
RUN pip install torch==2.11.0 torchvision torchaudio --index-url https://download.pytorch.org/whl/cu130

# Install Flash Attention 3 (prebuilt wheel for CUDA 13.0 + PyTorch 2.11)
RUN pip install https://github.com/windreamer/flash-attention3-wheels/releases/download/2026.03.30-a3a563f/flash_attn_3-3.0.0%2B20260330.cu130torch2110cxx11abitrue.98024f-cp39-abi3-linux_x86_64.whl

# Install custom nodes from list
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

# Install RunPod SDK and handler dependencies
RUN pip install runpod requests

# Copy handler and start script
COPY handler.py /handler.py
COPY start.sh /start.sh
RUN chmod +x /start.sh

CMD ["/start.sh"]
