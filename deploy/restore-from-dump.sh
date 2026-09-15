#!/bin/bash
# Restore a pg_dump custom-format file into the VM's shared postgres
# container as this instance's role. Stops the app during the restore so
# nothing writes mid-way; the worker follows the web container down and up.
#
# Usage (on the VM): ~/apps/<instance>/restore-from-dump.sh <file>.dump
set -euo pipefail
DUMP="${1:?usage: restore-from-dump.sh <dump file>}"
cd "$(dirname "$(readlink -f "$0")")"
INSTANCE=$(basename "$PWD")
docker compose stop
docker exec -i postgres pg_restore -U "$INSTANCE" -d "$INSTANCE" --clean --if-exists --no-owner --no-acl < "$DUMP"
docker compose start
echo "restored $DUMP into $INSTANCE"
