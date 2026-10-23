#!/usr/bin/env bash
# Sign a webhook payload like Jira does and send it to a running ticket2pr.
#   T2P_JIRA_WEBHOOK_SECRET=... ./examples/send-webhook.sh examples/jira-issue-created.json
# A body starts at most one job per process (replays are refused): to trigger again, edit
# the payload (e.g. add "timestamp": <epoch ms>) or restart the service.
set -euo pipefail
payload_file="${1:?usage: $0 <payload.json> [url]}"
url="${2:-http://127.0.0.1:8080/webhooks/jira}"
: "${T2P_JIRA_WEBHOOK_SECRET:?set T2P_JIRA_WEBHOOK_SECRET}"
signature=$(openssl dgst -sha256 -hmac "$T2P_JIRA_WEBHOOK_SECRET" -r <"$payload_file" | cut -d' ' -f1)
curl --fail-with-body -sS -X POST "$url" \
  -H "Content-Type: application/json" \
  -H "X-Hub-Signature: sha256=${signature}" \
  --data-binary @"$payload_file"
echo
