#!/usr/bin/env sh
# Builds the image on this machine and pushes it to GHCR. Much faster than a GitHub runner.
# Usage: docker/build-image.sh [tag] [platforms]   (defaults: edge, linux/amd64)
# Log in first: gh auth token | docker login ghcr.io -u Fundryi --password-stdin
set -e
tag="${1:-edge}"
platforms="${2:-linux/amd64}"
cd "$(dirname "$0")/.."
docker buildx build --platform "$platforms" --provenance=false -t "ghcr.io/fundryi/immich-organizer:$tag" --push .
