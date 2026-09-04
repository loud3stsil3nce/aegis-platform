#!/bin/sh
set -eu

fixture_dir=/tmp/aegis-proxy-build-20260903/services/docker_deployment/integration
state_dir=/tmp/aegis-proxy-integration-20260903/state
token_file=/tmp/aegis-proxy-integration-20260903/token
proxy_name=aegis-deployment-proxy-disposable
project_name=aegis-deployment-disposable
network_name=aegis-deployment-disposable_default
image='python@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea'

cleanup() {
  docker rm -f "$proxy_name" >/dev/null 2>&1 || true
  AEGIS_DISPOSABLE_IMAGE="$image" docker compose \
    --project-name "$project_name" --project-directory "$fixture_dir" \
    -f "$fixture_dir/compose.yaml" down >/dev/null 2>&1 || true
}
trap cleanup EXIT

mkdir -p "$state_dir"
chgrp 983 "$state_dir"
chmod 0770 "$state_dir"
install -m 0600 -o "$(id -u)" -g 983 /dev/null "$token_file"
openssl rand -hex -out "$token_file" 32
chmod 0440 "$token_file"

AEGIS_DISPOSABLE_IMAGE="$image" docker compose \
  --project-name "$project_name" --project-directory "$fixture_dir" \
  -f "$fixture_dir/compose.yaml" up -d --no-build disposable >/dev/null

docker run -d --name "$proxy_name" --network "$network_name" \
  --group-add 983 --read-only --cap-drop ALL --security-opt no-new-privileges \
  --tmpfs /tmp:size=16m,noexec,nosuid,nodev \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$fixture_dir:/fixtures:ro" \
  -v "$token_file:/run/secrets/deployment-token:ro" \
  -v "$state_dir:/var/lib/aegis-deployment" \
  -e AEGIS_DEPLOYMENT_PROXY_POLICY=/fixtures/policy.json \
  -e AEGIS_DEPLOYMENT_PROXY_TOKEN_FILE=/run/secrets/deployment-token \
  -e AEGIS_DEPLOYMENT_PROXY_STATE=/var/lib/aegis-deployment/idempotency.sqlite3 \
  aegis/docker-deployment:test >/dev/null

sleep 2
docker exec "$proxy_name" id
docker exec "$proxy_name" docker inspect --format '{{json .State}}|{{json .Config.Image}}' \
  aegis-deployment-disposable-disposable-1
docker exec "$proxy_name" python -c '
import json, pathlib, urllib.error, urllib.request, uuid
token = pathlib.Path("/run/secrets/deployment-token").read_text().strip()
base = "http://127.0.0.1:8013/v1/targets/disposable"
headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
state = json.load(urllib.request.urlopen(urllib.request.Request(base + "/state", headers=headers), timeout=5))
assert state["status"] == "running" and "@sha256:" in state["image"]
body = json.dumps({"image_reference": state["image"], "idempotency_key": str(uuid.uuid4()) + ":deploy"}).encode()
result = json.load(urllib.request.urlopen(urllib.request.Request(base + "/image", data=body, headers=headers, method="POST"), timeout=30))
assert result["status"] == "accepted"
replay = json.load(urllib.request.urlopen(urllib.request.Request(base + "/image", data=body, headers=headers, method="POST"), timeout=5))
assert replay["status"] == "already_applied"
pathlib.Path("/var/lib/aegis-deployment/replay.json").write_bytes(body)
for url, bad_body, expected in ((base.replace("disposable", "database") + "/state", None, 404), (base + "/image", b"{}", 422)):
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=bad_body, headers=headers, method="POST" if bad_body else "GET"), timeout=5)
        raise AssertionError("denial expected")
    except urllib.error.HTTPError as error:
        assert error.code == expected
print("disposable proxy integration passed")
'

docker restart "$proxy_name" >/dev/null
sleep 2
docker exec "$proxy_name" python -c '
import json, pathlib, urllib.request
token = pathlib.Path("/run/secrets/deployment-token").read_text().strip()
headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
body = pathlib.Path("/var/lib/aegis-deployment/replay.json").read_bytes()
request = urllib.request.Request("http://127.0.0.1:8013/v1/targets/disposable/image", data=body, headers=headers, method="POST")
assert json.load(urllib.request.urlopen(request, timeout=5))["status"] == "already_applied"
print("persistent replay integration passed")
'

docker inspect "$proxy_name" --format '{{json .HostConfig.PortBindings}}' | grep -qx 'null'
docker inspect aegis-deployment-disposable-disposable-1 --format '{{.State.Health.Status}}' | grep -qx healthy
