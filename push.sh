#!/usr/bin/env bash
# Build tzdelay and push it to Docker Hub.
#
#   ./push.sh <dockerhub-username> [version]
#   ./push.sh joesexton 0.2
#
# Builds for both Intel/AMD (Unraid) and ARM (Apple silicon Macs,
# Raspberry Pi), so the image runs on Unraid even when built on a Mac.
set -euo pipefail

USER_NAME="${1:?usage: ./push.sh <dockerhub-username> [version]}"
VERSION="${2:-0.2}"
IMAGE="docker.io/${USER_NAME}/tzdelay"

cd "$(dirname "$0")"

if ! docker info >/dev/null 2>&1; then
  echo "Docker isn't running. Start Docker Desktop (or the docker service) and try again." >&2
  exit 1
fi

echo "Logging in to Docker Hub as ${USER_NAME} (use an access token as the password)"
docker login -u "${USER_NAME}"

# A builder that can produce multi-platform images
docker buildx inspect tzdelay-builder >/dev/null 2>&1 \
  || docker buildx create --name tzdelay-builder --use >/dev/null
docker buildx use tzdelay-builder

docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -t "${IMAGE}:${VERSION}" \
  -t "${IMAGE}:latest" \
  --push .

echo
echo "Pushed ${IMAGE}:${VERSION} and ${IMAGE}:latest"
echo "On Unraid:  docker pull ${IMAGE}:latest"
