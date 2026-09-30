#!/bin/bash
# Based on Clancey's original build script:
# https://gist.githubusercontent.com/Clancey/8ec54003939c4cb7293f88c7b494b65e/raw/build.sh
set -e

# Define color variables.
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Helper functions for colored output.
info() {
  echo -e "${BLUE}[INFO] $1${NC}"
}

success() {
  echo -e "${GREEN}[SUCCESS] $1${NC}"
}

warn() {
  echo -e "${YELLOW}[WARNING] $1${NC}"
}

error() {
  echo -e "${RED}[ERROR] $1${NC}"
}

# Print the script's content before running any commands.
info "===== build.sh content start ====="
cat "$0"
info "===== build.sh content end ====="

info "Running Godot import"
godot --headless -e --quit-after 200

info "Zipping project"
eval "$ZIPCOMMAND"

info "Initiating upload process..."

# Step 1: Request a direct R2 upload URL and a completion token.
release_tag="${version:-${GITHUB_REF_NAME:-1.0}}"
godot_version="${GODOT_VERSION:-$release_tag}"
file_size=$(wc -c < build.zip | tr -d '[:space:]')
query_params="tag=${release_tag}&godot_version=${godot_version}&runNumber=${GITHUB_RUN_NUMBER}&tags=${TAGS}&iconUrl=${ICON_URL}&file_size=${file_size}"

if [ -n "${GODOT_ASSET_ID}" ] && [ "${GODOT_ASSET_ID}" != "0" ]; then
    query_params="godot_id=${GODOT_ASSET_ID}&${query_params}"
fi

if [ -n "${XOGOT_ASSET_ID}" ] && [ "${XOGOT_ASSET_ID}" != "0" ]; then
    query_params="xogot_id=${XOGOT_ASSET_ID}&${query_params}"
fi

response_file=$(mktemp)
error_file=$(mktemp)
trap 'rm -f "$response_file" "$error_file"' EXIT
if ! http_status=$(curl -sS -o "$response_file" -w "%{http_code}" -X POST "${uploadUrl}?${query_params}${EXTRA_ARGS}" \
  -H "apiKey: ${apiKey}" -d "" 2>"$error_file"); then
    error "Upload initiation request failed"
    exit 1
fi
response=$(cat "$response_file")

if [ "$http_status" -lt 200 ] || [ "$http_status" -ge 300 ]; then
    error "Upload initiation failed with HTTP $http_status"
    exit 1
fi

if [ -z "$response" ]; then
    error "Empty response from upload initiation, HTTP $http_status"
    exit 1
fi

# Parse the JSON response for the uploadUrl property.
upload_url=$(echo "$response" | jq -er '.uploadUrl | select(type == "string" and length > 0)' 2>"$error_file") || {
    error "Failed to retrieve uploadUrl from response"
    exit 1
}
complete_url=$(echo "$response" | jq -er '.completeUrl | select(type == "string" and length > 0)' 2>"$error_file") || {
    error "Failed to retrieve completeUrl from response"
    exit 1
}
upload_token=$(echo "$response" | jq -er '.uploadToken | select(type == "string" and length > 0)' 2>"$error_file") || {
    error "Failed to retrieve uploadToken from response"
    exit 1
}

info "Upload authorization retrieved"

# Step 2: Stream the archive from disk to R2. These headers are signed by the API.
if ! http_status=$(curl -sS -o "$response_file" -w "%{http_code}" --retry 3 --retry-delay 2 \
  --upload-file build.zip "$upload_url" -H "Content-Type: application/zip" -H "If-None-Match: *" 2>"$error_file"); then
    error "Zip upload request failed"
    exit 1
fi
if { [ "$http_status" -lt 200 ] || [ "$http_status" -ge 300 ]; } && [ "$http_status" != "412" ]; then
    error "Zip upload failed with HTTP $http_status"
    exit 1
fi

# A retry can return 412 after an earlier PUT succeeded. The API verifies that
# the immutable object exists and matches this token's expected size either way.
info "Verifying uploaded archive"
completion_body=$(jq -n --arg uploadToken "$upload_token" '{uploadToken: $uploadToken}')
if ! http_status=$(curl -sS -o "$response_file" -w "%{http_code}" -X POST "$complete_url" \
  -H "apiKey: ${apiKey}" -H "Content-Type: application/json" -d "$completion_body" 2>"$error_file"); then
    error "Upload completion request failed"
    exit 1
fi
if [ "$http_status" -lt 200 ] || [ "$http_status" -ge 300 ]; then
    error "Upload completion failed with HTTP $http_status"
    exit 1
fi

success "Zip upload verified and published"
