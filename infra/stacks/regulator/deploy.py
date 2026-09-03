#!/usr/bin/env python3
"""Deploy (or update) the Regulator control-plane swarm stack via the Portainer API.

Creates a swarm stack from stack.yml, injecting env from this directory's
.env, and ensures the ``regulator_master_key`` swarm secret exists (created
once from a generated Fernet key persisted in .env so encrypted credentials
survive redeploys). Idempotent: an existing stack is updated in place. The
same shape as Stoker's deploy.py, because it is the same job.

Config: reads this directory's ``.env`` (copy .env.example), falling back to the
process env. Required: PORTAINER_HOST, PORTAINER_TOKEN, REG_ADMIN_PASSWORD.

Usage:
  python deploy.py            # create or update the stack
  python deploy.py --dry-run  # show what would happen, change nothing
  python deploy.py --status   # show the current stack + service
"""
from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings()

HERE = Path(__file__).resolve().parent
STACK_NAME = "regulator"
SECRET_NAME = "regulator_master_key"
ENV_KEYS = [
    "REG_ADMIN_PASSWORD", "REG_API_TOKENS",
    "REGULATOR_NODE", "REGULATOR_PORT", "REGULATOR_HOST", "REGULATOR_IMAGE",
    "REG_HEC_URL", "REG_HEC_TOKEN", "REG_HEC_INDEX", "REG_HEC_VERIFY_TLS",
    "REG_SEED_TARGET_NAME", "REG_SEED_TARGET_URL", "REG_SEED_TARGET_WEB_URL",
    "REG_SEED_TARGET_TOKEN", "REG_SEED_TARGET_USERNAME", "REG_SEED_TARGET_PASSWORD",
    "REG_SEED_TARGET_VERIFY_TLS",
    "REG_MAX_VIRTUAL_USERS", "REG_MAX_CONCURRENT_RUNS", "REG_MAX_RUN_DURATION_S",
]
SECRET_KEYS = {
    "REG_ADMIN_PASSWORD", "REG_API_TOKENS", "REG_HEC_TOKEN",
    "REG_SEED_TARGET_TOKEN", "REG_SEED_TARGET_PASSWORD",
}


def load_env(path: Path) -> dict:
    found = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                # Trailing comments, .env.example style.
                value = value.split("  #", 1)[0].strip()
                found[key.strip()] = value.strip()
    return found


def portainer_base(host: str) -> str:
    return host.rstrip("/") if host.startswith("http") else f"https://{host}:9443"


def generate_master_key() -> str:
    try:
        from cryptography.fernet import Fernet

        return Fernet.generate_key().decode()
    except Exception:  # noqa: BLE001 - cryptography not installed here
        return base64.urlsafe_b64encode(os.urandom(32)).decode()


def docker_api(base: str, headers: dict, endpoint: int, method: str, path: str, **kw):
    return requests.request(
        method,
        f"{base}/api/endpoints/{endpoint}/docker{path}",
        headers=headers,
        verify=False,
        timeout=60,
        **kw,
    )


def ensure_master_key_secret(base: str, headers: dict, endpoint: int, key_value: str, dry: bool) -> bool:
    """Create the swarm secret if it does not already exist. Secrets are immutable."""
    response = docker_api(base, headers, endpoint, "GET", "/secrets")
    if response.status_code != 200:
        print(f"ERROR listing secrets: HTTP {response.status_code} {response.text[:200]}", file=sys.stderr)
        return False
    for secret in response.json():
        if secret.get("Spec", {}).get("Name") == SECRET_NAME:
            print(f"Secret '{SECRET_NAME}' already exists (id={secret.get('ID', '')[:12]}), reusing.")
            return True
    if dry:
        print(f"[dry-run] would create swarm secret '{SECRET_NAME}'")
        return True
    body = {
        "Name": SECRET_NAME,
        "Data": base64.b64encode(key_value.encode()).decode(),
        "Labels": {"app": "regulator"},
    }
    response = docker_api(base, headers, endpoint, "POST", "/secrets/create", json=body)
    if response.status_code in (200, 201):
        print(f"Created swarm secret '{SECRET_NAME}'.")
        return True
    print(f"ERROR creating secret: HTTP {response.status_code} {response.text[:300]}", file=sys.stderr)
    return False


def swarm_id(base: str, headers: dict, endpoint: int) -> str:
    response = docker_api(base, headers, endpoint, "GET", "/swarm")
    response.raise_for_status()
    return response.json()["ID"]


def find_stack(base: str, headers: dict):
    response = requests.get(f"{base}/api/stacks", headers=headers, verify=False, timeout=30)
    response.raise_for_status()
    return next((s for s in response.json() if s.get("Name") == STACK_NAME), None)


def show_status(base: str, headers: dict, endpoint: int) -> int:
    stack = find_stack(base, headers)
    if not stack:
        print(f"Stack '{STACK_NAME}' not deployed.")
        return 0
    print(f"Stack '{STACK_NAME}' id={stack['Id']} status={stack.get('Status')}")
    response = docker_api(base, headers, endpoint, "GET", "/services")
    if response.status_code == 200:
        for service in response.json():
            name = service.get("Spec", {}).get("Name", "")
            if name.startswith("regulator"):
                mode = service.get("Spec", {}).get("Mode", {}).get("Replicated", {})
                print(f"  service {name}: replicas={mode.get('Replicas', '?')}")
    return 0


def main() -> int:
    env = {
        **load_env(HERE / ".env"),
        **{k: v for k, v in os.environ.items() if k in ENV_KEYS or k.startswith("PORTAINER")},
    }
    host, token = env.get("PORTAINER_HOST", ""), env.get("PORTAINER_TOKEN", "")
    if not host or not token:
        print("ERROR: PORTAINER_HOST / PORTAINER_TOKEN not set (see .env.example).", file=sys.stderr)
        return 1
    endpoint = int(env.get("PORTAINER_ENDPOINT", "6"))
    base = portainer_base(host)
    headers = {"X-API-Key": token, "Content-Type": "application/json"}

    if "--status" in sys.argv:
        return show_status(base, headers, endpoint)
    dry = "--dry-run" in sys.argv

    if not env.get("REG_ADMIN_PASSWORD"):
        print(
            "ERROR: REG_ADMIN_PASSWORD not set. The control plane refuses to start without one.",
            file=sys.stderr,
        )
        return 1
    if bool(env.get("REG_HEC_URL")) != bool(env.get("REG_HEC_TOKEN")):
        print("ERROR: REG_HEC_URL and REG_HEC_TOKEN must be set together, or neither.", file=sys.stderr)
        return 1

    stack_env_path = HERE / ".env"
    master_key = load_env(stack_env_path).get("REG_MASTER_KEY") or env.get("REG_MASTER_KEY")
    if not master_key:
        master_key = generate_master_key()
        if not dry:
            with open(stack_env_path, "a", encoding="utf-8") as handle:
                handle.write(f"\nREG_MASTER_KEY={master_key}\n")
            print(f"Generated a new master key and appended it to {stack_env_path}. Back that file up.")
    if not ensure_master_key_secret(base, headers, endpoint, master_key, dry):
        return 1

    compose = (HERE / "stack.yml").read_text()
    env_pairs = [{"name": k, "value": env[k]} for k in ENV_KEYS if env.get(k)]
    print(f"Env injected: {[e['name'] for e in env_pairs]} (secret values not shown)")

    existing = find_stack(base, headers)
    if dry:
        print(
            f"[dry-run] would {'UPDATE' if existing else 'CREATE'} stack '{STACK_NAME}' "
            f"on endpoint {endpoint} ({len(compose)} bytes)."
        )
        return 0

    if existing:
        sid = existing["Id"]
        print(f"Stack exists (id={sid}), updating in place...")
        body = {"stackFileContent": compose, "env": env_pairs, "prune": False, "pullImage": True}
        response = requests.put(
            f"{base}/api/stacks/{sid}?endpointId={endpoint}",
            headers=headers, json=body, verify=False, timeout=180,
        )
    else:
        print(f"Creating swarm stack '{STACK_NAME}'...")
        body = {
            "name": STACK_NAME,
            "swarmID": env.get("REGULATOR_SWARM_ID") or swarm_id(base, headers, endpoint),
            "stackFileContent": compose,
            "env": env_pairs,
            "fromAppTemplate": False,
        }
        response = requests.post(
            f"{base}/api/stacks/create/swarm/string?endpointId={endpoint}",
            headers=headers, json=body, verify=False, timeout=180,
        )
        if response.status_code == 404:
            response = requests.post(
                f"{base}/api/stacks?type=1&method=string&endpointId={endpoint}",
                headers=headers, json=body, verify=False, timeout=180,
            )

    print("HTTP", response.status_code)
    try:
        out = response.json()
    except Exception:  # noqa: BLE001
        print(response.text[:1000])
        return 0 if response.ok else 1
    if response.ok:
        print(f"OK: stack id={out.get('Id')} name={out.get('Name')}")
        print(
            f"Reach it on the LAN at http://<swarm-node-ip>:{env.get('REGULATOR_PORT', '8092')} "
            f"and, once DNS exists, https://{env.get('REGULATOR_HOST', 'regulator.mydomain.com')}"
        )
        return 0
    print("ERROR:", json.dumps(out)[:1000], file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
