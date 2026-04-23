#!/usr/bin/env bash
# Prune old custom Compute Engine images in the agent VM family.
#
# Why this exists: Packer adds a new image every time agent/ changes. GCP caps
# images per project; without pruning, provisioning eventually fails. Running
# VMs already booted from older images are unaffected by deletion — the boot
# disk stops referencing the source image once the VM is up.
#
# Usage:
#   scripts/gcp-image-cleanup.sh [options]
#
# Options:
#   --keep N              Keep only the N most-recent images (delete the rest)
#   --delete-oldest N     Delete the N oldest images (explicit count)
#   --family NAME         Image family (default: blueprint-agent)
#   --project ID          GCP project (default: $GCP_PROJECT_ID or gcloud config)
#   --dry-run             Print what would be deleted without deleting
#   --yes                 Skip interactive confirmation
#   --help                Show this help
#
# Exactly one of --keep / --delete-oldest is required.
#
# Examples:
#   # Preview what a keep=10 policy would delete
#   scripts/gcp-image-cleanup.sh --keep 10 --dry-run
#
#   # Clear the 80 oldest images, with confirmation
#   scripts/gcp-image-cleanup.sh --delete-oldest 80
#
#   # Non-interactive (CI / cron)
#   scripts/gcp-image-cleanup.sh --keep 10 --yes

set -euo pipefail

KEEP=""
DELETE_OLDEST=""
FAMILY="blueprint-agent"
PROJECT="${GCP_PROJECT_ID:-}"
DRY_RUN=0
ASSUME_YES=0

die() { echo "error: $*" >&2; exit 2; }
usage() { sed -n '1,/^set -euo/p' "$0" | grep -E '^#( |$)' | sed 's/^# \?//'; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --keep)          [[ $# -ge 2 ]] || die "--keep needs a value";          KEEP="$2"; shift 2 ;;
    --delete-oldest) [[ $# -ge 2 ]] || die "--delete-oldest needs a value"; DELETE_OLDEST="$2"; shift 2 ;;
    --family)        [[ $# -ge 2 ]] || die "--family needs a value";        FAMILY="$2"; shift 2 ;;
    --project)       [[ $# -ge 2 ]] || die "--project needs a value";       PROJECT="$2"; shift 2 ;;
    --dry-run)       DRY_RUN=1; shift ;;
    --yes|-y)        ASSUME_YES=1; shift ;;
    -h|--help)       usage; exit 0 ;;
    *) die "unknown option: $1 (use --help)" ;;
  esac
done

# Validate mode
if [[ -n "$KEEP" && -n "$DELETE_OLDEST" ]]; then
  die "--keep and --delete-oldest are mutually exclusive"
fi
if [[ -z "$KEEP" && -z "$DELETE_OLDEST" ]]; then
  die "one of --keep or --delete-oldest is required (use --help)"
fi
[[ -n "$KEEP" && ! "$KEEP" =~ ^[0-9]+$ ]] && die "--keep must be a non-negative integer"
[[ -n "$DELETE_OLDEST" && ! "$DELETE_OLDEST" =~ ^[0-9]+$ ]] && die "--delete-oldest must be a non-negative integer"
if [[ -n "$KEEP" && "$KEEP" -lt 1 ]]; then
  die "--keep must be >= 1 (must retain at least one image for VM provisioning)"
fi

# Resolve project
if [[ -z "$PROJECT" ]]; then
  PROJECT="$(gcloud config get-value project 2>/dev/null || true)"
fi
[[ -n "$PROJECT" ]] || die "no GCP project set (--project, \$GCP_PROJECT_ID, or gcloud config)"

echo "project: $PROJECT"
echo "family:  $FAMILY"

# List images in family, newest first (creationTimestamp is ISO-8601 → lexically sortable).
# Windows gcloud emits CRLF; tr -d '\r' strips trailing CRs so image names pass the name regex.
mapfile -t NAMES < <(
  gcloud compute images list \
    --project="$PROJECT" \
    --filter="family=$FAMILY" \
    --format="value(name)" \
    --sort-by="~creationTimestamp" | tr -d '\r'
)

TOTAL="${#NAMES[@]}"
echo "images in family: $TOTAL"

if [[ "$TOTAL" -eq 0 ]]; then
  echo "nothing to do."
  exit 0
fi

# Build the delete list.
if [[ -n "$KEEP" ]]; then
  if [[ "$TOTAL" -le "$KEEP" ]]; then
    echo "kept $TOTAL (<= --keep $KEEP), nothing to delete."
    exit 0
  fi
  TO_DELETE=("${NAMES[@]:$KEEP}")
else
  # --delete-oldest: always preserve at least the newest image.
  if [[ "$TOTAL" -le 1 ]]; then
    echo "only $TOTAL image exists; refusing to delete the single remaining image."
    exit 0
  fi
  MAX_DELETE=$(( TOTAL - 1 ))
  if [[ "$DELETE_OLDEST" -gt "$MAX_DELETE" ]]; then
    echo "capping delete count at $MAX_DELETE (must keep at least one image)."
    N="$MAX_DELETE"
  else
    N="$DELETE_OLDEST"
  fi
  if [[ "$N" -eq 0 ]]; then
    echo "nothing to delete."
    exit 0
  fi
  TO_DELETE=("${NAMES[@]: -$N}")
fi

echo ""
echo "will delete ${#TO_DELETE[@]} images (oldest first):"
for n in "${TO_DELETE[@]}"; do
  echo "  $n"
done

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo ""
  echo "--dry-run: no deletions performed."
  exit 0
fi

if [[ "$ASSUME_YES" -ne 1 ]]; then
  echo ""
  read -r -p "proceed with deletion? [y/N] " reply
  case "$reply" in
    [yY]|[yY][eE][sS]) ;;
    *) echo "aborted."; exit 1 ;;
  esac
fi

FAILED=()
for name in "${TO_DELETE[@]}"; do
  if gcloud compute images delete "$name" --project="$PROJECT" --quiet; then
    echo "deleted: $name"
  else
    FAILED+=("$name")
    echo "FAILED: $name" >&2
  fi
done

echo ""
echo "done. deleted=$(( ${#TO_DELETE[@]} - ${#FAILED[@]} )) failed=${#FAILED[@]}"
if [[ "${#FAILED[@]}" -gt 0 ]]; then
  echo "failures:"
  for n in "${FAILED[@]}"; do echo "  $n"; done
  exit 1
fi
