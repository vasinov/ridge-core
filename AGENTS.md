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
the scope of the requested change. Update the owning document when behavior or
an architectural decision changes. Update `README.md` only for material
user-facing changes.

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
