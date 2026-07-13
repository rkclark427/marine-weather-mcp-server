"""NOAA U.S. Coast Pilot full-text search - national scope, all 10 volumes.

Unlike the other sources/ modules, this one is a local SQLite index, not
a live API client. The Coast Pilot has no structured API - it's PDF-only
(confirmed live 2026-07-13; the "xml2html" tool on the NOAA site turned
out to just be a PDF/ZIP download hub, not an HTML/XML rendering). The
one-time ingest (scripts/ingest_coast_pilot.py) downloads all 10 volume
PDFs (~77MB total, confirmed live), parses them into numbered-paragraph
rows, and writes them here. Tools query the local DB - no network at
query time. Re-run the ingest script manually to refresh (Coast Pilot
editions are roughly annual with weekly paragraph-level corrections;
per-paragraph diffing against those weekly corrections was judged not
worth the complexity for v1 - just re-ingest when you want fresh data).

Structure confirmed live across two widely separated volumes (Great
Lakes/Vol 6 and Eastport ME/Vol 1), so it's a series-wide convention,
not a Great-Lakes quirk:
- Every substantive paragraph is prefixed "(NNN)" with a sequential
  number - this is NOAA's own citation unit for weekly corrections, so
  it's a natural, stable chunk boundary already provided by the source.
- A bare "(NNN)" marker (nothing else on the line) is followed by a
  section heading line (e.g. a harbor name, or "Channels"/"Coast
  Guard"/"Small-craft facilities") rather than body text - parse_pages
  treats these as heading updates, not content paragraphs.
- Every body page carries a running header "U.S. Coast Pilot {vol},
  Chapter {chapter}" (order varies - date/pagenum before or after
  depending on odd/even page) - used to tag each paragraph's chapter
  without needing per-chapter file discovery.
- Not every paragraph states its own coordinates (most describing a
  harbor's facilities don't repeat the harbor's position) - roughly 1
  in 6 paragraphs has an explicit "DD°MM'SS\"N., DDD°MM'SS\"W." pair
  in this server's sampling. `extract_coordinates` pulls whatever is
  there; `parse_pages` propagates the last-seen coordinate forward
  within the same section heading as an approximate position, not an
  exact one - don't treat inherited coordinates as authoritative.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from marine_wx_mcp.sources._geo import haversine_nm

CACHE_DIR = Path.home() / ".marine_wx_mcp_cache" / "coast_pilot"
DB_PATH = CACHE_DIR / "coast_pilot.db"

VOLUME_URL_TEMPLATE = (
    "https://nauticalcharts.noaa.gov/publications/coast-pilot/files/cp{n}/CPB{n}_WEB.pdf"
)

VOLUME_COVERAGE = {
    1: "Eastport, ME to Cape Cod, MA",
    2: "Cape Cod, MA to Sandy Hook, NJ",
    3: "Sandy Hook, NJ to Cape Henry, VA",
    4: "Cape Henry, VA to Key West, FL",
    5: "Gulf Coast, Puerto Rico and Virgin Islands",
    6: "Great Lakes (Huron, Ontario, Michigan, Erie, Superior, St. Lawrence River)",
    7: "California",
    8: "Alaska: Dixon Entrance to Cape Spencer",
    9: "Alaska: Cape Spencer to Beaufort Sea",
    10: "Oregon, Washington, Hawaii and Pacific Islands",
}


class CoastPilotError(Exception):
    """Raised for unrecoverable Coast Pilot search failures with an actionable message."""


_HEADER_RE = re.compile(r"U\.S\.\s+Coast\s+Pilot\s+(\d+),\s*Chapter\s+(\d+)")
_PARAGRAPH_RE = re.compile(r"\((\d+)\)\s*")
_COORD_RE = re.compile(
    r"(\d{1,3})°(\d{1,2})[\'’]?(\d{1,2})?\"?\s*([NS])\.?,?\s*"
    r"(\d{1,3})°(\d{1,2})[\'’]?(\d{1,2})?\"?\s*([EW])"
)
# A heading line is short, has no sentence-ending punctuation, and isn't
# itself a run of body prose - e.g. "Waukegan", "Small-craft facilities".
_HEADING_LIKE_RE = re.compile(r"^[A-Z][A-Za-z0-9 ,\-'&/]{1,60}$")

# Standard Coast Pilot subsection category labels, reused across every
# harbor entry in every volume (confirmed live in Chapter 11: "Channels"
# appears under 100+ different harbors, "Small-craft facilities" under
# 90+, etc.) - distinct from a harbor/place-name heading, which appears
# once per place. A heading matching one of these keeps the current
# harbor-level heading in place; anything else starts a new harbor.
# Matched by prefix (case-sensitive on the first word) since a few carry
# a lake/river name suffix, e.g. "Weather, Lake Michigan" vs "Weather,
# Lake Huron".
_SUBSECTION_LABEL_PREFIXES = (
    "Channels", "Anchorages", "Bridges", "Bridge", "Cable Ferry", "Ferry",
    "Coast Guard", "Communications", "Dangers", "Prominent features",
    "Quarantine", "Pilotage", "Towage", "Small-craft facilities",
    "Supplies", "Repairs", "Wharves", "Harbor regulations", "Routes",
    "Locks", "Charts", "Chart Datum", "Caution", "Currents", "Weather",
    "Winds", "Ice", "Regulations", "Traffic",
)


def _is_subsection_label(heading: str) -> bool:
    return heading.startswith(_SUBSECTION_LABEL_PREFIXES)


def _dms_to_decimal(deg: str, minute: str, second: str | None, hemi: str) -> float:
    value = int(deg) + int(minute) / 60 + (int(second) if second else 0) / 3600
    return -value if hemi in ("S", "W") else value


def extract_coordinates(text: str) -> list[tuple[float, float]]:
    """All DD°MM'SS"N., DDD°MM'SS"W. style coordinate pairs in a text block."""
    results = []
    for m in _COORD_RE.finditer(text):
        lat_d, lat_m, lat_s, hemi_lat, lon_d, lon_m, lon_s, hemi_lon = m.groups()
        lat = _dms_to_decimal(lat_d, lat_m, lat_s, hemi_lat)
        lon = _dms_to_decimal(lon_d, lon_m, lon_s, hemi_lon)
        results.append((lat, lon))
    return results


def find_chapter(page_text: str) -> int | None:
    m = _HEADER_RE.search(page_text)
    return int(m.group(2)) if m else None


def _is_heading_like(candidate: str) -> bool:
    candidate = candidate.strip()
    # Real Coast Pilot headings (harbor names and subsection labels alike)
    # are always proper Title Case words, never bare abbreviation-like
    # fragments - confirmed live that plain-text extraction occasionally
    # yields short all-caps noise (e.g. "NNN", likely a table/column
    # artifact) that would otherwise pass as a heading and silently
    # become a bogus new "harbor" for everything that follows it.
    if not candidate or len(candidate) < 4 or len(candidate) > 60:
        return False
    if candidate.endswith((".", ",", ";")):
        return False
    if not any(c.islower() for c in candidate):
        return False
    return bool(_HEADING_LIKE_RE.match(candidate))


def parse_pages(pages: list[str], volume: int) -> list[dict[str, Any]]:
    """Parse a volume's per-page extracted text into numbered-paragraph rows.

    `pages` is the raw text of each page, in order (as returned by a PDF
    text extractor, one entry per page - headers/footers are not
    pre-stripped, this function ignores them by construction since it
    only looks at paragraph-marker-delimited spans).
    """
    # Concatenate with page-boundary offsets so each match can be tagged
    # with its source page and (via the running header) chapter.
    full_text = ""
    page_starts: list[int] = []
    page_chapters: list[int | None] = []
    for page_text in pages:
        page_starts.append(len(full_text))
        page_chapters.append(find_chapter(page_text))
        full_text += page_text + "\n"

    # Forward-fill chapter across pages where the running header didn't
    # match (title/index/image-heavy pages) - without this, those pages'
    # paragraphs get chapter=None even though they're clearly within a
    # known chapter, and (more importantly) a None reads as "chapter
    # changed" below, wrongly resetting heading state on every such page.
    last_known_chapter: int | None = None
    for i, chapter in enumerate(page_chapters):
        if chapter is not None:
            last_known_chapter = chapter
        else:
            page_chapters[i] = last_known_chapter

    markers = list(_PARAGRAPH_RE.finditer(full_text))
    rows: list[dict[str, Any]] = []
    harbor_heading: str | None = None
    subsection_heading: str | None = None
    current_chapter: int | None = None
    last_coord: tuple[float, float] | None = None
    last_harbor_for_coord: str | None = None

    for i, marker in enumerate(markers):
        number = int(marker.group(1))
        start = marker.end()
        end = markers[i + 1].start() if i + 1 < len(markers) else len(full_text)
        content = " ".join(full_text[start:end].split())

        # Which page did this marker fall on?
        page_idx = 0
        for p, offset in enumerate(page_starts):
            if offset <= marker.start():
                page_idx = p
            else:
                break
        chapter = page_chapters[page_idx]

        # A heading (and any coordinate it anchors) never applies across
        # a chapter boundary - confirmed live that without this, a
        # heading from e.g. Chapter 2's regulatory text could keep
        # propagating as the "harbor" for real harbor entries many
        # chapters later, since the whole volume is one text stream.
        if chapter is not None and chapter != current_chapter:
            current_chapter = chapter
            harbor_heading = None
            subsection_heading = None

        if _is_heading_like(content):
            if _is_subsection_label(content):
                subsection_heading = content
            else:
                harbor_heading = content
                subsection_heading = None
            continue

        if not content:
            continue

        coords = extract_coordinates(content)
        if coords:
            lat, lon = coords[0]
            last_coord, last_harbor_for_coord = (lat, lon), harbor_heading
        elif harbor_heading is not None and harbor_heading == last_harbor_for_coord:
            lat, lon = last_coord
        else:
            lat, lon = None, None

        rows.append(
            {
                "volume": volume,
                "chapter": chapter,
                "paragraph_number": number,
                "harbor_heading": harbor_heading,
                "subsection_heading": subsection_heading,
                "text": content,
                "lat": lat,
                "lon": lon,
                "source_page": page_idx + 1,
            }
        )

    return rows


def _connect() -> sqlite3.Connection:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(DB_PATH)


def rebuild_database(paragraphs: list[dict[str, Any]]) -> None:
    """Drop and recreate the search index from scratch (used by the ingest script)."""
    conn = _connect()
    try:
        conn.executescript(
            """
            DROP TABLE IF EXISTS paragraphs;
            DROP TABLE IF EXISTS paragraphs_fts;
            CREATE TABLE paragraphs (
                id INTEGER PRIMARY KEY,
                volume INTEGER NOT NULL,
                chapter INTEGER,
                paragraph_number INTEGER,
                harbor_heading TEXT,
                subsection_heading TEXT,
                text TEXT NOT NULL,
                lat REAL,
                lon REAL,
                source_page INTEGER
            );
            CREATE VIRTUAL TABLE paragraphs_fts USING fts5(
                text, harbor_heading, subsection_heading,
                content='paragraphs', content_rowid='id'
            );
            """
        )
        conn.executemany(
            """INSERT INTO paragraphs
               (volume, chapter, paragraph_number, harbor_heading, subsection_heading,
                text, lat, lon, source_page)
               VALUES (:volume, :chapter, :paragraph_number, :harbor_heading,
                       :subsection_heading, :text, :lat, :lon, :source_page)""",
            paragraphs,
        )
        conn.execute(
            "INSERT INTO paragraphs_fts (rowid, text, harbor_heading, subsection_heading) "
            "SELECT id, text, harbor_heading, subsection_heading FROM paragraphs"
        )
        conn.commit()
    finally:
        conn.close()


def database_status() -> dict[str, Any]:
    if not DB_PATH.exists():
        return {"exists": False, "row_count": 0, "volumes": [], "ingested_at": None}
    conn = _connect()
    try:
        row_count = conn.execute("SELECT COUNT(*) FROM paragraphs").fetchone()[0]
        volumes = [r[0] for r in conn.execute("SELECT DISTINCT volume FROM paragraphs ORDER BY volume")]
        mtime = DB_PATH.stat().st_mtime
    finally:
        conn.close()
    import datetime

    return {
        "exists": True,
        "row_count": row_count,
        "volumes": volumes,
        "ingested_at": datetime.datetime.fromtimestamp(mtime).isoformat(),
    }


def search(
    query: str,
    limit: int = 10,
    volume: int | None = None,
    lat: float | None = None,
    lon: float | None = None,
    radius_nm: float | None = None,
) -> list[dict[str, Any]]:
    """Full-text search over ingested Coast Pilot paragraphs.

    Raises CoastPilotError (actionable, names the ingest script) if the
    database hasn't been built yet. If lat/lon/radius_nm are given,
    results are additionally filtered to paragraphs with a (possibly
    inherited) coordinate within radius_nm - note this drops paragraphs
    with no coordinate signal at all, which is most of them; omit
    lat/lon for a pure text search across the whole country.
    """
    if not DB_PATH.exists():
        raise CoastPilotError(
            "Coast Pilot search index hasn't been built yet. Run "
            "`python scripts/ingest_coast_pilot.py` (one-time, ~77MB download, "
            "takes several minutes) before using marine_search_coast_pilot."
        )

    conn = _connect()
    try:
        sql = """
            SELECT p.volume, p.chapter, p.paragraph_number, p.harbor_heading,
                   p.subsection_heading, p.text, p.lat, p.lon, p.source_page,
                   bm25(paragraphs_fts) AS rank
            FROM paragraphs_fts
            JOIN paragraphs p ON p.id = paragraphs_fts.rowid
            WHERE paragraphs_fts MATCH ?
        """
        params: list[Any] = [query]
        if volume is not None:
            sql += " AND p.volume = ?"
            params.append(volume)
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit if lat is None else max(limit * 5, 50))

        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        raise CoastPilotError(f"Coast Pilot search query failed ({exc!r}).") from exc
    finally:
        conn.close()

    results = [
        {
            "volume": r[0],
            "chapter": r[1],
            "paragraph_number": r[2],
            "harbor_heading": r[3],
            "subsection_heading": r[4],
            "text": r[5],
            "lat": r[6],
            "lon": r[7],
            "source_page": r[8],
        }
        for r in rows
    ]

    if lat is not None and lon is not None and radius_nm is not None:
        filtered = []
        for row in results:
            if row["lat"] is None or row["lon"] is None:
                continue
            distance = haversine_nm(lat, lon, row["lat"], row["lon"])
            if distance <= radius_nm:
                filtered.append({**row, "_distance_nm": round(distance, 1)})
        results = sorted(filtered, key=lambda r: r["_distance_nm"])[:limit]

    return results
