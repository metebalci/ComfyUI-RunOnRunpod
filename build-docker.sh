#!/bin/bash
set -e

REPO="comfyanonymous/ComfyUI"
IMAGE="metebalci/comfyui-runonrunpod"

# Refuse to build with uncommitted worker changes — every published
# tag must map back to a real commit so it stays reproducible.
if ! git diff --quiet HEAD -- worker/ || ! git diff --quiet --cached HEAD -- worker/; then
    echo "Error: worker/ has uncommitted changes. Commit them before building."
    exit 1
fi

# Both worker_version and protocol_version live inside worker/Dockerfile
# as ARG defaults — single source of truth for everything that ends up
# in the image.
WORKER_VERSION=$(sed -n 's/^ARG WORKER_VERSION=\(.*\)$/\1/p' worker/Dockerfile | head -1)
if [ -z "$WORKER_VERSION" ]; then
    echo "Error: could not read WORKER_VERSION from worker/Dockerfile"
    exit 1
fi
PROTOCOL_VERSION=$(sed -n 's/^ARG PROTOCOL_VERSION=\(.*\)$/\1/p' worker/Dockerfile | head -1)
if [ -z "$PROTOCOL_VERSION" ]; then
    echo "Error: could not read PROTOCOL_VERSION from worker/Dockerfile"
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
IMAGE_TAG="${WORKER_VERSION}-p${PROTOCOL_VERSION}-comfyui${COMFYUI_VERSION}"

echo "Building worker v$WORKER_VERSION (protocol $PROTOCOL_VERSION) with ComfyUI $COMFYUI_TAG -> $IMAGE:$IMAGE_TAG"

docker build \
    --build-arg COMFYUI_TAG="$COMFYUI_TAG" \
    --build-arg WORKER_VERSION="$WORKER_VERSION" \
    --build-arg PROTOCOL_VERSION="$PROTOCOL_VERSION" \
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
