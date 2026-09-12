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
- Choose a **Focused** or **Standard** change scope, controlling how aggressively the
  model expands an implementation beyond the immediate request.
- Reuse bounded, SHA-256-verified file ranges across turns and fresh continuation segments.
- Checkpoint unfinished tasks, retrieve compact related records from other project chats,
  and navigate large projects with a dependency-free source-symbol index.
- Create canonical `AGENTS.md` guidance for writable projects and preserve it as stable,
  explicitly maintained project instructions rather than an automatic turn log.
- Per-project approval controls: `Ask before changes`, `Allow changes`, or `Read only`.

## Model Tools

The model has access to these tools for reading, editing, and inspecting the project:

| Tool | Description |
|---|---|
| `read_file` | Read any text file with line numbers |
| `read_files` | Batch-read up to 8 files in a single call |
| `replace_lines` | Replace a verified, current line range without reproducing its exact text |
| `write_file` | Create or replace a file atomically |
| `edit_file` | Apply multiple find-and-replace edits, including replace-all edits, in one step |
| `replace_in_file` | Single find-and-replace in a file |
| `delete_file` | Delete a project file |
| `create_directory` | Create a directory and its parents |
| `rename_file` | Rename or move a file within the project |
| `copy_file` | Atomically copy a text or binary file within the project |
| `list_files` | List project files, optionally filtered by glob |
| `search_files` | Search project files for text or regex patterns |
| `run_command` | Run an approved shell command with the project as its working directory |
| `run_lint` | Auto-detect and run a project's lint, typecheck, or test command |
| `project_commands` | Discover standard project commands without running them |
| `git_status` | Show the current branch and concise working-tree state |
| `git_diff` | Show staged and unstaged changes |
| `git_log` | Show recent commit history |
| `web_fetch` | Read a URL (private/loopback targets blocked, always requires approval) |
| `ask_user` | Prompt the user with a question when ambiguous |

Tool names stay stable across Ollama and OpenAI-compatible providers, but LocalCode serializes
each provider's tool messages in its native shape. A central registry supplies every definition,
input bound, approval rule, and mutation policy. Results distinguish completed work, no-ops,
benign skips, blocked actions, and errors; changed paths and commands are recorded independently
of the model's prose. File mutations include a bounded SHA-256-backed post-edit excerpt, so the
model normally does not need an immediate verification reread.

LocalCode sends a phase-sized subset of that registry on each model step. Inspection requests get
read, search, history, status, and web tools; implementation requests get project inspection plus
editing tools; after a recorded mutation the palette switches to verification and repair tools. A
failed verification reopens the editing palette. Structural file operations remain available
during verification so multi-part changes can finish after their first mutation. This keeps
irrelevant schemas out of small-model context without weakening execution-time permissions. With
the current definitions, the inspect, edit, and verify palettes are 10, 15, and 17 tools instead
of sending all 20 every time.

**Security:** every file tool is confined to the project root and rejects symlinks that escape it.
Shell commands run in an interactive login shell to match your full environment.

**Approval modes** (per project, chosen in the composer):

- **Ask before changes** — every file change and every shell command asks for approval.
- **Allow changes** — file edits proceed without prompts; `run_command`, `run_lint`, and
  `web_fetch` always require approval.
- **Read only** — the model may read, search, inspect Git state, and converse, but cannot
  change project files, run commands, or create `AGENTS.md`. A read-only session leaves the
  project folder byte-for-byte untouched.

**Network access:** `web_fetch` only follows `http`/`https` URLs. Before every request and on
every redirect, LocalCode resolves the host and refuses loopback, private (RFC1918,
carrier-grade NAT, `fc00::/7`), link-local, unspecified, multicast, and reserved addresses —
IPv4 and IPv6 alike. This blocks hostname tricks such as DNS rebinding and redirects to
`127.0.0.1`. Each fetch still requires explicit user approval.

**Shell execution:** commands run through `bash -ilc` — an *interactive login* shell — so they
inherit the same environment as the user's terminal. This is deliberate: on Ubuntu, tools
installed through nvm, pyenv, or rvm are wired into `.bashrc`, which is only loaded for
interactive shells. The tradeoff is that profile noise can appear in command output.

## Requirements

- Ubuntu with GTK 4.10+, Libadwaita 1.5+, and PyGObject
- Python 3.11 or newer
- Ollama running at `http://127.0.0.1:11434` (or an OpenAI-compatible API endpoint)
- At least one model with the `completion` capability; tool support is strongly recommended

## Choosing a Model

LocalCode needs models that support both chat and tool calling. Use `ollama list`
to see what you have, and `ollama pull <model>` to add a new one.

| Model | Best for | Notes |
|---|---|---|
| **qwen3:8b** | Everyday local agent work | Produces structured tool calls and has enough capacity for focused repositories. A strong default for 12 GB GPUs. |
| **qwen3:14b** | Multi-step refactors, tests, reviews | More capable, but likely to offload at larger contexts on a 12 GB GPU. Reserve it for difficult work. |
| **gemma4:e2b** | Fast inspection and simple changes | Lightweight and structured-tool compatible; useful when responsiveness matters most. |
| **ornith:9b** | Candidate agentic coding model | Coding-agent tuned and compact enough for 12 GB GPUs. Verify its tool protocol before relying on it. |
| **granite4.2:8b** | Candidate general coding/tool model | Recent 5.3 GB model with coding, tool use, thinking, and structured-output support. Verify locally first. |

Ollama capability metadata is not sufficient by itself: some models advertise tool support but
emit a tool request as ordinary response text. Confirm that a candidate produces structured
`tool_calls` before using it for file-changing work.

For a single 12 GB NVIDIA GPU, `docs/ollama-performance.conf` enables Flash Attention, uses a
Q8 KV cache, prevents parallel contexts and multiple resident models from multiplying memory,
and keeps the active model warm for 30 minutes. Install it with:

```bash
sudo install -m 0644 docs/ollama-performance.conf \
  /etc/systemd/system/ollama.service.d/localcode-performance.conf
sudo systemctl daemon-reload
sudo systemctl restart ollama
```

| Task | Recommended model | Change scope |
|---|---|---|
| New feature from scratch | qwen3:8b | Standard |
| Complex bug fix | qwen3:14b | Standard |
| Simple refactor or rename | qwen3:8b | Focused |
| One-line edit | gemma4:e2b | Focused |
| Code review request | qwen3:14b | Standard |
| Writing tests | qwen3:8b | Standard |

## Agent Evaluations

LocalCode includes a dependency-free headless evaluation runner for checking real agent behavior
against installed Ollama models. Every scenario uses a disposable project and isolated database,
config, cache, and transcript directories. Shell tools are denied unless the command exactly
matches that scenario's built-in allowlist.

Run all smoke scenarios for a model:

```bash
python3 -m localcode.evals --model granite4.2:8b
```

Run one scenario, retain its disposable workspace for inspection, or compare another model:

```bash
python3 -m localcode.evals --model qwen3:8b --scenario verified_edit
python3 -m localcode.evals --model gemma4:12b --scenario inspect_only --keep-workdirs
python3 -m localcode.evals --list
```

The runner scores runtime completion, checkpoint state, required and unexpected file changes,
tool and command evidence, independent verification, `AGENTS.md` permission behavior, and final
response presence. It prints a compact terminal table and writes a timestamped JSON report under
`eval-results/` by default. Use `--output` to select a different report path. Reports contain the
structured checks, token counts, continuation count, tool activities, notices, and retained
workspace path when requested; they never include provider API keys.

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
Open **Preferences** (Ctrl+,), open the **API Providers** section, and choose **Add**. Fill
in a display name, the endpoint, an optional API key, and the context window, then press
**Add Provider**. Added providers are scanned for models immediately, and their models
appear in the header dropdown as `model-name (Provider Name)`. Agent turns, tool calls,
and streaming work identically regardless of whether the model is local or remote.

Each provider needs:

- A display name (no `/` characters)
- The base URL (e.g. `http://192.168.1.50:1234/v1` for a LAN machine, or
  `https://api.openai.com/v1` for a cloud service)
- An API key (stored only in the local database)
- A context window size

Provider API keys are stored in the local `0600` SQLite database and are never written to
transcripts, command output, logs, or exception messages.

## Context Accounting

Ollama does not expose a public tokenizer endpoint and does not report when it truncates an input
prompt. LocalCode uses two complementary measurements:

1. Before a request, it conservatively estimates messages and the complete tool schema from UTF-8
   byte length, then reserves space for the model response. The default threshold is 78 percent.
2. After every model or tool step, it reads exact token counts where the provider reports them —
   Ollama's stream counters, or API-provider `usage` (LocalCode requests
   `stream_options: {"include_usage": true}` and retries without it if an endpoint rejects the
   option).

Every displayed count is one of three things:

- **Exact** — reported by the provider and shown without a prefix.
- **Estimated** — LocalCode's heuristic, shown with a `~` prefix.
- **Unavailable** — the provider reported no usage at all. LocalCode never presents `0` as an
  exact count in this case; it shows a labelled estimate instead.

The header meter states whether a count is estimated (`~`) or exact, and the tooltip explains the
reason. Yellow means compaction is approaching. Red means the context is critical or was
exhausted. If a model fills its context, LocalCode explicitly reports that event, preserves the
full transcript, and compacts before the next turn.

Compaction creates a code-focused handoff containing user intent, durable decisions, changed file
paths, test outcomes, and open work. It intentionally relies on current source files and
`AGENTS.md` for implementation detail instead of treating a lossy chat summary as source code.

File reads use a separate bounded working set. LocalCode can retain several disjoint ranges from
the same file, up to 24 observations per chat. It validates each observation against the current
file's SHA-256 hash and injects only a small token-budgeted selection. Changed or deleted files
invalidate cached observations automatically. Repeated or contained unchanged reads are
collapsed, and older file payloads may be released from a tool-heavy live context while their
complete activity records remain stored. Temporary tool pressure alone does not trigger
conversation compaction.

The default agent-step limit is 16 and can be changed in Preferences. When a tool or context limit
ends a segment, LocalCode can automatically rebuild a fresh prompt from the task checkpoint,
verified file ranges, and current symbol locations. The default is two segments per request; set
**Continuation segments** to 1 to disable automatic continuation, or up to 4 for bounded longer
runs. A final unfinished checkpoint makes a later “continue” request resume the same objective.
Malformed JSON tool calls from a local model are also checkpointed and retried when a fresh
segment is available. `edit_file` is intentionally limited to eight localized replacements per
call so smaller models are less likely to truncate a large JSON payload.

LocalCode also reconciles strong completion claims with recorded tool activity. A response that
names changed files or successful command checks without matching evidence is not saved as a
successful completion. It receives one bounded fresh-segment retry when available; a repeated
unsupported claim is checkpointed as unfinished. File-changing checkpoint outcomes are assembled
from verified tool actions rather than the model's narrative.

After successful turns, LocalCode stores one compact task record per chat in SQLite. New requests
search only records from other chats in the same project using SQLite FTS5 when available (with a
plain SQLite fallback). Retrieved records are explicitly untrusted and token-bounded. The source
symbol index stores names and line locations—not file bodies—and refreshes entries by content
hash. This supplies useful cross-chat navigation without an embedding model, GPU allocation, or
background vector database.

## Change Scope

LocalCode lets you set a change scope in Preferences (Ctrl+,) that changes how broadly the model
approaches implementation:

- **Focused** — uses a YAGNI decision ladder to question whether each piece of code
  needs to exist at all. Prioritises standard library and existing project utilities,
  native platform features, and installed dependencies before writing anything new. The
  rule is "minimum that works" without compromising validation, error handling, security,
  or accessibility.
- **Standard (default)** — no additional implementation-scope guidance. The model uses its
  normal judgment.

## Local Data

- SQLite database: `~/.local/share/localcode/localcode.db`
- Verbatim transcripts: `~/.local/share/localcode/transcripts/`
- Project instructions: `<project>/AGENTS.md`

LocalCode creates the managed section on the first writable turn, then leaves it stable. It is
changed only when you explicitly ask the agent to update durable project guidance. Automatic
cross-chat continuity comes from structured task checkpoints and project-memory records in the
SQLite database, not from repeatedly rewriting `AGENTS.md`.

For a custom data root, set `LOCALCODE_DATA_HOME` before launching.
LocalCode enforces owner-only permissions on its data directories and persisted chat files. Data is
local but is not encrypted at rest.

## Verification

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q localcode tests
desktop-file-validate data/io.localcode.LocalCode.desktop
```

The tests cover local persistence and migrations, checkpoints, cross-chat retrieval, symbol
indexing, automatic continuation, compaction boundaries, context states, project-root security,
file tools, approval modes, `AGENTS.md` preservation, Ollama
streaming/counters, API-provider streaming (usage, malformed streams, cancellation),
backend routing for local and API models, web-fetch network boundaries, and complete
mocked coding turns including multi-round tool loops, tool failure, approval denial,
cancellation, and read-only turns.
