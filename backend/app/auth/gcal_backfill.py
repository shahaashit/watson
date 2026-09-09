"""Manage the gcal_contacts store — the name → email map the classifier
uses when drafting Google Calendar events.

Watson only fetches TODAY's events during normal sync, so a colleague you
don't meet with today isn't automatically known. Three subcommands:

    # Scan the past N days (default 90) and harvest attendees:
    .venv/bin/python -m app.auth.gcal_backfill backfill [--days 180]

    # Add a contact by hand (for colleagues you haven't met with yet):
    .venv/bin/python -m app.auth.gcal_backfill add "Morgan Lee" morgan.lee@example.com

    # List currently-known contacts (freshest first, name filter optional):
    .venv/bin/python -m app.auth.gcal_backfill list [--search morgan]

    # Remove a contact by email:
    .venv/bin/python -m app.auth.gcal_backfill remove morgan.lee@example.com
"""
import argparse
import sys

from ..db import connect, init_db
from ..models import now_iso
from ..services import gcal_client


def _cmd_backfill(args, conn):
    if not gcal_client.configured():
        print("Google Calendar is not configured — run `python -m app.auth.gcal` first.",
              file=sys.stderr)
        sys.exit(2)
    n = gcal_client.backfill_contacts(conn, days_back=args.days)
    print(f"Upserted {n} unique contact(s) from the past {args.days} days.")
    rows = conn.execute(
        "SELECT name, email FROM gcal_contacts"
        " WHERE name != '' ORDER BY last_seen_at DESC LIMIT 15"
    ).fetchall()
    if rows:
        print("Most recent contacts:")
        for r in rows:
            print(f"  {r['name'] or '(no name)':30}  {r['email']}")


def _cmd_add(args, conn):
    email = args.email.strip().lower()
    name = args.name.strip()
    if "@" not in email or "." not in email.split("@")[-1]:
        print(f"invalid email: {email!r}", file=sys.stderr)
        sys.exit(2)
    conn.execute(
        "INSERT INTO gcal_contacts (email, name, last_seen_at)"
        " VALUES (?, ?, ?)"
        " ON CONFLICT(email) DO UPDATE SET"
        "   name = CASE WHEN excluded.name != '' THEN excluded.name ELSE gcal_contacts.name END,"
        "   last_seen_at = excluded.last_seen_at",
        (email, name, now_iso()),
    )
    conn.commit()
    print(f"added: {name!r} <{email}>")


def _cmd_list(args, conn):
    where, params = "", ()
    if args.search:
        where = " WHERE lower(name) LIKE ? OR lower(email) LIKE ?"
        pat = f"%{args.search.lower()}%"
        params = (pat, pat)
    rows = conn.execute(
        f"SELECT name, email, last_seen_at FROM gcal_contacts{where}"
        " ORDER BY last_seen_at DESC LIMIT 100",
        params,
    ).fetchall()
    if not rows:
        print("(no contacts)" if not args.search else f"(no matches for {args.search!r})")
        return
    for r in rows:
        print(f"  {r['name'] or '(no name)':30}  {r['email']:36}  {r['last_seen_at'] or ''}")


def _cmd_remove(args, conn):
    cur = conn.execute("DELETE FROM gcal_contacts WHERE email = ?", (args.email.lower(),))
    conn.commit()
    print(f"removed {cur.rowcount} contact(s)")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Manage Watson's gcal contacts")
    sub = parser.add_subparsers(dest="cmd", required=False)

    p_backfill = sub.add_parser("backfill", help="Import attendees from past calendar events")
    p_backfill.add_argument("--days", type=int, default=90)
    p_backfill.set_defaults(func=_cmd_backfill)

    p_add = sub.add_parser("add", help="Manually add a contact")
    p_add.add_argument("name", help='Display name — quote if it contains spaces')
    p_add.add_argument("email")
    p_add.set_defaults(func=_cmd_add)

    p_list = sub.add_parser("list", help="Show current contacts")
    p_list.add_argument("--search", "-s", help="Filter (case-insensitive substring)")
    p_list.set_defaults(func=_cmd_list)

    p_remove = sub.add_parser("remove", help="Remove a contact by email")
    p_remove.add_argument("email")
    p_remove.set_defaults(func=_cmd_remove)

    args = parser.parse_args(argv)
    if not args.cmd:
        # No subcommand → default to the historical `backfill` behavior so the
        # bare `python -m app.auth.gcal_backfill` command still does what its
        # name suggests.
        args = parser.parse_args((argv or []) + ["backfill"])

    init_db()
    conn = connect()
    try:
        args.func(args, conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
