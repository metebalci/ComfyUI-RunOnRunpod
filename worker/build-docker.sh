#!/bin/bash
set -e

REPO="comfyanonymous/ComfyUI"
IMAGE="metebalci/comfyui-runonrunpod"

# Find the latest tag starting with 'v'
TAG=$(git ls-remote --tags --sort=-v:refname "https://github.com/$REPO.git" 'v*' \
    | head -1 \
    | sed 's/.*refs\/tags\///' \
    | sed 's/\^{}//')

if [ -z "$TAG" ]; then
    echo "Error: could not find latest ComfyUI tag"
    exit 1
fi

# Strip the 'v' prefix for the image tag
VERSION="${TAG#v}"

echo "Building with ComfyUI $TAG -> $IMAGE:$VERSION"

docker build \
    --build-arg COMFYUI_TAG="$TAG" \
    -t "$IMAGE:$VERSION" \
    -t "$IMAGE:latest" \
    .

echo ""
echo "Built: $IMAGE:$VERSION"
echo "Built: $IMAGE:latest"
echo ""
echo "To push:"
echo "  docker push $IMAGE:$VERSION"
echo "  docker push $IMAGE:latest"
