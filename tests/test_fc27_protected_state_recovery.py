"""Comprehensive durable host-rebuild, protected state recovery & maintenance tests (Scenarios 41-70)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
from typing import Any
import zipfile

import psycopg
import pytest

from src.fc27_backup_restore import (
    EXPECTED_MIGRATION_VERSION,
    FC27_DOMAIN_TABLES,
    create_fc27_backup,
    verify_fc27_backup_restore,
)
from src.fc27_community_provider import normalize_community_match_records
from src.fc27_match_results import ReconciliationTokenStore
from src.fc27_polling_worker import persist_polled_match_envelope
from src.fc27_privacy import EAST_COASTERS_CLUB_ID, pseudonymize_east_coasters_identifier
from src.fc27_protected_state import (
    CONTAINER_RUNTIME_UID,
    CONTAINER_SHARED_SECRET_GID,
    DEFAULT_MANIFEST_PATH,
    ProtectedStateError,
    ProtectedStatePermissionError,
    apply_restrictive_permissions,
    compute_alias_hmac_key_fingerprint,
    export_protected_state_bundle,
    import_protected_state_bundle,
    load_protected_state_manifest,
    main as protected_state_cli_main,
    parse_and_validate_service_status_output,
    run_recovery_readiness_checks,
    verify_container_secret_readability,
    verify_post_start_health,
    verify_postgres_volume_readiness,
    verify_restrictive_permissions,
)
from src.fc27_repository import (
    FC27AliasAutoPromotionPolicy,
    evaluate_and_auto_promote_alias_candidates,
    run_fc27_alias_maintenance,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SYNTHETIC_APP_ID = "900000000000000010"
SYNTHETIC_GUILD_ID = "900000000000000011"
SYNTHETIC_RESULTS_CH = "900000000000000012"
SYNTHETIC_CHAT_CH = "900000000000000013"
SYNTHETIC_OP_ROLE_ID = "900000000000000014"
SYNTHETIC_HMAC_KEY = b"synthetic-fc27-hmac-key-material-0123456789abcdef-test-only"


def _fake_acl_runner_factory(*, inject_bad_ace: bool = False):
    """Return a deterministic subprocess runner simulating Windows icacls ACL behavior."""
    state_by_target: dict[str, str] = {}

    def _runner(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[:2] == ["whoami", "/user"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout='"SYNTHETICHOST\\operator","S-1-5-21-1000-2000-3000-1001"\n', stderr=""
            )
        if cmd[0] == "icacls":
            target = cmd[1]
            if "/inheritance:r" in cmd:
                state_by_target[target] = (
                    f"{target} S-1-5-21-1000-2000-3000-1001:(F)\n"
                    f"         NT AUTHORITY\\SYSTEM:(F)\n"
                    f"         BUILTIN\\Administrators:(F)\n"
                    "Successfully processed 1 files; Failed processing 0 files\n"
                )
                return subprocess.CompletedProcess(cmd, 0, stdout="processed 1 file\n", stderr="")
            if inject_bad_ace:
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    stdout=(
                        f"{target} BUILTIN\\Users:(I)(RX)\n"
                        f"         NT AUTHORITY\\SYSTEM:(I)(F)\n"
                        "Successfully processed 1 files; Failed processing 0 files\n"
                    ),
                    stderr="",
                )
            default_out = (
                f"{target} SYNTHETICHOST\\operator:(F)\n"
                f"         NT AUTHORITY\\SYSTEM:(F)\n"
                f"         BUILTIN\\Administrators:(F)\n"
                "Successfully processed 1 files; Failed processing 0 files\n"
            )
            return subprocess.CompletedProcess(
                cmd, 0, stdout=state_by_target.get(target, default_out), stderr=""
            )
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="unsupported command")

    return _runner


_VALID_BOT_STATUS_STDOUT = "\n".join(
    [
        "DISCORD_BOT_ENABLED=YES",
        "DISCORD_APPLICATION_ID_CONFIGURED=YES",
        "DISCORD_TOKEN_CONFIGURED=YES",
        "DISCORD_ALLOWLISTS_CONFIGURED=YES",
        "DISCORD_ALLOWED_GUILD_COUNT=1",
        "DISCORD_ALLOWED_CHANNEL_COUNT=2",
        "DISCORD_ALLOWED_ROLE_COUNT=1",
        "DISCORD_PROVIDER_REQUESTS=0",
        "DISCORD_DATABASE_WRITE_ACCESS=NO",
        "DISCORD_DISPATCH_ENABLED=NO",
        "DISCORD_MAX_JOBS_PER_CYCLE=5",
        "DISCORD_LEASE_MINUTES=5",
        "DISCORD_SINGLE_FLIGHT=YES",
        "DISCORD_GLOBAL_COMMAND_COUNT=0",
        "DISCORD_GUILD_TOP_LEVEL_COMMAND_COUNT=14",
        "DISCORD_GROUP_SUBCOMMAND_COUNT=13",
    ]
)

_VALID_WORKER_STATUS_STDOUT = "\n".join(
    [
        "PROVIDER_ID=ea_fc27_community",
        "ALIAS_HMAC_KEY_CONFIGURED=YES",
        "PERSISTENCE_BEFORE_ANALYTICS=YES",
        "RAW_RESPONSE_RETENTION=NO",
        "RECOMMENDATIONS_ENABLED=NO",
        "APPROVAL_ENABLED=NO",
        "DISPATCH_ENABLED=NO",
        "WORKER_ENABLED=YES",
        "WORKER_RUNNING=YES",
        "CIRCUIT_STATE=CLOSED",
        "PROVIDER_REQUESTS=0",
    ]
)


def _fake_posix_chown_and_stat(
    *,
    default_uid: int = CONTAINER_RUNTIME_UID,
    default_gid: int = CONTAINER_SHARED_SECRET_GID,
    mode_override_file: int | None = None,
    mode_override_dir: int | None = None,
    support_chown: bool = True,
):
    """Simulate POSIX UID/GID ownership and mode bits cleanly across Windows and non-root CI."""
    owners: dict[str, tuple[int, int]] = {}

    def _chown(path: Path, uid: int, gid: int) -> None:
        if not support_chown:
            raise PermissionError("Operation not permitted without root")
        key = os.path.abspath(str(path))
        cur_u, cur_g = owners.get(key, (default_uid, default_gid))
        owners[key] = (
            cur_u if uid == -1 else uid,
            cur_g if gid == -1 else gid,
        )

    def _stat(path: Path) -> os.stat_result:
        real_st = path.lstat()
        key = os.path.abspath(str(path))
        eff_u, eff_g = owners.get(key, (default_uid, default_gid))
        is_dir = stat.S_ISDIR(real_st.st_mode)
        if is_dir and mode_override_dir is not None:
            eff_mode = stat.S_IFMT(real_st.st_mode) | mode_override_dir
        elif not is_dir and mode_override_file is not None:
            eff_mode = stat.S_IFMT(real_st.st_mode) | mode_override_file
        else:
            eff_mode = real_st.st_mode
        return os.stat_result(
            (
                eff_mode,
                real_st.st_ino,
                real_st.st_dev,
                real_st.st_nlink,
                eff_u,
                eff_g,
                real_st.st_size,
                int(real_st.st_atime),
                int(real_st.st_mtime),
                int(real_st.st_ctime),
            )
        )

    return _chown, _stat


def _populate_synthetic_repo(root: Path, *, include_override: bool = True) -> Path:
    """Populate an isolated temporary repository with tracked manifests/compose and synthetic protected state."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "ops").mkdir(parents=True, exist_ok=True)
    shutil.copy2(DEFAULT_MANIFEST_PATH, root / "ops" / "fc27-protected-state-manifest.json")
    for compose_name in (
        "docker-compose.yml",
        "docker-compose.community-worker.yml",
        "docker-compose.discord.yml",
        "docker-compose.ghcr.yml",
    ):
        src = REPO_ROOT / compose_name
        if src.is_file():
            shutil.copy2(src, root / compose_name)

    (root / ".env").write_text(
        "REDIS_PASSWORD=synthetic-redis-pass\n"
        "FC27_CLUB_ID=330323\n"
        "FC_PLATFORM=common-gen5\n",
        encoding="utf-8",
    )
    local_data = root / ".local-data"
    local_data.mkdir(parents=True, exist_ok=True)
    (local_data / "discord.env").write_text(
        f"DISCORD_ENABLED=false\n"
        f"FC27_DISCORD_BOT_ENABLED=false\n"
        f"DISCORD_APPLICATION_ID={SYNTHETIC_APP_ID}\n"
        f"FC27_DISCORD_ALLOWED_GUILD_IDS={SYNTHETIC_GUILD_ID}\n"
        f"FC27_DISCORD_ALLOWED_CHANNEL_IDS={SYNTHETIC_RESULTS_CH},{SYNTHETIC_CHAT_CH}\n"
        f"FC27_DISCORD_RESULTS_CHANNEL_ID={SYNTHETIC_RESULTS_CH}\n"
        f"FC27_DISCORD_TEAM_CHAT_CHANNEL_ID={SYNTHETIC_CHAT_CH}\n"
        f"FC27_DISCORD_IDENTITY_OPERATOR_ROLE_IDS={SYNTHETIC_OP_ROLE_ID}\n",
        encoding="utf-8",
    )
    (local_data / "polling.env").write_text(
        "FC27_COMMUNITY_POLLING_ENABLED=false\n"
        "FC27_ALIAS_AUTO_PROMOTION_ENABLED=false\n"
        "FC27_ALIAS_AUTO_PROMOTION_SHADOW_MODE=true\n",
        encoding="utf-8",
    )
    fp = compute_alias_hmac_key_fingerprint(SYNTHETIC_HMAC_KEY)
    (local_data / "runtime-deployment.json").write_text(
        json.dumps(
            {
                "compose_project": "fc27-full-stack",
                "migration_version": "0020",
                "grants_policy_version": 21,
                "alias_hmac_key_fingerprint_sha256": fp,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    if include_override:
        (local_data / "compose").mkdir(parents=True, exist_ok=True)
        (local_data / "compose" / "ghcr-promotion.yml").write_text(
            "services:\n"
            "  fc27-discord-bot:\n"
            "    image: ghcr.io/synthetic/fc27-discord-bot@sha256:" + ("a" * 64) + "\n",
            encoding="utf-8",
        )

    secrets_dir = root / "secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    secret_values = {
        "postgres_password": "synthetic-pg-password",
        "postgres_admin_uri": "postgresql://fc27:synthetic-pg-password@postgres:5432/fc27",
        "redis_password": "synthetic-redis-password",
        "fc27_migrator_password": "synthetic-migrator-password",
        "fc27_migrator_uri": "postgresql://fc27_migrator:synthetic-migrator-password@postgres:5432/fc27",
        "fc27_app_password": "synthetic-app-password",
        "fc27_app_uri": "postgresql://fc27_app:synthetic-app-password@postgres:5432/fc27",
        "fc27_dispatch_password": "synthetic-dispatch-password",
        "fc27_relay_password": "synthetic-relay-password",
        "fc27_match_results_writer_password": "synthetic-mrw-password",
        "fc27_poll_writer_password": "synthetic-pw-password",
        "fc27_poll_writer_uri": "postgresql://fc27_poll_writer:synthetic-pw-password@postgres:5432/fc27",
        "fc27_player_dev_writer_password": "synthetic-pdw-password",
        "fc27_player_dev_writer_uri": "postgresql://fc27_player_dev_writer:synthetic-pdw-password@postgres:5432/fc27",
        "fc27_discord_reader_password": "synthetic-dr-password",
        "fc27_discord_reader_uri": "postgresql://fc27_discord_reader:synthetic-dr-password@postgres:5432/fc27",
        "discord_bot_token": "synthetic-discord-bot-token-not-real",
    }
    for fname, content in secret_values.items():
        (secrets_dir / fname).write_text(content + "\n", encoding="utf-8")
    (secrets_dir / "fc27_alias_hmac_key").write_bytes(SYNTHETIC_HMAC_KEY)
    if os.name != "nt":
        os.chmod(root / ".env", 0o600)
        for dpath in (local_data, local_data / "compose", secrets_dir):
            if dpath.is_dir():
                os.chmod(dpath, 0o700)
        for fpath in [*local_data.rglob("*"), *secrets_dir.rglob("*")]:
            if fpath.is_file():
                os.chmod(fpath, 0o600)
    return root


class TestProtectedStateRecoveryScenarios:
    """Scenarios 41-70 covering manifest, export, import, ACLs, readiness, backup/restore, and maintenance."""

    def test_41_tracked_manifest_valid_and_free_of_secrets(self) -> None:
        """41. ops/fc27-protected-state-manifest.json parses cleanly and contains zero secrets or live Discord IDs."""
        manifest = load_protected_state_manifest(DEFAULT_MANIFEST_PATH)
        assert manifest["database_expected_state"]["expected_migration_version"] == "0001_THROUGH_0020"
        assert manifest["database_expected_state"]["grants_policy_version"] == 21
        assert len(manifest["recovery_order"]) >= 7
        raw_text = DEFAULT_MANIFEST_PATH.read_text(encoding="utf-8")
        assert re.search(r"\b\d{17,20}\b", raw_text) is None
        assert "postgresql://" not in raw_text

    def test_42_full_export_produces_zip_sha256_and_manifest(self, tmp_path: Path) -> None:
        """42. Full protected-state export creates ZIP, .sha256 sidecar, and bundle-manifest.json."""
        repo = _populate_synthetic_repo(tmp_path / "repo")
        out_zip = tmp_path / "offline-vault" / "bundle.zip"
        acl = _fake_acl_runner_factory()

        res = export_protected_state_bundle(repo_root=repo, output_path=out_zip, acl_runner=acl)
        assert Path(res.bundle_path).is_file()
        assert Path(res.sha256_sidecar_path).is_file()
        assert res.permissions_restricted is True
        assert "secrets/fc27_alias_hmac_key" in res.included_files
        assert ".local-data/discord.env" in res.included_files
        with zipfile.ZipFile(out_zip, "r") as zf:
            assert "bundle-manifest.json" in zf.namelist()
            bm = json.loads(zf.read("bundle-manifest.json").decode("utf-8"))
            assert bm["alias_hmac_key_fingerprint_sha256"] == compute_alias_hmac_key_fingerprint(SYNTHETIC_HMAC_KEY)

    def test_43_export_refuses_destination_inside_git_working_tree(self, tmp_path: Path) -> None:
        """43. Exporting into the repository working tree is refused fail-closed."""
        repo = _populate_synthetic_repo(tmp_path / "repo")
        with pytest.raises(ProtectedStateError, match="repository working tree"):
            export_protected_state_bundle(
                repo_root=repo,
                output_path=repo / "ops" / "leak-bundle.zip",
                acl_runner=_fake_acl_runner_factory(),
            )
        with pytest.raises(ProtectedStateError, match="repository working tree"):
            export_protected_state_bundle(
                repo_root=repo,
                output_path=repo / "bundle.zip",
                acl_runner=_fake_acl_runner_factory(),
            )

    def test_44_export_refuses_symlink_or_reparse_source_and_destination(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """44. Export rejects symlinks/junctions in source protected paths and output target."""
        repo = _populate_synthetic_repo(tmp_path / "repo")
        out_zip = tmp_path / "vault" / "bundle.zip"
        secret_file = repo / "secrets" / "discord_bot_token"

        import src.fc27_protected_state as ps_mod

        orig_check = ps_mod._is_reparse_or_symlink
        monkeypatch.setattr(
            ps_mod,
            "_is_reparse_or_symlink",
            lambda p: True if p.resolve(strict=False) == secret_file.resolve(strict=False) else orig_check(p),
        )
        with pytest.raises(ProtectedStateError, match="Symlink or NTFS junction"):
            export_protected_state_bundle(
                repo_root=repo, output_path=out_zip, acl_runner=_fake_acl_runner_factory()
            )

    def test_45_export_strict_fails_when_required_file_missing(self, tmp_path: Path) -> None:
        """45. Strict export fails closed if any required secret or env file is missing."""
        repo = _populate_synthetic_repo(tmp_path / "repo")
        (repo / "secrets" / "fc27_poll_writer_uri").unlink()
        with pytest.raises(ProtectedStateError, match="missing required"):
            export_protected_state_bundle(
                repo_root=repo,
                output_path=tmp_path / "vault" / "bundle.zip",
                strict=True,
                acl_runner=_fake_acl_runner_factory(),
            )

    def test_46_export_rejects_placeholder_or_empty_secret(self, tmp_path: Path) -> None:
        """46. Export refuses empty or replace_with_* placeholder secrets."""
        repo = _populate_synthetic_repo(tmp_path / "repo")
        (repo / "secrets" / "discord_bot_token").write_text("replace_with_real_token", encoding="utf-8")
        with pytest.raises(ProtectedStateError, match="placeholder"):
            export_protected_state_bundle(
                repo_root=repo,
                output_path=tmp_path / "vault" / "bundle.zip",
                acl_runner=_fake_acl_runner_factory(),
            )

    def test_47_export_rejects_short_alias_hmac_key(self, tmp_path: Path) -> None:
        """47. Export enforces >= 32-byte minimum length for secrets/fc27_alias_hmac_key."""
        repo = _populate_synthetic_repo(tmp_path / "repo")
        (repo / "secrets" / "fc27_alias_hmac_key").write_bytes(b"too-short")
        with pytest.raises(ProtectedStateError, match="32"):
            export_protected_state_bundle(
                repo_root=repo,
                output_path=tmp_path / "vault" / "bundle.zip",
                acl_runner=_fake_acl_runner_factory(),
            )

    def test_48_export_rejects_invalid_or_duplicate_discord_snowflakes(self, tmp_path: Path) -> None:
        """48. Export refuses non-numeric, zero/negative, or duplicate Discord role/channel IDs."""
        repo = _populate_synthetic_repo(tmp_path / "repo")
        for bad_line in ("bad_tag_name", "0", f"{SYNTHETIC_OP_ROLE_ID},{SYNTHETIC_OP_ROLE_ID}"):
            (repo / ".local-data" / "discord.env").write_text(
                f"DISCORD_APPLICATION_ID={SYNTHETIC_APP_ID}\n"
                f"FC27_DISCORD_ALLOWED_GUILD_IDS={SYNTHETIC_GUILD_ID}\n"
                f"FC27_DISCORD_ALLOWED_CHANNEL_IDS={SYNTHETIC_RESULTS_CH},{SYNTHETIC_CHAT_CH}\n"
                f"FC27_DISCORD_RESULTS_CHANNEL_ID={SYNTHETIC_RESULTS_CH}\n"
                f"FC27_DISCORD_TEAM_CHAT_CHANNEL_ID={SYNTHETIC_CHAT_CH}\n"
                f"FC27_DISCORD_IDENTITY_OPERATOR_ROLE_IDS={bad_line}\n",
                encoding="utf-8",
            )
            with pytest.raises(ProtectedStateError):
                export_protected_state_bundle(
                    repo_root=repo,
                    output_path=tmp_path / "vault" / "bundle.zip",
                    acl_runner=_fake_acl_runner_factory(),
                )

    def test_49_export_rejects_mutable_tags_or_inline_credentials_in_override(self, tmp_path: Path) -> None:
        """49. Export rejects local Compose overrides with mutable image tags or embedded credentials."""
        repo = _populate_synthetic_repo(tmp_path / "repo")
        ovr = repo / ".local-data" / "compose" / "ghcr-promotion.yml"
        ovr.write_text("services:\n  fc27-discord-bot:\n    image: ghcr.io/org/bot:latest\n", encoding="utf-8")
        with pytest.raises(ProtectedStateError, match="@sha256"):
            export_protected_state_bundle(
                repo_root=repo,
                output_path=tmp_path / "vault" / "bundle.zip",
                acl_runner=_fake_acl_runner_factory(),
            )

        ovr.write_text(
            "services:\n  fc27-discord-bot:\n    environment:\n      URL: postgresql://u:p@host/db\n",
            encoding="utf-8",
        )
        with pytest.raises(ProtectedStateError, match="inline credentials"):
            export_protected_state_bundle(
                repo_root=repo,
                output_path=tmp_path / "vault" / "bundle.zip",
                acl_runner=_fake_acl_runner_factory(),
            )

    def test_50_export_metadata_contains_zero_secrets_or_discord_ids(self, tmp_path: Path) -> None:
        """50. Bundle manifest and export result JSON never contain secret values, URIs, or raw Discord IDs."""
        repo = _populate_synthetic_repo(tmp_path / "repo")
        out_zip = tmp_path / "vault" / "bundle.zip"
        res = export_protected_state_bundle(
            repo_root=repo, output_path=out_zip, acl_runner=_fake_acl_runner_factory()
        )
        with zipfile.ZipFile(out_zip, "r") as zf:
            manifest_txt = zf.read("bundle-manifest.json").decode("utf-8")
        combined = res.model_dump_json() + "\n" + manifest_txt
        for secret_token in (
            "synthetic-discord-bot-token-not-real",
            "synthetic-pg-password",
            "synthetic-fc27-hmac-key-material",
            SYNTHETIC_APP_ID,
            SYNTHETIC_GUILD_ID,
            SYNTHETIC_OP_ROLE_ID,
        ):
            assert secret_token not in combined

    def test_51_import_verifies_sha256_sidecar_and_rejects_tampering(self, tmp_path: Path) -> None:
        """51. Import fails closed if .sha256 sidecar is missing or does not match archive digest."""
        src_repo = _populate_synthetic_repo(tmp_path / "src")
        dst_repo = _populate_synthetic_repo(tmp_path / "dst")
        out_zip = tmp_path / "vault" / "bundle.zip"
        acl = _fake_acl_runner_factory()
        export_protected_state_bundle(repo_root=src_repo, output_path=out_zip, acl_runner=acl)

        sidecar = Path(f"{out_zip}.sha256")
        sidecar.write_text(("0" * 64) + " *bundle.zip\n", encoding="utf-8")
        with pytest.raises(ProtectedStateError, match="SHA-256 verification failed"):
            import_protected_state_bundle(repo_root=dst_repo, bundle_path=out_zip, acl_runner=acl)

        sidecar.unlink()
        with pytest.raises(ProtectedStateError, match="sidecar file is missing"):
            import_protected_state_bundle(repo_root=dst_repo, bundle_path=out_zip, acl_runner=acl)

    def test_52_import_rejects_member_payload_tampering(self, tmp_path: Path) -> None:
        """52. Import fails closed if a member's bytes diverge from bundle-manifest.json."""
        repo = _populate_synthetic_repo(tmp_path / "src")
        out_zip = tmp_path / "vault" / "bundle.zip"
        acl = _fake_acl_runner_factory()
        export_protected_state_bundle(repo_root=repo, output_path=out_zip, acl_runner=acl)

        tampered_zip = tmp_path / "vault" / "tampered.zip"
        with zipfile.ZipFile(out_zip, "r") as zin, zipfile.ZipFile(tampered_zip, "w") as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename == "secrets/discord_bot_token":
                    data = b"tampered-same-len-0000000000000000000"
                zout.writestr(item, data)
        sha = hashlib.sha256(tampered_zip.read_bytes()).hexdigest()
        Path(f"{tampered_zip}.sha256").write_text(f"{sha} *tampered.zip\n", encoding="utf-8")

        with pytest.raises(ProtectedStateError, match="mismatch"):
            import_protected_state_bundle(
                repo_root=_populate_synthetic_repo(tmp_path / "dst"),
                bundle_path=tampered_zip,
                acl_runner=acl,
            )

    def test_53_import_rejects_zip_path_traversal(self, tmp_path: Path) -> None:
        """53. Import rejects archive entries containing .., leading slashes, backslashes, or drive letters."""
        for bad_name in ("../outside.txt", "/etc/passwd", "secrets\\escaped", "C:drive"):
            bad_zip = tmp_path / f"bad_{hashlib.md5(bad_name.encode()).hexdigest()[:6]}.zip"
            with zipfile.ZipFile(bad_zip, "w") as zf:
                zf.writestr(bad_name, b"evil")
            with pytest.raises(ProtectedStateError):
                import_protected_state_bundle(
                    repo_root=_populate_synthetic_repo(tmp_path / "dst_trav"),
                    bundle_path=bad_zip,
                    verify_sidecar=False,
                    acl_runner=_fake_acl_runner_factory(),
                )

    def test_54_import_rejects_unallowlisted_archive_member(self, tmp_path: Path) -> None:
        """54. Import rejects ZIP archives containing any path outside the manifest allowlist."""
        bad_zip = tmp_path / "unallowlisted.zip"
        with zipfile.ZipFile(bad_zip, "w") as zf:
            zf.writestr("src/arbitrary_code.py", b"print('hi')")
        with pytest.raises(ProtectedStateError, match="unallowlisted"):
            import_protected_state_bundle(
                repo_root=_populate_synthetic_repo(tmp_path / "dst_unallow"),
                bundle_path=bad_zip,
                verify_sidecar=False,
                acl_runner=_fake_acl_runner_factory(),
            )

    def test_55_import_rejects_symlink_entry_inside_zip(self, tmp_path: Path) -> None:
        """55. Import refuses ZIP entries marked with S_IFLNK mode bits."""
        bad_zip = tmp_path / "symlink_entry.zip"
        with zipfile.ZipFile(bad_zip, "w") as zf:
            zinfo = zipfile.ZipInfo(".env")
            zinfo.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(zinfo, b"/etc/shadow")
        with pytest.raises(ProtectedStateError, match="Symlink archive entry"):
            import_protected_state_bundle(
                repo_root=_populate_synthetic_repo(tmp_path / "dst_sym"),
                bundle_path=bad_zip,
                verify_sidecar=False,
                acl_runner=_fake_acl_runner_factory(),
            )

    def test_56_import_rejects_destination_symlink_escape(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """56. Import refuses restoring into a destination containing a symlink or NTFS junction."""
        src_repo = _populate_synthetic_repo(tmp_path / "src")
        dst_repo = _populate_synthetic_repo(tmp_path / "dst")
        out_zip = tmp_path / "vault" / "bundle.zip"
        acl = _fake_acl_runner_factory()
        export_protected_state_bundle(repo_root=src_repo, output_path=out_zip, acl_runner=acl)

        import src.fc27_protected_state as ps_mod

        orig_check = ps_mod._is_reparse_or_symlink
        monkeypatch.setattr(
            ps_mod,
            "_is_reparse_or_symlink",
            lambda p: True if p.name == "secrets" else orig_check(p),
        )
        with pytest.raises(ProtectedStateError, match="Symlink or NTFS junction"):
            import_protected_state_bundle(repo_root=dst_repo, bundle_path=out_zip, acl_runner=acl)

    def test_57_import_dry_run_validates_without_writing_files(self, tmp_path: Path) -> None:
        """57. Import with dry_run=True validates signatures and manifests without touching disk."""
        src_repo = _populate_synthetic_repo(tmp_path / "src")
        out_zip = tmp_path / "vault" / "bundle.zip"
        acl = _fake_acl_runner_factory()
        export_protected_state_bundle(repo_root=src_repo, output_path=out_zip, acl_runner=acl)

        empty_repo = tmp_path / "clean_reclone"
        (empty_repo / "ops").mkdir(parents=True, exist_ok=True)
        shutil.copy2(DEFAULT_MANIFEST_PATH, empty_repo / "ops" / "fc27-protected-state-manifest.json")

        res = import_protected_state_bundle(
            repo_root=empty_repo, bundle_path=out_zip, dry_run=True, acl_runner=acl
        )
        assert res.dry_run is True
        assert res.verified_member_count >= 21
        assert res.restored_files == ()
        assert not (empty_repo / "secrets").exists()
        assert not (empty_repo / ".env").exists()

    def test_58_import_no_overwrite_fails_when_destination_exists(self, tmp_path: Path) -> None:
        """58. Import with overwrite=False fails closed before overwriting existing protected config."""
        src_repo = _populate_synthetic_repo(tmp_path / "src")
        dst_repo = _populate_synthetic_repo(tmp_path / "dst")
        out_zip = tmp_path / "vault" / "bundle.zip"
        acl = _fake_acl_runner_factory()
        export_protected_state_bundle(repo_root=src_repo, output_path=out_zip, acl_runner=acl)

        with pytest.raises(ProtectedStateError, match="overwrite=False"):
            import_protected_state_bundle(
                repo_root=dst_repo, bundle_path=out_zip, overwrite=False, acl_runner=acl
            )

    def test_59_full_import_restores_files_and_hmac_continuity(self, tmp_path: Path) -> None:
        """59. Import cleanly restores all allowlisted files into a fresh clone while preserving HMAC continuity."""
        src_repo = _populate_synthetic_repo(tmp_path / "src")
        out_zip = tmp_path / "vault" / "bundle.zip"
        acl = _fake_acl_runner_factory()
        exp = export_protected_state_bundle(repo_root=src_repo, output_path=out_zip, acl_runner=acl)

        fresh_repo = tmp_path / "fresh_clone"
        (fresh_repo / "ops").mkdir(parents=True, exist_ok=True)
        shutil.copy2(DEFAULT_MANIFEST_PATH, fresh_repo / "ops" / "fc27-protected-state-manifest.json")

        chown_fn, stat_fn = _fake_posix_chown_and_stat(default_uid=1001, default_gid=1001)
        imp = import_protected_state_bundle(
            repo_root=fresh_repo,
            bundle_path=out_zip,
            acl_runner=acl,
            chown_fn=chown_fn,
            stat_fn=stat_fn,
        )
        assert imp.dry_run is False
        assert imp.permissions_restricted is True
        assert imp.existing_postgres_data_untouched is True
        assert imp.alias_hmac_key_fingerprint_sha256 == exp.alias_hmac_key_fingerprint_sha256
        assert (fresh_repo / "secrets" / "fc27_alias_hmac_key").read_bytes() == SYNTHETIC_HMAC_KEY
        assert (fresh_repo / ".local-data" / "compose" / "ghcr-promotion.yml").is_file()

    def test_60_windows_and_posix_permission_enforcement_and_verification(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """60. Permission enforcement locks down ACEs and rejects inherited or broad Users/Everyone permissions."""
        secret = tmp_path / "secret_token"
        secret.write_text("secret-value", encoding="utf-8")

        with monkeypatch.context() as mp:
            mp.setattr("src.fc27_protected_state.os.name", "nt")
            res_ok = apply_restrictive_permissions(secret, acl_runner=_fake_acl_runner_factory())
            assert res_ok.passed is True

            res_bad = verify_restrictive_permissions(
                secret, acl_runner=_fake_acl_runner_factory(inject_bad_ace=True)
            )
            assert res_bad.passed is False

        # Also exercise the host OS's real icacls/chmod directly on an isolated tmp_path file.
        real_file = tmp_path / "host_acl_test.txt"
        real_file.write_text("isolated", encoding="utf-8")
        real_res = apply_restrictive_permissions(real_file)
        assert real_res.passed is True
        if os.name != "nt":
            os.chmod(real_file, 0o644)
            assert verify_restrictive_permissions(real_file).passed is False

    def test_61_verify_postgres_volume_passes_with_mock_or_real_db(self, pg_db_migrated: Any, tmp_path: Path) -> None:
        """61. verify-postgres-volume confirms volume mount, HMAC key continuity, Migration 0020, and Grants 21."""
        repo = _populate_synthetic_repo(tmp_path / "repo")
        res = verify_postgres_volume_readiness(
            repo_root=repo,
            db_uri=pg_db_migrated.admin,
            docker_volume_inspect_fn=lambda name: {"Name": name, "Mountpoint": f"/var/lib/docker/volumes/{name}/_data"},
        )
        assert res.passed is True
        assert res.postgres_volume_present is True
        assert res.redis_volume_present is True
        assert res.latest_migration == "0020"
        assert res.grants_policy_version == 21
        assert res.required_tables_present is True
        assert res.alias_hmac_key_continuity_ok is True

    def test_62_verify_postgres_volume_fails_on_hmac_key_fingerprint_mismatch(
        self, pg_db_migrated: Any, tmp_path: Path
    ) -> None:
        """62. Changing secrets/fc27_alias_hmac_key after capture fails verify-postgres-volume fail-closed."""
        repo = _populate_synthetic_repo(tmp_path / "repo")
        (repo / "secrets" / "fc27_alias_hmac_key").write_bytes(b"different-key-material-0123456789abcdef-broken")
        res = verify_postgres_volume_readiness(
            repo_root=repo,
            db_uri=pg_db_migrated.discord_reader,
            docker_volume_inspect_fn=lambda name: {"Name": name, "Mountpoint": f"/var/lib/docker/volumes/{name}/_data"},
        )
        assert res.passed is False
        assert res.alias_hmac_key_continuity_ok is False
        assert "fingerprint mismatch" in res.detail

    def test_63_verify_postgres_volume_fails_when_volume_or_schema_behind(self, tmp_path: Path) -> None:
        """63. Missing postgres volume or stale schema/grants causes verify-postgres-volume to fail closed."""
        repo = _populate_synthetic_repo(tmp_path / "repo")
        res_no_vol = verify_postgres_volume_readiness(
            repo_root=repo,
            docker_volume_inspect_fn=lambda _name: None,
            db_probe_fn=lambda _uri: {
                "reachable": True,
                "latest_migration": "0019_fc27_player_alias_candidates.sql",
                "grants_policy_version": 20,
                "required_tables_present": False,
            },
        )
        assert res_no_vol.passed is False
        assert res_no_vol.postgres_volume_present is False
        assert "migration version behind" in res_no_vol.detail
        assert "grants policy version behind" in res_no_vol.detail

    def test_64_readiness_passes_all_23_sanitized_checks(self, pg_db_migrated: Any, tmp_path: Path) -> None:
        """64. 23-point recovery readiness returns READY with all 23 checks passing and zero secret leaks."""
        repo = _populate_synthetic_repo(tmp_path / "repo")
        _, stat_fn = _fake_posix_chown_and_stat(
            default_uid=CONTAINER_RUNTIME_UID,
            default_gid=CONTAINER_SHARED_SECRET_GID,
        )
        report = run_recovery_readiness_checks(
            repo_root=repo,
            db_uri=pg_db_migrated.admin,
            docker_volume_inspect_fn=lambda name: {"Name": name, "Mountpoint": f"/var/lib/docker/volumes/{name}/_data"},
            acl_runner=_fake_acl_runner_factory(),
            stat_fn=stat_fn,
            git_ls_files_fn=lambda _r: ["ops/fc27-protected-state-manifest.json", "src/fc27_protected_state.py"],
        )
        assert report.status == "READY"
        assert report.overall_ready is True
        assert report.checks_total_count == 23
        assert report.checks_passed_count == 23
        dumped = report.model_dump_json()
        assert SYNTHETIC_OP_ROLE_ID not in dumped
        assert "synthetic-pg-password" not in dumped

    def test_65_readiness_fails_on_shared_role_db_uri_user(self, pg_db_migrated: Any, tmp_path: Path) -> None:
        """65. Readiness fails role_db_uri_separation_valid when any least-privilege URI reuses another role."""
        repo = _populate_synthetic_repo(tmp_path / "repo")
        _, stat_fn = _fake_posix_chown_and_stat(
            default_uid=CONTAINER_RUNTIME_UID,
            default_gid=CONTAINER_SHARED_SECRET_GID,
        )
        (repo / "secrets" / "fc27_discord_reader_uri").write_text(
            "postgresql://fc27_poll_writer:wrong-reuse@postgres:5432/fc27\n", encoding="utf-8"
        )
        report = run_recovery_readiness_checks(
            repo_root=repo,
            db_uri=pg_db_migrated.migrator,
            docker_volume_inspect_fn=lambda name: {"Name": name, "Mountpoint": f"/var/lib/docker/volumes/{name}/_data"},
            acl_runner=_fake_acl_runner_factory(),
            stat_fn=stat_fn,
            git_ls_files_fn=lambda _r: ["ops/fc27-protected-state-manifest.json"],
        )
        assert report.overall_ready is False
        by_name = {c.name: c for c in report.checks}
        assert by_name["role_db_uri_separation_valid"].passed is False

    def test_66_readiness_fails_on_compose_secret_isolation_violation(
        self, pg_db_migrated: Any, tmp_path: Path
    ) -> None:
        """66. Readiness fails compose_secret_isolation_valid if Discord bot mounts poll-writer secret."""
        repo = _populate_synthetic_repo(tmp_path / "repo")
        _, stat_fn = _fake_posix_chown_and_stat(
            default_uid=CONTAINER_RUNTIME_UID,
            default_gid=CONTAINER_SHARED_SECRET_GID,
        )
        (repo / "docker-compose.discord.yml").write_text(
            "services:\n  fc27-discord-bot:\n    secrets:\n      - discord_bot_token\n      - fc27_poll_writer_uri\n",
            encoding="utf-8",
        )
        report = run_recovery_readiness_checks(
            repo_root=repo,
            db_uri=pg_db_migrated.migrator,
            docker_volume_inspect_fn=lambda name: {"Name": name, "Mountpoint": f"/var/lib/docker/volumes/{name}/_data"},
            acl_runner=_fake_acl_runner_factory(),
            stat_fn=stat_fn,
            git_ls_files_fn=lambda _r: ["ops/fc27-protected-state-manifest.json"],
        )
        assert report.overall_ready is False
        by_name = {c.name: c for c in report.checks}
        assert by_name["compose_secret_isolation_valid"].passed is False

    def test_67_readiness_fails_if_git_index_tracks_secret_or_env(
        self, pg_db_migrated: Any, tmp_path: Path
    ) -> None:
        """67. Readiness fails git_working_tree_clean_of_secrets if secrets/* or .env* appear in git ls-files."""
        repo = _populate_synthetic_repo(tmp_path / "repo")
        _, stat_fn = _fake_posix_chown_and_stat(
            default_uid=CONTAINER_RUNTIME_UID,
            default_gid=CONTAINER_SHARED_SECRET_GID,
        )
        report = run_recovery_readiness_checks(
            repo_root=repo,
            db_uri=pg_db_migrated.migrator,
            docker_volume_inspect_fn=lambda name: {"Name": name, "Mountpoint": f"/var/lib/docker/volumes/{name}/_data"},
            acl_runner=_fake_acl_runner_factory(),
            stat_fn=stat_fn,
            git_ls_files_fn=lambda _r: ["secrets/discord_bot_token"],
        )
        assert report.overall_ready is False
        by_name = {c.name: c for c in report.checks}
        assert by_name["git_working_tree_clean_of_secrets"].passed is False

    def test_68_verify_post_start_health_checks_services_and_zero_provider_calls(self, pg_db_migrated: Any) -> None:
        """68. verify-post-start succeeds for healthy services with 0 provider requests and fails on unhealthy service."""
        healthy_rows = [
            {"Service": svc, "State": "running", "Status": "Up 2 minutes (healthy)", "Names": f"fc27-full-stack-{svc}-1"}
            for svc in ("postgres", "redis", "fc27-discord-bot", "fc27-community-worker")
        ]
        res_ok = verify_post_start_health(
            db_uri=pg_db_migrated.admin,
            docker_ps_fn=lambda _proj: healthy_rows,
            container_status_fn=lambda svc, _cmd: (
                0,
                _VALID_BOT_STATUS_STDOUT if svc == "fc27-discord-bot" else _VALID_WORKER_STATUS_STDOUT,
            ),
        )
        assert res_ok.passed is True
        assert res_ok.provider_requests_executed == 0
        assert res_ok.worker_status_ok is True
        assert res_ok.bot_status_ok is True
        assert res_ok.grants_policy_version == 21

        degraded_rows = [
            {"Service": "postgres", "State": "running", "Status": "Up 2 minutes (healthy)"},
            {"Service": "redis", "State": "exited", "Status": "Exited (1)"},
        ]
        res_bad = verify_post_start_health(
            docker_ps_fn=lambda _proj: degraded_rows,
            container_status_fn=lambda svc, _cmd: (
                0,
                _VALID_BOT_STATUS_STDOUT if svc == "fc27-discord-bot" else _VALID_WORKER_STATUS_STDOUT,
            ),
        )
        assert res_bad.passed is False
        assert "redis" in res_bad.unhealthy_services

    def test_69_backup_restore_roundtrip_covers_migration_0020_tables(
        self, pg_db_migrated: Any, pg_admin_uri: str, tmp_path: Path
    ) -> None:
        """69. Backup & restore verification covers fc27_player_alias_promotion_audit & review_exceptions at 0020."""
        assert EXPECTED_MIGRATION_VERSION == "0001_THROUGH_0020"
        assert "fc27_player_alias_promotion_audit" in FC27_DOMAIN_TABLES
        assert "fc27_player_alias_review_exceptions" in FC27_DOMAIN_TABLES

        raw_m1 = {
            "match_id": "996901",
            "match_type": "leagueMatch",
            "timestamp": 1764000000,
            "clubs": [
                {"club_id": EAST_COASTERS_CLUB_ID, "club_name": "East Coasters", "goals": 2, "goals_against": 1, "result": 1, "wins": 1, "ties": 0, "losses": 0, "winner_by_dnf": 0, "region_id": 4464981, "team_id": 112},
                {"club_id": "880901", "club_name": "Harbor United", "goals": 1, "goals_against": 2, "result": 2, "wins": 0, "ties": 0, "losses": 1, "winner_by_dnf": 0, "region_id": 4464981, "team_id": 115},
            ],
            "players": [
                {"club_id": EAST_COASTERS_CLUB_ID, "player_id": "6901", "player_name": "BakAuto", "position": "ST", "rating": 8.0, "goals": 1, "assists": 0, "shots": 2, "passes_made": 15, "pass_attempts": 18, "tackles_made": 1, "tackle_attempts": 2, "saves": 0, "red_cards": 0, "mom": 0, "seconds_played": 5400, "archetype_id": 1, "event_mask": 2}
            ],
            "aggregate": {EAST_COASTERS_CLUB_ID: {"passesmade": 90}},
            "raw": {"matchId": "996901"},
        }
        raw_m2 = dict(raw_m1, match_id="996902", timestamp=1764003600, raw={"matchId": "996902"})
        hmac_key = b"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        for m in (raw_m1, raw_m2):
            env, normalized_matches = normalize_community_match_records([m], alias_hmac_key=hmac_key)
            assert len(normalized_matches) == 1
            persist_polled_match_envelope(pg_db_migrated.poll_writer, match_envelope=env)

        promo_res = evaluate_and_auto_promote_alias_candidates(
            pg_db_migrated.poll_writer,
            policy=FC27AliasAutoPromotionPolicy(enabled=True, shadow_mode=False),
        )
        assert promo_res.promoted_count == 1
        backup_dir = tmp_path / ".local-data" / "backups"
        archive_path, manifest_path, manifest = create_fc27_backup(
            pg_db_migrated.admin, backup_dir=backup_dir
        )
        assert manifest.migration_version == "0001_THROUGH_0020"
        assert manifest.table_counts.fc27_player_alias_promotion_audit == 1
        report = verify_fc27_backup_restore(
            manifest_path=manifest_path,
            archive_path=archive_path,
            admin_db_uri=pg_admin_uri,
        )
        assert report.restore_verification == "PASS"
        assert report.restored_counts.fc27_player_alias_promotion_audit == 1

    def test_70_bounded_maintenance_purges_old_resolved_and_tokens_preserves_audit_and_open(
        self, pg_db_migrated: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """70. Disabled-by-default maintenance purges only expired tokens & old resolved exceptions cleanly."""
        monkeypatch.delenv("FC27_ALIAS_MAINTENANCE_ENABLED", raising=False)
        disabled_summary = run_fc27_alias_maintenance(pg_db_migrated.match_results_writer)
        assert disabled_summary.enabled is False
        assert disabled_summary.executed is False

        pid_old_res = pseudonymize_east_coasters_identifier("7001", club_id=EAST_COASTERS_CLUB_ID)
        pid_recent_res = pseudonymize_east_coasters_identifier("7002", club_id=EAST_COASTERS_CLUB_ID)
        pid_open = pseudonymize_east_coasters_identifier("7003", club_id=EAST_COASTERS_CLUB_ID)
        now = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
        old_ts = now - timedelta(days=120)
        recent_ts = now - timedelta(days=10)

        with psycopg.connect(pg_db_migrated.admin) as aconn:
            aconn.execute(
                """
                INSERT INTO public.fc27_player_alias_review_exceptions (
                    player_id, exception_reason, exception_state,
                    first_detected_at, last_detected_at, resolved_at, created_at, updated_at
                ) VALUES
                    (%s, 'NO_PROVIDER_ALIAS', 'RESOLVED', %s, %s, %s, %s, %s),
                    (%s, 'NO_PROVIDER_ALIAS', 'REJECTED', %s, %s, %s, %s, %s),
                    (%s, 'NO_PROVIDER_ALIAS', 'OPEN', %s, %s, NULL, %s, %s)
                """,
                (
                    pid_old_res, old_ts, old_ts, old_ts, old_ts, old_ts,
                    pid_recent_res, recent_ts, recent_ts, recent_ts, recent_ts, recent_ts,
                    pid_open, old_ts, old_ts, old_ts, old_ts,
                ),
            )
            aconn.execute(
                """
                INSERT INTO public.fc27_player_alias_promotion_audit (
                    player_id, promotion_mode, policy_version, minimum_distinct_matches,
                    distinct_match_count, prior_verification_status, prior_conflict_status,
                    resulting_roster_revision, promoted_at, outcome_category, created_at
                ) VALUES (%s, 'MANUAL_OPERATOR_CONFIRMATION', 'stable-v1', 2, 2, 'NONE', 'NONE', 'u0001', %s, 'PROMOTED', %s)
                """,
                (pid_old_res, old_ts, old_ts),
            )
            aconn.commit()

        store = ReconciliationTokenStore(
            writer_conn_or_uri=pg_db_migrated.match_results_writer,
            reader_conn_or_uri=pg_db_migrated.discord_reader,
            clock=lambda: now,
        )
        store.issue_tokens(
            [pid_open],
            actor_discord_user_id="900000000000000099",
            ttl_seconds=300,
        )
        with psycopg.connect(pg_db_migrated.admin) as aconn:
            aconn.execute(
                """
                INSERT INTO public.fc27_roster_reconciliation_tokens (
                    actor_discord_user_id, token, player_id, issued_at, expires_at
                ) VALUES ('900000000000000099', 'T9', %s, %s, %s)
                """,
                (pid_old_res, old_ts, old_ts + timedelta(minutes=5)),
            )
            aconn.commit()

        monkeypatch.setenv("FC27_ALIAS_MAINTENANCE_ENABLED", "true")
        monkeypatch.setenv("FC27_ALIAS_REVIEW_EXCEPTION_RETENTION_DAYS", "90")
        monkeypatch.setenv("FC27_RECONCILIATION_TOKEN_CLEANUP_ENABLED", "true")
        monkeypatch.setenv("FC27_RECONCILIATION_TOKEN_RETENTION_HOURS", "24")

        summary = run_fc27_alias_maintenance(pg_db_migrated.match_results_writer, now=now)
        assert summary.enabled is True
        assert summary.executed is True
        assert summary.resolved_exceptions_deleted == 1
        assert summary.expired_tokens_deleted == 1
        assert summary.audit_rows_deleted == 0
        assert summary.open_exceptions_preserved == 1

        with psycopg.connect(pg_db_migrated.admin) as aconn:
            cnt_row = aconn.execute("SELECT COUNT(*) FROM public.fc27_player_alias_promotion_audit").fetchone()
            assert cnt_row is not None and cnt_row[0] == 1
            states = [
                r[0]
                for r in aconn.execute(
                    "SELECT exception_state FROM public.fc27_player_alias_review_exceptions ORDER BY exception_state"
                ).fetchall()
            ]
            assert states == ["OPEN", "REJECTED"]

    def test_cli_smoke_readiness_exit_codes(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """CLI exits cleanly with sanitized JSON on errors without exposing secrets."""
        rc = protected_state_cli_main(["readiness", "--repo-root", str(tmp_path / "missing_repo")])
        captured = capsys.readouterr()
        assert rc != 0
        assert "overall_ready" in captured.out

    def test_71_export_fails_closed_and_cleans_up_artifacts_on_acl_rejection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """71. Export rejects weak ACLs, unlinks partial/final ZIP and sidecar, and returns non-zero in CLI."""
        repo = _populate_synthetic_repo(tmp_path / "repo")
        vault_dir = tmp_path / "vault"
        out_zip = vault_dir / "bundle.zip"
        monkeypatch.setattr("src.fc27_protected_state.os.name", "nt")

        with pytest.raises(ProtectedStatePermissionError):
            export_protected_state_bundle(
                repo_root=repo,
                output_path=out_zip,
                acl_runner=_fake_acl_runner_factory(inject_bad_ace=True),
            )
        assert not out_zip.exists()
        assert not Path(f"{out_zip}.sha256").exists()
        assert not vault_dir.exists() or list(vault_dir.iterdir()) == []

        import src.fc27_protected_state as ps_mod
        from src.fc27_protected_state import PermissionCheckResult

        monkeypatch.setattr(
            ps_mod,
            "apply_restrictive_permissions",
            lambda p, **_kw: PermissionCheckResult(
                path=p.name, platform="nt", passed=False, detail="injected ACL rejection"
            ),
        )
        rc = protected_state_cli_main(
            ["export", "--repo-root", str(repo), "--output", str(out_zip)]
        )
        assert rc != 0
        assert not out_zip.exists()
        assert not Path(f"{out_zip}.sha256").exists()

    def test_72_posix_container_uid_secret_readability_and_bundle_0600(
        self, pg_db_migrated: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """72. POSIX enforces container UID 10001 readability for runtime secrets while keeping bundles 0600."""
        src_repo = _populate_synthetic_repo(tmp_path / "src")
        out_zip = tmp_path / "vault" / "bundle.zip"
        export_protected_state_bundle(
            repo_root=src_repo,
            output_path=out_zip,
            acl_runner=_fake_acl_runner_factory(),
        )

        monkeypatch.setattr("src.fc27_protected_state._is_windows", lambda: False)

        # Host-owned 0600 file (UID 1001 != container UID 10001) without chown must fail closed
        no_chown, bad_stat = _fake_posix_chown_and_stat(
            default_uid=1001,
            default_gid=1001,
            mode_override_file=0o600,
            mode_override_dir=0o700,
            support_chown=False,
        )
        bad_chk = verify_container_secret_readability(
            src_repo / "secrets" / "discord_bot_token",
            stat_fn=bad_stat,
        )
        assert bad_chk.passed is False
        assert "cannot read host-owned" in bad_chk.detail

        fresh_repo = tmp_path / "fresh_clone"
        (fresh_repo / "ops").mkdir(parents=True, exist_ok=True)
        shutil.copy2(DEFAULT_MANIFEST_PATH, fresh_repo / "ops" / "fc27-protected-state-manifest.json")
        with pytest.raises(ProtectedStatePermissionError):
            import_protected_state_bundle(
                repo_root=fresh_repo,
                bundle_path=out_zip,
                chown_fn=no_chown,
                stat_fn=bad_stat,
            )

        rdy_fail = run_recovery_readiness_checks(
            repo_root=src_repo,
            db_uri=pg_db_migrated.admin,
            docker_volume_inspect_fn=lambda name: {"Name": name, "Mountpoint": f"/var/lib/docker/volumes/{name}/_data"},
            stat_fn=bad_stat,
            git_ls_files_fn=lambda _r: ["ops/fc27-protected-state-manifest.json"],
        )
        by_name = {c.name: c for c in rdy_fail.checks}
        assert by_name["file_permissions_restricted"].passed is False
        assert rdy_fail.overall_ready is False

        # Shared group 0640 for GID 65534 passes runtime readability, while offline bundle rejects 0640
        _, grp_stat = _fake_posix_chown_and_stat(
            default_uid=1001,
            default_gid=CONTAINER_SHARED_SECRET_GID,
            mode_override_file=0o640,
            mode_override_dir=0o750,
            support_chown=False,
        )
        assert verify_container_secret_readability(
            src_repo / "secrets" / "discord_bot_token",
            stat_fn=grp_stat,
        ).passed is True
        assert verify_restrictive_permissions(
            out_zip,
            allow_runtime_group_read=False,
            stat_fn=grp_stat,
        ).passed is False

        # Container-owned 0600 (10001:65534) via chown passes import & readiness
        good_chown, good_stat = _fake_posix_chown_and_stat(
            default_uid=1001,
            default_gid=1001,
            mode_override_file=0o600,
            mode_override_dir=0o700,
            support_chown=True,
        )
        fresh_ok = tmp_path / "fresh_ok"
        (fresh_ok / "ops").mkdir(parents=True, exist_ok=True)
        shutil.copy2(DEFAULT_MANIFEST_PATH, fresh_ok / "ops" / "fc27-protected-state-manifest.json")
        imp_ok = import_protected_state_bundle(
            repo_root=fresh_ok,
            bundle_path=out_zip,
            chown_fn=good_chown,
            stat_fn=good_stat,
        )
        assert imp_ok.permissions_restricted is True

    def test_73_import_persists_hmac_fingerprint_and_continuity_fails_closed_without_baseline(
        self, tmp_path: Path
    ) -> None:
        """73. Import persists runtime-deployment.json HMAC fingerprint and continuity fails when candidates lack a baseline."""
        src_repo = _populate_synthetic_repo(tmp_path / "src")
        (src_repo / ".local-data" / "runtime-deployment.json").unlink()
        out_zip = tmp_path / "vault" / "bundle.zip"
        acl = _fake_acl_runner_factory()
        exp = export_protected_state_bundle(repo_root=src_repo, output_path=out_zip, acl_runner=acl)

        fresh_repo = tmp_path / "fresh_clone"
        (fresh_repo / "ops").mkdir(parents=True, exist_ok=True)
        shutil.copy2(DEFAULT_MANIFEST_PATH, fresh_repo / "ops" / "fc27-protected-state-manifest.json")
        shutil.copy2(REPO_ROOT / "docker-compose.yml", fresh_repo / "docker-compose.yml")
        shutil.copy2(REPO_ROOT / "docker-compose.discord.yml", fresh_repo / "docker-compose.discord.yml")
        shutil.copy2(REPO_ROOT / "docker-compose.community-worker.yml", fresh_repo / "docker-compose.community-worker.yml")

        chown_fn, stat_fn = _fake_posix_chown_and_stat(default_uid=1001, default_gid=1001)
        imp = import_protected_state_bundle(
            repo_root=fresh_repo,
            bundle_path=out_zip,
            acl_runner=acl,
            chown_fn=chown_fn,
            stat_fn=stat_fn,
        )
        meta_path = fresh_repo / ".local-data" / "runtime-deployment.json"
        assert meta_path.is_file()
        assert ".local-data/runtime-deployment.json" in imp.restored_files
        assert (
            json.loads(meta_path.read_text(encoding="utf-8"))["alias_hmac_key_fingerprint_sha256"]
            == exp.alias_hmac_key_fingerprint_sha256
        )

        # Replaced HMAC key fails continuity even when runtime-deployment.json wasn't manually exported
        (fresh_repo / "secrets" / "fc27_alias_hmac_key").write_bytes(
            b"replaced-hmac-key-0123456789abcdef-breaks-continuity"
        )
        res_mismatch = verify_postgres_volume_readiness(
            repo_root=fresh_repo,
            docker_volume_inspect_fn=lambda name: {"Name": name, "Mountpoint": f"/var/lib/docker/volumes/{name}/_data"},
        )
        assert res_mismatch.passed is False
        assert res_mismatch.alias_hmac_key_continuity_ok is False

        # Deleting baseline when existing alias candidate rows exist fails closed
        meta_path.unlink()
        (fresh_repo / "secrets" / "fc27_alias_hmac_key").write_bytes(SYNTHETIC_HMAC_KEY)
        res_no_base = verify_postgres_volume_readiness(
            repo_root=fresh_repo,
            db_uri="postgresql://fc27@127.0.0.1:5432/fc27",
            docker_volume_inspect_fn=lambda name: {"Name": name, "Mountpoint": f"/var/lib/docker/volumes/{name}/_data"},
            db_probe_fn=lambda _u: {
                "reachable": True,
                "latest_migration": "0020",
                "grants_policy_version": 21,
                "required_tables_present": True,
                "candidate_row_count": 3,
                "roster_row_count": 2,
            },
        )
        assert res_no_base.passed is False
        assert res_no_base.alias_hmac_key_continuity_ok is False
        assert "missing alias HMAC key fingerprint baseline" in res_no_base.detail

    def test_74_compose_secret_isolation_catches_local_override_escalation(
        self, pg_db_migrated: Any, tmp_path: Path
    ) -> None:
        """74. Merged Compose isolation catches forbidden secret mounts introduced by local override files."""
        repo = _populate_synthetic_repo(tmp_path / "repo")
        _, stat_fn = _fake_posix_chown_and_stat(
            default_uid=CONTAINER_RUNTIME_UID,
            default_gid=CONTAINER_SHARED_SECRET_GID,
        )
        override_path = repo / "docker-compose.override.yml"
        override_path.write_text(
            "services:\n"
            "  fc27-discord-bot:\n"
            "    secrets:\n"
            "      - fc27_migrator_uri\n",
            encoding="utf-8",
        )
        report = run_recovery_readiness_checks(
            repo_root=repo,
            db_uri=pg_db_migrated.admin,
            docker_volume_inspect_fn=lambda name: {"Name": name, "Mountpoint": f"/var/lib/docker/volumes/{name}/_data"},
            acl_runner=_fake_acl_runner_factory(),
            stat_fn=stat_fn,
            git_ls_files_fn=lambda _r: ["ops/fc27-protected-state-manifest.json"],
        )
        by_name = {c.name: c for c in report.checks}
        assert by_name["local_overrides_valid"].passed is True
        assert by_name["compose_secret_isolation_valid"].passed is False
        assert report.overall_ready is False

        override_path.unlink()
        (repo / "docker-compose.local.yml").write_text(
            "services:\n"
            "  fc27-community-worker:\n"
            "    secrets:\n"
            "      - discord_bot_token\n",
            encoding="utf-8",
        )
        report_w = run_recovery_readiness_checks(
            repo_root=repo,
            db_uri=pg_db_migrated.admin,
            docker_volume_inspect_fn=lambda name: {"Name": name, "Mountpoint": f"/var/lib/docker/volumes/{name}/_data"},
            acl_runner=_fake_acl_runner_factory(),
            stat_fn=stat_fn,
            git_ls_files_fn=lambda _r: ["ops/fc27-protected-state-manifest.json"],
        )
        by_name_w = {c.name: c for c in report_w.checks}
        assert by_name_w["compose_secret_isolation_valid"].passed is False
        assert report_w.overall_ready is False

    def test_75_verify_post_start_health_rejects_disabled_or_unconfigured_status_stdout(
        self, pg_db_migrated: Any
    ) -> None:
        """75. verify-post-start rejects exit-0 --status responses when required readiness fields fail."""
        healthy_rows = [
            {"Service": svc, "State": "running", "Status": "Up 2 minutes (healthy)", "Names": f"fc27-full-stack-{svc}-1"}
            for svc in ("postgres", "redis", "fc27-discord-bot", "fc27-community-worker")
        ]
        bad_bot_out = _VALID_BOT_STATUS_STDOUT.replace("DISCORD_TOKEN_CONFIGURED=YES", "DISCORD_TOKEN_CONFIGURED=NO")
        res_bot_bad = verify_post_start_health(
            db_uri=pg_db_migrated.admin,
            docker_ps_fn=lambda _proj: healthy_rows,
            container_status_fn=lambda svc, _cmd: (
                0,
                bad_bot_out if svc == "fc27-discord-bot" else _VALID_WORKER_STATUS_STDOUT,
            ),
        )
        assert res_bot_bad.passed is False
        assert res_bot_bad.bot_status_ok is False
        assert res_bot_bad.worker_status_ok is True

        bad_worker_out = _VALID_WORKER_STATUS_STDOUT.replace("WORKER_ENABLED=YES", "WORKER_ENABLED=NO")
        res_worker_bad = verify_post_start_health(
            db_uri=pg_db_migrated.admin,
            docker_ps_fn=lambda _proj: healthy_rows,
            container_status_fn=lambda svc, _cmd: (
                0,
                _VALID_BOT_STATUS_STDOUT if svc == "fc27-discord-bot" else bad_worker_out,
            ),
        )
        assert res_worker_bad.passed is False
        assert res_worker_bad.bot_status_ok is True
        assert res_worker_bad.worker_status_ok is False

        res_json_only = verify_post_start_health(
            db_uri=pg_db_migrated.admin,
            docker_ps_fn=lambda _proj: healthy_rows,
            container_status_fn=lambda _svc, _cmd: (0, '{"status":"READY"}'),
        )
        assert res_json_only.passed is False
        assert res_json_only.bot_status_ok is False
        assert res_json_only.worker_status_ok is False

    def test_76_bot_status_cli_outputs_command_counts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """76. scripts/fc27_discord_bot.py --status emits command counts satisfying post-start contract."""
        from scripts import fc27_discord_bot as bot_cli

        tok_file = tmp_path / "discord_bot_token"
        tok_file.write_text("synthetic-bot-token-for-status-test\n", encoding="utf-8")
        monkeypatch.setenv("FC27_DISCORD_BOT_ENABLED", "true")
        monkeypatch.setenv("DISCORD_APPLICATION_ID", SYNTHETIC_APP_ID)
        monkeypatch.setenv("FC27_DISCORD_ALLOWED_GUILD_IDS", SYNTHETIC_GUILD_ID)
        monkeypatch.setenv("FC27_DISCORD_ALLOWED_CHANNEL_IDS", f"{SYNTHETIC_RESULTS_CH},{SYNTHETIC_CHAT_CH}")
        monkeypatch.setenv("FC27_DISCORD_ALLOWED_ROLE_IDS", SYNTHETIC_OP_ROLE_ID)
        monkeypatch.setenv("DISCORD_BOT_TOKEN_FILE", str(tok_file))

        rc = bot_cli.main(["--status"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "DISCORD_GLOBAL_COMMAND_COUNT=0" in out
        assert "DISCORD_GUILD_TOP_LEVEL_COMMAND_COUNT=14" in out
        assert "DISCORD_GROUP_SUBCOMMAND_COUNT=13" in out
        ok, detail = parse_and_validate_service_status_output("fc27-discord-bot", out)
        assert ok is True, detail

    def test_77_default_db_probe_falls_back_to_internal_compose_postgres_without_password_leak(
        self,
    ) -> None:
        """77. _default_db_probe resolves host='postgres' via internal Compose psql with stdin password and redacts errors."""
        from src.fc27_protected_state import _default_db_probe

        recorded_cmds: list[tuple[list[str], str | None]] = []

        def _fake_connect(_uri: str, **_kw: Any) -> Any:
            raise psycopg.OperationalError("could not translate host name 'postgres'")

        def _fake_runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            recorded_cmds.append((list(cmd), kwargs.get("input")))
            if cmd[:2] == ["docker", "ps"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="fc27-full-stack-postgres-1\n", stderr="")
            if cmd[:2] == ["docker", "port"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            if cmd[:2] == ["docker", "exec"]:
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    stdout="0020_fc27_alias_auto_promotion_and_exceptions.sql|21|t|5|4\n",
                    stderr="",
                )
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="unexpected")

        res = _default_db_probe(
            "postgresql://fc27_migrator:synthetic-super-secret-pgpw@postgres:5432/fc27",
            subprocess_runner=_fake_runner,
            connect_fn=_fake_connect,
        )
        assert res == {
            "reachable": True,
            "latest_migration": "0020_fc27_alias_auto_promotion_and_exceptions.sql",
            "grants_policy_version": 21,
            "required_tables_present": True,
            "candidate_row_count": 5,
            "roster_row_count": 4,
        }
        exec_calls = [c for c in recorded_cmds if c[0][:2] == ["docker", "exec"]]
        assert len(exec_calls) == 1
        exec_cmd, exec_input = exec_calls[0]
        assert exec_input == "synthetic-super-secret-pgpw\n"
        assert "synthetic-super-secret-pgpw" not in " ".join(exec_cmd)
        assert "default_transaction_read_only=on" in " ".join(exec_cmd)

        def _failing_runner(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            if cmd[:2] == ["docker", "ps"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="fc27-full-stack-postgres-1\n", stderr="")
            if cmd[:2] == ["docker", "port"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(
                cmd,
                2,
                stdout="",
                stderr="psql: postgresql://fc27_migrator:synthetic-super-secret-pgpw@postgres:5432/fc27 refused",
            )

        with pytest.raises(ProtectedStateError) as exc_info:
            _default_db_probe(
                "postgresql://fc27_migrator:synthetic-super-secret-pgpw@postgres:5432/fc27",
                subprocess_runner=_failing_runner,
                connect_fn=_fake_connect,
            )
        assert "synthetic-super-secret-pgpw" not in str(exc_info.value)
