# codexs-via-leantime
Status: **unfinished**. a bunch of codex agents jump into docker containers with their own git credentials then do tasks from leantime

## Quick start: spin up local agents

Use the root-level CLI to build the agent image, run multiple containers on consecutive ports, and broadcast a prompt to each one:

```bash
python run_agents.py "Say hello" -n 4 --start-port 28000 --image codex-agent:latest
```

- Builds `docker/agent` with your opencode config (skip with `--no-build`).
- Launches agents on ports 28000, 28001, ... and waits for health.
- Sends the prompt to every agent concurrently and prints their responses.
