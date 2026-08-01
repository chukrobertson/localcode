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
- Choose code style from Ponytail (YAGNI minimal), Balanced, or Verbose, controlling how
  much code the model produces per turn.
- Maintain canonical `AGENTS.md` guidance for every added project, auto-refreshed after
  file-changing turns without overwriting human notes.
- Optional MemPalace integration for verbatim local memory and retrieval, with automatic
  CUDA GPU acceleration when an NVIDIA GPU is detected.
- Per-project approval controls: `Ask before changes`, `Allow changes`, or `Read only`.

## Model Tools

The model has access to these tools for reading, editing, and inspecting the project:

| Tool | Description |
|---|---|
| `read_file` | Read any text file with line numbers |
| `read_files` | Batch-read up to 8 files in a single call |
| `write_file` | Create or replace a file atomically |
| `edit_file` | Apply multiple find-and-replace edits to a file in one step |
| `replace_in_file` | Single find-and-replace in a file |
| `delete_file` | Delete a project file |
| `create_directory` | Create a directory and its parents |
| `rename_file` | Rename or move a file within the project |
| `list_files` | List project files, optionally filtered by glob |
| `search_files` | Search project files for text or regex patterns |
| `run_command` | Run any shell command inside the project (always requires approval) |
| `run_lint` | Auto-detect and run a project's lint, typecheck, or test command |
| `git_diff` | Show staged and unstaged changes |
| `git_log` | Show recent commit history |
| `web_fetch` | Read a URL (loopback blocked, always requires approval) |
| `ask_user` | Prompt the user with a question when ambiguous |

**Security:** every file tool is confined to the project root and rejects symlinks that escape it.
Shell commands run in an interactive login shell to match your full environment.

## Requirements

- Ubuntu with GTK 4.10+, Libadwaita 1.5+, and PyGObject
- Python 3.11 or newer
- `python3-venv` or `python3-pip` when installing the optional MemPalace companion
- Ollama running at `http://127.0.0.1:11434` (or an OpenAI-compatible API endpoint)
- At least one model with the `completion` capability; tool support is strongly recommended

## Choosing a Model

LocalCode needs models that support both chat and tool calling. Use `ollama list`
to see what you have, and `ollama pull <model>` to add a new one.

| Model | Best for | Notes |
|---|---|---|
| **qwen3:14b** | Multi-step refactors, test suites, code reviews | Strongest tool calling and reasoning on consumer hardware. Use for complex work where you want it to get things right on the first pass. |
| **mistral-nemo:12b** | Single-file edits, quick fixes, fast turns | Smaller footprint, lower latency. Good when you know exactly what you want and just need the change. |
| **codellama:13b / :34b** | Pure code generation | Purpose-built for coding, no tool-calling overhead. Pair with a tool-calling model if you need the agent loop. |
| **gemma4:e2b** (~5B) | Lightweight, quick feedback | Runs on modest hardware. Tool-capable but less precise than the 12-14B tier for complex tasks. |
| **deepseek-coder-v2:16b** | Complex algorithm work, large codebases | Strong at understanding existing code. Good second choice if qwen3 isn't available. |

**For most users:** start with `qwen3:14b` as your default. It handles the full
range — reading, editing, testing, and reasoning about your code. Switch to a
lighter model for simple one-line changes if you want faster response times.

| Task | Recommended model | Code style |
|---|---|---|
| New feature from scratch | qwen3:14b | Balanced |
| Complex bug fix | qwen3:14b | Balanced |
| Simple refactor or rename | mistral-nemo:12b | Ponytail |
| One-line edit | mistral-nemo:12b | Ponytail |
| Code review request | qwen3:14b | Verbose |
| Writing tests | qwen3:14b | Balanced |

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

## Code Styles

LocalCode lets you set a code style in Preferences (Ctrl+,) that changes how the model
approaches implementation. It's injected as a section of the system prompt every turn:

- **Ponytail (YAGNI)** — uses a decision ladder to question whether each piece of code
  needs to exist at all. Prioritises standard library and existing project utilities,
  native platform features, and installed dependencies before writing anything new. The
  rule is "minimum that works" with zero compromise on validation, error handling,
  security, or accessibility. Ideal for API providers where you pay per token.
- **Balanced (default)** — no additional guidance. The model behaves as it normally would.
- **Verbose** — thorough with comments, docstrings, error messages, and usage examples.
  Favours readability and maintainability over brevity.

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
- mines project files and transcripts after completed turns, throttled to once per minute;
- scopes each project to a stable ID-suffixed wing and prunes deleted or newly ignored sources;
- performs a synchronous transcript export before context compaction.

On first installation, LocalCode detects NVIDIA GPUs via `nvidia-smi` and automatically
installs `onnxruntime-gpu` with `MEMPALACE_EMBEDDING_DEVICE=cuda`, so the embedding model
loads and runs on the GPU instead of CPU.

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
