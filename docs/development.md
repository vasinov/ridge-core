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

Ridge starts at **0.1.0**. `pyproject.toml` owns the version; uv updates its entry
in `uv.lock`. Tags use `vX.Y.Z`. During 0.x, patches contain compatible fixes;
minor releases contain features or breaking changes with explicit upgrade notes.
At 1.0, standard [Semantic Versioning](https://semver.org/) applies: major for
breaking changes, minor for compatible features, patch for compatible fixes.
The release command supports final three-component versions only.

Compatibility covers documented Python/provider APIs, CLI behavior, MCP tools and
schemas, configuration, and persisted workspace state. Private implementation and
unpublished snapshots have no compatibility guarantee. A breaking 0.x minor may
require a new state directory instead of migration; document that requirement and
preserve old user state.

### Prepare

Review every commit and the aggregate diff since the last successfully published
tag, including merged work. Confirm the baseline agrees on GitHub and PyPI; a
failed release's tag is not a published baseline. For example:

```bash
git log --reverse --format='%h %s' v0.1.0..HEAD
git diff v0.1.0..HEAD
```

For the first release, review all public history and current supported workflows.
Agents draft a nonempty `## X.Y.Z` section in `CHANGELOG.md`, newest first, grouping
important features, fixes, upgrade requirements, and limitations by user impact.
Check completeness against the entire range and verify claims against docs/tests;
omit internal churn and private notes. Preserve historical entries. Review, commit,
and integrate the notes and related changes before releasing.

Inspect source history and package contents for unintended private data, and run
realistic acceptance for affected workflows. Release CI runs the test matrix,
quality/strict-docs checks, metadata validation, and fresh wheel/source installs
with CLI copy and MCP read checks. Those checks do not replace backend or actual
agent-client acceptance.

### Configure once

Install uv and `gh`, authenticate Git/GitHub for `vasinov/ridge-core`, and enable
Actions. Configure the PyPI Trusted Publisher with owner `vasinov`, repository
`ridge-core`, workflow `workflow.yml`, and environment `pypi`. The GitHub `pypi`
environment should allow only **tags** matching `v*`; required reviewers are
optional. PyPI's `(Any)` environment works but does not enforce that binding.
Trusted Publishing needs no stored PyPI token, and a pending publisher does not
reserve the package name. See [PyPI setup](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/).

### Release

From clean, integrated `main`:

```bash
uv sync --locked --group docs
uv run --no-sync python scripts/release.py 0.1.0 --dry-run
# After reviewing the preview and authorizing publication:
uv run --no-sync python scripts/release.py 0.1.0
```

The preview reads remote refs and prints outgoing commits and notes; it does not
run tests or verify PyPI authentication. The actual command holds the integration
lock, checks the branch/remote/version/tag, bumps and checks the version, commits
only version files, and atomically pushes `main` and its annotated release tag.
The first release keeps the existing `0.1.0` without an empty commit. Branch/tag
protection must permit this operation.

CI validates the tagged source, publishes its verified artifacts to PyPI, then
publishes those same files and reviewed notes as a GitHub Release. The command
watches that run and reports both publications. To rehearse without publishing,
manually run **Release** on `main` in Actions after the workflow is pushed.
After publication, check a fresh `uv pip install ridge-core==X.Y.Z`, the PyPI
badge/listing, GitHub notes/assets, and documentation links. Remove the temporary
pre-PyPI source-install fallback when preparing the first release.

### Recover

Failures preserve local state and completed publication steps. Never move a
published tag, force-push to recover, or rebuild an uploaded version.

- **Before tagging:** inspect and correct retained changes, commit/integrate them,
  then retry the target version. Exit 75 means lock contention: wait and retry.
- **Push failed:** verify local/remote refs still identify the intended source and
  the remote tag is unused, then retry
  `git push --atomic origin main:refs/heads/main refs/tags/vX.Y.Z`. Resolve diverged
  source intent before publishing.
- **CI failed:** rerun transient failures on the same source. Source corrections
  after a published tag require a new commit and version/tag.
- **Partial upload or GitHub failure:** use **Re-run failed jobs** or
  `gh run rerun RUN_ID --failed` to reuse the original artifacts. uv skips identical
  files by hash; GitHub completes its draft and workflow-owned assets.

Do not rerun all jobs after uploading: rebuilt bytes may differ. Artifacts expire
after 30 days; recover exact originals and verify published hashes if necessary.
A bad release needs a new version, with PyPI yanking considered separately. If the
watch times out or awaits approval, inspect the existing run rather than releasing
again.
