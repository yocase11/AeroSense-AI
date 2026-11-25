from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from backend.database import ApiSource, City, SessionLocal
from backend.ingestion import ingest_city, ingest_source

MAX_HISTORICAL_DAYS = 90
HISTORY_CHUNK_DAYS = 30


def _historical_windows(
    days: int, ending_at: datetime
) -> list[tuple[datetime, datetime]]:
    if not 1 <= days <= MAX_HISTORICAL_DAYS:
        raise ValueError(f"Historical backfills must be between 1 and {MAX_HISTORICAL_DAYS} days.")
    start = ending_at - timedelta(days=days)
    windows = []
    while start < ending_at:
        end = min(start + timedelta(days=HISTORY_CHUNK_DAYS), ending_at)
        windows.append((start, end))
        start = end
    return windows


def main() -> int:
    parser = argparse.ArgumentParser(description="Run real provider ingestion once.")
    parser.add_argument("--city", help="City ID; defaults to all active cities.")
    parser.add_argument("--source", help="Run only this provider source ID.")
    parser.add_argument(
        "--allow-provider-errors",
        action="store_true",
        help="Treat recorded provider-level failures as partial success.",
    )
    parser.add_argument(
        "--historical-days",
        type=int,
        choices=range(1, MAX_HISTORICAL_DAYS + 1),
        help="For OpenAQ only, backfill the last 1-90 days in 30-day chunks.",
    )
    args = parser.parse_args()
    if args.historical_days and args.source != "openaq":
        parser.error("--historical-days is supported only with --source openaq.")

    with SessionLocal() as session:
        city_query = select(City).where(City.active.is_(True)).order_by(City.id)
        if args.city:
            city_query = city_query.where(City.id == args.city)
        cities = session.scalars(city_query).all()
        if args.city and not cities:
            parser.error(f"City is not seeded: {args.city}")
        source = session.get(ApiSource, args.source) if args.source else None
        if args.source and (source is None or not source.active):
            parser.error(f"Source is not seeded or active: {args.source}")

    results = []
    backfill_end = datetime.now(timezone.utc)
    history_windows = (
        _historical_windows(args.historical_days, backfill_end)
        if args.historical_days
        else []
    )
    for city in cities:
        if args.source:
            if history_windows:
                for chunk_index, (history_start, history_end) in enumerate(
                    history_windows, start=1
                ):
                    try:
                        result = ingest_source(
                            args.source,
                            city.id,
                            history_start=history_start,
                            history_end=history_end,
                        )
                    except Exception as error:
                        result = {
                            "source": args.source,
                            "city_id": city.id,
                            "status": "failed",
                            "error": str(error),
                            "history_start": history_start.isoformat(),
                            "history_end": history_end.isoformat(),
                        }
                    result["chunk"] = chunk_index
                    result["chunk_count"] = len(history_windows)
                    results.append(result)
                    if result["status"] == "failed":
                        break
            else:
                try:
                    results.append(
                        ingest_source(args.source, city.id, args.historical_days)
                    )
                except Exception as error:
                    results.append(
                        {
                            "source": args.source,
                            "city_id": city.id,
                            "status": "failed",
                            "error": str(error),
                        }
                    )
        else:
            results.extend(ingest_city(city.id))
    print(json.dumps(results, ensure_ascii=False, default=str, indent=2))
    has_provider_errors = any(row.get("status") == "failed" for row in results)
    return 1 if has_provider_errors and not args.allow_provider_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
