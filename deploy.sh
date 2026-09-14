#!/usr/bin/env bash
set -euo pipefail

# Configuration
APP_NAME="${APP_NAME:-german-market-sales}"
IMAGE_NAME="${IMAGE_NAME:-german-market-sales:latest}"
HOST_PORT="${HOST_PORT:-5000}"
CONTAINER_PORT="${CONTAINER_PORT:-5000}"
DEFAULT_ZIP_CODE="${DEFAULT_ZIP_CODE:-14195}"
CACHE_DIR="$(pwd)/data/cache"

echo "=========================================="
echo " Starting deployment: ${APP_NAME}"
echo "=========================================="

# 1. Ensure cache directory exists for volume mounting
mkdir -p "${CACHE_DIR}"

# 2. Pull latest changes from git
echo ">>> Pulling latest changes from git..."
git pull

# 3. Build Docker image
echo ">>> Building Docker image: ${IMAGE_NAME}..."
docker build -t "${IMAGE_NAME}" .

# 4. Stop existing container if running
if [ "$(docker ps -q -f name=^/${APP_NAME}$)" ]; then
  echo ">>> Stopping existing container '${APP_NAME}'..."
  docker stop "${APP_NAME}"
fi

# 5. Remove existing container if present
if [ "$(docker ps -aq -f name=^/${APP_NAME}$)" ]; then
  echo ">>> Removing existing container '${APP_NAME}'..."
  docker rm "${APP_NAME}"
fi

# 6. Run new container
echo ">>> Starting new container '${APP_NAME}' on port ${HOST_PORT}..."
docker run -d \
  --name "${APP_NAME}" \
  -p "${HOST_PORT}:${CONTAINER_PORT}" \
  -e DEFAULT_ZIP_CODE="${DEFAULT_ZIP_CODE}" \
  -v "${CACHE_DIR}:/app/data/cache" \
  --restart unless-stopped \
  "${IMAGE_NAME}"

# 7. Verification / Health check
echo ">>> Verifying container status..."
sleep 2

if [ "$(docker ps -q -f name=^/${APP_NAME}$)" ]; then
  echo "=========================================="
  echo " Deployment successful!"
  echo " App running at: http://localhost:${HOST_PORT}"
  echo " Container status:"
  docker ps -f name=^/${APP_NAME}$
  echo "=========================================="
else
  echo "=========================================="
  echo " ERROR: Container failed to start!"
  echo " Container logs:"
  docker logs "${APP_NAME}"
  echo "=========================================="
  exit 1
fi

