import asyncio
import os
import subprocess
import textwrap
import time
from pathlib import Path
from typing import Dict, List, Optional

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

AGENT_NAME = os.getenv("AGENT_NAME", "agent")
AGENT_ID = os.getenv("AGENT_ID", "0")
AGENT_PORT = int(os.getenv("AGENT_PORT", "8000"))
WORKSPACE = Path(os.getenv("WORKSPACE_DIR", "/workspace")).resolve()
WORKSPACE.mkdir(parents=True, exist_ok=True)
DEFAULT_MODEL = os.getenv("OPENCODE_MODEL", "openai/gpt-5.1-codex-max")


class WebRequest(BaseModel):
    method: str = "GET"
    url: str
    headers: Optional[Dict[str, str]] = None
    body: Optional[str] = None
    timeout: float = 20.0


class RunCode(BaseModel):
    language: str = "python"
    code: str
    argv: List[str] = []
    timeout: float = 20.0


class WriteFile(BaseModel):
    path: str
    content: str
    append: bool = False


class ApplyPatch(BaseModel):
    patch: str


class OpencodeRun(BaseModel):
    prompt: str
    agent: Optional[str] = None
    model: Optional[str] = None
    format: str = "json"
    attach: Optional[str] = None
    timeout: float = 300.0
    session: Optional[str] = None


app = FastAPI(title="Code Agent", version="1.0.0")


def ensure_workspace_path(path: str) -> Path:
    target = (WORKSPACE / path).resolve()
    if WORKSPACE not in target.parents and target != WORKSPACE:
        raise HTTPException(status_code=400, detail="Path escapes workspace")
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


@app.get("/health")
async def health() -> Dict[str, str]:
    return {"status": "ok", "agent": AGENT_NAME, "id": AGENT_ID}


@app.post("/web_request")
async def web_request(payload: WebRequest) -> Dict[str, object]:
    try:
        response = requests.request(
            payload.method,
            payload.url,
            headers=payload.headers,
            data=payload.body,
            timeout=payload.timeout,
        )
        truncated_body = response.text[:8192]
        return {
            "status_code": response.status_code,
            "headers": dict(response.headers),
            "body": truncated_body,
            "truncated": len(response.text) > len(truncated_body),
        }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def run_subprocess(
    cmd: List[str], input_text: Optional[str], timeout: float
) -> Dict[str, object]:
    """Run a subprocess with line streaming to logs and timeout."""

    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE if input_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=WORKSPACE,
        )

        if input_text is not None and proc.stdin:
            proc.stdin.write(input_text)
            proc.stdin.close()

        stdout_lines: List[str] = []

        # Stream stdout lines to container logs for visibility during long runs.
        while True:
            line = proc.stdout.readline() if proc.stdout else ""
            if line:
                stdout_lines.append(line)
                print(line.rstrip())  # flush to container logs
            elif proc.poll() is not None:
                break

            if time.monotonic() - start > timeout:
                proc.kill()
                raise HTTPException(status_code=408, detail=f"Command timed out: {cmd}")

        # Collect any remaining output after process exit
        if proc.stdout:
            remainder = proc.stdout.read()
            if remainder:
                stdout_lines.append(remainder)
                print(remainder.rstrip())

        return {
            "return_code": proc.returncode,
            "stdout": "".join(stdout_lines),
            "stderr": "",
        }
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/run_code")
async def run_code(payload: RunCode) -> Dict[str, object]:
    language = payload.language.lower()
    if language in {"python", "py"}:
        cmd = ["python", "-u", "-c", payload.code, *payload.argv]
    elif language in {"bash", "sh", "shell"}:
        script = textwrap.dedent(payload.code)
        cmd = ["bash", "-lc", script]
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported language: {language}")

    return run_subprocess(cmd, None, payload.timeout)


@app.post("/write_file")
async def write_file(payload: WriteFile) -> Dict[str, object]:
    target = ensure_workspace_path(payload.path)
    mode = "a" if payload.append else "w"
    try:
        with target.open(mode, encoding="utf-8") as handle:
            handle.write(payload.content)
        return {"path": str(target), "bytes_written": len(payload.content)}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/apply_patch")
async def apply_patch(payload: ApplyPatch) -> Dict[str, object]:
    # patch expects paths relative to current working directory (workspace)
    result = run_subprocess(["patch", "-p0", "-N"], payload.patch, timeout=20)
    if result["return_code"] != 0:
        raise HTTPException(status_code=422, detail=result)
    return result


@app.post("/opencode_run")
async def opencode_run(payload: OpencodeRun) -> Dict[str, object]:
    cmd = ["opencode", "run", payload.prompt]
    if payload.agent:
        cmd.extend(["--agent", payload.agent])
    model = payload.model or DEFAULT_MODEL
    if model:
        cmd.extend(["--model", model])
    if payload.attach:
        cmd.extend(["--attach", payload.attach])

    # If a session is provided, pass it (ensuring the required "ses" prefix). Otherwise
    # let opencode generate a fresh session per call to avoid ENOENT lookups.
    if payload.session:
        session = (
            payload.session
            if payload.session.startswith("ses")
            else f"ses_{payload.session}"
        )
        cmd.extend(["--session", session])

    cmd.extend(["--format", payload.format or "json"])

    # Do not force --continue; rely on new sessions by default.
    return run_subprocess(cmd, None, payload.timeout)


async def main() -> None:
    config = {
        "host": "0.0.0.0",
        "port": AGENT_PORT,
    }
    import uvicorn

    await uvicorn.Server(
        uvicorn.Config(
            app,
            host=config["host"],
            port=config["port"],
            log_level="info",
        )
    ).serve()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
