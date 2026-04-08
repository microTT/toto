# Local Memory System

This directory contains a self-contained local memory system for Codex-style workflows.

By default, the system stores control state in `~/.codex/memories/<workspace_instance_id>`. If that location is not writable, it falls back to `<workspace>/.memory-system`. Installed global hooks and the `memory-local` MCP should resolve this path per workspace; do not hardcode a single `CODEX_MEMORY_HOME`, `--cwd`, or `--memory-home` in global Codex config, or multiple repositories will share one store.

Within `memory_home`, the layout is:

- `control/` for SQLite state and job queue
- `global/` for durable memory files
- `workspace/recent`, `workspace/archive`, `workspace/runtime`, and `workspace/audit`

## Components

- `memory/bin/memory-hook`
  Hook entrypoint for `session-start`, `user-prompt-submit`, and `stop`.
- `memory/bin/memoryd`
  Worker entrypoint for one-shot processing, daemon polling, retry/backoff, and stale-recent archiving.
- `memory/bin/memory-admin`
  Manual operations for bootstrap, context, upsert, delete, pin, archive, search, and index rebuild.
- `memory/bin/memory-mcp`
  Minimal stdio MCP server. Installed usage should enable `--allow-writes` so `memory.upsert`, `memory.delete`, and `memory.rebuild_index` are exposed alongside read tools.
- `memory/memory_system/`
  Python implementation.
- `memory/schemas/memory_patch.schema.json`
  Patch-plan schema for the summarizer worker.

## Quick Start

1. Bootstrap the filesystem layout:

```bash
memory/bin/memory-admin --cwd /path/to/workspace bootstrap
```

2. Add a global preference:

```bash
memory/bin/memory-admin --cwd /path/to/workspace upsert \
  --scope global \
  --type preference \
  --subject "package manager" \
  --summary "Prefer pnpm unless repo requires npm" \
  --tags "javascript,tooling" \
  --scope-reason "cross-workspace and durable"
```

3. Add a local task context:

```bash
memory/bin/memory-admin --cwd /path/to/workspace upsert \
  --scope local \
  --type task_context \
  --subject "auth flaky tests" \
  --summary "Snapshots fail on CI" \
  --next-use "re-check snapshots before auth middleware" \
  --scope-reason "repo-specific and near-term"
```

4. Inspect the current auto-loaded snapshot:

```bash
memory/bin/memory-admin --cwd /path/to/workspace context
```

5. Print the recommended `hooks.json` payload:

```bash
memory/bin/memory-admin --cwd /path/to/workspace print-hooks-config
```

The generated hooks are workspace-dynamic. They intentionally do not export `CODEX_MEMORY_HOME`; `memory-hook` resolves the correct `memory_home` from the current hook payload `cwd`.

Before using hooks, ensure the feature is enabled:

```bash
codex features list
codex features enable codex_hooks
```

6. Rebuild the archive search index:

```bash
memory/bin/memory-admin --cwd /path/to/workspace rebuild-index --json
```

7. Run one worker pass:

```bash
memory/bin/memoryd run-once --cwd /path/to/workspace --backend heuristic
```

8. Run the worker as a daemon:

```bash
memory/bin/memoryd daemon --cwd /path/to/workspace --backend qwen --poll-interval 5
```

9. Optional Qwen summarizer + embedding configuration in `memory/.env`:

```bash
cp -n memory/.env.example memory/.env
```

Then edit `memory/.env`:

```dotenv
CODEX_MEMORY_SUMMARIZER_PROVIDER=auto
CODEX_MEMORY_SUMMARIZER_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
CODEX_MEMORY_SUMMARIZER_API_KEY=
CODEX_MEMORY_SUMMARIZER_MODEL=qwen3-max
CODEX_MEMORY_SUMMARIZER_ENDPOINT_MODE=openai
CODEX_MEMORY_SUMMARIZER_TIMEOUT_SECONDS=120
CODEX_MEMORY_SUMMARIZER_TEMPERATURE=0
CODEX_MEMORY_SUMMARIZER_MAX_OUTPUT_TOKENS=4096

CODEX_MEMORY_EMBEDDING_PROVIDER=auto
CODEX_MEMORY_EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
CODEX_MEMORY_EMBEDDING_API_KEY=
CODEX_MEMORY_EMBEDDING_MODEL=text-embedding-v4
CODEX_MEMORY_EMBEDDING_ENDPOINT_MODE=openai
CODEX_MEMORY_EMBEDDING_DIMENSIONS=1024
```

`CODEX_MEMORY_SUMMARIZER_*` and `CODEX_MEMORY_EMBEDDING_*` are intentionally separate, so you can switch the summarizer model and the embedding model independently. The defaults point to the Beijing-region Alibaba Cloud Model Studio OpenAI-compatible endpoint. In the common case, you only need to fill:

- `CODEX_MEMORY_SUMMARIZER_API_KEY`
- `CODEX_MEMORY_EMBEDDING_API_KEY`

By default, the worker uses `qwen3-max` for summarization and `text-embedding-v4` for archive retrieval. If `CODEX_MEMORY_SUMMARIZER_API_KEY` or `CODEX_MEMORY_SUMMARIZER_BASE_URL` are left empty, the summarizer will reuse the embedding-side API key / base URL as a migration-friendly fallback; setting the summarizer variables explicitly always takes precedence. If your key belongs to the international region instead, replace `CODEX_MEMORY_SUMMARIZER_BASE_URL` and `CODEX_MEMORY_EMBEDDING_BASE_URL` with `https://dashscope-intl.aliyuncs.com/compatible-mode/v1`. Process environment variables still override `.env`, so CI or local shells can pin different values without editing the file.

Embedding-specific behavior:

- With `CODEX_MEMORY_EMBEDDING_PROVIDER=auto`, the system only uses the remote endpoint after `CODEX_MEMORY_EMBEDDING_API_KEY` is present; otherwise it falls back to lexical retrieval so the local stack keeps working.
- `qwen_hf` is also supported when local `torch` + `transformers` are available.
- If neither a remote endpoint nor a local model is available, the code falls back to a lexical hash embedding so retrieval remains functional.

10. Validate the installed stack end-to-end:

```bash
memory/scripts/validate_installed_stack.py \
  --workspace /path/to/workspace \
  --memory-home ~/.codex/memories/<workspace_instance_id>
```

If you do not already know the workspace-specific home, derive it first:

```bash
memory/bin/memory-admin --cwd /path/to/workspace bootstrap
```

## Tests

```bash
python3 -m unittest discover -s memory/tests -t .
```

One-command smoke E2E (includes compileall, unit tests, schema smoke, isolated command E2E, and installed live validation):

```bash
memory/scripts/smoke_e2e.sh \
  --workspace /path/to/workspace \
  --memory-home ~/.codex/memories/<workspace_instance_id>
```

If you only want repository-local checks (skip installed live validation):

```bash
memory/scripts/smoke_e2e.sh --skip-live
```

## LaunchAgent

Install (auto-start on login/reboot):

```bash
memory/scripts/install_launchd.sh \
  --workspace /path/to/workspace \
  --memory-home ~/.codex/memories/<workspace_instance_id>
```

Uninstall:

```bash
memory/scripts/uninstall_launchd.sh
```
