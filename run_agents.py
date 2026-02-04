#!/usr/bin/env python3
"""Launch multiple agent containers and broadcast an instruction.

This CLI builds the agent image (unless skipped), starts N containers on
consecutive ports beginning at 28000 by default, waits for each to report
healthy, sends an instruction to every agent concurrently, then prints their
responses.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import string
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, List, Tuple


def run_cmd(cmd: List[str], env: Dict[str, str] | None = None) -> None:
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
    )
    if proc.returncode != 0:
        output = proc.stdout.decode(errors="replace") if proc.stdout else ""
        raise RuntimeError(
            f"Command failed ({proc.returncode}): {' '.join(cmd)}\n{output}"
        )


def random_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*-_"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def build_image(image: str, dockerfile: Path, context: Path, config_host: Path) -> None:
    env = os.environ.copy()
    env.setdefault("DOCKER_BUILDKIT", "1")

    cmd = [
        "docker",
        "build",
        "-f",
        str(dockerfile),
        "-t",
        image,
        "--build-arg",
        f"OPENCODE_CONFIG_HOST={config_host}",
        str(context),
    ]
    print(f"Building image {image}...")
    run_cmd(cmd, env=env)


def start_container(
    name: str,
    image: str,
    port: int,
    agent_id: int,
    gitea_token: str | None,
    leantime_token: str | None,
    gitea_username: str | None = None,
    gitea_password: str | None = None,
    gitea_repo: str | None = None,
) -> None:
    # Clean up any stale container with the same name.
    subprocess.run(
        ["docker", "rm", "-f", name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    cmd = [
        "docker",
        "run",
        "-d",
        "--rm",
        "--name",
        name,
        "-p",
        f"{port}:8000",
        "-e",
        f"AGENT_NAME={name}",
        "-e",
        f"AGENT_ID={agent_id}",
    ]

    if gitea_token:
        cmd.extend(["-e", f"GITEA_MCP_TOKEN={gitea_token}"])
    if leantime_token:
        cmd.extend(["-e", f"LEANTIME_MCP_TOKEN={leantime_token}"])
    if gitea_username:
        cmd.extend(["-e", f"GITEA_USERNAME={gitea_username}"])
    if gitea_password:
        cmd.extend(["-e", f"GITEA_PASSWORD={gitea_password}"])
    if gitea_repo:
        cmd.extend(["-e", f"GITEA_REPO={gitea_repo}"])

    cmd.append(image)
    run_cmd(cmd)


async def http_get(url: str) -> Tuple[int, str]:
    def _do_get() -> Tuple[int, str]:
        with urllib.request.urlopen(url, timeout=3) as resp:
            body = resp.read().decode(errors="replace")
            return resp.getcode(), body

    return await asyncio.to_thread(_do_get)


async def http_post(url: str, payload: Dict[str, object]) -> Tuple[int, str]:
    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}

    def _do_post() -> Tuple[int, str]:
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        # Allow long-running tasks; align with agent default timeout (300s) plus slack.
        with urllib.request.urlopen(req, timeout=360) as resp:
            body = resp.read().decode(errors="replace")
            return resp.getcode(), body

    return await asyncio.to_thread(_do_post)


async def wait_for_health(port: int, retries: int = 40, delay: float = 0.5) -> None:
    url = f"http://localhost:{port}/health"
    for _ in range(retries):
        try:
            status, _ = await http_get(url)
            if status == 200:
                return
        except Exception:
            await asyncio.sleep(delay)
            continue
    raise RuntimeError(f"Agent on port {port} did not become healthy")


async def send_instruction(port: int, prompt: str) -> Dict[str, object]:
    url = f"http://localhost:{port}/opencode_run"
    try:
        status, body = await http_post(url, {"prompt": prompt, "format": "json"})
        parsed = json.loads(body)
        return {"port": port, "status": status, "response": parsed}
    except Exception as exc:  # noqa: BLE001
        return {"port": port, "status": "error", "error": str(exc)}


async def orchestrate(args: argparse.Namespace) -> None:
    dockerfile = Path("docker/agent/Dockerfile").resolve()
    context = dockerfile.parent
    config_host = Path(args.config_host).expanduser().resolve()

    def create_gitea_token(username: str) -> str:
        if not args.gitea_admin_token:
            return ""
        token_name = f"agent-auto-{username}-{int(time.time())}"
        data = urllib.parse.urlencode({"name": token_name}).encode()
        req = urllib.request.Request(
            f"{args.gitea_url}/api/v1/users/{username}/tokens",
            data=data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"token {args.gitea_admin_token}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read().decode(errors="replace")
                parsed = json.loads(body)
                return parsed.get("sha1", "")
        except Exception:
            return ""

    def ensure_gitea_user(username: str, password: str, email: str) -> None:
        if not args.gitea_admin_token:
            return
        payload = {
            "username": username,
            "login_name": username,
            "email": email,
            "password": password,
            "must_change_password": False,
        }
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{args.gitea_url}/api/v1/admin/users",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"token {args.gitea_admin_token}",
            },
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=15).read()
        except urllib.error.HTTPError as exc:
            if exc.code == 422:
                # likely already exists
                return
        except Exception:
            return

    if not args.no_build:
        build_image(args.image, dockerfile, context, config_host)

    agents: List[Tuple[str, int, str, str, str, str]] = []

    for idx in range(args.count):
        port = args.start_port + idx
        name = f"agent-{port}"
        print(f"Starting {name} on port {port}...")
        username = (
            f"{args.gitea_user_prefix}{idx + 1}" if args.gitea_user_prefix else ""
        )
        password = random_password()
        email = f"{username or 'agent'}{int(time.time())}@example.com"
        ensure_gitea_user(username, password, email)
        token = create_gitea_token(username) if username else ""
        start_container(
            name,
            args.image,
            port,
            idx,
            token or args.gitea_token,
            args.leantime_token,
            gitea_username=username,
            gitea_password=password,
            gitea_repo=args.gitea_repo,
        )
        agents.append(
            (
                name,
                port,
                username,
                password,
                token or args.gitea_token,
                args.gitea_repo,
            )
        )

    print("Waiting for agents to become healthy...")
    await asyncio.gather(*(wait_for_health(port) for _, port, _, _, _, _ in agents))

    print("Sending instruction to agents...")
    results = []
    for _, port, username, password, token, repo in agents:
        cred_note = (
            f"\nCredentials for you: username={username}, password={password}, token={token}. "
            f"Repo={repo}."
        )
        payload_prompt = args.prompt + cred_note
        results.append(await send_instruction(port, payload_prompt))

    print("\nResponses:")
    for result in results:
        port = result["port"]
        status = result.get("status")
        if status == "error":
            print(f"- {port}: ERROR - {result.get('error')}")
            continue
        print(f"- {port}: status {status}")
        resp = result.get("response")
        if resp is not None:
            text = json.dumps(resp)[:800]
            print(f"    {text}")


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run multiple agent containers and broadcast a prompt"
    )
    parser.add_argument(
        "prompt", nargs="?", default="Say hello and include your agent name and id."
    )
    parser.add_argument(
        "-n", "--count", type=int, default=4, help="Number of agents to start"
    )
    parser.add_argument(
        "--start-port", type=int, default=28000, help="Host port to start from"
    )
    parser.add_argument(
        "--image", default="codex-agent:latest", help="Docker image tag to run"
    )
    parser.add_argument(
        "--config-host",
        default=str(Path.home() / ".config/opencode"),
        help="Host opencode config dir for build arg",
    )
    parser.add_argument(
        "--no-build", action="store_true", help="Skip building the image before running"
    )
    parser.add_argument(
        "--gitea-token",
        default=os.environ.get("GITEA_MCP_TOKEN", ""),
        help="Gitea MCP token to pass to agents",
    )
    parser.add_argument(
        "--leantime-token",
        default=os.environ.get("LEANTIME_MCP_TOKEN", ""),
        help="Leantime MCP token to pass to agents",
    )
    parser.add_argument(
        "--gitea-admin-token",
        default=os.environ.get("GITEA_ADMIN_TOKEN", ""),
        help="Admin token used to mint per-user PATs",
    )
    parser.add_argument(
        "--gitea-url",
        default=os.environ.get("GITEA_URL", "http://172.17.0.1:3000"),
        help="Base URL of Gitea",
    )
    parser.add_argument(
        "--gitea-user-prefix",
        default=os.environ.get("GITEA_USER_PREFIX", "agent-fun"),
        help="Prefix for agent usernames (suffix will be 1..N)",
    )
    parser.add_argument(
        "--gitea-password",
        default=os.environ.get("GITEA_PASSWORD", "Agent!12345"),
        help="Password for agent accounts",
    )
    parser.add_argument(
        "--gitea-repo",
        default=os.environ.get(
            "GITEA_REPO", "http://172.17.0.1:3000/wa/agentic_playground.git"
        ),
        help="Repo URL for agents",
    )
    return parser.parse_args(argv)


def main(argv: List[str]) -> None:
    args = parse_args(argv)
    try:
        asyncio.run(orchestrate(args))
    except KeyboardInterrupt:
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main(sys.argv[1:])
