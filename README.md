# THIS README IS HUMAN-MADE
## First docker compose up
```bash
docker compose up --build -d
```
## setup gitea admin account, issue a token, use token in command below
```bash
DOCKER_BUILDKIT=0 \
python run_agents.py \
  "YOU JUST MADE SOME TASKS. Those tasks are your responsibility. Current step is a brainstorm process. Check all tasks that are under your agent id. Look at your agent id if its one then you go and take any tasks with username agent-fun1-* YOU MUST JUST MAKE MARKDOWN DESCRIBING HOW, EXACTLY IN CURRENT GAME CONCEPT THE ISSUE MUST BE REALISED. check issues in gitea and make prs describing what will you do under the whole concept of the game. check other issues to understand exact game plan." \
  -n 10 \
  --start-port 8000 \
  --image codex-agent:latest \
  --no-build \
  --gitea-admin-token a65f68110b82f38cdff1b5106f9370e71be3db31 \
  --gitea-url http://172.17.0.1:3000 \
  --gitea-user-prefix agent-fun \
  --gitea-repo http://172.17.0.1:3000/wa/agentic_playground.git \
  --gitea-mcp-url http://172.17.0.1:8082 \
  --leantime-mcp-url http://172.17.0.1:3101/mcp \
  --model openai/gpt-5.1-codex-mini
```
# SETTINGS
- n is the agent number
- start port is the port from which counting per each agent starts
- model flag sets model(opencode style)
- gitea url is set to docker bound linux url
- leantime is not needed at the time
