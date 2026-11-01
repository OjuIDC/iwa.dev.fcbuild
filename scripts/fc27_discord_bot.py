"""CLI entrypoint for the read-only East Coasters FC27 Discord analytics bot (`PHASE 3`).

Supports:
- `--status`: Print configuration, allowlist, and token readiness without starting the bot (`DISCORD_PROVIDER_REQUESTS=0`).
- `--command <subcommand>`: Execute one bounded offline slash-command simulation over persisted data for operator verification.
- `--run`: Start the read-only Discord bot when `FC27_DISCORD_BOT_ENABLED=true`, token secret file is present, and guild/channel allowlists are configured.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.fc27_discord_bot import (  # noqa: E402
    DISCORD_DATABASE_WRITE_ACCESS,
    DISCORD_PROVIDER_REQUESTS,
    FC27DiscordBotConfig,
    FC27DiscordBotService,
    FC27DiscordError,
    SUPPORTED_DISCORD_SUBCOMMANDS,
    load_discord_bot_token,
)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only East Coasters FC27 Discord analytics bot CLI."
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print read-only Discord bot readiness and exit.",
    )
    parser.add_argument(
        "--command",
        choices=SUPPORTED_DISCORD_SUBCOMMANDS,
        default=None,
        help="Execute one bounded slash command against persisted PostgreSQL data.",
    )
    parser.add_argument(
        "--guild-id",
        default=None,
        help="Guild ID for --command verification.",
    )
    parser.add_argument(
        "--channel-id",
        default=None,
        help="Channel ID for --command verification.",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="Start the Discord gateway bot (requires token and allowlists).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        config = FC27DiscordBotConfig.from_env(strict=False)
    except FC27DiscordError:
        config = FC27DiscordBotConfig(enabled=False)

    token = load_discord_bot_token(required=False)
    token_configured = token is not None
    allowlists_configured = bool(config.allowed_guild_ids and config.allowed_channel_ids)

    if args.status or (not args.command and not args.run):
        print(f"DISCORD_BOT_ENABLED={'YES' if config.enabled else 'NO'}")
        print(f"DISCORD_TOKEN_CONFIGURED={'YES' if token_configured else 'NO'}")
        print(f"DISCORD_ALLOWLISTS_CONFIGURED={'YES' if allowlists_configured else 'NO'}")
        print(f"DISCORD_ALLOWED_GUILD_COUNT={len(config.allowed_guild_ids)}")
        print(f"DISCORD_ALLOWED_CHANNEL_COUNT={len(config.allowed_channel_ids)}")
        print(f"DISCORD_PROVIDER_REQUESTS={DISCORD_PROVIDER_REQUESTS}")
        print(f"DISCORD_DATABASE_WRITE_ACCESS={'YES' if DISCORD_DATABASE_WRITE_ACCESS else 'NO'}")
        return 0

    if args.command:
        if not config.enabled or not allowlists_configured:
            print("DISCORD_COMMAND_STATUS=DISABLED_OR_UNCONFIGURED")
            return 2
        service = FC27DiscordBotService(
            config,
            secret_tokens_to_scrub=(token,) if token else (),
        )
        gid = args.guild_id or next(iter(config.allowed_guild_ids))
        cid = args.channel_id or next(iter(config.allowed_channel_ids))
        resp = service.handle_slash_command(
            args.command,
            guild_id=gid,
            channel_id=cid,
        )
        print(f"DISCORD_COMMAND={resp.command}")
        print(f"DISCORD_STATUS={resp.status}")
        print(f"DISCORD_ROW_COUNT={resp.row_count}")
        print(f"DISCORD_PROVIDER_REQUESTS={resp.provider_requests_executed}")
        print(resp.content)
        return 0 if resp.status == "OK" else 1

    if args.run:
        if not config.enabled or not token_configured or not allowlists_configured:
            print("DISCORD_BOT_RUNNING=NO")
            print("REASON=MISSING_OPERATOR_TOKEN_OR_ALLOWLISTS")
            return 2
        print("DISCORD_BOT_READY=YES")
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
