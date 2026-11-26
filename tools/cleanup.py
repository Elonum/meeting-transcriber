"""Delete meetings older than a number of days, once, from the command line.

The service can do this on its own (``TRANSCRIBE_RETENTION_DAYS``), but an
operator sometimes wants to free space now instead of waiting for the sweep.

    .venv\\Scripts\\python.exe tools\\cleanup.py --days 30 --dry-run
    .venv\\Scripts\\python.exe tools\\cleanup.py --days 30
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app import protocol as p  # noqa: E402
from app.retention import is_expired, sweep  # noqa: E402
from app.session import SessionManager  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, required=True, help="хранить встречи столько дней")
    parser.add_argument("--dry-run", action="store_true", help="только показать, что будет удалено")
    args = parser.parse_args()

    if args.days <= 0:
        print("--days должно быть положительным: иначе удалять нечего")
        return 2

    data_dir = Path(os.environ.get("TRANSCRIBE_DATA_DIR", ROOT / "data"))
    manager = SessionManager(data_dir)
    loaded = manager.load_from_disk()
    print(f"загружено встреч: {loaded} из {data_dir}")

    expired = [session for session in manager.sessions.values() if is_expired(session, args.days)]
    total_bytes = 0
    for session in expired:
        size = sum(f.stat().st_size for f in session.dir.rglob("*") if f.is_file())
        total_bytes += size
        state = session.state if session.state in p.TERMINAL_STATES else session.state
        print(f"  {session.id}  {state:9}  {session.title[:40]:40}  {size / 1_000_000:6.1f} МБ")

    print(f"к удалению: {len(expired)}, {total_bytes / 1_000_000:.1f} МБ")
    if args.dry_run:
        print("dry-run: ничего не удалено")
        return 0

    removed = asyncio.run(sweep(manager, days=args.days))
    print(f"удалено: {len(removed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
