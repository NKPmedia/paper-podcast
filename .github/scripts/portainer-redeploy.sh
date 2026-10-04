#!/usr/bin/env bash
# Redeploy a Portainer stack with freshly pulled images through the Portainer API.
# Works with Portainer Community Edition (stack webhooks need Business Edition).
#
# Environment:
#   PORTAINER_URL       e.g. https://portainer.example.org:9443 (no trailing /api)
#   PORTAINER_API_KEY   access token (Portainer → My account → Access tokens)
#   PORTAINER_STACK_ID  numeric ID, from the stack's URL: …/stacks/<ID>?…
#   PORTAINER_INSECURE  "true" to accept a self-signed certificate (optional)
set -euo pipefail

if [ -z "${PORTAINER_URL:-}" ] || [ -z "${PORTAINER_API_KEY:-}" ] || [ -z "${PORTAINER_STACK_ID:-}" ]; then
  echo "::notice::PORTAINER_URL, PORTAINER_API_KEY or PORTAINER_STACK_ID is not set – skipping the redeploy."
  exit 0
fi
case "$PORTAINER_STACK_ID" in *[!0-9]*) echo "::error::PORTAINER_STACK_ID must be a number (from the stack's URL)."; exit 1;; esac

base="${PORTAINER_URL%/}"
base="${base%/api}/api"
curl_opts=(--silent --show-error --retry 3 --retry-delay 10 --retry-all-errors)
[ "${PORTAINER_INSECURE:-}" = "true" ] && curl_opts+=(--insecure)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# call METHOD PATH [BODY_FILE] -> response in $work/response.json, fails with a clear message
call() {
  local method=$1 path=$2 body=${3:-} status
  local args=("${curl_opts[@]}" --max-time 600 -X "$method" -H "X-API-Key: $PORTAINER_API_KEY"
              --output "$work/response.json" --write-out '%{http_code}')
  [ -n "$body" ] && args+=(-H "Content-Type: application/json" --data-binary "@$body")
  status=$(curl "${args[@]}" "$base$path") || {
    echo "::error::Portainer is not reachable at $base (network, DNS or TLS problem – for a self-signed certificate set PORTAINER_INSECURE=true)."
    exit 1
  }
  if [ "$status" -ge 200 ] && [ "$status" -lt 300 ]; then
    return 0
  fi
  case "$status" in
    401|403) echo "::error::Portainer rejected the API key (HTTP $status). Create a new access token under My account → Access tokens." ;;
    404) echo "::error::Portainer does not know stack $PORTAINER_STACK_ID (HTTP 404). Check PORTAINER_STACK_ID." ;;
    *) echo "::error::Portainer answered HTTP $status on $method $path." ;;
  esac
  head -c 2000 "$work/response.json" || true
  echo
  exit 1
}

call GET "/stacks/$PORTAINER_STACK_ID"
cp "$work/response.json" "$work/stack.json"
name=$(jq -r '.Name' "$work/stack.json")
endpoint=$(jq -r '.EndpointId' "$work/stack.json")
echo "Stack \"$name\" (ID $PORTAINER_STACK_ID) on environment $endpoint"

if [ "$(jq -r '.GitConfig != null' "$work/stack.json")" = "true" ]; then
  # Stack deployed from a Git repository: redeploy from Git and pull the images.
  jq '{env: (.Env // []), prune: false, pullImage: true, repositoryReferenceName: .GitConfig.ReferenceName}' \
    "$work/stack.json" > "$work/body.json"
  call PUT "/stacks/$PORTAINER_STACK_ID/git/redeploy?endpointId=$endpoint" "$work/body.json"
else
  # Stack from the web editor or an upload: send its current file back with pullImage.
  call GET "/stacks/$PORTAINER_STACK_ID/file"
  jq -n --slurpfile stack "$work/stack.json" --slurpfile file "$work/response.json" \
    '{stackFileContent: $file[0].StackFileContent, env: ($stack[0].Env // []), prune: false, pullImage: true}' \
    > "$work/body.json"
  call PUT "/stacks/$PORTAINER_STACK_ID?endpointId=$endpoint" "$work/body.json"
fi
echo "Portainer redeployed \"$name\" with freshly pulled images."
