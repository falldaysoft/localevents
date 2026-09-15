#!/bin/bash
# Deploy script run on the VM. Installed at ~/apps/<instance>/deploy.sh and
# wired as the forced command for that instance's CI deploy key in
# ~/.ssh/authorized_keys, so the key can only ever run this script. CI passes
# the image tag (the full git SHA) as the SSH command; it arrives here in
# SSH_ORIGINAL_COMMAND. Run by hand, the tag is the first argument.
#
# The instance is the directory this script lives in, so one copy of the
# file serves any instance on the machine.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

TAG="${SSH_ORIGINAL_COMMAND:-${1:-}}"
case "$TAG" in
  *[!A-Za-z0-9._-]*|"") echo "refusing bad tag: '$TAG'" >&2; exit 1 ;;
  latest|main)
    # A mutable tag does not deploy: `pull` may fetch a new image, but the
    # tag in .env is unchanged, so nothing records what is running and a
    # rollback has nothing to name. Deploy the full SHA CI published.
    echo "refusing mutable tag '$TAG'; deploy a full git SHA" >&2; exit 1 ;;
esac

if grep -q '^IMAGE_TAG=' .env; then
  sed -i "s/^IMAGE_TAG=.*/IMAGE_TAG=$TAG/" .env
else
  echo "IMAGE_TAG=$TAG" >> .env
fi

echo "Deploying ghcr.io/falldaysoft/localevents:$TAG to $(basename "$PWD")"
docker compose pull --quiet
docker compose up -d --remove-orphans
docker image prune -f >/dev/null
docker compose ps
