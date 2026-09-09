# Development

Use Python 3.11 or newer with uv from the repository root:

```bash
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run --group docs mkdocs build --strict
uv build
```

The source is under `src/ridge`, deterministic tests under `tests`, and public
documentation under `docs`. Read the root contributor instructions and
[architecture](architecture.md) before changing contracts. The public checkout
is sufficient; maintainer-local planning or environment files are not required.

Edit ephemeral helpers in `src/ridge/backends/_scripts` as ordinary Python source;
linting and type checking cover these files. Keep them standard-library-only and
safe to import without dispatch. After changing source delivery, verify that the
built wheel contains the helpers and exercise an installed package outside the
checkout, as well as the affected Docker/SSH transports.

For material changes, also run bounded realistic examples through CLI/MCP and
inspect outputs and side effects. Docker needs a running daemon and a worker
image with Python 3.11+. SSH needs an existing POSIX account, Python 3.11+,
noninteractive authentication, and verified host keys. S3 needs an explicitly authorized
disposable bucket prefix and ambient credentials. Mocks do not replace those
checks; report unavailable acceptance separately from passing unit tests.

When testing Ridge authorization through MCP, allow the request to reach Ridge
and inspect its authorization error and downstream side effects. A host approval
denial tests the host's gate, not Ridge's policy enforcement.

Follow the contributor instructions' documentation-impact review with each change:
update current contracts in their owning pages, consolidate repeated explanations,
and preserve actionable safety and recovery guidance. Review the relevant skills
and harness instructions too. The [delegation example](examples/delegation.md)
has process-level regression coverage; the README's scope grant is exercised
against a disposable workspace, not checked only for matching text.

Client adapters and plugin templates live in `integrations/`. The plugin builder
copies `skills/` from its canonical source; do not edit generated skill copies.
Run `integrations/agent_demo.py` for model-driven client acceptance separately
from ordinary pytest. That workflow returns newly issued scope handles through
the model conversation, so use disposable authority and approved provider data
handling. Never persist its raw responses. Framework examples use isolated
dependencies; `tests/test_integrations.py` covers launch configuration, bundle
contents, overwrite refusal, and the host lifecycle without model credentials.

Never use valuable data for destructive conformance or whole-tree replacement
tests. Create unique disposable roots/prefixes, bound payloads and time, and
clean up only artifacts created by your run. For S3, also inspect unfinished
multipart uploads and account for object versioning when cleaning up.

Before publication, inspect the intended source snapshot, built source/wheel
archives, and generated site for private context, job state, credentials, and
scratch artifacts. `.gitignore` helps normal staging but does not remove already
tracked files or sanitize arbitrary archives. Keep general runtime requirements
and reproducible development commands public.

## Continuous integration

The `Tests` GitHub Actions workflow runs on pull requests to `main`, pushes to
`main`, manual runs, and calls from the release workflow. It installs locked
dependencies and runs the deterministic test suite on Linux with Python 3.11–3.14
and on macOS with Python 3.14. A separate
Linux job runs lint, formatting, type checks, and wheel/source-distribution builds.
The repository's Actions tab reports workflow status and individual check results.

These checks do not require cloud credentials or provision external services.
They do not replace realistic Docker, SSH, or S3 acceptance when those workflows
change. Documentation is checked and published by the separate workflow below.

## Documentation publishing

The `Documentation` GitHub Actions workflow builds the MkDocs site on pull
requests to `main`, pushes to `main`, and manual runs. It uses Python 3.14 and
the documentation dependencies in `uv.lock`; a stale lockfile or a strict-build
warning fails the build. All changes trigger the check because the generated
Python API documentation also depends on source code.

After a successful build on `main`, the workflow publishes the `site/` artifact
to [GitHub Pages](https://vasinov.github.io/ridge-core/). Pull requests and manual
runs on other branches only build. Generated HTML is not committed. Deployment
uses the built-in `GITHUB_TOKEN` and OIDC, with deployment permissions limited
to the deploy job; no personal token or repository secret is required.

The deployment job links to the published site. Build failures appear in
**Build documentation**. If **Deploy documentation** fails, check that Pages uses
GitHub Actions as its source, the `github-pages` environment permits `main`, and
required deployment reviews and workflow permissions are satisfied.

## Versioning and releases

Ridge starts at **0.1.0**. `pyproject.toml` is the version source; uv maintains
the corresponding project entry in `uv.lock`. Git tags use `vX.Y.Z` and GitHub
Releases and PyPI distributions refer to that same source snapshot.

During 0.x, patch releases contain compatible fixes; minor releases contain new
features or breaking changes. Every breaking change needs explicit upgrade notes.
At 1.0, Ridge commits to a stable public contract and standard
[Semantic Versioning](https://semver.org/): major for breaking changes, minor for
compatible features, patch for compatible fixes. SemVer itself leaves 0.x unstable;
the stricter patch rule is Ridge's policy. The release command accepts final
three-component versions only; prerelease automation is not yet supported.

Compatibility covers documented Python/provider APIs, CLI behavior, MCP tools and
schemas, configuration, and persisted workspace state. Private implementation
details are outside that promise. During 0.x a breaking minor release may require
a new state directory instead of migration; describe the requirement, preserve old
state, and never delete user data as an upgrade step. Unpublished development
snapshots have no compatibility guarantee.

### Prepare the release

Review the complete public history since the previous release tag, including
merged work, and the aggregate diff. Select the latest published version tag
reachable from `main`, verifying that GitHub and PyPI agree; do not use a failed
publication's tag as the baseline. For example, after confirming `v0.1.0`:

```bash
git log --reverse --format='%h %s' v0.1.0..HEAD
git diff --stat v0.1.0..HEAD
git diff v0.1.0..HEAD
```

For the first release, review all public history and the current supported
workflows. Agents should draft `CHANGELOG.md` from that evidence, then check every
important change against the notes. Group by user impact: capabilities, fixes,
compatibility and upgrade requirements, and limitations. Consolidate incremental
implementation commits, omit internal churn, and distinguish verified support
from untested integrations. Do not copy private planning notes or merely generate
a list of commit subjects. Maintainers review the notes before publishing.

Add one nonempty `## X.Y.Z` section for the intended version, newest first. Commit
and integrate the reviewed notes, documentation, and feature changes before running
the release command. Keep historical entries; the workflow extracts only the
selected section as the GitHub Release body. The initial `0.1.0` entry remains a
draft until the first tag is published.

Review package metadata, both archives and source history for unintended private
content. Run relevant realistic acceptance for changes since the last release;
the release checks do not replace backend acceptance. The release workflow runs
the test matrix, quality checks and strict documentation build, builds once, checks
metadata, and installs both wheel and source distribution with fresh dependency
resolution from PyPI outside the checkout. Installed smoke checks exercise CLI
copy, MCP discovery/read, the package version, typing marker, and bundled helper
sources. They do not claim live Docker, SSH, S3, or agent-client acceptance.

The README and install guide use uv to install `ridge-core`; before first
publication their source-checkout fallback remains usable. The PyPI badge starts
reporting a version once publication succeeds. The documentation site continues
to track `main`, so it may describe unreleased changes; a release's tagged Markdown
and bundled notes describe that version. Reassess the fallback at first publication.

### One-time publishing setup

Install uv and the GitHub CLI (`gh`), authenticate Git and `gh` for
`vasinov/ridge-core`, and ensure GitHub Actions can run. The publishing workflow
filename is **`.github/workflows/workflow.yml`**, matching the PyPI publisher:

- Owner: `vasinov`; repository: `ridge-core`.
- Workflow: `workflow.yml`.
- Environment: `pypi` is recommended. A publisher configured with `(Any)` also
  accepts this workflow's `pypi` environment.

Create the GitHub `pypi` environment and restrict its deployment tags to `v*`.
For a tighter binding, set that same environment name in PyPI's pending publisher.
Required environment reviewers are optional; if configured, approve the deployment
in Actions when releasing. A pending publisher does not reserve the project name.
See [PyPI's first-project instructions](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/).
The workflow uses [uv Trusted Publishing](https://docs.astral.sh/uv/guides/integration/github/#publishing-to-pypi)
with a short-lived OIDC credential; no PyPI API token secret is needed.

### Run and observe

From the clean, integrated `main` checkout with its own uv environment:

```bash
uv sync --locked --group docs
uv run --no-sync python scripts/release.py 0.1.0 --dry-run
# After reviewing the preview and authorizing publication:
uv run --no-sync python scripts/release.py 0.1.0
```

The dry run reads the remote refs and previews outgoing commits and notes without
changing files or refs; it does not run tests or prove PyPI authentication. The
actual command holds the repository integration lock, checks the clean branch,
remote destination, forward-only version, and unused tag, bumps the version when
necessary, runs checks, and commits only version files. The first release keeps
the existing 0.1.0 version without an empty commit. It creates an annotated tag
and atomically pushes only `main` and that tag; diverged remote `main` is rejected.
Branch/tag protection must permit this maintainer operation; do not bypass it.

The command watches the tag's Actions run and reports success only after both
PyPI and GitHub publication succeed. The tag workflow validates that the version
matches and the commit belongs to `main`. Publishing consumes the verified wheel
and source archive, then uploads those same files and reviewed notes to a GitHub
Release. Only the PyPI job has OIDC permission; only the GitHub Release job has
repository write permission.

To rehearse Actions without publishing, manually run **Release** on `main` in
GitHub Actions. It runs verification and retains the built artifacts but skips
both publication jobs. This requires the workflow to have been pushed already.
After actual publication, verify a fresh `uv pip install ridge-core==X.Y.Z`, the
PyPI listing/badge, GitHub Release notes and assets, and documentation links.

### Recover a stopped release

Git, PyPI, and GitHub publication are separate durable steps. The command never
resets your checkout, rolls back uploaded packages, or moves release tags.

- Before tagging: inspect retained changes, correct the failure, then commit and
  integrate reviewed changes before retrying the same target version.
- Tag created but push failed: inspect local and remote refs. If the intended
  commit and unused remote tag are unchanged, retry only
  `git push --atomic origin main:refs/heads/main refs/tags/vX.Y.Z`. If remote `main`
  advanced or the source needs correction, resolve the intended source before
  publishing; do not force-push or move a published tag.
- Tag pushed but checks failed: inspect the Actions logs. Retry transient failures
  on that exact source. Code corrections require a new commit and version/tag.
- PyPI upload partially completed, or GitHub failed after PyPI succeeded: use
  **Re-run failed jobs**, or `gh run rerun RUN_ID --failed`. This reuses the original
  retained build artifacts. uv's hash-aware duplicate check skips identical
  uploads and refuses differing content; GitHub publication can finish its draft
  and replace the workflow-owned assets with those same files.

Do not rerun all jobs after any upload: rebuilding can produce different bytes.
Artifacts are retained for 30 days. If they expire, recover the exact original
files and verify their hashes against published files; do not silently rebuild
an already published version. A bad published version needs a new patch or minor
release as appropriate; consider yanking it on PyPI separately. If the local
watch times out or needs an environment approval, inspect the existing run rather
than launching another release. Exit 75 means integration lock contention: wait
and retry.
