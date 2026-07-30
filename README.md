# LocalCode

LocalCode is a local-first LLM coding workspace for Ubuntu and GNOME. It talks to local
Ollama models and any OpenAI-compatible API endpoint, manages projects and project-scoped
chats, lets models work through approval-gated file and shell tools, and makes context
pressure visible before a model can silently truncate a prompt.

The application is native GTK 4/Libadwaita. It has no web runtime, account, telemetry, or cloud
fallback.

## Current Features

- Add and switch local project folders.
- Create, revisit, and delete project chats.
- Discover local Ollama completion models and stream their responses.
- Add any OpenAI-compatible API provider (LM Studio, vLLM, llama.cpp server, cloud APIs, LAN
  machines) and browse their models in the same dropdown.
- Let tool-capable models list, search, read, write, replace, and delete project files or run tests.
- Keep every file tool confined to the selected project root, including symlink resolution.
- Require per-command confirmation for shell execution even when file changes are otherwise allowed;
  shell commands are powerful and are not an operating-system sandbox.
- Choose `Ask before changes`, `Allow changes`, or `Read only` per project.
- Show estimated preflight context and exact token counts after every agent step.
- Detect generation-time context exhaustion from the model's effective loaded context and counters.
- Stop tool loops before their accumulated output can trigger silent input truncation.
- Compact older turns automatically while retaining the complete SQLite and JSONL transcript.
- Create canonical `AGENTS.md` guidance for every added project and refresh its managed section
  after file-changing model turns without overwriting human notes.
- Retrieve and archive project history through an isolated, bundled MemPalace checkout.

## Requirements

- Ubuntu with GTK 4.10+, Libadwaita 1.5+, and PyGObject
- Python 3.11 or newer
- `python3-venv` or `python3-pip` when installing the optional MemPalace companion
- Ollama running at `http://127.0.0.1:11434` (or an OpenAI-compatible API endpoint)
- At least one model with the `completion` capability; tool support is strongly recommended

## Run From Source

```bash
cd ~/attic/SharedArchive/Projects/LocalCode
python3 localcode.py
```

## Install For GNOME

```bash
./scripts/install.sh
```

The installer places the application under `~/.local/lib/localcode`, adds a launcher at
`~/.local/bin/localcode`, and installs the desktop entry and icon for the current user. It does
not require `sudo`.

## API Providers

In addition to local Ollama models, LocalCode supports any OpenAI-compatible API endpoint.
Open **Preferences** (Ctrl+,) and use the **API Providers** section to add endpoints.

Each provider needs:

- A display name
- The base URL (e.g. `http://192.168.1.50:1234/v1` for a LAN machine, or
  `https://api.openai.com/v1` for a cloud service)
- An API key
- A context window size

Added providers are scanned for models on the next refresh, and their models appear in the
header dropdown as `model-name (Provider Name)`. Agent turns, tool calls, and streaming work
identically regardless of whether the model is local or remote.

## Context Accounting

Ollama does not expose a public tokenizer endpoint and does not report when it truncates an input
prompt. API providers report `prompt_tokens` and `completion_tokens` directly. LocalCode uses
two complementary measurements:

1. Before a request, it conservatively estimates messages and the complete tool schema from UTF-8
   byte length, then reserves space for the model response. The default threshold is 78 percent.
2. After every model or tool step, it reads exact token counts from the response, plus the
   effective loaded context from `/api/ps` (Ollama) or the configured context window (API providers).

The header meter always states whether a count is estimated (`~`) or exact. Yellow
means compaction is approaching. Red means the context is critical or was exhausted. If a model
fills its context, LocalCode explicitly reports that event, preserves the full transcript, and
compacts before the next turn.

Compaction creates a code-focused handoff containing user intent, durable decisions, changed file
paths, test outcomes, and open work. It intentionally relies on current source files and
`AGENTS.md` for implementation detail instead of treating a lossy chat summary as source code.

## MemPalace

MemPalace is vendored at `vendor/mempalace` and installed under
`~/.local/share/localcode/mempalace`, isolated from the GTK process. LocalCode prefers a virtual
environment and automatically falls back to an isolated `pip --target` package directory on
Ubuntu installations that do not provide `python3-venv`. Install it either from the Memory
preferences page or with:

```bash
./scripts/bootstrap-mempalace.sh
```

When memory is enabled for a project, LocalCode:

- searches the project wing before a model turn and labels retrieval as potentially stale;
- treats retrieved text as untrusted reference data rather than model instructions;
- exports the complete LocalCode chat as compatible JSONL records;
- mines project files and transcripts after completed turns;
- scopes each project to a stable ID-suffixed wing and prunes deleted or newly ignored sources;
- performs a synchronous transcript export before context compaction.

LocalCode invokes `mine` directly, so it does not create or overwrite `mempalace.yaml`,
`entities.json`, or `.gitignore` in an added project. MemPalace may download its local embedding
model on first initialization. No API key is required.

## Local Data

- SQLite database: `~/.local/share/localcode/localcode.db`
- Verbatim transcripts: `~/.local/share/localcode/transcripts/`
- MemPalace environment and palace: `~/.local/share/localcode/mempalace/`
- Project instructions: `<project>/AGENTS.md`

For a custom data root, set `LOCALCODE_DATA_HOME` before launching.
LocalCode enforces owner-only permissions on its data directories and persisted chat files. Data is
local but is not encrypted at rest.

## Verification

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q localcode tests
desktop-file-validate data/io.localcode.LocalCode.desktop
```

The tests cover local persistence, compaction boundaries, context states, project-root security,
file tools, approval modes, `AGENTS.md` preservation, Ollama streaming/counters, a complete
mocked coding turn, and API provider integration.
