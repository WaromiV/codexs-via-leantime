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
    alphabet = string.ascii_letters + string.digits
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
    gitea_repo_secret: str | None = None,
    gitea_repo: str | None = None,
    leantime_username: str | None = None,
    leantime_password: str | None = None,
    leantime_mcp_url: str | None = None,
    model: str | None = None,
    config_host: str | None = None,
    auth_host: str | None = None,
    openai_token: str | None = None,
    opencode_config_content: str | None = None,
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
    if gitea_repo_secret:
        cmd.extend(["-e", f"GITEA_REPO_SECRET={gitea_repo_secret}"])
    if gitea_repo:
        cmd.extend(["-e", f"GITEA_REPO={gitea_repo}"])
    if leantime_username:
        cmd.extend(["-e", f"LEANTIME_USERNAME={leantime_username}"])
    if leantime_password:
        cmd.extend(["-e", f"LEANTIME_PASSWORD={leantime_password}"])
    if leantime_mcp_url:
        cmd.extend(["-e", f"LEANTIME_MCP_URL={leantime_mcp_url}"])
    if model:
        cmd.extend(["-e", f"OPENCODE_MODEL={model}"])
    if config_host:
        cmd.extend(["-v", f"{config_host}:/root/.config/opencode"])
    if auth_host:
        cmd.extend(["-v", f"{auth_host}:/root/.local/share/opencode"])
    if openai_token:
        cmd.extend(["-e", f"OPENAI_API_KEY={openai_token}"])
    if opencode_config_content:
        cmd.extend(["-e", f"OPENCODE_CONFIG_CONTENT={opencode_config_content}"])

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


def format_repo_with_auth(repo_url: str, username: str, password: str) -> str:
    parsed = urllib.parse.urlparse(repo_url)
    netloc = parsed.netloc
    auth_netloc = f"{username}:{password}@{netloc}"
    return parsed._replace(netloc=auth_netloc).geturl()


def init_repo(container: str, repo_url: str, username: str, secret: str) -> None:
    auth_url = format_repo_with_auth(repo_url, username, secret)
    cmds = [
        f"rm -rf /workspace && git clone {auth_url} /workspace",
        f"git -C /workspace config user.name {username}",
        f"git -C /workspace config user.email {username}@example.com",
        f"git -C /workspace remote set-url origin {auth_url}",
        "git config --global credential.helper store",
        f"printf '%s\\n' '{auth_url}' > /root/.git-credentials",
    ]
    for cmd in cmds:
        run_cmd(["docker", "exec", container, "sh", "-c", cmd])


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
    auth_host = Path(args.auth_host).expanduser().resolve()

    openai_token: str | None = None
    user_passwords: Dict[str, str] = {}
    auth_file = auth_host / "auth.json"
    if auth_file.exists():
        try:
            data = json.loads(auth_file.read_text())
            openai_entry = data.get("openai") or {}
            openai_token = openai_entry.get("access")
        except Exception:
            openai_token = None

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

    def add_gitea_collaborator(username: str) -> None:
        if not args.gitea_admin_token:
            return
        parsed = urllib.parse.urlparse(args.gitea_repo)
        path = parsed.path.lstrip("/")
        if path.endswith(".git"):
            path = path[:-4]
        if "/" not in path:
            return
        owner, repo = path.split("/", 1)
        url = f"{args.gitea_url}/api/v1/repos/{owner}/{repo}/collaborators/{username}?permission=write"
        req = urllib.request.Request(
            url,
            data=b"{}",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"token {args.gitea_admin_token}",
            },
            method="PUT",
        )
        try:
            urllib.request.urlopen(req, timeout=15).read()
        except Exception:
            return

    def create_leantime_user(
        email: str, password: str, firstname: str, lastname: str
    ) -> None:
        if not args.leantime_mcp_url:
            return
        payload = {
            "name": "leantime_create_user",
            "args": {
                "email": email,
                "password": password,
                "firstname": firstname,
                "lastname": lastname,
                "role": "20",
            },
        }
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            args.leantime_mcp_url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=15).read()
        except Exception:
            return

    if not args.no_build:
        build_image(args.image, dockerfile, context, config_host)

    agents: List[Tuple[str, int, str, str, str, str, str, str]] = []

    for idx in range(args.count):
        port = args.start_port + idx
        name = f"agent-{port}"
        print(f"Starting {name} on port {port}...")
        ts_suffix = int(time.time())
        username = (
            f"{args.gitea_user_prefix}{idx + 1}-{ts_suffix}"
            if args.gitea_user_prefix
            else f"agent-{idx + 1}-{ts_suffix}"
        )
        password = random_password()
        email = f"{username or 'agent'}{int(time.time())}@example.com"
        ensure_gitea_user(username, password, email)
        user_passwords[username] = password
        token = ""
        lt_password = random_password()
        lt_email = f"{username or 'agent'}{int(time.time())}@lt.local"
        create_leantime_user(lt_email, lt_password, "Agent", username or "Agent")
        add_gitea_collaborator(username)

        gitea_headers = {}
        leantime_headers = (
            {"Authorization": f"Bearer {args.leantime_token}"}
            if args.leantime_token
            else {}
        )
        mcp_config = {
            "$schema": "https://opencode.ai/config.json",
            "mcp": {
                "gitea_mcp": {
                    "type": "remote",
                    "url": args.gitea_mcp_url,
                    "enabled": True,
                    "headers": gitea_headers,
                },
                "leantime_mcp": {
                    "type": "remote",
                    "url": args.leantime_mcp_url,
                    "enabled": True,
                    "headers": leantime_headers,
                },
            },
        }
        config_content = json.dumps(mcp_config)
        start_container(
            name,
            args.image,
            port,
            idx,
            args.gitea_token,
            args.leantime_token,
            gitea_username=username,
            gitea_password=password,
            gitea_repo_secret=password,
            gitea_repo=args.gitea_repo,
            leantime_username=lt_email,
            leantime_password=lt_password,
            leantime_mcp_url=args.leantime_mcp_url,
            model=args.model,
            config_host=str(config_host),
            auth_host=str(auth_host),
            openai_token=openai_token,
            opencode_config_content=config_content,
        )
        agents.append(
            (
                name,
                port,
                username,
                password,
                args.gitea_repo,
                lt_email,
                lt_password,
                password,
            )
        )

    print("Waiting for agents to become healthy...")
    await asyncio.gather(
        *(wait_for_health(port) for _, port, _, _, _, _, _, _ in agents)
    )

    print("Initializing repositories inside agents...")
    for name, _, username, _, repo, _, _, repo_secret in agents:
        if username and repo:
            init_repo(name, repo, username, repo_secret)

    print("Sending instruction to agents...")
    results = []
    total_agents = len(agents)
    rules_path = Path("AGENT_RULE.md")
    rules_text = ""
    if rules_path.exists():
        try:
            rules_text = rules_path.read_text().strip()
        except Exception:
            rules_text = ""

    for idx, (_, port, username, password, _, _, _, repo_secret) in enumerate(
        agents, start=1
    ):
        pr_note = (
            "\nAfter pushing, if gh is unavailable, create a PR via Gitea API: "
            "curl -u {user}:{pwd} -H 'Content-Type: application/json' "
            '-d \'{{"title":"<title>","head":"<branch>","base":"main","body":"<body>"}}\' '
            "{gitea}/api/v1/repos/wa/agentic_playground/pulls"
        ).format(user=username, pwd=repo_secret, gitea=args.gitea_url)
        rules_path = Path("AGENT_RULE.md")
        rules_text = ""
        if rules_path.exists():
            try:
                rules_text = rules_path.read_text().strip()
            except Exception:
                rules_text = ""
        guide_note = "\nRULES (one-time):\n" + rules_text if rules_text else ""
        ident_note = f"\nYou are agent {idx} of {total_agents}."
        payload_prompt = args.prompt + ident_note + guide_note + pr_note
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
        "--leantime-mcp-url",
        default=os.environ.get("LEANTIME_MCP_URL", "http://172.17.0.1:3101/mcp/call"),
        help="Leantime MCP server endpoint for provisioning",
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
    parser.add_argument(
        "--gitea-mcp-url",
        default=os.environ.get("GITEA_MCP_URL", "http://172.17.0.1:8082"),
        help="Gitea MCP URL",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("OPENCODE_MODEL", "openai/gpt-5.1-codex-mini"),
        help="Model to pass to agents (provider/model)",
    )
    parser.add_argument(
        "--auth-host",
        default=str(Path.home() / ".local/share/opencode"),
        help="Host opencode auth dir to mount into agents",
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
