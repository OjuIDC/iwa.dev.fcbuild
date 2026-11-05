# FC27 Container Build, Scan, Base-Image Governance, and GHCR Publication

- **Status**: Current
- **Audience**: Security Reviewers, Platform Operators, CI/CD Engineers, Developers
- **Last verified commit**: `a1dc93fa2b2724ba902c32cdcdf3f5e94972151a`
- **Source of truth**: `Dockerfile.community-worker`, `Dockerfile.discord`, `Dockerfile.community-eval`, `.dockerignore`, `.github/workflows/container-images.yml`, `scripts/build_container_report.py`, `docker-compose.ghcr.yml`, `tests/test_github_workflows.py`

This document specifies the container image build, vulnerability and secret scanning, SPDX-2.3 SBOM generation, SLSA v1 provenance, Sigstore Cosign keyless signing, base-image upgrade and waiver-removal evidence, and private GitHub Container Registry (`ghcr.io`) publication posture for `iwa.dev.fcbuild`.

---

## 1. Repository Container Image Inventory

| Image Name | Dockerfile | Target Platform | Runtime User | Default Command / Healthcheck | GHCR Repository (`PRIVATE`) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `fc27-community-worker` | `Dockerfile.community-worker` | `linux/amd64` | `10001:65534` | `ENTRYPOINT ["python", "scripts/fc27_polling_worker.py"]` / `CMD ["--status"]` | `ghcr.io/iwa-ojuidc/iwa.dev.fcbuild/fc27-community-worker` |
| `fc27-discord-bot` | `Dockerfile.discord` | `linux/amd64` | `10001:65534` | `ENTRYPOINT ["python", "scripts/fc27_discord_bot.py"]` / `CMD ["--status"]` | `ghcr.io/iwa-ojuidc/iwa.dev.fcbuild/fc27-discord-bot` |
| `fc27-community-client-evaluation` | `Dockerfile.community-eval` | `linux/amd64` | `10001:65534` | `ENTRYPOINT ["python", "scripts/fc27_community_eval.py"]` | Local one-shot evaluation only (not published to GHCR) |

---

## 2. Digest-Pinned Python Base-Image Remediation & Waiver Removal

### 2.1 Old vs. Selected Patched Base-Image Digest

| Attribute | Previous Base Image (`main` @ `a1dc93fa2b2724ba902c32cdcdf3f5e94972151a`) | Candidate Remediated Base Image (`security/fc27-base-image-waiver-remediation`) |
| :--- | :--- | :--- |
| **Image Reference** | `python:3.11.16-slim` | `python:3.11.18-slim` |
| **Pinned Digest** | `sha256:e41613d42d4891e4930f79523f93f81bbc7632584ec65e36ab055f41a800b41e` | `sha256:064f01707f2fc8b06bae86bb43875266c6e41bac7994e77ec5dc623c4a4d0545` |
| **Python Runtime** | CPython `3.11.16` | CPython `3.11.18` |
| **OS Family / Distro** | Debian GNU/Linux 13 (`trixie`) | Debian GNU/Linux 13 (`trixie`) |
| **Target Architecture** | `linux/amd64` | `linux/amd64` |
| **Base Image Size** | `178.35 MB` (`187,017,053` bytes) | `178.43 MB` (`187,099,229` bytes) |
| **Pre-Bundled Build Tool Hardening** | Base `setuptools 79.0.1` (vendored `jaraco.context 5.3.0`), `wheel 0.45.1` | Upgraded in `RUN` layer to `setuptools 84.2.1` (`jaraco.context 6.1.0`) and `wheel 0.48.0` (replacing base-image `wheel 0.46.3`) |

### 2.2 Candidate Base-Image Comparison Matrix

Four official Docker Hub `python:3.11*-slim*` candidates were evaluated locally against Aqua Trivy (`--scanners vuln,secret --pkg-types os,library`):

| Candidate Image + Digest | Python / OS | Size (MB) | CRITICAL | HIGH (Total) | HIGH (Fixed) | OS HIGH (Fixed) | Lang HIGH (Fixed) | Evaluation Outcome |
| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: | :--- |
| `python:3.11.16-slim@sha256:e41613d42d4891e4930f79523f93f81bbc7632584ec65e36ab055f41a800b41e` | `3.11.16` / Debian 13 (`trixie`) | `178.35` | `0` | `54` | `8` | `6` (`apt`, `libapt-pkg7.0`, `libpcre2-8-0`, `openssl`, `libssl3t64`, `openssl-provider-legacy`) | `2` (`jaraco.context`, `wheel`) | Baseline (`WAIVER-FC27-BASE-IMAGE-DEBIAN-3.11.16-SLIM` required) |
| `python:3.11.17-slim@sha256:b7a9e5f6a93d6d4a31f129bbd4bdee69efccb6ded0fb230d78aafaf37af98377` | `3.11.17` / Debian 13 (`trixie`) | `178.38` | `0` | `50` | `4` | `2` (`apt`, `libapt-pkg7.0`) | `2` (`jaraco.context`, `wheel`) | Rejected (superseded; retains 2 fixed OS HIGH findings) |
| `python:3.11.18-slim-bookworm@sha256:6cbe849bdc876ecd413a4a09a919a0d861d908589de9eba61e886c29af793a07` | `3.11.18` / Debian 12 (`bookworm`) | `186.05` | `2` | `39` | `2` | `0` | `2` (`jaraco.context`, `wheel`) | Rejected (`2` `CRITICAL` OS vulnerabilities in Debian 12 `zlib1g` / `libbsd0`) |
| `python:3.11.18-slim@sha256:064f01707f2fc8b06bae86bb43875266c6e41bac7994e77ec5dc623c4a4d0545` | `3.11.18` / Debian 13 (`trixie`) | `178.43` | `0` | `46` | `2` (base) -> `0` (with `setuptools>=84.2.1`, `wheel>=0.48.0`) | `0` | `0` (after `setuptools` / `wheel` upgrade) | **SELECTED** (`CRITICAL=0`, `HIGH_FIXED=0`, waiver eliminated) |

### 2.3 Before / After Trivy Scan & Image-Size Comparison

| Image | Metric | Previous (`python:3.11.16-slim`) | Remediated (`python:3.11.18-slim`) | Delta |
| :--- | :--- | :--- | :--- | :--- |
| `fc27-community-worker` | Image Size | `242.9 MB` (`254,673,828` B) | `243.1 MB` (`254,946,069` B) | `+0.2 MB` |
| `fc27-community-worker` | `CRITICAL` / `HIGH_FIXED` | `0` / `8` (waived) | `0` / `0` (unwaived) | `-8 HIGH_FIXED` |
| `fc27-community-worker` | `HIGH` (unfixed upstream) / `MEDIUM` / `LOW` | `46` / `77` / `63` | `46` / `77` / `63` | `0` |
| `fc27-community-worker` | Secret Scan / SPDX-2.3 SBOM | `0` secrets / `PASS` | `0` secrets / `PASS` (`113` pkgs) | Pass |
| `fc27-discord-bot` | Image Size | `314.6 MB` (`329,925,112` B) | `314.9 MB` (`330,197,353` B) | `+0.3 MB` |
| `fc27-discord-bot` | `CRITICAL` / `HIGH_FIXED` | `0` / `8` (waived) | `0` / `0` (unwaived) | `-8 HIGH_FIXED` |
| `fc27-discord-bot` | `HIGH` (unfixed upstream) / `MEDIUM` / `LOW` | `46` / `77` / `63` | `46` / `77` / `63` | `0` |
| `fc27-discord-bot` | Secret Scan / SPDX-2.3 SBOM | `0` secrets / `PASS` | `0` secrets / `PASS` (`129` pkgs) | Pass |

### 2.4 Waiver Removal Policy (`WAIVER_DECISION=REMOVE_WAIVER`)

- The temporary time-bounded waiver `WAIVER-FC27-BASE-IMAGE-DEBIAN-3.11.16-SLIM` (which previously covered Debian 13 OS `HIGH_FIXED` packages and pre-bundled Python build tools until `2026-12-31`) has been **removed** from `scripts/build_container_report.py` (`waiver_id="NONE"`, `max_os_high_fixed=0`, `waived_base_python_build_pkgs=()`).
- `parse_trivy_vulnerabilities` in `scripts/build_container_report.py` now fails closed (`trivy_high_critical_gate="FAIL"`) on **any** `CRITICAL > 0` or **any** `HIGH_FIXED > 0` vulnerability across both OS (`os-pkgs`) and Python (`lang-pkgs`) packages without exception.

---

## 3. Runtime Compatibility & Operational Separation

### 3.1 Preserved Runtime Contracts
Both remediated images (`fc27-community-worker` and `fc27-discord-bot`) preserve every runtime contract under `--network none --read-only --cap-drop=ALL --security-opt=no-new-privileges:true`:
- Non-root user `10001:65534` (`uid=10001(app) gid=65534(nogroup)`).
- Pinned upstream community provider archive `1erkandogan/fc27-clubs-api@26a5ff12ea793042bd776a858c74f3f44dd427f5` (in `fc27-community-worker`) and `discord.py==2.7.1` (in `fc27-discord-bot`).
- `FC27_COMMUNITY_POLL_INTERVAL_SECONDS=43200` (`12` hours, `ONCE_EVERY_12_HOURS`), `FC27_RECOMMENDATIONS_ENABLED=false`, and `DISPATCH_ENABLED=false`.
- Clean `--status` readiness output and clean `SIGTERM` shutdown (`exit 0`).

### 3.2 Operational Separation Before Merge, Publication, and Promotion
- Updating `Dockerfile.community-worker`, `Dockerfile.discord`, `Dockerfile.community-eval`, and `scripts/build_container_report.py` on a candidate branch **does not** modify `docker-compose.ghcr.yml` or redeploy running operational containers.
- Operational containers in `fc27-full-stack` continue running the approved, signed GHCR digests documented in [`FC27_GHCR_RUNTIME_PROMOTION.md`](FC27_GHCR_RUNTIME_PROMOTION.md) (`ghcr.io/iwa-ojuidc/iwa.dev.fcbuild/fc27-discord-bot@sha256:b8b571088b9a490fa4a1a517c60e1f43305ad863f3ecea9fc5df636541e7e084` and `ghcr.io/iwa-ojuidc/iwa.dev.fcbuild/fc27-community-worker@sha256:8628acc26c9c73d9360a5e47d66c10c988e03d8273b5f15bb1f7cd3e1dc1fb79`) until:
  1. The candidate branch is pushed and reviewed in a pull request where `test-fast`, `ci`, `security-scan`, and `container-images` pass with zero waivers.
  2. The pull request is squash-merged to `main` and `.github/workflows/container-images.yml` publishes, attests (SLSA v1 + SPDX-2.3), and signs (Cosign keyless OIDC) the new GHCR digests.
  3. An operator performs a controlled, one-service-at-a-time digest promotion and rollback verification following [`FC27_GHCR_RUNTIME_PROMOTION.md`](FC27_GHCR_RUNTIME_PROMOTION.md).
