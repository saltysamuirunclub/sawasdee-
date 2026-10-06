"""Admin commands: python -m app.cli <command> (run from the server/ folder)."""
import argparse
import sys

from . import strava, webhook
from .db import get_db, init_db
from .logging_setup import setup_logging


def cmd_backfill(args) -> None:
    with get_db() as conn:
        users = [args.user] if args.user else [r[0] for r in conn.execute(
            "SELECT user_id FROM strava_tokens")]
        for user_id in users:
            n = strava.backfill(conn, user_id, weeks=args.weeks)
            print(f"user {user_id}: imported {n} runs")


def cmd_webhook(args) -> None:
    if args.action == "create":
        print("Created:", webhook.create_subscription())
    elif args.action == "list":
        print(webhook.list_subscriptions() or "No subscriptions")
    elif args.action == "delete":
        for sub in webhook.list_subscriptions():
            webhook.delete_subscription(sub["id"])
            print("Deleted subscription", sub["id"])


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("backfill", help="Import past runs from Strava")
    p.add_argument("--weeks", type=int, default=8)
    p.add_argument("--user", type=int, help="users.id (default: all users)")
    p.set_defaults(func=cmd_backfill)

    p = sub.add_parser("webhook", help="Manage the Strava webhook subscription")
    p.add_argument("action", choices=["create", "list", "delete"])
    p.set_defaults(func=cmd_webhook)

    args = parser.parse_args(argv)
    setup_logging()
    init_db()
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
