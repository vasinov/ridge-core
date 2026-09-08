# Ridge — AGENTS.md

## Mission

Ridge is an experimental resource abstraction layer for AI agents. It gives a
caller named resources with explicit capabilities while keeping backend
mechanics outside agent reasoning. Ridge is not an orchestrator, scheduler,
sandbox, or hosted control plane.

## Optional local context

After reading this file, check for `.agent-local/CONTEXT.md` relative to the
repository root containing this `AGENTS.md`, not the current working directory.
If present, read it and follow its routing to task-relevant local notes. Its
absence is normal: continue with public instructions and do not create it
automatically.

Local context supplements these rules; it must not override public project
contracts or authorize additional work. Surface conflicts before acting. Keep
private context out of tracked files and public outputs.

## Documentation map

Keep each document focused:

- `README.md`: public overview, installation, and first successful workflow;
- `docs/`: detailed user, provider, API, and security documentation;
- `docs/guides/`, resource guides, and frontend references: supported workflows;
- `docs/architecture.md`: architecture, ownership, and invariants;
- `AGENTS.md`: contributor and agent operating rules.

Do not duplicate implementation history across these files. Completed detail
belongs in tests and version history unless it remains necessary to explain a
current contract.

Before a significant architectural or cross-cutting change, read the relevant
sections of `docs/architecture.md` and the owning workflow guides, then confirm
the scope of the requested change.

For every feature addition, modification, or bug fix, review documentation impact
before implementation and recheck it against the verified result. Inspect the
existing owning docs and relevant README sections, examples, configuration,
CLI/MCP help, and API/provider references. Update affected claims and examples in
the same change; do not defer correctness to a later documentation pass. Update
`README.md` only for material user-facing changes. If no updates are needed,
briefly state why in the task handoff.

Prefer revising existing coverage over appending sections or creating new pages.
Consolidate overlapping explanations, compact repetitive prose, remove superseded
guidance, and link to one owning explanation where appropriate. Keep this review
scoped to affected workflows, not an unrelated documentation rewrite. Lead with
supported behavior and retain accurate, actionable limitations and safety warnings;
remove defensive repetition without overstating guarantees.

## Repository structure

- `src/ridge/`: application service, models, authorization, jobs, CLI, and MCP;
- `src/ridge/backends/`: provider mechanisms and shared remote helpers;
- `tests/`: deterministic contract and regression coverage;
- `docs/` and `mkdocs.yml`: user and extension documentation;
- `ridge.example.yaml`: portable example configuration;
- `pyproject.toml` and `uv.lock`: package metadata, tooling, and dependencies.

Public development workflows must work without maintainer-local instruction or
planning files. Keep credentials, personal infrastructure coordinates, and
temporary task state out of tracked instructions and documentation.

## Architectural rules

Record project-specific architecture, ownership boundaries, and invariants in
`docs/architecture.md` as they are established. Until then:

- generalize only when a concrete current use case requires it;
- prefer composition and explicit data over speculative framework machinery;
- keep policy and mechanism separate where doing so clarifies ownership;
- make authoritative state and side effects explicit;
- ensure claims about behavior are supported by tests or persisted evidence.

## Change process

Before implementing a new user workflow or materially extending one, perform a
design checkpoint with the user. Surface choices that materially affect goals,
semantics, invariants, failure behavior, compatibility, or scope; recommend one
path and wait for approval. Record accepted behavioral decisions in the owning
design or workflow document.

Routine bug fixes and behavior-preserving refactors do not require a checkpoint
unless they uncover an unresolved material decision.

For configuration changes, explicitly review `ridge.example.yaml` alongside the
configuration reference and loader tests. Update applicable fields, comments,
and interacting examples (including resource aliases and state placement), and
validate the example with the current loader. Keep the example portable; it need
not enumerate every option.

Treat Ridge as active-development software with no backward-compatibility
obligation unless the project documents establish one. When an internal or
persisted shape changes, update the current implementation, fixtures, and
documentation together. Do not retain migrations, deprecated aliases, schema
dispatch, dual representations, or wrappers solely for earlier development
versions.

Delete superseded paths and documentation once their replacement is proven.
Never delete user data as part of code cleanup.

## Verification

Add deterministic coverage for contracts, invariants, validation, failure
behavior, and regressions as those surfaces emerge. Keep validation testable
independently from external side effects where practical.

Use Python 3.11 or newer and the project's uv-managed environment. Docker, SSH,
and S3 acceptance additionally require the relevant runtime or service access;
see the resource guides for prerequisites. Use these canonical commands:

- setup: `uv sync`;
- tests: `uv run pytest`;
- lint: `uv run ruff check .`;
- formatting: `uv run ruff format --check .`;
- type checking: `uv run pyright`;
- documentation: `uv run --group docs mkdocs build --strict`;
- package build: `uv build`.

### Realistic acceptance

Before a material feature or cross-cutting change is complete, run bounded,
realistic scenarios through the canonical user workflow. Select them
contextually to falsify assumptions introduced by the change.

- Exercise every affected workflow and recheck relevant working capabilities.
- Inspect inputs, decisions, side effects, outputs, and failure behavior—not
  merely the exit code.
- Look for unsupported substitutions, contradictions, redundant work, and
  misleading presentation.
- Turn concrete defects into deterministic regression tests when feasible.
- Report the scenario, inspected behavior, outcome, and anything that could not
  run. Missing credentials, hardware, artifacts, or external availability means
  acceptance is incomplete; a mock does not replace the realistic exercise.

This is a judgment-driven acceptance discipline, not a fixed live-scenario
catalog or an expensive default test suite.

### External smoke-test safety

Use explicitly authorized disposable targets and unique per-run directories or
prefixes. Verify the target scope is empty before writing and bound payloads and
execution time. Use ambient credentials; never persist or print secrets. Verify
that endpoint overrides do not substitute a different service for the intended
backend. Do not change infrastructure or permissions merely to enable a test.

Clean up only exact artifacts created by the run, accounting for object
versioning where applicable. Inspect unfinished multipart uploads after S3
transfer tests, including failure and cancellation scenarios when in scope.
Report unavailable inspections and retained artifacts explicitly; successful
transfers alone do not prove failure cleanup.

## Working style

- Keep changes small and scoped to the requested task.
- Prefer the smallest implementation that satisfies the documented workflow.
- Keep comments for non-obvious reasoning, not narration or project history.
- Persist durable state rather than relying on model chat history.
- Preserve user changes in dirty worktrees and avoid destructive operations.
- Commit task-owned changes at coherent, verified stopping points unless the
  user asks otherwise. Inspect the staged diff, include only changes belonging
  to that checkpoint, and use a message that states the delivered behavior.
  Do not include unrelated user edits or create empty commits. Report checks
  and the commit briefly. Never push or rewrite history without explicit user
  authorization; a commit does not authorize publication.

### Task lifecycle: isolate, verify, integrate, clean up

1. **Isolate.** Inspect the path, branch, worktree list, and working tree status.
   Use a dedicated worktree and task branch from an identified committed base
   for each independent editing task; reuse a checkout already owned by the task.
   Read-only investigations need no new worktree; an explicit user choice of
   checkout takes precedence. Never relocate a running agent, switch its branch,
   or move, stash, stage, or commit its work. Worktrees do not isolate Git refs,
   ports, services, external targets, or shared notes. Editing helpers need
   separate worktrees or explicit file ownership; read-only helpers may share.
2. **Implement and verify.** Edit, check, and commit in the task worktree using
   the change and verification rules above. Ensure IDE tools and interpreters
   target that checkout, with its own environment. Do not copy credentials or
   rely on another checkout's mutable environment; ignored files and optional
   context are not automatically copied.
3. **Integrate.** Finishing includes local integration, without another approval
   round. Use the primary checkout unless another is designated; it must be clean
   and not in use for another editing task. Run `scripts/with_integration_lock.py`
   with the project's Python interpreter from that checkout, followed by `--`
   and a command or script covering the entire integration sequence. Its OS lock
   at `ridge-integration.lock` in Git's common directory is shared by worktrees.
   Hold it across status/target rechecks, integration, combined checks, cleanup,
   and any authorized push. Never delete the lock file or infer ownership from
   its existence; ownership ends when its process holders exit. Exit 75 means
   the 30-second wait elapsed: wait and retry, not an immediate handoff.
   Under the lock, recheck the branch, clean status, target HEAD, and incoming
   commits. Fast-forward when possible; otherwise merge without rewriting history.
   Review combined behavior even without conflicts and run appropriate checks.
   Resolve mechanical conflicts in scope; ask about conflicting intent or material
   decisions. On failure, preserve and report local state; do not push or reset
   others' work. All integration-checkout writers must participate in this lock.
4. **Clean up.** After successful integration and verification, leave the task
   checkout and remove its inactive worktree with `git worktree remove PATH`.
   First inspect tracked, untracked, and ignored files; preserve user data and
   needed evidence. Inspected disposable environments, build output, and caches
   may go with the worktree. Never force worktree removal. Then confirm no
   worktree uses the task branch and its work is integrated into the intended
   target, and delete it with `git branch -d BRANCH`. Do not retain integrated
   task branches merely as backups. If squash/cherry-pick integration makes normal
   deletion refuse, verify all changes are preserved and obtain explicit approval
   before forced branch deletion. A `codex/` prefix or clean status is not proof.
   Cleanup covers only the agent's own completed task unless the user authorizes
   a wider sweep. Do not delete remote branches, active tasks, or the primary
   checkout; retain unintegrated or unpreserved work and explain why.
5. **Report.** State the commit, checks, integration and cleanup results, and
   anything retained. Local integration and cleanup do not authorize a push;
   publication, force-pushing, and history rewriting require explicit permission.

### Evidence-first collaboration

- Treat contributor and user proposals as hypotheses, not instructions to
  rationalize. Check them against current contracts, code, and evidence.
- State material disagreement early and concretely, especially when proposed
  work repeats a failed evaluation, adds surface area without testing a product
  claim, or generalizes beyond a demonstrated need.
- Recommend against low-value work when the evidence supports doing so. Separate
  observed facts, inferences, and preferences so the user can challenge each.
- Do not manufacture objections or block routine progress to appear independent.
  The goal is calibrated judgment, not reflexive contrarianism.
