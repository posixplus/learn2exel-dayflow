"""CLI entrypoint.  `dayflow brief` runs the morning brief."""
from __future__ import annotations

import argparse
import os
import sys

import anthropic
from dotenv import load_dotenv

from dayflow import loop, prompts


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="dayflow")
    sub = parser.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("brief", help="Generate the morning brief")
    b.add_argument("--verbose", "-v", action="store_true")
    b.add_argument("--max-steps", type=int, default=10)
    b.add_argument("--token-budget", type=int, default=60_000)
    args = parser.parse_args(argv)

    if os.environ.get("MOCK", "1") != "1":
        print("Real adapters arrive in lesson 87. Set MOCK=1 for now.", file=sys.stderr)
        return 2

    if args.cmd == "brief":
        try:
            result = loop.run(
                prompts.MORNING_BRIEF,
                "Give me my morning brief for today.",
                max_steps=args.max_steps,
                token_budget=args.token_budget,
                verbose=args.verbose,
            )
        except loop.LoopError as e:
            print(f"loop stopped: {e}", file=sys.stderr)
            return 1
        except anthropic.AuthenticationError:
            print("auth failed: check ANTHROPIC_API_KEY in .env", file=sys.stderr)
            return 3
        except anthropic.BadRequestError as e:
            # Billing, bad model id, malformed request. Show the message, not the traceback.
            print(f"request rejected: {e.body.get('error', {}).get('message', e)}", file=sys.stderr)
            return 3
        except anthropic.APIConnectionError:
            print("could not reach api.anthropic.com: check your network", file=sys.stderr)
            return 3
        print(result.text)
        print(
            f"\n[{result.steps} steps · {len(result.tool_calls)} tool calls · "
            f"{result.input_tokens} in / {result.output_tokens} out]",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
