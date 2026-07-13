"""One-time (or manually re-run) ingest of all 10 U.S. Coast Pilot volumes.

Downloads each volume's combined PDF (~77MB total across all 10, confirmed
live 2026-07-13), extracts and parses paragraphs, and rebuilds the local
SQLite search index at ~/.marine_wx_mcp_cache/coast_pilot/coast_pilot.db.

Run with the project's venv:
    .venv\\Scripts\\python.exe scripts\\ingest_coast_pilot.py [volume ...]

With no arguments, ingests all 10 volumes. Pass one or more volume numbers
to build the index from just a subset (e.g. `... ingest_coast_pilot.py 6 1`
for just Great Lakes + Northeast). Requires network access; takes several
minutes for the full series. Coast Pilot editions are roughly annual with
weekly paragraph-level corrections - re-run this script whenever you want
fresh data.

**Every run is a full rebuild, not an incremental update.** Whatever
volumes you pass become the entire contents of the index - if you'd
previously ingested all 10 and re-run with just `6`, the other 9 volumes'
data is gone until you re-ingest them too. There's no partial-refresh
mode; pass every volume you want kept.
"""

import io
import sys
import time
from pathlib import Path

import httpx
import pypdf

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from marine_wx_mcp.sources import coast_pilot  # noqa: E402

REQUEST_DELAY_SECONDS = 1.0  # be a good citizen against NOAA's server
FETCH_RETRIES = 3
RETRY_DELAY_SECONDS = 5.0


def fetch_volume_pages(volume: int) -> list[str]:
    url = coast_pilot.VOLUME_URL_TEMPLATE.format(n=volume)
    last_exc: Exception | None = None
    for attempt in range(1, FETCH_RETRIES + 1):
        try:
            print(f"  fetching {url} (attempt {attempt}/{FETCH_RETRIES}) ...")
            resp = httpx.get(url, timeout=120.0, follow_redirects=True)
            resp.raise_for_status()
            reader = pypdf.PdfReader(io.BytesIO(resp.content))
            print(f"  {len(reader.pages)} pages, extracting text...")
            return [page.extract_text() for page in reader.pages]
        except httpx.HTTPError as exc:
            last_exc = exc
            print(f"  attempt {attempt} failed ({exc!r})")
            if attempt < FETCH_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS)
    assert last_exc is not None
    raise last_exc


def main() -> None:
    volumes = [int(v) for v in sys.argv[1:]] or list(range(1, 11))

    all_paragraphs = []
    failed_volumes = []
    for volume in volumes:
        coverage = coast_pilot.VOLUME_COVERAGE.get(volume, "unknown")
        print(f"Volume {volume}: {coverage}")
        try:
            pages = fetch_volume_pages(volume)
        except httpx.HTTPError as exc:
            print(f"  FAILED to fetch volume {volume} after {FETCH_RETRIES} attempts: "
                  f"{exc!r} - skipping")
            failed_volumes.append(volume)
            continue
        rows = coast_pilot.parse_pages(pages, volume)
        print(f"  parsed {len(rows)} paragraphs "
              f"({sum(1 for r in rows if r['lat'] is not None)} with coordinates)")
        all_paragraphs.extend(rows)
        time.sleep(REQUEST_DELAY_SECONDS)

    print(f"\nWriting {len(all_paragraphs)} paragraphs to {coast_pilot.DB_PATH} ...")
    coast_pilot.rebuild_database(all_paragraphs)
    status = coast_pilot.database_status()
    print(f"Done. {status['row_count']} rows, volumes {status['volumes']}.")
    if failed_volumes:
        print(
            f"\n*** WARNING: {len(failed_volumes)} volume(s) FAILED and are NOT in the "
            f"index: {failed_volumes}. Re-run this script (optionally passing just "
            f"those volume numbers along with every volume you want kept - see the "
            f"module docstring on full-rebuild semantics) once the network issue clears. ***"
        )


if __name__ == "__main__":
    main()
