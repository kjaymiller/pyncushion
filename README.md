# pin-versions

A CLI tool and pre-commit hook that pins all unpinned dependencies in `pyproject.toml` to their currently installed versions.

## Why pin versions?

Unpinned dependencies (e.g. `requests` instead of `requests==2.31.0`) mean your project silently picks up whatever version happens to be newest at install time. This causes real problems:

- **Broken builds** -- a new release of a dependency can introduce breaking changes or bugs that suddenly fail your CI or production deploys, even though *your* code hasn't changed.
- **"Works on my machine"** -- different team members install at different times and get different versions, leading to bugs that are impossible to reproduce.
- **Non-reproducible deployments** -- deploying the same commit twice can produce different behavior if a dependency was updated between deploys.
- **Silent security risk** -- without knowing exactly what you're running, auditing your dependency tree for vulnerabilities is guesswork.

Pinning gives you control: upgrades happen when you choose, not when a package author publishes.

## How versions are resolved

`pin-versions` first checks what is currently installed in your virtual environment. For any unpinned package that isn't installed locally, the latest version is automatically fetched from PyPI.

A dependency will remain unpinned if:

- **The package name doesn't match** -- the name in `pyproject.toml` differs from the distribution name (underscores vs hyphens, etc.) and can't be matched to an installed package.
- **PyPI lookup fails** -- if the network request to PyPI errors out, the package is left as-is rather than guessing.

In all of these cases, `pin-versions` reports the unpinned packages so you can address them.

### Pre-release versions

When fetching versions from PyPI, only stable (non-pre-release) versions are considered by default. Pre-release versions (alpha, beta, release candidates, dev builds) are filtered out. If you need to pin to a pre-release version, pass the `--prereleases` flag:

```bash
pin-versions --prereleases --fix
```

If a package has *only* pre-release versions on PyPI, `pin-versions` will fall back to the latest available version regardless of the `--prereleases` flag.

## Installation

```bash
pip install pin-versions
```

Or with [uv](https://docs.astral.sh/uv/):

```bash
uv add pin-versions
```

## Usage

Run in a project directory with a `pyproject.toml` and a virtual environment:

```bash
pin-versions
```

This pins dependencies in `[project].dependencies`, `[project.optional-dependencies]`, and `[dependency-groups]`.

By default, `pin-versions` runs in dry-run mode — it shows what would change without modifying the file. Use `--fix` to apply the pins.

### Options

| Flag | Description |
|---|---|
| `--fix` | Apply changes to `pyproject.toml` (default is dry run) |
| `--operator`, `-o` | Version pin operator (default: `==`). Supports `>=`, `~=`, etc. |
| `--pyproject`, `-p` | Path to `pyproject.toml` (default: `./pyproject.toml`) |
| `--venv` | Path to the virtual environment (default: `.venv`) |
| `--prereleases` | Include pre-release versions when fetching from PyPI |

### Pre-commit hook

Add to your `.pre-commit-config.yaml`:

```yaml
repos:
  - repo: https://github.com/kjaymiller/pin-versions
    rev: v0.1.0
    hooks:
      - id: pin-versions
```

## Contributing

1. Fork the repo and clone it locally.
2. Create a virtual environment and install the project in editable mode:
   ```bash
   uv venv && uv pip install -e ".[dev]"
   ```
3. Create a branch for your changes:
   ```bash
   git checkout -b my-feature
   ```
4. Make your changes and ensure they work by running:
   ```bash
   pin-versions --dry-run
   ```
5. Open a pull request against `main`.
