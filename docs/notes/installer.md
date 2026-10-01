# Windows installer notes (host_agent/install.ps1)

## Behaviour
- All paths derive from `$PSScriptRoot`; the caller's cwd is irrelevant.
- `$ErrorActionPreference='Stop'`, `Set-StrictMode -Version Latest`; native commands are checked via `$LASTEXITCODE`.
- Launchers (`start_capture_agent.bat`, `calibrate_capture.bat`) are written to the repo root and use `cd /d "%~dp0"` plus `%~dp0`-relative paths.
- Re-running is safe: existing venv reused, PATH entry not duplicated, `.env` never overwritten (only a missing `TESSERACT_CMD` line is appended).
- Dependencies come from `host_agent/requirements_windows.txt`; the fallback retries the same list minus dxcam (mss path).

## Download integrity
Installers are SHA-256 checked (`Get-FileHash`). Supply `-TesseractSha256` / `-VcRedistSha256`
(or edit the parameter defaults). With no hash, the installer is not downloaded/run unless
`-AllowUnverifiedDownload` is passed. No hash values are shipped in the repo; take them from the publisher.

## Tesseract
The installer adds `C:\Program Files\Tesseract-OCR` to the persistent user PATH and writes
`TESSERACT_CMD=<full path>` to `host_agent\.env`.

**Follow-up for the capture_agent.py owner:** after `load_dotenv(...)`, honor it, e.g.
`pytesseract.pytesseract.tesseract_cmd = os.getenv("TESSERACT_CMD", pytesseract.pytesseract.tesseract_cmd)`.
Also, capture_agent logs `REDIS_URL` at startup (line ~353), which would print the Redis password; redact it.

## Redis
`.env` ships `REDIS_URL=redis://:CHANGE_ME_REDIS_PASSWORD@127.0.0.1:6379/0`. Replace the password manually; the installer generates and prints no secrets.

## Not verified
Script has not been executed (no pwsh in CI); tests are static only.
