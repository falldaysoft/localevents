#!/usr/bin/env bash
#
# Deploy a localevents instance to its VM, by hand.
#
# CI does this after every image it pushes, through a restricted deploy key
# (see .github/workflows/build.yml). This is the same deploy from a keyboard,
# for a rollback or a redeploy that should not wait for a commit — both paths
# run the same script on the VM and refuse the same things.
#
#   ./scripts/deploy.sh <instance> [image-tag]
#
# <instance> names instances/<instance>.env, your local copy of the overlay
# that lives at ~/apps/<instance>/.env on the VM. Only DEPLOY_HOST is read
# from it here; everything else is the VM's business. Those files are not
# part of the reusable product and are not committed; see instances/example.env.
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
    ls instances/*.env 2>/dev/null | grep -v example | sed 's|instances/|  |; s|\.env$||' >&2 || echo "  (none)" >&2
    exit 1
fi

ENV_FILE="instances/${INSTANCE}.env"
if [[ ! -f "$ENV_FILE" ]]; then
    echo "error: $ENV_FILE not found" >&2
    echo "copy instances/example.env (or the VM's ~/apps/$INSTANCE/.env) and fill it in." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Resolving the tag
#
# CI tags every image with `github.sha` — the full 40-character SHA — and with
# `latest`. A short SHA therefore names an image that does not exist, and the
# failure is a `pull` error on the VM after the connection is already made.
# Cheap to rule out here.
# ---------------------------------------------------------------------------

if [[ -z "$TAG" ]]; then
    TAG=$(git rev-parse HEAD 2>/dev/null || true)
    if [[ -z "$TAG" ]]; then
        echo "error: no tag given and this is not a git checkout." >&2
        echo "pass one explicitly: $0 $INSTANCE <full-sha>" >&2
        exit 1
    fi
    echo "==> no tag given, using HEAD"
elif [[ "$TAG" == "latest" || "$TAG" == "main" ]]; then
    # A mutable tag does not deploy. The VM's deploy.sh records the tag it
    # ran in .env so a rollback has something to name; `latest` records
    # nothing, and `pull` may or may not fetch anything new. Under Kubernetes
    # this was worse — the pod spec did not change, so nothing rolled while
    # the migrate job ran anyway and left the database ahead of the code.
    # Compose has no such split, but the rule is kept because it is cheap and
    # the habit is worth having.
    echo "error: refusing to deploy the mutable tag '$TAG'." >&2
    echo "" >&2
    echo "Deploy an immutable tag instead:" >&2
    echo "" >&2
    echo "  make deploy INSTANCE=$INSTANCE                       # HEAD" >&2
    echo "  make deploy INSTANCE=$INSTANCE TAG=\$(git rev-parse HEAD)" >&2
    exit 1
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
if git cat-file -e "${TAG}^{commit}" 2>/dev/null; then
    if ! git branch -r --contains "$TAG" 2>/dev/null | grep -q .; then
        echo "warning: $TAG is on no known remote branch — did CI ever build it?" >&2
    fi
fi

# ---------------------------------------------------------------------------

# Where the instance lives is a fact about the instance, so it is read from
# the overlay rather than assumed. A machine that also administers other
# hosts has nothing to get wrong this way.
DEPLOY_HOST=$(grep -E '^DEPLOY_HOST=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"'"'")
if [[ -z "$DEPLOY_HOST" ]]; then
    echo "error: $ENV_FILE does not set DEPLOY_HOST (user@host of the VM)" >&2
    exit 1
fi

echo "==> instance:  $INSTANCE"
echo "==> host:      $DEPLOY_HOST"
echo "==> image:     ${IMAGE_REPO}:${TAG}"
echo

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
        echo "this commit finished." >&2
        exit 1
        ;;
    *) echo "    can't tell (not logged in to ghcr, or docker missing) — continuing." ;;
esac

# The VM's script does the rest: writes the tag into .env, pulls, and brings
# the containers up. Run through the user's own key, not the CI one — the CI
# key is bound to a forced command and would work too, but this is the
# machine's own identity doing something on purpose.
echo "==> running ~/apps/$INSTANCE/deploy.sh $TAG on $DEPLOY_HOST"
exec ssh "$DEPLOY_HOST" "~/apps/$INSTANCE/deploy.sh $TAG"
