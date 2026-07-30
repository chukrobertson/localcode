# AGENTS.md

<!-- localcode:managed:start -->
## Product

LocalCode is a local-first GNOME coding workspace. The UI is Python with GTK 4 and
Libadwaita. It streams local Ollama chat and OpenAI-compatible API endpoints, gives models
root-confined file tools and explicitly approved shell commands,
tracks context pressure, preserves full transcripts, compacts active model context,
updates project `AGENTS.md` files, and optionally retrieves with MemPalace.

## Architecture

- `localcode/ui.py` owns the Libadwaita window, navigation, dialogs, and worker-to-main-loop callbacks.
- `localcode/agent.py` orchestrates a turn, tool loops, context compaction, transcript export, and `AGENTS.md` refresh.
- `localcode/backend.py` dispatches chat calls to Ollama or API providers based on model origin.
- `localcode/ollama.py` implements the native Ollama HTTP and NDJSON APIs without third-party networking dependencies.
- `localcode/providers.py` implements an OpenAI-compatible SSE streaming client with `/v1/models` discovery.
- `localcode/projects.py` contains project scanning and root-confined coding tools.
- `localcode/database.py` persists projects, chats, full messages, activities, providers, and settings in SQLite.
- `localcode/memory.py` installs and invokes the isolated MemPalace companion.
- `localcode/context.py` owns conservative preflight estimates and exact post-response status classification.
- `tests/` uses the Python standard library `unittest` runner.

## Development Commands

- `python3 -m unittest discover -s tests -v`
- `python3 -m compileall -q localcode tests`
- `python3 localcode.py`
- `desktop-file-validate data/io.localcode.LocalCode.desktop`

## Constraints

- Keep the core dependency-free beyond PyGObject, GTK 4, and Libadwaita supplied by the OS.
- Never discard stored chat messages during compaction; only advance the active-context boundary.
- Never allow model file tools to resolve outside the selected project root.
- Keep GTK calls on the main loop. Ollama, API calls, shell commands, and MemPalace run in workers.
- Preserve text outside the managed markers when updating any project `AGENTS.md`.
- Treat Ollama `prompt_eval_count` as exact only after a request; preflight counts remain estimates.
- API provider API keys are stored in the local 0600 SQLite database and never transmitted elsewhere.
<!-- localcode:managed:end -->

## Project Notes

MemPalace is vendored at `vendor/mempalace` from its upstream `develop` branch and retains its own
MIT license. Avoid modifying the vendored checkout as part of LocalCode changes.
