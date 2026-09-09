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
`main`, and manual runs. It installs locked dependencies and runs the deterministic
test suite on Linux with Python 3.11–3.14 and on macOS with Python 3.14. A separate
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
