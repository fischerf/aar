"""
__main__.py — CLI entry-point for Signal MCP

Usage:
    python -m signal_mcp              # start MCP server (stdio transport)
    python -m signal_mcp onboard      # interactive setup wizard
    python -m signal_mcp send +15551234567 "Hello!"
    python -m signal_mcp receive
"""
from __future__ import annotations

import asyncio
import sys


def _usage() -> None:
    print(__doc__)
    sys.exit(0)


async def _main() -> None:
    args = sys.argv[1:]

    if not args or args[0] in ("-h", "--help", "help"):
        _usage()

    command = args[0]

    if command == "onboard":
        from .auth import run_onboarding
        await run_onboarding()

    elif command == "send":
        if len(args) < 3:
            print("Usage: python -m signal_mcp send <recipient> <message>")
            sys.exit(1)
        from .config import load_config
        from .logging_config import setup_logging
        from .signal_client import SignalClient
        cfg = load_config()
        setup_logging(cfg.log_level)
        async with SignalClient(cfg.api_base_url, cfg.account_number or "") as client:
            result = await client.send_message(recipients=[args[1]], message=" ".join(args[2:]))
            print("Sent!" if result.success else f"Failed: {result.error}")

    elif command == "receive":
        from .config import load_config
        from .logging_config import setup_logging
        from .signal_client import SignalClient
        cfg = load_config()
        setup_logging(cfg.log_level)
        async with SignalClient(cfg.api_base_url, cfg.account_number or "") as client:
            messages = await client.receive_messages()
            if not messages:
                print("No new messages.")
            for m in messages:
                print(m)

    else:
        # Default: run MCP server
        from .server import serve
        await serve()


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
