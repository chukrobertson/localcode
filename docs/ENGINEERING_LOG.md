# LocalCode Engineering Log

Handoff record for daily-driver reliability work. Append new entries at the top of the
"Passes" section. Each entry records what was found, why it mattered, what changed, and
how it was verified so a later session can continue without rediscovering anything.

## Passes

### Pass 17 — Recover safe exact edits from indentation drift (2026-09)

- **Observed failure:** in the live ResumAI chat, Gemma correctly read
  `src/lib/llm/providers.ts` and identified the `llama3.1` default, but its `edit_file` payload
  reproduced the block with four/six-space indentation instead of the source's two/four spaces.
  The exact edit failed, a second exact replacement failed, and two identical recovery reads were
  omitted before the progress guard stopped the turn. The guard prevented runaway work but could
  not complete the already-understood one-token change.
- **Correction:** `edit_file` and `replace_in_file` now recover a single uniquely identifiable
  horizontal-whitespace variant. Every line break and non-whitespace token in `old_text` must
  match, the old/new token layouts must align, token boundaries cannot be embedded in larger
  tokens, and ambiguous matches still fail. The mutation substitutes only the new tokens while
  preserving the source's actual spaces, tabs, and line endings. Unrecoverable misses explicitly
  direct the model to the already-verified `replace_lines` coordinates instead of another reread.
- **Regression coverage:** tool tests cover both exact-edit paths, formatting preservation,
  ambiguous candidates, embedded-token rejection, and the `replace_lines` recovery hint. An
  agent-level test replays the observed read and mismatched `edit_file` payload and now completes
  in three provider steps with a clean checkpoint. The evaluation suite adds the ResumAI-shaped
  `indentation_edit` scenario with a required final-content assertion.
- **Live verification:** `gemma4:12b` naturally reproduced the same indentation-mismatched
  `edit_file` call in the disposable scenario. LocalCode reported one indentation recovery,
  changed only `src/lib/llm/providers.ts`, and completed successfully in one segment without a
  repeated read or progress-guard stop. The full 135-test suite, `compileall`, desktop-file
  validation, and `git diff --check` pass.

### Pass 16 — Reproducible real-model evaluation harness (2026-09)

- **Motivation:** reliability fixes through Pass 15F came from valuable but ad-hoc live-model
  trials. Unit tests preserved each discovered failure, but there was no repeatable way to compare
  actual Ollama models or detect behavioral regressions before another daily-driver session.
- **Harness:** `python3 -m localcode.evals` now runs named scenarios in disposable project and app
  directories. The initial smoke suite covers a focused verified repair, inspection without
  mutation, and read-only enforcement. Each result scores runtime errors, checkpoint status,
  required and unexpected file changes, recorded tools and commands, independent verification,
  `AGENTS.md` permission behavior, and response presence.
- **Safety and reporting:** shell actions are approved only when the complete approval description
  exactly matches a built-in scenario command and root working directory; suffix matches,
  multi-line prefixes, alternate working directories, lint auto-detection, and network tools are
  denied. Reports aggregate streaming metrics rather than storing every token chunk, retain
  bounded structured activities and model usage, print a compact terminal table, and write atomic
  timestamped JSON under the ignored `eval-results/` directory by default.
- **Verification:** five harness tests cover scenario invariants, exact command approval including
  a multi-line injection shape, evidence-based scoring, JSON round trips, and list mode. The full
  Granite `granite4.2:8b` suite passed all three scenarios in 25.3 seconds; a second verified-edit
  run passed after the stricter approval parser was applied.

### Pass 15F — Verification-loop guard across edits (2026-09)

- **Observed failure:** while asked to replace one focused test file and run one unittest command,
  Granite rewrote that file repeatedly, reran the same failing test suite, and launched many ad-hoc
  debug commands. Each write reset the ordinary no-progress guard, allowing the failing edit/test
  cycle to consume three context segments before identical calls finally stopped it.
- **Root cause:** mutation is normally strong evidence of progress, so `ToolLoopGuard` correctly
  clears per-generation call and result fingerprints after a file change. It did not retain any
  cross-generation record that the same verification command continued to fail.
- **Correction:** failed `run_command` and `run_lint` calls now retain an argument-aware failure
  count across file mutations and fresh continuation segments. The third failure of the same check
  stops immediately with the distinct `verification_loop` reason. No automatic continuation is
  spent; the latest failure and verified file state receive a `needs-continuation` checkpoint and a
  focused replan notice. A successful execution of that same check clears its retained streak.
- **Regression coverage:** an agent-level test performs three different file rewrites followed by
  the same failing command and verifies that the third failure stops in the first segment with the
  latest file preserved. Existing tests continue to allow distinct silent successful commands and
  stop ordinary identical read loops.

### Pass 15E — Output-limit continuation and checkpoint integrity (2026-09)

- **Observed failure:** Granite used exactly the configured 4,096 response tokens and ended with
  provider reason `length` while only 18,936 of 48,128 context tokens were occupied. It stopped
  mid-proposal without calling a mutation or verification tool, yet LocalCode treated the turn as
  complete and stored the truncated narrative in a complete checkpoint.
- **Root cause:** `length` was considered continuable only through the context-exhaustion test,
  which deliberately requires the prompt and response to fill the effective context window. A
  response-token cap with ample context therefore fell through as ordinary completion.
- **Correction:** provider reason `length` is now an explicit incomplete/continuable state. A
  bounded fresh segment receives a focused recovery instruction to skip repeated analysis, use the
  smallest necessary tools, verify, and answer concisely. If all segments hit the response cap,
  the task remains `needs-continuation`; unsupported partial narration is not promoted into the
  checkpoint outcome. UI activity, notices, and context-report reasons distinguish this output cap
  from genuine context exhaustion.
- **Regression coverage:** one agent test reproduces the exact ample-context 4,096-token cap and
  verifies recovery through a real file tool; a second proves repeated caps cannot produce a
  complete checkpoint; a third keeps full-context exhaustion on its separate continuation path.

### Pass 15D — Argument-aware command progress detection (2026-09)

- **Observed failure:** after a real `main.py` mutation, Granite ran five distinct successful
  verification commands. Three produced the same silent exit-zero output, so the progress guard's
  result-only fingerprint (`tool + status + output`) treated them as one repeated result and
  stopped the turn. The edit and commands were preserved, but the checkpoint incorrectly remained
  `needs-continuation` even though this was not an identical-call loop.
- **Correction:** `run_command` and `run_lint` result fingerprints now incorporate normalized call
  arguments as well as status and output. Distinct commands may therefore share empty or generic
  success output without colliding. The separate exact-call fingerprint remains unchanged, so a
  model that literally repeats one command is still omitted and stopped at the existing bound.
  Result-based loop detection for searches and other observational tools is unchanged.
- **Regression coverage:** an agent-level test performs one file mutation followed by five
  distinct silent commands and verifies a complete checkpoint with all commands recorded and no
  progress-guard activity. The pre-existing identical-read loop test continues to verify genuine
  repetition is stopped. The full 123-test suite passes.

### Pass 15C — Explicit-tool routing and stronger completion evidence (2026-09)

- **Observed failures:** a continuation explicitly instructed Granite to call `replace_lines`,
  but the intent classifier selected the inspect palette because underscored tool names were not
  recognized and later words such as “summarize” won the heuristic. The unavailable edit tool led
  to repeated `project_commands` calls and a progress-guard stop. A follow-up reached the edit
  palette but skipped the mutation, ran two syntax/import checks against the unchanged file, and
  claimed “Changes made to `main.py`” and “The fix has been applied correctly.” The original
  evidence guard accepted that wording and marked the checkpoint complete with no changed files.
- **Routing correction:** mentioning any registered mutating tool now selects the edit palette
  before generic intent heuristics, while read-only mode remains authoritative. Direct positive
  instructions such as “call `replace_lines`” or “repair using `replace_lines`” are separately
  extracted; negative instructions such as “do not call `edit_file`” are not treated as required
  calls.
- **Evidence correction:** project tools now record the names actually invoked. A completion is
  rejected when a directly requested mutating tool was never called, including an otherwise empty
  response. For edit-intent turns, the claim detector also recognizes change-summary headings,
  past-tense action bullets, first-person mutation claims, and “fix applied/complete” language.
  The broader patterns are gated to edit intent so review-only summaries of existing changes are
  not rejected. Unsupported prose is discarded and the checkpoint remains `needs-continuation`.
- **Regression coverage:** agent tests reproduce both exact failure shapes, verify `replace_lines`
  is advertised despite competing inspection language, exclude a prohibited `edit_file` from the
  required-call set, reject the unsupported completion wording, and preserve review behavior.
  Tool tests cover invoked-tool bookkeeping. The full 122-test suite passes.

### Pass 15B.1 — Multi-step palette and non-Git status correction (2026-09)

- **Smoke-test evidence:** Granite completed an 11-tool, one-segment test with verified files and
  command results, but used `write_file` to recreate `smoke-copy.txt` rather than the requested
  `copy_file`. The first file creation had already moved the agent to verification, whose palette
  omitted structural mutations. The expected “Not a Git repository” status was also recorded as
  a failure, leaving an otherwise complete checkpoint ineligible for clean cross-chat recall.
- **Correction:** verification is now explicitly repair-capable: `delete_file`,
  `create_directory`, `rename_file`, and `copy_file` join its existing targeted edit tools. This
  allows ordered multi-part changes to continue after the first mutation. The verify palette is
  now 17 tools/~2,429 conservative schema tokens, still below the full 20/~2,809 registry.
  `git_status` classifies a non-Git project as a benign `skipped` result while retaining real Git
  command failures as errors.
- **Regression coverage:** an agent-level create-then-copy test verifies that `copy_file` remains
  advertised and executes after the first mutation. Palette tests cover all structural repair
  tools, and project-tool tests distinguish a real Git status result from a skipped non-repository.
  The full 119-test suite, `compileall`, desktop-file validation, and `git diff --check` pass.

### Pass 15B — Phase-aware palettes and focused tool expansion (2026-09)

- **Context problem:** even after centralizing the protocol, every request still sent every tool
  schema. Small local models paid that context and choice cost while inspecting, editing, and
  verifying, including for tools that were irrelevant to the current step.
- **Phase palettes:** LocalCode now selects `inspect`, `edit`, or `verify` from the current user
  intent and project permission mode. Registry metadata builds the actual provider schema subset
  on every round. A recorded mutation advances to verification; a failed verification returns to
  editing. Read-only projects never advertise mutating tools. The full registry is 20 tools and
  roughly 2,809 conservative schema tokens; inspect is 10/~1,259, edit is 15/~2,209, and verify is
  13/~1,945. Execution-time root and approval policy remains authoritative.
- **Focused expansion:** added `git_status` for fresh branch/working-tree evidence,
  `project_commands` to reveal bounded manifest-derived checks without running them, and
  `copy_file` for atomic root-confined copying of text or binary assets. Copying rejects symlinks,
  same-path copies, copying over the protected `AGENTS.md`, sources over 100 MB, and accidental
  overwrites; identical targets are explicit no-ops and successful targets return post-copy
  evidence.
- **Cleanup and regression coverage:** removed the obsolete pre-registry schema helper. Tests cover
  palette membership, read-only filtering, intent selection, edit-to-verify transitions, binary
  copy/no-op/overwrite behavior, Git status, and detected commands in addition to the Pass 15A
  protocol tests. The full 118-test suite, `compileall`, desktop-file validation, and
  `git diff --check` pass.

### Pass 15A — Tool protocol foundation (2026-09)

- **Observed failure:** a small local model could complete an edit and its checks, then issue one
  redundant read that LocalCode labeled as an error. Tool schema, approval, mutation, and
  checkpoint metadata were also maintained in separate hard-coded lists, while OpenAI-compatible
  follow-up messages omitted the standard `tool_call_id`. These mismatches made successful work
  look failed and made protocol changes easy to apply inconsistently.
- **Registry and schemas:** all 17 existing model-facing tools now come from one `ToolSpec`
  registry containing their schema, handler, risk, valid phases, mutation behavior, approval
  requirement, and action-recording policy. Names remain compatible. Object schemas reject
  additional properties, bounded inputs expose their actual limits, and definitions can be
  filtered for a later phase-aware palette without changing execution policy.
- **Structured outcomes:** `ToolResult` now distinguishes `success`, `noop`, `skipped`, `blocked`,
  and `error`, while retaining the existing pass/fail compatibility property. Changed paths,
  commands, and verified file observations travel with each result and are aggregated centrally.
  A repeated unchanged read is a benign `skipped` result, not a checkpoint failure; the progress
  guard can still stop a genuine repetition loop.
- **Evidence and wire formats:** text mutations return a bounded numbered post-edit excerpt plus a
  SHA-256 observation, so the model can verify the applied state without immediately rereading the
  file. The agent invalidates stale observations before saving this new evidence. Canonical live
  tool messages carry both call ID and tool name; the Ollama client emits Ollama-shaped messages,
  while OpenAI-compatible providers receive stringified function arguments and `tool_call_id`.
- **Regression coverage:** tests cover the single registry, strict and nested schemas, filtered
  definitions, unexpected arguments, explicit result states, verified mutation evidence, benign
  duplicate reads, canonical agent-loop IDs, fresh post-edit working-file state, and both provider
  serializers. The full 114-test suite passes.

### Pass 14 — Completion evidence guard (2026-09)

- **Observed failure:** Granite returned a detailed account of reading and editing `gui.py`,
  running compilation and import checks, and passing both checks. The turn recorded zero tool
  activities, zero changed files, and zero commands; the file remained unchanged. The fabricated
  narrative was nevertheless saved as a complete checkpoint and made eligible for project recall.
- **Root cause:** a provider `stop` response was accepted as successful based solely on its prose.
  LocalCode tracked tool evidence but did not reconcile strong completion claims against it before
  persisting the assistant response and task status.
- **Fix:** responses that name changed files or successful command/check results without matching
  tool activity are classified as `unverified_completion`. Their unsupported prose is excluded
  from both the live message feed and the persisted assistant result, the discrepancy is recorded
  as an activity and failure, and one bounded fresh-segment retry is attempted when available.
  Repeated unsupported claims remain
  `needs-continuation` and receive a specific warning. File-changing checkpoint outcomes now come
  from verified tool actions before model prose. The recall gate also rejects older complete
  records whose narrative claims conflict with their empty structured evidence, so existing chat
  history can remain intact without contaminating new tasks.
- **Regression coverage:** agent tests cover successful recovery through a real file tool and a
  repeated evidence-free claim that is rejected, warned, and kept out of cross-chat recall.

### Pass 13 — Verified line-range editing (2026-09)

- **Observed failure:** on a two-line startup repair, Granite read the correct source but
  invented an adjacent three-line `old_text` block that omitted an intervening queue assignment.
  Both exact edit tools rejected it; after a narrow reread the model repeated the same stale
  replacement and the progress guard stopped the turn with no file changes.
- **Root cause:** every scoped editing path required the model to reproduce exact source text,
  indentation, and adjacency. The existing numbered read output gave a small model a simpler and
  more reliable coordinate system, but no mutation tool could use it.
- **Fix:** added `replace_lines`, which replaces an inclusive range of at most 200 lines only when
  that range is contained in a previously observed version of the same file and its SHA-256 still
  matches. An intervening edit or external change makes the observation stale and forces a reread.
  Rewrites remain atomic, root-confined, approval-aware, and subject to `AGENTS.md` integrity
  validation.
- **Regression coverage:** project-tool tests cover unread-range rejection, the exact duplicate
  root/title repair pattern, newline preservation, and stale-hash rejection. Agent mutation
  bookkeeping recognizes the new tool for checkpoints, working-file invalidation, and progress.

### Pass 12 — Manual-only durable project guidance (2026-09)

- **Observed failure:** after a successful, verified three-substitution GUI repair, the
  automatic follow-up completion expanded `AGENTS.md` from concise project guidance into
  duplicated command and implementation sections. It also emitted a malformed command example
  and promoted unsupported thread-safety claims into every future chat's system context.
- **Decision:** `AGENTS.md` is stable project guidance rather than an automatic task journal.
  LocalCode still creates a conservative managed section on the first writable turn, reads it
  into coding and compaction context, and protects its markers and human-owned text. There is no
  post-turn model completion and no automatic rewrite after source changes.
- **Memory ownership:** structured task checkpoints and project-memory records remain the
  automatic cross-chat continuity mechanism. An agent can change `AGENTS.md` through protected
  file tools only when the user explicitly requests a durable guidance change.
- **Cleanup:** removed the dead update prompt, model-application path, completion method, and
  automatic-update system prompt. The coding prompt and README now document the explicit-only
  behavior.
- **Regression coverage:** a complete file-changing turn asserts that `AGENTS.md` remains at its
  initial content and that no secondary completion call occurs. Existing tests continue to cover
  explicit protected edits, marker integrity, and preservation of human notes.

### Pass 11 — Repeated exact edits and search recovery (2026-09)

- **Observed failure:** Granite was given three explicit mechanical substitutions. It selected
  `edit_file`, but the first target occurred six times and the tool rejected it. The model then
  escaped literal searches as regular expressions without enabling regex mode, received no
  matches, repeated those searches, and was correctly stopped by the progress guard without
  changing the project.
- **Root cause:** `replace_in_file` supported `replace_all`, while the preferred compact
  multi-edit tool did not expose equivalent behavior. Its error suggested splitting the edit,
  making a simple all-occurrences request unnecessarily difficult for a small model. Empty
  literal search results also did not explain the search mode.
- **Fix:** each `edit_file` specification can now set `replace_all=true`; replacements remain
  ordered, validated, atomic, and bounded to eight specifications. Ambiguous edits explicitly
  recommend that field. Empty search results now state that literal mode does not need regex
  escaping and explain how to opt into regex mode.
- **Regression coverage:** project-tool tests cover ambiguous-edit rollback, a mixed atomic edit
  containing an all-occurrences substitution, replacement counts, and the literal-search hint.

### Pass 10 — Incomplete-turn guidance gate (2026-09)

- **Observed failure:** the progress guard correctly stopped a repeated edit loop after
  several source mutations, but the post-turn lifecycle still launched a model-driven
  `AGENTS.md` refresh. The refresh recorded duplicated and unverified implementation claims
  even though the task checkpoint was explicitly `needs-continuation` and runtime verification
  had not completed.
- **Root cause:** automatic guidance refresh was gated only on whether a non-`AGENTS.md` file
  changed. It did not consider the structured checkpoint status produced immediately before it.
- **Fix:** automatic `AGENTS.md` refresh now requires a `complete` checkpoint as well as a
  non-guidance file change. Stalled, interrupted, context-guarded, malformed-tool, and
  step-limited turns retain their source changes and checkpoints without promoting partial work
  into durable cross-chat instructions.
- **Regression coverage:** `tests/test_agent.py` exercises a turn that changes `main.py` and
  then stalls on repeated reads; it asserts that the checkpoint needs continuation and that the
  automatic completion call and `AGENTS.md` mutation never occur.

### Pass 9 — Progress guard and managed-file integrity (2026-09)

- **Observed failures:** a fresh `granite4.2:8b` chat used both 16-round segments while
  context was only 44 percent full. It made no edits, repeated file reads and Tkinter probes,
  and stopped at the continuation limit. A subsequent narrowly-scoped turn completed its
  model response and rewrote `AGENTS.md`, but omitted the closing LocalCode marker; optional
  post-turn maintenance then surfaced the misleading fatal error “The model could not finish.”
- **Progress guard:** tool-call fingerprints now span continuation segments. Identical calls
  are omitted, three identical results stop the loop, eight calls without a project change
  inject a replan/finish instruction, and twelve stop with the distinct `stalled` reason.
  Stalled work does not consume an automatic fresh segment and receives a specific UI notice.
- **Managed-file integrity:** root `AGENTS.md` writes and edits are validated before their
  atomic replacement. They must preserve exactly one ordered marker pair and all human-owned
  text outside it; deletion and rename are blocked. Approved shell/lint commands that damage
  the file are detected and the original is restored. A turn that changes only `AGENTS.md`
  no longer launches a redundant model-driven update, and optional post-processing cannot
  retroactively turn a completed model response into a fatal turn error.
- **Cleaner recall/checkpoints:** cross-chat prompt injection now accepts only records whose
  structured status is `complete` and whose structured failure list is empty; stalled,
  interrupted, failed, and continuation-needed records stay available to their source chat but
  cannot pollute a new one. Generic coding words were added to recall stopwords. Incomplete
  checkpoints store actual actions or an explicit no-change result instead of the model's
  accumulated “let me…” narration, and command/failure lists are deduplicated and bounded.
- **Command correctness:** shell and custom lint execution enable Bash `pipefail`, so a failed
  producer can no longer be reported as successful merely because `head` or another final
  pipeline command exited zero. The coding prompt also directs models toward scoped file tools,
  ordered objectives, and replanning after repeated failures.
- **Recovery:** the damaged `lofi-radio/AGENTS.md` closing marker was restored immediately
  before its human-owned Project Notes section. Its stale command transcript and unsupported
  architecture claims were replaced with concise guidance verified from the current sources.
- **Regression coverage:** tests cover malformed and human-note-changing `AGENTS.md` rewrites,
  shell-based marker damage with restoration, pipeline failure propagation, repeated-tool
  stalling without a second segment, completed-only cross-chat recall, concise stalled
  checkpoints, and skipping the redundant automatic `AGENTS.md` rewrite.

### Pass 8 — Recover malformed local-model tool calls (2026-09)

- **Observed failure:** Granite/Ollama could identify `edit_file` but end its JSON arguments
  early. Ollama reports this as `invalid tool call arguments ... unexpected end of JSON input`
  before LocalCode receives an executable call, so the whole turn previously ended with “The
  model could not finish.” No project edit was executed by that malformed call.
- **Recovery:** the agent loop now classifies only explicit malformed/invalid tool-call server
  errors as a recoverable `invalid_tool_call` segment stop. It records the failure in the activity
  log and checkpoint, rebuilds a fresh segment, and asks the model to retry with smaller localized
  calls. Other runtime/server errors remain fatal and are not hidden.
- **Smaller calls:** `edit_file` now advertises and enforces at most eight localized replacements
  per call. This reduces JSON-generation pressure for small local models; it does not attempt to
  guess or repair truncated arguments.
- **Regression coverage:** a mocked Ollama failure with the exact reported error must recover in
  segment two, retain the failure in its checkpoint, and finish without surfacing a fatal error.
  The full 97-test suite, `compileall`, desktop-file validation, and `git diff --check` pass.

### Pass 7 — Checkpoints, fresh continuation, and lightweight project recall (2026-09)

- **Task checkpoints:** schema version 4 stores one structured checkpoint per chat with the
  objective, status, completed narrative, changed files, commands, failures, next step, and
  continuation count. A short “continue” request retains the unfinished objective instead of
  replacing it. Checkpoints are navigation data and never replace the verbatim transcript.
- **Automatic continuation:** reaching the tool-round or guarded context limit can now start a
  fresh prompt segment inside the same request. The rebuilt prompt carries only the structured
  in-flight checkpoint, current project guidance, verified file excerpts, and symbol locations;
  live tool payloads are not copied. Preferences defaults to two segments and permits one
  (disabled) through four, keeping runaway local-model loops bounded.
- **Cross-chat recall:** each successful chat maintains one compact project task record. New
  requests search records from other chats in the same project with SQLite FTS5 when available
  and a dependency-free `LIKE` fallback. Results are relevance-filtered, token-bounded, and
  explicitly injected as untrusted, potentially stale data. Deleting the source chat deletes its
  task record.
- **Symbol map:** a hash-refreshed index extracts Python qualified classes/functions plus common
  type, function, heading, shell-function, and HTML-id locations. It stores names and line
  locations rather than source bodies and is used only for navigation before exact file reads.
- **Multi-range working set:** the verified cache now preserves useful disjoint ranges from the
  same file, reuses a broader range for contained reads, and invalidates every range for a path
  when its SHA-256 changes.
- **Approval bugs found during audit:** `edit_file` bypassed ask/read-only mutation controls, and
  `web_fetch` plus `run_lint` bypassed the approval branch even though the UI contract said they
  always prompt. Approval routing is now independent of mutation/read-only routing: all edit
  tools honor project mode, shell-backed lint and commands remain blocked in read-only mode, and
  network fetches remain available there only after explicit approval.
- **Verification:** regression coverage includes the version-3-to-4 migration, checkpoint and
  recall lifecycle, multi-range behavior, symbol refresh/invalidation, and a two-segment tool-loop
  handoff with no live tool-message carryover. The full 96-test suite, `compileall`, desktop-file
  validation, and `git diff --check` pass. A read-only backup of the live database migrated from
  version 3 to 4 while preserving its project, chat, and all 15 messages.

### Pass 6 — Verified working context and bounded tool pressure (2026-09)

- **Observed failure:** the `lofi-radio` trial completed useful work, but 42 successful
  `read_file` calls returned about 279,000 characters. The central generator file was read
  14 times. Tool results were retained in the activity log but disappeared from the next
  model turn, causing repeated reads, agent-step exhaustion, and avoidable context pressure.
- **Working files:** schema version 3 adds a per-chat `working_files` table. LocalCode keeps
  the most recent observed range for at most 24 files, injects a token-bounded recent subset,
  and only suppresses a repeated read when its path, range, and full-file SHA-256 still match.
  Cache entries are invalidated after file mutations and on any external hash mismatch.
- **Live-loop pressure:** `read_file` now defaults to 200 lines and caps explicit reads at
  400 lines/24,000 characters; batch reads use smaller per-file and total budgets. Once a
  tool loop reaches 82 percent of context including response reserve, older full file-read
  payloads are replaced by compact references while the complete activity records remain.
- **Compaction fix:** post-turn compaction now estimates the actual next-turn message set.
  It no longer compacts conversation history merely because temporary tool results made the
  final agent step large. Real compaction retains a token-bounded recent tail instead of a
  fixed number of messages.
- **Scope and steps:** Preferences now offers **Change scope** with **Focused** and
  **Standard**; the old Verbose option is removed and legacy Ponytail settings map to
  Focused. The default tool-round ceiling rises from 12 to 16 and is user-configurable.
- **Verification:** database migration, cache invalidation, unchanged-read deduplication,
  read budgets, live-context pruning, false-compaction prevention, change-scope migration,
  and token-budgeted compaction all have regression coverage. `compileall`,
  `desktop-file-validate`, `git diff --check`, and the full 89-test suite pass; the tested
  source was installed into the per-user application directory.

### Pass 5 — Remove MemPalace and simplify the runtime (2026-09)

- **Decision:** MemPalace retrieval and indexing were removed. The core product already
  preserves complete transcripts and compacts active context; external retrieval added a
  second Python environment, background indexing, prompt noise, and GPU contention before
  the base agent loop was reliable enough to justify that cost.
- **Files removed:** `localcode/memory.py`, `tests/test_memory.py`,
  `scripts/bootstrap-mempalace.sh`, `THIRD_PARTY.md`, and the `vendor/mempalace` submodule.
- **Integration removed:** recall injection, post-turn mining, project-memory settings,
  sidebar status/actions, installer copying, and MemPalace-specific project cleanup.
- **Database:** schema version 2 drops the obsolete `projects.memory_enabled` column while
  preserving existing projects. Fresh databases apply the same migration.
- **Related bug fixed:** after preflight compaction, `agent.py` passed an
  `AgentsFileManager` instance into `_build_messages` instead of the previously read
  `agents_content` string. Compacted turns now rebuild the system prompt with the actual
  project instructions.
- **Verification:** `python3 -m compileall -q localcode tests`, `git diff --check`, and the
  full 77-test suite pass. The migration suite includes a version-1 database with an
  existing project to verify data preservation.

### Pass 1 — Reliability fixes (2026-09)

#### 1. Add API Provider dialog never saved

- **Severity:** High — API providers were un-configurable from the UI.
- **Root cause:** `ui.py` defined `provider_chosen` inside `_show_add_provider_dialog`
  but never invoked it. The only connection was
  `dialog.connect("closed", lambda _dialog: provider_chosen)`, which returns the function
  without calling it, and the dialog had no confirm button.
- **Files changed:** `localcode/ui.py` — replaced the `Adw.PreferencesDialog` with an
  `Adw.Dialog` containing `Adw.EntryRow`/`Adw.SpinRow` fields and explicit
  **Cancel** / **Add Provider** buttons wired to `_save_provider`, which validates,
  persists via `database.add_provider`, closes, toasts, and refreshes model discovery.
  Provider names containing `/` are rejected because they corrupt canonical model IDs.
- **Before:** clicking Add in Preferences showed a form whose data was silently discarded.
- **After:** Add Provider saves, refreshes the model list, and is confirmed by a toast.
- **Regression test:** UI-only; verified by `compileall` and manual inspection. The
  persistence path (`database.add_provider`) was already covered by tests.

#### 2. API token accounting was misleading or absent

- **Severity:** High — the meter could show `0 / limit` as an *exact* count.
- **Root cause:** `providers.py` did not request
  `stream_options: {"include_usage": true}`, so OpenAI-compatible servers omitted `usage`.
  The old code then set `prompt_eval_count = 0` and `counts_exact = True`.
- **Files changed:** `localcode/providers.py` (request usage; track it wherever it
  appears in the stream; add `usage_unavailable` to `ProviderChatResult`),
  `localcode/agent.py` (when usage is unavailable, report a heuristic estimate marked
  `estimated` instead of an exact zero, with an explanatory reason).
- **Before:** `0` tokens reported as exact; exhaustion detection and compaction relied
  entirely on heuristics for API providers.
- **After:** exact counts when the provider supplies usage; a clearly-labelled estimate
  when it does not. Servers that reject `stream_options` with HTTP 400 are retried once
  without it.
- **Regression tests:** `tests/test_providers.py` (usage on final chunk, usage-only chunk
  before `[DONE]`, missing usage, stream-options rejection retry) and
  `tests/test_agent.py` (`test_missing_usage_is_reported_as_an_estimate_never_exact_zero`).

#### 3. Empty API stream could raise `NameError`

- **Severity:** Medium — the worker thread died without a user-facing error.
- **Root cause:** `providers.py` referenced `finish_reason` after the read loop without
  initializing it; an empty or non-SSE body broke out of the loop before assignment.
- **Files changed:** `localcode/providers.py` — `_chat_stream` rewritten: `done_reason`
  initialized, `received_data` tracked, empty/garbage streams raise `ProviderError`,
  premature EOF after content returns an interrupted result with partial content,
  malformed SSE lines are skipped, SSE comments are ignored, HTTPError bodies are closed.
  Cancellation now mirrors the proven Ollama pattern (background connect + socket-shutdown
  watcher), covering cancel while waiting for headers.
- **Before:** `NameError` escaped the worker; no error notice in the UI.
- **After:** a useful `ProviderError` reaches `agent.run_turn`'s error path and the UI.
- **Regression tests:** `tests/test_providers.py` — empty body, non-SSE body, malformed
  mixed stream, premature cut, cancel during streaming, cancel during connect.

#### 4. `web_fetch` network protections bypassable via redirects/DNS

- **Severity:** Medium — an approved URL could redirect to loopback/private targets.
- **Root cause:** only the literal hostname was checked once; urllib followed redirects
  without re-validation and hostname-to-IP tricks were unchecked.
- **Files changed:** `localcode/projects.py` — new `validate_web_url` resolves the host
  via `getaddrinfo` and rejects any address that is loopback, unspecified, multicast,
  link-local, private (RFC1918/`fc00::/7`), reserved, or CGNAT (`100.64.0.0/10`), for
  IPv4 and IPv6 including IPv4-mapped IPv6. A `_GuardedRedirectHandler` re-validates
  every redirect hop. The approval requirement for `web_fetch` is unchanged.
- **Before:** redirect to `127.0.0.1` or a DNS-rebinding hostname could reach local
  services.
- **After:** each hop is validated; blocked targets return a `Blocked: ...` tool result.
- **Regression tests:** `tests/test_projects.py` — blocked address classes, public
  addresses allowed, hostnames resolving private, mixed resolutions, unresolved
  hostnames, redirect validation, tool-level blocked result.
- **Remaining concern:** DNS is checked per hop at request time; a service that
  re-resolves between validation and connect (TOCTOU) is outside this tool's threat
  model. End-to-end redirect behavior needs a non-loopback test server and is verified
  via the handler unit tests plus manual testing.

#### 5. API provider models could never be routed to their provider

- **Severity:** Critical — selecting `gpt-4o (OpenAI)` stored the *display name* as the
  chat model, which `resolve_backend` could not match, so every API-model chat silently
  fell back to Ollama with a bogus model name.
- **Root cause:** UI persisted `ProviderModelInfo.display_name()` instead of a canonical
  identifier.
- **Files changed:** `localcode/providers.py` (`model_id` field, `provider/name` for API
  models, bare name for Ollama), `localcode/ui.py` (dropdown maps display entries to
  canonical `model_ids`; legacy display-name values are re-aligned on load),
  `localcode/backend.py` (`resolve_backend` also accepts the legacy
  `"model (provider)"` form so old chats keep working).
- **Before:** API models unusable end-to-end from the UI.
- **After:** selected API models route to the correct client with the correct model
  string.
- **Regression tests:** `tests/test_backend.py`.

### Pass 2 — Semantics and structure (2026-09)

#### 6. Read-only projects were mutated (AGENTS.md)

- **Severity:** Medium — read-only mode wrote `AGENTS.md` into the project.
- **Root cause:** `agent.run_turn` called `agents.ensure()` unconditionally, and the
  compaction prompt builder called `agents.read()` (which also ensures).
- **Files changed:** `localcode/agents_file.py` (new non-creating `read_existing`),
  `localcode/agent.py` (ensure/read only when the project is not read-only; compaction
  source uses `read_existing`), `localcode/ui.py` (adding a project no longer creates
  `AGENTS.md`; it is created on the first writable turn instead).
- **Before:** opening and chatting with a read-only project could create `AGENTS.md`.
- **After:** a read-only turn makes zero project changes: no AGENTS.md, no file
  mutations, no commands. `ask`/`allow` projects still get AGENTS.md on first use.
- **Regression test:** `tests/test_agent.py`
  `test_read_only_turn_does_not_mutate_the_project`.

#### 7. Provider-neutral labels

- **Severity:** Low — UI text said "Exact Ollama prompt count" for API providers.
- **Files changed:** `localcode/agent.py`, `localcode/widgets.py`, `localcode/ui.py` —
  wording now says "provider"/"model provider"; welcome and About copy updated.

#### 8. Database migration foundation

- **Severity:** Medium — future schema changes would have broken existing installs.
- **Files changed:** `localcode/database.py` — schema moved to `BASE_SCHEMA` (fresh
  databases or version-0 files get it and `user_version = 1`); ordered `MIGRATIONS`
  tuples apply incrementally, each bumping `user_version`.
- **Regression tests:** `tests/test_database.py` — fresh DB version, migration applies
  once and persists across reopen, legacy empty file gets the base schema.

### Pass 3 — Vertical workflow coverage (2026-09)

New tests in `tests/test_agent.py`: multi-round inspect-then-edit tool loop with changed
files reported; failed tool keeps the conversation usable; denied approval leaves files
untouched and records an error activity; cancellation between tool calls is recoverable
and the next turn works; context guard stops tool rounds before truncation.
`tests/test_projects.py` adds a git-repo test for `run_command` changed-file detection.

### Pass 4 — Investigations (2026-09)

- **`bash -ilc`:** kept. Ubuntu sources user tooling (nvm, pyenv, rvm) from `.bashrc`,
  which `.profile` only loads for *interactive* shells. Dropping `-i` or `-l` would
  silently change the user's environment. Documented in README.
- **Command changed-file detection:** measured `_file_state` at ~0.4-0.6 s per snapshot
  on a synthetic 20k-file tree (~1 s+ per command on large projects). `run_command` now
  uses `git status --porcelain --untracked-files=all` in Git repositories (millisecond
  range) and keeps the full walk as fallback for non-Git projects. Behavior for
  ignored/generated directories is unchanged in both paths.
- **Ollama/API cancellation:** the socket-shutdown watcher (`response.fp.raw._sock`)
  relies on private internals but is proven by tests to reliably interrupt blocked
  reads; `response.close()` alone does not. No better stable approach found in the
  standard library without rewriting the HTTP layer. Left as-is, recorded as a known
  fragile point.

## Remaining known concerns

- Cancellation watchers use private `http.client` internals (see Pass 4).
- `web_fetch` validation is per-hop; TOCTOU DNS rebinding between check and connect is
  not defended.
- UI code paths (provider dialog, model selection) have no automated GTK tests in this
  environment; they are compile-checked and covered at the service layer only.
- API endpoints that neither accept `stream_options` nor include usage will always show
  estimated (never exact) counts — by design.
