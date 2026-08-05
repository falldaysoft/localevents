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
#
# The tag defaults to the full SHA of HEAD, because that is what CI publishes
# and because "deploy what I have checked out" is what anyone running this
# actually means.

set -euo pipefail

INSTANCE="${1:-}"
TAG="${2:-}"
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

# ---------------------------------------------------------------------------
# Resolving the tag
#
# CI tags every image with `github.sha` — the full 40-character SHA — and with
# `latest`. A short SHA therefore names an image that does not exist, and the
# failure is not a message but a wait: the migrate hook sits in
# ImagePullBackOff until `helm --wait` gives up five minutes later, and the
# only thing helm says is "context canceled". That has now cost more time than
# every other failure of this script combined, so it is worth three lines to
# rule out.
# ---------------------------------------------------------------------------

if [[ -z "$TAG" ]]; then
    TAG=$(git rev-parse HEAD 2>/dev/null || true)
    if [[ -z "$TAG" ]]; then
        echo "error: no tag given and this is not a git checkout." >&2
        echo "pass one explicitly: $0 $INSTANCE <full-sha>" >&2
        exit 1
    fi
    echo "==> no tag given, using HEAD"
elif [[ "$TAG" =~ ^[0-9a-f]{4,39}$ ]]; then
    # Hex but too short to be a published tag. If git can expand it we know
    # exactly what was meant, so say so and carry on rather than failing.
    if FULL=$(git rev-parse --verify "${TAG}^{commit}" 2>/dev/null); then
        echo "==> '$TAG' is a short SHA; CI publishes full ones — using $FULL"
        TAG="$FULL"
    else
        echo "error: '$TAG' looks like a short SHA but does not resolve here." >&2
        echo "CI tags images with the full 40-character SHA." >&2
        exit 1
    fi
fi

# A commit that never reached the remote was never built, so the image cannot
# exist. Advisory only — the check depends on how recently anyone fetched.
if [[ "$TAG" != "latest" ]] && git cat-file -e "${TAG}^{commit}" 2>/dev/null; then
    if ! git branch -r --contains "$TAG" 2>/dev/null | grep -q .; then
        echo "warning: $TAG is on no known remote branch — did CI ever build it?" >&2
    fi
fi

# ---------------------------------------------------------------------------

echo "==> instance:  $INSTANCE"
echo "==> namespace: (reading $VALUES_FILE)"
echo "==> image:     ${IMAGE_REPO}:${TAG}"
echo

# The namespace is declared in the instance file rather than passed separately,
# so there is one source of truth for where this deploys.
NAMESPACE=$(grep -E '^namespace:' "$VALUES_FILE" | head -1 | awk '{print $2}' | tr -d '"')
if [[ -z "$NAMESPACE" ]]; then
    echo "error: $VALUES_FILE does not set 'namespace:'" >&2
    exit 1
fi
echo "==> namespace: $NAMESPACE"

# ---------------------------------------------------------------------------
# Which cluster
#
# Everything below runs against whatever context kubectl happens to be on, and
# nothing in a values file used to say which that should be. A machine that
# also administers other clusters therefore deploys wherever it was last
# pointed — and the failure is silent, because this script's next complaint is
# "secret 'ghcr-secret' missing", which reads like an ordinary first deploy
# rather than like being on someone else's cluster. It got as far as creating
# a namespace on an unrelated production cluster before stopping.
#
# `context:` is declared in the instance file for the same reason `namespace:`
# is: which cluster a community's site lives on is a fact about that instance,
# not about the product. It stays optional — a single-cluster machine has
# nothing to disambiguate — but when it is set it is enforced rather than
# assumed.
# ---------------------------------------------------------------------------

CONTEXT=$(grep -E '^context:' "$VALUES_FILE" | head -1 | awk '{print $2}' | tr -d '"')
CURRENT=$(kubectl config current-context 2>/dev/null || true)

if [[ -n "$CONTEXT" ]]; then
    if [[ "$CONTEXT" != "$CURRENT" ]]; then
        echo "==> context:   $CURRENT -> $CONTEXT"
        if ! kubectl config use-context "$CONTEXT" >/dev/null 2>&1; then
            echo "error: no kubectl context named '$CONTEXT'." >&2
            echo "$VALUES_FILE names it; 'kubectl config get-contexts' lists" >&2
            echo "what this machine has." >&2
            exit 1
        fi
    else
        echo "==> context:   $CONTEXT"
    fi
else
    # Say it out loud rather than deploying silently into the dark.
    echo "==> context:   ${CURRENT:-<none>} (no 'context:' in $VALUES_FILE)"
fi

# Ask the registry whether the tag is really there. Three answers, not two: the
# image is private, so a machine that is not logged in to ghcr cannot tell us
# anything, and refusing to deploy because a local docker login expired would
# be a worse failure than the one this prevents.
check_image() {
    local out rc
    command -v docker >/dev/null 2>&1 || return 2
    out=$(docker manifest inspect "${IMAGE_REPO}:${TAG}" 2>&1)
    rc=$?
    [[ $rc -eq 0 ]] && return 0
    case "$out" in
        *"manifest unknown"*|*MANIFEST_UNKNOWN*|*"not found"*) return 1 ;;
        *) return 2 ;;
    esac
}

echo "==> checking the image exists"
set +e
check_image
image_status=$?
set -e
case "$image_status" in
    0) echo "    found." ;;
    1)
        echo "error: ${IMAGE_REPO}:${TAG} is not in the registry." >&2
        echo "CI tags images with the full 40-character SHA; check the run for" >&2
        echo "this commit finished, or deploy 'latest'." >&2
        exit 1
        ;;
    *) echo "    can't tell (not logged in to ghcr, or docker missing) — continuing." ;;
esac

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

# When the pre-upgrade migrate hook fails, helm says "context canceled" or
# "timed out waiting for the condition" and stops. Neither names the cause,
# which is in the hook pod — a tag that will not pull, or a migration the
# database refused. Print it rather than making the next person go looking.
explain_failure() {
    local pod
    pod=$(kubectl get pods -n "$NAMESPACE" --sort-by=.metadata.creationTimestamp -o name 2>/dev/null \
          | grep -- '-migrate-' | tail -1 || true)
    [[ -n "$pod" ]] || return 0

    echo
    echo "==> the migrate hook is where this usually dies. $pod:"
    kubectl describe "$pod" -n "$NAMESPACE" 2>/dev/null | sed -n '/^Events:/,$p' | head -20
    echo
    echo "==> its logs, if it got far enough to produce any:"
    kubectl logs "$pod" -n "$NAMESPACE" --tail=30 2>/dev/null || echo "    (none — it never started)"
}

echo "==> helm upgrade --install"
if ! helm upgrade --install \
    --namespace "$NAMESPACE" \
    --values "$VALUES_FILE" \
    --set image="${IMAGE_REPO}:${TAG}" \
    --wait --timeout 5m \
    "$INSTANCE" helm/localevents/; then
    explain_failure
    exit 1
fi

echo
echo "==> deployed. rollout status:"
kubectl rollout status deployment -n "$NAMESPACE" --timeout=60s || true
