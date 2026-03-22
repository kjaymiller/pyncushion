"""Tests for pin_versions — a tool that pins dependencies in pyproject.toml to installed versions.

Tests cover parsing dependency strings, detecting version constraints,
pinning individual and grouped dependencies, fetching versions from PyPI,
and the end-to-end workflow via async_main.
"""

import json
import subprocess
from pathlib import Path

import httpx
import pytest
import tomlkit

from pin_versions.pin_versions import (
    async_main,
    collect_unpinned_deps,
    extract_package_name,
    get_installed_versions,
    get_latest_version,
    has_version_constraint,
    pin_dependency,
    resolve_missing_versions,
)


class TestExtractPackageName:
    """Tests for extract_package_name(dep: str) -> str.

    Given a dependency string that may contain extras (e.g. [argon2]) and/or
    version specifiers (>=, ==, etc.), should return only the bare package name.
    """

    @pytest.mark.parametrize(
        "dep, expected",
        [
            ("requests", "requests"),
            ("requests>=2.0", "requests"),
            ("requests==2.28.0", "requests"),
            ("requests~=2.28", "requests"),
            ("django[argon2]", "django"),
            ("django[argon2]>=4.0", "django"),
        ],
    )
    def test_extracts_name_from_various_formats(self, dep, expected):
        """Should strip version specifiers and extras to return the bare name."""
        assert extract_package_name(dep) == expected


class TestHasVersionConstraint:
    """Tests for has_version_constraint(dep: str) -> bool.

    Returns True when the dependency string contains any version operator
    (>=, ==, <=, !=, ~=, >), False otherwise. Extras like [argon2] alone
    should not be treated as version constraints.
    """

    @pytest.mark.parametrize(
        "dep, expected",
        [
            ("requests", False),
            ("django[argon2]", False),
            ("requests>=2.0", True),
            ("requests==2.28.0", True),
            ("requests!=2.0", True),
            ("requests~=2.28", True),
        ],
    )
    def test_detects_constraints(self, dep, expected):
        """Should return True only when a version operator is present."""
        assert has_version_constraint(dep) == expected


class TestPinDependency:
    """Tests for pin_dependency(dep, versions, operator, failed) -> str.

    Given a dependency string, a {name: version} mapping, and an operator,
    returns the dep string with the version appended. Skips already-pinned deps,
    normalizes underscores to hyphens for lookup, and appends to the failed list
    when no version is found.
    """

    def test_pins_with_installed_version(self):
        """'requests' with versions={'requests': '2.28.0'} and operator '==' returns 'requests==2.28.0'."""
        failed = []
        assert pin_dependency("requests", {"requests": "2.28.0"}, "==", failed) == "requests==2.28.0"
        assert failed == []

    def test_preserves_extras(self):
        """'django[argon2]' pins to 'django[argon2]==4.2.0', preserving the extras bracket."""
        failed = []
        assert pin_dependency("django[argon2]", {"django": "4.2.0"}, "==", failed) == "django[argon2]==4.2.0"

    def test_skips_already_pinned(self):
        """'requests>=2.0' is returned unchanged even when a newer version is available."""
        failed = []
        assert pin_dependency("requests>=2.0", {"requests": "2.28.0"}, "==", failed) == "requests>=2.0"

    def test_records_missing_version(self):
        """'unknown-pkg' with empty versions dict is left unpinned and added to the failed list."""
        failed = []
        assert pin_dependency("unknown-pkg", {}, "==", failed) == "unknown-pkg"
        assert failed == ["unknown-pkg"]

    def test_normalizes_underscores(self):
        """'my_package' matches versions key 'my-package' via underscore-to-hyphen normalization."""
        failed = []
        assert pin_dependency("my_package", {"my-package": "1.0.0"}, "==", failed) == "my_package==1.0.0"


class TestCollectUnpinnedDeps:
    """Tests for collect_unpinned_deps(data: dict) -> list[str].

    Scans project.dependencies, project.optional-dependencies, and
    dependency-groups for entries without version constraints. Returns
    normalized (lowercased, underscores replaced with hyphens) package names.
    """

    def test_from_all_sections(self):
        """Should collect unpinned deps from dependencies, optional-dependencies, and dependency-groups."""
        data = {
            "project": {
                "dependencies": ["requests", "flask>=2.0"],
                "optional-dependencies": {"dev": ["pytest"]},
            },
            "dependency-groups": {"test": ["coverage>=7.0", "hypothesis"]},
        }
        result = collect_unpinned_deps(data)
        assert set(result) == {"requests", "pytest", "hypothesis"}

    def test_normalizes_names(self):
        """Should normalize underscores to hyphens in collected names."""
        data = {"project": {"dependencies": ["my_package"]}}
        assert collect_unpinned_deps(data) == ["my-package"]

    def test_empty_data(self):
        """Should return an empty list when no dependency sections exist."""
        assert collect_unpinned_deps({}) == []


class TestGetInstalledVersions:
    """Tests for get_installed_versions(venv: Path) -> dict[str, str].

    Runs `uv pip list --format=json` and returns a {lowered_name: version} dict.
    When the venv path exists, passes --python to target that interpreter.
    """

    def test_with_existing_venv(self, tmp_path, monkeypatch):
        """With a valid .venv dir, passes --python <venv>/bin/python and lowercases package names."""
        venv = tmp_path / ".venv"
        venv.mkdir()
        (venv / "bin").mkdir()
        (venv / "bin" / "python").touch()

        pip_output = json.dumps([
            {"name": "requests", "version": "2.28.0"},
            {"name": "Flask", "version": "2.3.0"},
        ])

        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout=pip_output, stderr="")

        monkeypatch.setattr("pin_versions.pin_versions.subprocess.run", fake_run)
        result = get_installed_versions(venv)

        assert result == {"requests": "2.28.0", "flask": "2.3.0"}
        assert "--python" in calls[0]

    def test_without_venv(self, tmp_path, monkeypatch):
        """With a nonexistent venv path, omits --python and uses the default interpreter."""
        pip_output = json.dumps([{"name": "requests", "version": "2.28.0"}])

        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout=pip_output, stderr="")

        monkeypatch.setattr("pin_versions.pin_versions.subprocess.run", fake_run)
        get_installed_versions(tmp_path / "nonexistent")

        assert "--python" not in calls[0]


class TestGetLatestVersion:
    """Tests for get_latest_version(client, package_name) -> str | None.

    Makes a GET to https://pypi.org/pypi/{name}/json. Returns the version
    string on success (200) or raises httpx.HTTPStatusError on failure.
    """

    async def test_success(self, httpx_mock):
        """PyPI returns 200 with stable releases -> returns latest stable version."""
        httpx_mock.add_response(
            url="https://pypi.org/pypi/requests/json",
            json={
                "info": {"version": "3.0.0"},
                "releases": {"2.28.0": [], "3.0.0": []},
            },
        )

        async with httpx.AsyncClient() as client:
            assert await get_latest_version(client, "requests") == "3.0.0"

    async def test_filters_prereleases_by_default(self, httpx_mock):
        """When prereleases=False (default), pre-release versions are excluded."""
        httpx_mock.add_response(
            url="https://pypi.org/pypi/requests/json",
            json={
                "info": {"version": "3.0.0rc1"},
                "releases": {"2.28.0": [], "3.0.0rc1": [], "2.31.0": []},
            },
        )

        async with httpx.AsyncClient() as client:
            assert await get_latest_version(client, "requests") == "2.31.0"

    async def test_includes_prereleases_when_requested(self, httpx_mock):
        """When prereleases=True, the info.version (which may be a pre-release) is returned."""
        httpx_mock.add_response(
            url="https://pypi.org/pypi/requests/json",
            json={
                "info": {"version": "3.0.0rc1"},
                "releases": {"2.28.0": [], "3.0.0rc1": []},
            },
        )

        async with httpx.AsyncClient() as client:
            assert await get_latest_version(client, "requests", prereleases=True) == "3.0.0rc1"

    async def test_falls_back_to_info_version_when_no_stable(self, httpx_mock):
        """When all releases are pre-releases, falls back to info.version."""
        httpx_mock.add_response(
            url="https://pypi.org/pypi/requests/json",
            json={
                "info": {"version": "1.0.0a1"},
                "releases": {"1.0.0a1": [], "1.0.0b1": []},
            },
        )

        async with httpx.AsyncClient() as client:
            assert await get_latest_version(client, "requests") == "1.0.0a1"

    async def test_not_found(self, httpx_mock):
        """PyPI returns 404 for an unknown package -> raises httpx.HTTPStatusError."""
        httpx_mock.add_response(
            url="https://pypi.org/pypi/nonexistent/json",
            status_code=404,
        )

        async with httpx.AsyncClient() as client:
            with pytest.raises(httpx.HTTPStatusError):
                await get_latest_version(client, "nonexistent")


class TestResolveMissingVersions:
    """Tests for resolve_missing_versions(client, missing) -> dict[str, str].

    Concurrently fetches latest versions for a list of package names.
    Returns a dict of {name: version} on success; raises on any failed lookup.
    """

    async def test_resolves_all_available(self, httpx_mock):
        """['requests', 'flask'] -> {'requests': '2.31.0', 'flask': '3.0.0'}."""
        httpx_mock.add_response(
            url="https://pypi.org/pypi/requests/json",
            json={"info": {"version": "2.31.0"}, "releases": {"2.31.0": []}},
        )
        httpx_mock.add_response(
            url="https://pypi.org/pypi/flask/json",
            json={"info": {"version": "3.0.0"}, "releases": {"3.0.0": []}},
        )

        async with httpx.AsyncClient() as client:
            result = await resolve_missing_versions(client, ["requests", "flask"])
        assert result == {"requests": "2.31.0", "flask": "3.0.0"}

    async def test_raises_on_missing_package(self, httpx_mock):
        """A 404 from PyPI propagates as httpx.HTTPStatusError."""
        httpx_mock.add_response(
            url="https://pypi.org/pypi/nonexistent/json",
            status_code=404,
        )

        async with httpx.AsyncClient() as client:
            with pytest.raises(httpx.HTTPStatusError):
                await resolve_missing_versions(client, ["nonexistent"])


class TestAsyncMain:
    """Integration tests for async_main(operator, pyproject, venv, fix, prereleases).

    Exercises the full workflow: reading pyproject.toml, resolving versions,
    pinning across all dependency sections, and writing (or skipping) the result.
    Uses a temp-dir pyproject.toml and mocked version lookups.
    """

    @pytest.fixture
    def sample_pyproject(self, tmp_path):
        """Create a sample pyproject.toml with unpinned and pinned deps."""
        content = tomlkit.dumps({
            "project": {
                "dependencies": ["requests", "flask>=2.0"],
                "optional-dependencies": {"dev": ["pytest"]},
            },
            "dependency-groups": {"test": ["coverage"]},
        })
        path = tmp_path / "pyproject.toml"
        path.write_text(content)
        return path

    @pytest.fixture
    def mock_versions(self):
        """Version mapping for test packages."""
        return {"requests": "2.28.0", "pytest": "7.4.0", "coverage": "7.3.0"}

    async def test_pins_all_sections(self, sample_pyproject, mock_versions, tmp_path, monkeypatch):
        """Writes pinned versions to all three sections; leaves already-constrained deps untouched."""
        monkeypatch.setattr("pin_versions.pin_versions.get_installed_versions", lambda _: mock_versions)
        await async_main("==", str(sample_pyproject), str(tmp_path / ".venv"), True)

        data = tomlkit.loads(sample_pyproject.read_text())
        assert data["project"]["dependencies"][0] == "requests==2.28.0"
        assert data["project"]["dependencies"][1] == "flask>=2.0"
        assert data["project"]["optional-dependencies"]["dev"][0] == "pytest==7.4.0"
        assert data["dependency-groups"]["test"][0] == "coverage==7.3.0"

    async def test_dry_run_does_not_write(self, sample_pyproject, mock_versions, tmp_path, monkeypatch):
        """With fix=False and unpinned deps, pyproject.toml is unchanged and SystemExit(1) is raised."""
        original = sample_pyproject.read_text()

        monkeypatch.setattr("pin_versions.pin_versions.get_installed_versions", lambda _: mock_versions)
        with pytest.raises(SystemExit):
            await async_main("==", str(sample_pyproject), str(tmp_path / ".venv"), False)

        assert sample_pyproject.read_text() == original

    async def test_dry_run_passes_when_all_pinned(self, tmp_path, monkeypatch):
        """With fix=False and all deps already pinned, no SystemExit is raised."""
        content = tomlkit.dumps({
            "project": {
                "dependencies": ["requests==2.28.0", "flask>=2.0"],
            },
        })
        path = tmp_path / "pyproject.toml"
        path.write_text(content)

        monkeypatch.setattr("pin_versions.pin_versions.get_installed_versions", lambda _: {})
        await async_main("==", str(path), str(tmp_path / ".venv"), False)

    async def test_fix_writes(self, sample_pyproject, mock_versions, tmp_path, monkeypatch):
        """With fix=True, pyproject.toml is updated with pinned versions."""
        original = sample_pyproject.read_text()

        monkeypatch.setattr("pin_versions.pin_versions.get_installed_versions", lambda _: mock_versions)
        await async_main("==", str(sample_pyproject), str(tmp_path / ".venv"), True)

        assert sample_pyproject.read_text() != original

    async def test_custom_operator(self, sample_pyproject, mock_versions, tmp_path, monkeypatch):
        """With operator='>=' pins as 'requests>=2.28.0' instead of 'requests==2.28.0'."""
        monkeypatch.setattr("pin_versions.pin_versions.get_installed_versions", lambda _: mock_versions)
        await async_main(">=", str(sample_pyproject), str(tmp_path / ".venv"), True)

        data = tomlkit.loads(sample_pyproject.read_text())
        assert data["project"]["dependencies"][0] == "requests>=2.28.0"

    async def test_exits_on_missing_versions(self, sample_pyproject, tmp_path, monkeypatch, httpx_mock):
        """With an empty versions dict and PyPI failure, all deps fail to resolve and SystemExit(1) is raised."""
        monkeypatch.setattr("pin_versions.pin_versions.get_installed_versions", lambda _: {})
        # The first lookup raises HTTPStatusError, which is caught and aborts the batch
        httpx_mock.add_response(status_code=404)
        with pytest.raises(SystemExit):
            await async_main("==", str(sample_pyproject), str(tmp_path / ".venv"), False)

    async def test_fetches_from_pypi_for_uninstalled(self, sample_pyproject, tmp_path, monkeypatch, httpx_mock):
        """Uninstalled package 'coverage' is fetched from PyPI and pinned to '7.3.0'."""
        installed = {"requests": "2.28.0", "pytest": "7.4.0"}
        monkeypatch.setattr("pin_versions.pin_versions.get_installed_versions", lambda _: installed)

        httpx_mock.add_response(
            url="https://pypi.org/pypi/coverage/json",
            json={"info": {"version": "7.3.0"}, "releases": {"7.3.0": []}},
        )

        await async_main("==", str(sample_pyproject), str(tmp_path / ".venv"), True)

        data = tomlkit.loads(sample_pyproject.read_text())
        assert data["dependency-groups"]["test"][0] == "coverage==7.3.0"
