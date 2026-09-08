#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${OPEN_WEBUI_URL:-http://100.115.220.54:3000}"
CONTAINER="${OPEN_WEBUI_CONTAINER:-open-webui}"
TEST_EMAIL="aegis-phase5-rbac-test@invalid.local"

admin_token=$(docker exec "$CONTAINER" python -c 'import os,sqlite3; os.environ["WEBUI_SECRET_KEY"]=open("/app/backend/data/.webui_secret_key").read().strip(); from datetime import timedelta; from open_webui.utils.auth import create_token; uid=sqlite3.connect("/app/backend/data/webui.db").execute("select id from user where role=\"admin\" limit 1").fetchone()[0]; print(create_token({"id":uid},timedelta(minutes=5)))' 2>/dev/null)
admin_auth=(-H "Authorization: Bearer $admin_token" -H "Content-Type: application/json")

existing_id=$(curl -fsS "${admin_auth[@]}" "$BASE_URL/api/v1/users/all" | jq -r --arg email "$TEST_EMAIL" '.users[] | select(.email==$email) | .id' | head -n 1)
if [[ -n "$existing_id" ]]; then
  curl -fsS -X DELETE "${admin_auth[@]}" "$BASE_URL/api/v1/users/$existing_id" >/dev/null
fi

test_password=$(openssl rand -base64 24)
created=$(jq -nc --arg email "$TEST_EMAIL" --arg password "$test_password" '{name:"Aegis Phase 5 RBAC Test",email:$email,password:$password,role:"user"}' |
  curl -fsS "${admin_auth[@]}" --data-binary @- "$BASE_URL/api/v1/auths/add")
test_user_id=$(jq -r .id <<<"$created")
test -n "$test_user_id"

cleanup() {
  curl -fsS -X DELETE "${admin_auth[@]}" "$BASE_URL/api/v1/users/$test_user_id" >/dev/null 2>&1 || true
}
trap cleanup EXIT

user_token=$(docker exec "$CONTAINER" python -c "import os; os.environ['WEBUI_SECRET_KEY']=open('/app/backend/data/.webui_secret_key').read().strip(); from datetime import timedelta; from open_webui.utils.auth import create_token; print(create_token({'id':'$test_user_id'},timedelta(minutes=5)))" 2>/dev/null)
user_auth=(-H "Authorization: Bearer $user_token")
group_id=$(curl -fsS "${admin_auth[@]}" "$BASE_URL/api/v1/groups/" | jq -r 'map(select(.name=="Aegis Operators"))[0].id')

tool_visible() {
  curl -fsS "${user_auth[@]}" "$BASE_URL/api/v1/tools/" | jq -e 'any(.id == "server:mcp:aegis-observability")' >/dev/null
}

curl -fsS "${admin_auth[@]}" "$BASE_URL/api/v1/tools/" | jq -e 'any(.id == "server:mcp:aegis-observability")' >/dev/null
echo "ADMIN_DISCOVERY=pass"

if tool_visible; then
  echo "NON_OPERATOR_DENIAL=fail" >&2
  exit 10
fi
echo "NON_OPERATOR_DENIAL=pass"

jq -nc --arg uid "$test_user_id" '{user_ids:[$uid]}' |
  curl -fsS "${admin_auth[@]}" --data-binary @- "$BASE_URL/api/v1/groups/id/$group_id/users/add" >/dev/null
tool_visible
echo "OPERATOR_GROUP_GRANT=pass"

jq -nc --arg uid "$test_user_id" '{user_ids:[$uid]}' |
  curl -fsS "${admin_auth[@]}" --data-binary @- "$BASE_URL/api/v1/groups/id/$group_id/users/remove" >/dev/null
if tool_visible; then
  echo "OPERATOR_GROUP_REVOCATION=fail" >&2
  exit 11
fi
echo "OPERATOR_GROUP_REVOCATION=pass"

cleanup
trap - EXIT
remaining=$(curl -fsS "${admin_auth[@]}" "$BASE_URL/api/v1/users/all" | jq --arg email "$TEST_EMAIL" '[.users[] | select(.email==$email)] | length')
[[ "$remaining" == 0 ]]
echo "TEMP_USER_CLEANUP=pass"
