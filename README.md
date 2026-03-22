# pin-versions

A CLI tool and pre-commit hook that pins all unpinned dependencies in `pyproject.toml` to their currently installed versions.

## Why pin versions?

Unpinned dependencies (e.g. `requests` instead of `requests==2.31.0`) mean your project silently picks up whatever version happens to be newest at install time. This causes real problems:

- **Broken builds** -- a new release of a dependency can introduce breaking changes or bugs that suddenly fail your CI or production deploys, even though *your* code hasn't changed.
- **"Works on my machine"** -- different team members install at different times and get different versions, leading to bugs that are impossible to reproduce.
- **Non-reproducible deployments** -- deploying the same commit twice can produce different behavior if a dependency was updated between deploys.
- **Silent security risk** -- without knowing exactly what you're running, auditing your dependency tree for vulnerabilities is guesswork.

Pinning gives you control: upgrades happen when you choose, not when a package author publishes.

## Why some dependencies are left unpinned

`pin-versions` resolves versions by checking what is currently installed in your virtual environment. A dependency will remain unpinned if:

- **It isn't installed** -- the package appears in `pyproject.toml` but is not present in the target virtual environment (e.g. an optional dependency you haven't installed locally).
- **The package name doesn't match** -- the name in `pyproject.toml` differs from the distribution name (underscores vs hyphens, etc.) and can't be matched to an installed package.
- **PyPI lookup fails** (when using `--pin-latest`) -- if the network request to PyPI errors out, the package is left as-is rather than guessing.

In all of these cases, `pin-versions` reports the unpinned packages so you can address them. You can use `--pin-latest` to automatically fetch the latest version from PyPI for any package that isn't installed locally.

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

### Options

| Flag | Description |
|---|---|
| `--operator`, `-o` | Version pin operator (default: `==`). Supports `>=`, `~=`, etc. |
| `--pyproject`, `-p` | Path to `pyproject.toml` (default: `./pyproject.toml`) |
| `--venv` | Path to the virtual environment (default: `.venv`) |
| `--pin-latest` | Pin uninstalled packages to their latest version on PyPI |
| `--dry-run` | Preview changes without modifying the file |

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
