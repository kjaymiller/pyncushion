"""Pin all dependencies in pyproject.toml to their currently installed versions.

Usage:
    uv run pin_versions.py [OPTIONS]
"""

# /// script
# requires-python = ">=3.10"
# dependencies = ["tomlkit", "click", "httpx", "rich"]
# ///

import asyncio
import json
import subprocess
from pathlib import Path

import click
import httpx
import tomlkit
from packaging.version import Version, InvalidVersion
from rich.console import Console, Group as RichGroup
from rich.table import Table
from rich.panel import Panel
from rich.text import Text

console = Console()


def get_installed_versions(venv: Path) -> dict[str, str]:
    """Get a mapping of package name -> installed version."""
    cmd = ["uv", "pip", "list", "--format=json"]
    if venv.exists():
        cmd += ["--python", str(venv / "bin" / "python")]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    packages = json.loads(result.stdout)
    return {pkg["name"].lower(): pkg["version"] for pkg in packages}


async def get_latest_version(client: httpx.AsyncClient, package_name: str, prereleases: bool = False) -> str:
    """Get the latest version of a package from PyPI.

    By default, only stable (non-prerelease) versions are considered.
    Set prereleases=True to include pre-release versions.
    """
    response = await client.get(f"https://pypi.org/pypi/{package_name}/json")
    response.raise_for_status()
    data = response.json()

    if prereleases:
        return data["info"]["version"]

    # Filter to stable versions only
    stable_versions = []
    for ver_str in data["releases"]:
        try:
            v = Version(ver_str)
            if not v.is_prerelease:
                stable_versions.append(v)
        except InvalidVersion:
            continue

    if not stable_versions:
        return data["info"]["version"]

    return str(max(stable_versions))


def extract_package_name(dep: str) -> str:
    """Extract the package name from a dependency string."""
    return dep.split("[")[0].split(">")[0].split("<")[0].split("=")[0].split("!")[0].split("~")[0].strip()


def has_version_constraint(dep: str) -> bool:
    """Check if a dependency string already has a version constraint."""
    return any(op in dep for op in [">=", "<=", "==", "!=", "~=", ">"])


async def resolve_missing_versions(
    client: httpx.AsyncClient,
    missing: list[str],
    prereleases: bool = False,
) -> dict[str, str]:
    """Fetch latest versions for all missing packages concurrently."""
    tasks = {name: get_latest_version(client, name, prereleases=prereleases) for name in missing}
    results = {}
    for name, coro in tasks.items():
        results[name] = await coro
    return results


def collect_unpinned_deps(data: dict) -> list[str]:
    """Collect all unpinned dependency names that are not in the installed versions."""
    deps = []

    if "project" in data:
        if "dependencies" in data["project"]:
            deps.extend(data["project"]["dependencies"])
        if "optional-dependencies" in data["project"]:
            for group_deps in data["project"]["optional-dependencies"].values():
                deps.extend(group_deps)

    if "dependency-groups" in data:
        for group_deps in data["dependency-groups"].values():
            deps.extend(group_deps)

    return [
        extract_package_name(dep).lower().replace("_", "-")
        for dep in deps
        if not has_version_constraint(dep)
    ]


def pin_dependency(dep: str, versions: dict[str, str], operator: str, failed: list[str]) -> str:
    """Add version pin to a dependency string if it does not already have one."""
    if has_version_constraint(dep):
        return dep

    name = extract_package_name(dep)
    normalized = name.lower().replace("_", "-")

    version = versions.get(normalized)
    if version:
        return f"{dep}{operator}{version}"

    failed.append(name)
    return dep


def _add_section_rows(table: Table, group: str, deps, versions: dict[str, str], operator: str, failed: list[str]) -> None:
    """Pin deps in a section and add rows to the unified table."""
    for i, dep in enumerate(deps):
        if has_version_constraint(str(dep)):
            continue
        name = extract_package_name(str(dep))
        deps[i] = pin_dependency(str(dep), versions, operator, failed)
        pinned = str(deps[i])
        if has_version_constraint(pinned):
            version_part = pinned[len(name):]
            style = "green"
        else:
            version_part = "[unpinned]"
            style = "yellow"
        table.add_row(group, name, Text(version_part, style=style))


async def async_main(operator: str, pyproject: str, venv: str, fix: bool, prereleases: bool = False):
    pyproject_path = Path(pyproject)
    data = tomlkit.loads(pyproject_path.read_text())
    versions = get_installed_versions(Path(venv))
    failed: list[str] = []
    unpinned = collect_unpinned_deps(data)
    errors: list[tuple[str, str]] = []

    missing = [name for name in unpinned if name not in versions]
    if missing:
        console.print(f"Looking up latest versions for [bold]{len(missing)}[/bold] uninstalled packages...")
        try:
            async with httpx.AsyncClient() as client:
                latest = await resolve_missing_versions(client, missing, prereleases=prereleases)
            versions.update(latest)
        except httpx.HTTPStatusError as e:
            errors.append(("PyPI lookup", f"HTTP {e.response.status_code} for {e.request.url}"))
        except httpx.RequestError as e:
            errors.append(("PyPI lookup", str(e)))

    # Summary header
    total_deps = len(unpinned)
    summary_table = Table(show_header=False, box=None, padding=(0, 1))
    summary_table.add_column("Label", style="bold", justify="right")
    summary_table.add_column("Value")
    summary_table.add_row("Total Unpinned:", str(total_deps))

    group_table = Table(show_header=False, box=None, padding=(0, 1))
    group_table.add_column("Group", style="bold", justify="left")
    group_table.add_column("Status")

    if "project" in data and "dependencies" in data["project"]:
        group_unpinned = [dep for dep in data["project"]["dependencies"] if not has_version_constraint(dep)]
        group_table.add_row("[project].dependencies:", f"{len(group_unpinned)} unpinned" if group_unpinned else "all pinned")

    if "project" in data and "optional-dependencies" in data["project"]:
        for group, deps in data["project"]["optional-dependencies"].items():
            group_unpinned = [dep for dep in deps if not has_version_constraint(dep)]
            group_table.add_row(f"[project.optional-dependencies].{group}:", f"{len(group_unpinned)} unpinned" if group_unpinned else "all pinned")

    if "dependency-groups" in data:
        for group, deps in data["dependency-groups"].items():
            group_unpinned = [dep for dep in deps if not has_version_constraint(dep)]
            group_table.add_row(f"[dependency-groups].{group}:", f"{len(group_unpinned)} unpinned" if group_unpinned else "all pinned")

    console.print(Panel(RichGroup(summary_table, group_table), title="Summary", title_align="left"))
    console.print()

    # Build unified table
    dep_table = Table(show_header=True, title_style="bold cyan")
    dep_table.add_column("Group", style="cyan")
    dep_table.add_column("Package", style="white")
    dep_table.add_column("Version", style="green")

    if "project" in data and "dependencies" in data["project"]:
        _add_section_rows(dep_table, "[project].dependencies", data["project"]["dependencies"], versions, operator, failed)

    if "project" in data and "optional-dependencies" in data["project"]:
        for group, deps in data["project"]["optional-dependencies"].items():
            _add_section_rows(dep_table, f"[project.optional-dependencies].{group}", deps, versions, operator, failed)

    if "dependency-groups" in data:
        for group, deps in data["dependency-groups"].items():
            _add_section_rows(dep_table, f"[dependency-groups].{group}", deps, versions, operator, failed)

    console.print(dep_table)

    # Error report
    if failed:
        for name in failed:
            errors.append(("Version not found", f"No version found for '{name}', left unpinned"))

    if errors:
        error_table = Table(title="Errors", show_header=True, title_style="bold red")
        error_table.add_column("Type", style="red")
        error_table.add_column("Detail", style="white")
        for err_type, detail in errors:
            error_table.add_row(err_type, detail)
        console.print()
        console.print(Panel(error_table, border_style="red"))

    # Summary
    console.print()
    if fix:
        pyproject_path.write_text(tomlkit.dumps(data))
        console.print(f"[bold green]Updated {pyproject_path}[/bold green]")
    elif unpinned:
        console.print("[bold red]Found unpinned dependencies. Use `[orange]pin-versions[/orange] --fix` to apply pins.[/bold red]")
        raise SystemExit(1)

    if failed:
        raise SystemExit(1)


@click.command()
@click.option("--operator", "-o", default="==", help="Version pin operator (e.g. ==, >=, ~=)")
@click.option("--pyproject", "-p", default="pyproject.toml", type=click.Path(exists=True), help="Path to pyproject.toml")
@click.option("--venv", default=".venv", type=click.Path(), help="Path to the project virtualenv")
@click.option("--fix", is_flag=True, default=False, help="Apply changes to pyproject.toml (default is dry run)")
@click.option("--prereleases", is_flag=True, default=False, help="Include pre-release versions when fetching from PyPI")
def main(operator: str, pyproject: str, venv: str, fix: bool, prereleases: bool):
    """Pin all unpinned dependencies in pyproject.toml to their installed versions."""
    asyncio.run(async_main(operator, pyproject, venv, fix, prereleases))


if __name__ == "__main__":
    main()
