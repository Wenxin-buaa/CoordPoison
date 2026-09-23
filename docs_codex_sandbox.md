# CoordPoison Codex Sandbox

Build the Codex runtime image:

```bash
docker build -f CoordPoison/Dockerfile.codex \
  -t coordpoison-codex-runtime:latest CoordPoison
```

Install the OpenSandbox Python dependencies:

```bash
python3.11 -m pip install -r CoordPoison/requirements-sandbox.txt
```

Start OpenSandbox:

```bash
OPENSANDBOX_INSECURE_SERVER=YES nohup opensandbox-server \
  --config CoordPoison/.opensandbox.config.toml \
  > ./coordpoison-opensandbox.log 2>&1 &

curl http://localhost:8080/health
```

Run a parallel experiment after initializing the benchmark pack and experiment:

```bash
PVF_PAYLOAD_POOL=skillject \
PVF_LLM_API_KEY="$OPENAI_API_KEY" \
PVF_LLM_BASE_URL="$OPENAI_BASE_URL" \
PVF_ATTACK_LLM_API_KEY="$OPENAI_API_KEY" \
PVF_ATTACK_LLM_BASE_URL="$OPENAI_BASE_URL" \
PVF_SANDBOX_DOMAIN=localhost:8080 \
PVF_SANDBOX_IMAGE=coordpoison-codex-runtime:latest \
python3.11 CoordPoison/scripts/run_experiment_parallel.py \
  --pack <pack_id> \
  --experiment-id <experiment_id> \
  --workers 3 \
  --d2-provider codex-sandbox \
  --d5-provider codex-sandbox \
  --local-provider openai-compatible
```

Use `claude-code-sandbox` for D2/D5 when `PVF_SANDBOX_IMAGE` points at a
Claude Code runtime and `PVF_CLAUDE_*` environment variables are configured.
