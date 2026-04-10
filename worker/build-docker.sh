#!/bin/bash
set -e

REPO="comfyanonymous/ComfyUI"
IMAGE="metebalci/comfyui-runonrunpod"
CUDA_VERSION="cu130"
TORCH_VERSION="torch211"

# Find the latest ComfyUI tag starting with 'v'
COMFYUI_TAG=$(git ls-remote --tags --sort=-v:refname "https://github.com/$REPO.git" 'v*' \
    | head -1 \
    | sed 's/.*refs\/tags\///' \
    | sed 's/\^{}//')

if [ -z "$COMFYUI_TAG" ]; then
    echo "Error: could not find latest ComfyUI tag"
    exit 1
fi

# Strip 'v' prefix and dots for the image tag (v0.18.5 -> comfyui0185)
COMFYUI_VERSION=$(echo "${COMFYUI_TAG#v}" | tr -d '.')
IMAGE_TAG="${CUDA_VERSION}_${TORCH_VERSION}_comfyui${COMFYUI_VERSION}"

echo "Building with ComfyUI $COMFYUI_TAG -> $IMAGE:$IMAGE_TAG"

docker build \
    --build-arg COMFYUI_TAG="$COMFYUI_TAG" \
    -t "$IMAGE:$IMAGE_TAG" \
    -t "$IMAGE:latest" \
    .

echo ""
echo "Built: $IMAGE:$IMAGE_TAG"
echo "Built: $IMAGE:latest"
echo ""
echo "Pushing..."
docker push "$IMAGE:$IMAGE_TAG"
docker push "$IMAGE:latest"
echo "Done."
