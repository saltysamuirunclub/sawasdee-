"""Admin commands: python -m app.cli <command> (run from the server/ folder)."""
import argparse
import sys

from . import pipeline, push, scheduler, strava, webhook
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


def cmd_vapid(args) -> None:
    public, private = push.generate_vapid_keys()
    print("Add these to your .env file:")
    print(f"VAPID_PUBLIC_KEY={public}")
    print(f"VAPID_PRIVATE_KEY={private}")


def cmd_retry(args) -> None:
    with get_db() as conn:
        print(f"Re-coached {pipeline.retry_failed(conn)} runs")


def cmd_weekly(args) -> None:
    with get_db() as conn:
        users = [args.user] if args.user else [r[0] for r in conn.execute(
            "SELECT id FROM users WHERE coach_enabled = 1")]
        for user_id in users:
            ok = scheduler.send_weekly_plan(conn, user_id, force=args.force)
            print(f"user {user_id}: {'sent' if ok else 'not sent (see log)'}")


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

    sub.add_parser("vapid", help="Generate push notification keys").set_defaults(func=cmd_vapid)
    sub.add_parser("retry", help="Retry coach analysis for failed runs").set_defaults(func=cmd_retry)

    p = sub.add_parser("weekly", help="Send the weekly plan now")
    p.add_argument("--user", type=int)
    p.add_argument("--force", action="store_true", help="even if this week's plan exists")
    p.set_defaults(func=cmd_weekly)

    args = parser.parse_args(argv)
    setup_logging()
    init_db()
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
