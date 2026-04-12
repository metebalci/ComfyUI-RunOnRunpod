#!/bin/bash
set -e

REPO="comfyanonymous/ComfyUI"
IMAGE="metebalci/comfyui-runonrunpod"
CUDA_VERSION="cu130"
TORCH_VERSION="torch211"

# Refuse to build with uncommitted worker changes — every published tag
# must map back to a real commit so the tag is reproducible.
if ! git diff --quiet HEAD -- worker/ || ! git diff --quiet --cached HEAD -- worker/; then
    echo "Error: worker/ has uncommitted changes. Commit them before building."
    exit 1
fi

# Use the short SHA of the last commit that touched worker/. This keeps
# the tag stable when unrelated parts of the repo change and only bumps
# when the image content actually changes.
WORKER_SHA=$(git log -1 --format=%h -- worker/)
if [ -z "$WORKER_SHA" ]; then
    echo "Error: could not find a commit for worker/"
    exit 1
fi

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
IMAGE_TAG="${CUDA_VERSION}_${TORCH_VERSION}_comfyui${COMFYUI_VERSION}_${WORKER_SHA}"

echo "Building with ComfyUI $COMFYUI_TAG and worker $WORKER_SHA -> $IMAGE:$IMAGE_TAG"

docker build \
    --build-arg COMFYUI_TAG="$COMFYUI_TAG" \
    -t "$IMAGE:$IMAGE_TAG" \
    -t "$IMAGE:latest" \
    ./worker

echo ""
echo "Built: $IMAGE:$IMAGE_TAG"
echo "Built: $IMAGE:latest"
echo ""
echo "Pushing..."
docker push "$IMAGE:$IMAGE_TAG"
docker push "$IMAGE:latest"
echo "Done."
