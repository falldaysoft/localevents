#!/usr/bin/env bash
#
# Deploy a localevents instance to LKE.
#
# Run this from a machine whose IP is on the Linode API allowlist — CI cannot
# reach the cluster, so it builds and pushes the image and stops there.
#
#   ./scripts/deploy.sh <instance> [image-tag]
#
# <instance> names a values file at instances/<instance>.yaml, which supplies
# the host, namespace, and regional settings. Those files are not part of the
# reusable product and are not committed; see instances/example.yaml.

set -euo pipefail

INSTANCE="${1:-}"
TAG="${2:-latest}"
IMAGE_REPO="ghcr.io/falldaysoft/localevents"

if [[ -z "$INSTANCE" ]]; then
    echo "usage: $0 <instance> [image-tag]" >&2
    echo "" >&2
    echo "available instances:" >&2
    ls instances/*.yaml 2>/dev/null | sed 's|instances/|  |; s|\.yaml$||' >&2 || echo "  (none)" >&2
    exit 1
fi

VALUES_FILE="instances/${INSTANCE}.yaml"
if [[ ! -f "$VALUES_FILE" ]]; then
    echo "error: $VALUES_FILE not found" >&2
    echo "copy instances/example.yaml and fill it in." >&2
    exit 1
fi

# The namespace is declared in the instance file rather than passed separately,
# so there is one source of truth for where this deploys.
NAMESPACE=$(grep -E '^namespace:' "$VALUES_FILE" | head -1 | awk '{print $2}' | tr -d '"')
if [[ -z "$NAMESPACE" ]]; then
    echo "error: $VALUES_FILE does not set 'namespace:'" >&2
    exit 1
fi

echo "==> instance:  $INSTANCE"
echo "==> namespace: $NAMESPACE"
echo "==> image:     ${IMAGE_REPO}:${TAG}"
echo

# Fail early with a clear message rather than a kubectl timeout if the ACL is
# blocking us — that is the single most likely reason this script fails.
if ! kubectl cluster-info --request-timeout=10s >/dev/null 2>&1; then
    echo "error: cannot reach the cluster API." >&2
    echo "Is this machine's current IP on the Linode API allowlist?" >&2
    echo "Current IP: $(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || echo 'unknown')" >&2
    exit 1
fi

echo "==> ensuring namespace"
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

echo "==> checking prerequisites"
for secret in ghcr-secret postgres-secret; do
    if ! kubectl get secret "$secret" -n "$NAMESPACE" >/dev/null 2>&1; then
        echo "error: secret '$secret' missing in namespace '$NAMESPACE'." >&2
        echo "See README.md, 'First deploy of a new instance'." >&2
        exit 1
    fi
done

echo "==> helm upgrade --install"
helm upgrade --install \
    --namespace "$NAMESPACE" \
    --values "$VALUES_FILE" \
    --set image="${IMAGE_REPO}:${TAG}" \
    --wait --timeout 5m \
    "$INSTANCE" helm/localevents/

echo
echo "==> deployed. rollout status:"
kubectl rollout status deployment -n "$NAMESPACE" --timeout=60s || true
