#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${OPEN_WEBUI_URL:-http://100.115.220.54:3000}"
ENV_FILE="${AEGIS_CORE_ENV_FILE:-/home/rafiurrahman/projects/aegis-core-next/.env}"
CONTAINER="${OPEN_WEBUI_CONTAINER:-open-webui}"

admin_id=$(docker exec "$CONTAINER" python -c 'import sqlite3; print(sqlite3.connect("/app/backend/data/webui.db").execute("select id from user where role=\"admin\" limit 1").fetchone()[0])')
token=$(docker exec "$CONTAINER" python -c 'import os,sqlite3; os.environ["WEBUI_SECRET_KEY"]=open("/app/backend/data/.webui_secret_key").read().strip(); from datetime import timedelta; from open_webui.utils.auth import create_token; uid=sqlite3.connect("/app/backend/data/webui.db").execute("select id from user where role=\"admin\" limit 1").fetchone()[0]; print(create_token({"id":uid},timedelta(minutes=5)))' 2>/dev/null)
test -n "$token"

auth=(-H "Authorization: Bearer $token" -H "Content-Type: application/json")
groups=$(curl -fsS "${auth[@]}" "$BASE_URL/api/v1/groups/")
group_id=$(jq -r 'map(select(.name=="Aegis Operators"))[0].id // empty' <<<"$groups")
if [[ -z "$group_id" ]]; then
  created=$(jq -nc '{name:"Aegis Operators",description:"Operators allowed to use the private read-only Aegis Observability MCP",permissions:null,data:{}}' |
    curl -fsS "${auth[@]}" --data-binary @- "$BASE_URL/api/v1/groups/create")
  group_id=$(jq -r .id <<<"$created")
  echo "OPERATOR_GROUP=created"
else
  echo "OPERATOR_GROUP=existing"
fi

jq -nc --arg uid "$admin_id" '{user_ids:[$uid]}' |
  curl -fsS "${auth[@]}" --data-binary @- "$BASE_URL/api/v1/groups/id/$group_id/users/add" >/dev/null
echo "ADMIN_MEMBERSHIP=present"

model_payload=$(jq -nc --arg gid "$group_id" '{
  id:"qwen3:8b",
  base_model_id:null,
  name:"Aegis Local Operator",
  meta:{description:"Private local model for Aegis operator diagnostics",capabilities:{tool_calling:true}},
  params:{think:false},
  access_grants:[{principal_type:"group",principal_id:$gid,permission:"read"}],
  is_active:true
}')
existing_model=$(curl -fsS -G "${auth[@]}" --data-urlencode 'id=qwen3:8b' "$BASE_URL/api/v1/models/model" 2>/dev/null || true)
if jq -e '.id=="qwen3:8b"' >/dev/null 2>&1 <<<"$existing_model"; then
  model_result=$(curl -fsS "${auth[@]}" --data-binary "$model_payload" "$BASE_URL/api/v1/models/model/update")
  echo "OPERATOR_MODEL=updated"
else
  model_result=$(curl -fsS "${auth[@]}" --data-binary "$model_payload" "$BASE_URL/api/v1/models/create")
  echo "OPERATOR_MODEL=created"
fi
jq -e '.id=="qwen3:8b" and (.access_grants|length)==1' >/dev/null <<<"$model_result"
echo "OPERATOR_MODEL_ACL=present"

obsolete_model=$(curl -fsS -G "${auth[@]}" --data-urlencode 'id=qwen2.5-coder:7b-instruct' "$BASE_URL/api/v1/models/model" 2>/dev/null || true)
if jq -e '.id=="qwen2.5-coder:7b-instruct"' >/dev/null 2>&1 <<<"$obsolete_model"; then
  jq -nc '{id:"qwen2.5-coder:7b-instruct"}' |
    curl -fsS "${auth[@]}" --data-binary @- "$BASE_URL/api/v1/models/model/delete" >/dev/null
  echo "OBSOLETE_OPERATOR_MODEL_OVERRIDE=removed"
fi

broken_wrapper=$(curl -fsS -G "${auth[@]}" --data-urlencode 'id=aegis-local-operator' "$BASE_URL/api/v1/models/model" 2>/dev/null || true)
if jq -e '.id=="aegis-local-operator"' >/dev/null 2>&1 <<<"$broken_wrapper"; then
  jq -nc '{id:"aegis-local-operator"}' |
    curl -fsS "${auth[@]}" --data-binary @- "$BASE_URL/api/v1/models/model/delete" >/dev/null
  echo "BROKEN_MODEL_WRAPPER=removed"
fi

observability_token=$(sed -n 's/^AEGIS_OBSERVABILITY_TOKEN=//p' "$ENV_FILE" | tail -n 1)
observability_token=${observability_token#\"}
observability_token=${observability_token%\"}
test -n "$observability_token"

connection=$(jq -nc --arg key "$observability_token" --arg gid "$group_id" '{
  url:"http://observability-mcp:8020/mcp",
  path:"",
  type:"mcp",
  auth_type:"bearer",
  headers:null,
  key:$key,
  config:{enable:true,access_grants:[{principal_type:"group",principal_id:$gid,permission:"read"}]},
  info:{id:"aegis-observability",name:"Aegis Observability",description:"Authenticated, read-only health, logs, and metrics for registered plugins"}
}')

verified=$(curl -fsS "${auth[@]}" --data-binary "$connection" "$BASE_URL/api/v1/configs/tool_servers/verify")
echo "MCP_VERIFY_STATUS=$(jq -r .status <<<"$verified") TOOL_COUNT=$(jq '.specs|length' <<<"$verified")"
jq -r '.specs[]?.name | "MCP_TOOL=" + .' <<<"$verified"

current=$(curl -fsS "${auth[@]}" "$BASE_URL/api/v1/configs/tool_servers")
payload=$(jq -nc --argjson current "$current" --argjson connection "$connection" '{
  TOOL_SERVER_CONNECTIONS: (($current.TOOL_SERVER_CONNECTIONS // [] | map(select((.info.id // "") != "aegis-observability"))) + [$connection])
}')
saved=$(curl -fsS "${auth[@]}" --data-binary "$payload" "$BASE_URL/api/v1/configs/tool_servers")
echo "MCP_SAVED_COUNT=$(jq '.TOOL_SERVER_CONNECTIONS|length' <<<"$saved")"
jq -r '.TOOL_SERVER_CONNECTIONS[] | select(.info.id=="aegis-observability") | "MCP_SAVED_TYPE="+.type+" ACL_GRANTS="+(.config.access_grants|length|tostring)' <<<"$saved"
