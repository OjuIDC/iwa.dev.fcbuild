from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath
import shutil
import subprocess
import xml.etree.ElementTree as ET

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PROJECT_PATH = REPO_ROOT / "iwa.dev.fcbuild.pyproj"
SOLUTION_PATH = REPO_ROOT / "iwa.dev.fcbuild.sln"
MSBUILD_NS = "http://schemas.microsoft.com/developer/msbuild/2003"


def _project_root() -> ET.Element:
    return ET.parse(PROJECT_PATH).getroot()


def test_solution_includes_the_python_project() -> None:
    solution = SOLUTION_PATH.read_text(encoding="utf-8")
    assert '"iwa.dev.fcbuild", "iwa.dev.fcbuild.pyproj"' in solution
    assert "{F27EC330-3230-4FC2-8700-330323000001}.Debug|Any CPU.Build.0" in solution
    assert "{F27EC330-3230-4FC2-8700-330323000001}.Release|Any CPU.Build.0" in solution


def test_project_imports_visual_studio_python_targets() -> None:
    imports = _project_root().findall(f"{{{MSBUILD_NS}}}Import")
    assert len(imports) == 1
    assert imports[0].get("Project") == (
        "$(MSBuildExtensionsPath32)"
        r"\Microsoft\VisualStudio\v$(VisualStudioVersion)\Python Tools\Microsoft.PythonTools.targets"
    )


def test_project_does_not_directly_import_dotnet_compilation_targets() -> None:
    project = _project_root()
    imported_projects = [
        node.get("Project", "") for node in project.findall(f"{{{MSBUILD_NS}}}Import")
    ]
    incompatible_targets = (
        "Microsoft.Common.targets",
        "Microsoft.Common.CurrentVersion.targets",
        "Microsoft.CSharp.targets",
    )
    assert not any(name.endswith(incompatible_targets) for name in imported_projects)


def test_project_uses_a_repository_relative_existing_interpreter() -> None:
    interpreters = _project_root().findall(f".//{{{MSBUILD_NS}}}Interpreter")
    assert len(interpreters) == 1
    interpreter = interpreters[0]
    include = PureWindowsPath(interpreter.get("Include", ""))
    python_path = PureWindowsPath(interpreter.findtext(f"{{{MSBUILD_NS}}}InterpreterPath", ""))
    assert include == PureWindowsPath(".venv\\")
    assert python_path == PureWindowsPath("Scripts\\python.exe")
    assert not include.is_absolute() and not include.drive
    assert not python_path.is_absolute() and not python_path.drive


def test_project_defines_the_template_supported_core_compile_target() -> None:
    core_compile = _project_root().findall(f".//{{{MSBUILD_NS}}}Target[@Name='CoreCompile']")
    assert len(core_compile) == 1
    assert len(core_compile[0]) == 0
    assert core_compile[0].get("DependsOnTargets") is None


def test_project_keeps_pytest_test_explorer_configuration() -> None:
    project = _project_root()
    assert project.findtext(f"{{{MSBUILD_NS}}}PropertyGroup/{{{MSBUILD_NS}}}TestFramework") == "Pytest"
    assert project.findtext(f"{{{MSBUILD_NS}}}PropertyGroup/{{{MSBUILD_NS}}}UnitTestRootDirectory") == "tests"
    assert project.findtext(f"{{{MSBUILD_NS}}}PropertyGroup/{{{MSBUILD_NS}}}UnitTestPattern") == "test_*.py"
    assert project.findtext(f"{{{MSBUILD_NS}}}PropertyGroup/{{{MSBUILD_NS}}}StartupFile") == (
        r"scripts\check_documentation_consistency.py"
    )


def test_visual_studio_user_state_is_ignored_and_project_files_are_tracked() -> None:
    for path in (
        ".vs/VisualStudioState.json",
        ".venv/Scripts/python.exe",
        "obj/Debug/project.intermediate",
    ):
        ignored = subprocess.run(
            ["git", "check-ignore", "--quiet", "--no-index", path],
            cwd=REPO_ROOT,
            check=False,
        )
        assert ignored.returncode == 0, f"Expected {path} to be ignored"

    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "iwa.dev.fcbuild.sln", "iwa.dev.fcbuild.pyproj"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert tracked.returncode == 0, tracked.stderr
    assert set(tracked.stdout.splitlines()) == {"iwa.dev.fcbuild.sln", "iwa.dev.fcbuild.pyproj"}


def test_project_includes_only_existing_repository_files_and_excludes_removed_b1_paths() -> None:
    project = _project_root()
    included_paths: list[str] = []
    for tag in ("Compile", "Content"):
        for node in project.findall(f".//{{{MSBUILD_NS}}}{tag}"):
            inc = node.get("Include", "").strip()
            if inc:
                included_paths.append(inc)
                rel = Path(inc.replace("\\", "/"))
                assert (REPO_ROOT / rel).is_file(), f"Missing file in .pyproj {tag}: {inc}"

    removed_b1_and_b2 = {
        r"src\fc27_authorized_provider.py",
        r"src\fc27_authorized_transport.py",
        r"src\fc27_authorized_api.py",
        r"src\fc27_provider_contract.py",
        r"src\fc27_clubs.py",
        r"scripts\fc27_club_lookup.py",
        r"config\fc27_safe_adapter_bundle_manifest.json",
        r"tests\test_fc27_authorized_provider.py",
        r"tests\test_fc27_authorized_api.py",
        r"tests\test_fc27_api_ingestion.py",
        r"tests\test_fc27_clubs_compatibility.py",
        r"tests\test_fc27_safe_adapter_bundle_manifest.py",
        r"src\voice_gateway.py",
        r"voice\__init__.py",
        r"voice\stt_service.py",
        r"voice\tts_service.py",
        r"src\rtsp_frame_analyzer.py",
        r"src\ocr_parsing.py",
        r"host_agent\capture_agent.py",
        r"host_agent\install.ps1",
        r"host_agent\requirements_windows.txt",
        r"src\live_match_watcher.py",
        r"src\post_match_rundown.py",
        r"src\live_ws_server.py",
        r"src\fc27_mcp_server.py",
        r"src\mcp_retry.py",
        r"src\mcp_healthcheck.py",
        r"src\swarms_backend.py",
        r"src\model_selection.py",
        r"src\benchmark.py",
        r"src\tracing.py",
        r"scripts\model_health.py",
        r"scripts\ollama-init.sh",
        r"config\model_catalog.json",
        r"config\role_benchmark_matrix.json",
        r"Dockerfile.mcp",
        r"Dockerfile.stt",
        r"Dockerfile.tts",
        r"docker-compose.cpu.yml",
        r"docker-compose.gpu.yml",
        r"docker-compose.langfuse.yml",
        r"docker-compose.voice-gpu.yml",
        r"requirements-langfuse.txt",
        r"requirements-swarms.txt",
        r"docs\VOICE_CAPABILITIES.md",
        r"docs\MODEL_SELECTION.md",
        r"docs\ROLE_MODEL_BENCHMARK_MATRIX.md",
        r"src\approval.py",
        r"src\approval_store.py",
        r"src\audit_query.py",
        r"src\correlation.py",
        r"src\decision.py",
        r"src\dispatch\worker.py",
        r"src\dispatch\outbox_relay.py",
        r"src\fc27_coaching.py",
        r"src\graph.py",
        r"src\graph_api.py",
        r"src\hierarchy.py",
        r"src\identity\webauthn.py",
        r"src\lead_packages.py",
        r"src\roles.py",
        r"src\thread_ids.py",
        r"scripts\commissioner_decide.py",
    }
    assert removed_b1_and_b2.isdisjoint(included_paths)


def _find_visual_studio_msbuild() -> Path:
    if os.name != "nt":
        pytest.skip("Visual Studio MSBuild integration requires Windows")

    candidates: list[Path] = []
    msbuild_from_path = shutil.which("MSBuild.exe")
    if msbuild_from_path:
        candidates.append(Path(msbuild_from_path))

    program_files_x86 = os.environ.get("ProgramFiles(x86)")
    if program_files_x86:
        vswhere = Path(program_files_x86) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
        if vswhere.is_file():
            installations = subprocess.run(
                [
                    str(vswhere),
                    "-all",
                    "-products",
                    "*",
                    "-requires",
                    "Microsoft.Component.MSBuild",
                    "-property",
                    "installationPath",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            ).stdout.splitlines()
            for installation in installations:
                install_path = Path(installation.strip())
                candidates.append(install_path / "MSBuild" / "Current" / "Bin" / "MSBuild.exe")

    for candidate in candidates:
        if not candidate.is_file():
            continue
        install_path = candidate.parents[3]
        python_targets = install_path / "MSBuild" / "Microsoft" / "VisualStudio"
        if any(
            (python_targets / version / "Python Tools" / "Microsoft.PythonTools.targets").is_file()
            for version in ("v18.0",)
        ):
            return candidate

    pytest.skip("Visual Studio MSBuild with the Python workload is not installed")


def test_visual_studio_msbuild_builds_the_solution_when_available() -> None:
    msbuild = _find_visual_studio_msbuild()
    result = subprocess.run(
        [str(msbuild), "iwa.dev.fcbuild.sln", "/t:Build", "/v:minimal"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "MSB4057" not in output
