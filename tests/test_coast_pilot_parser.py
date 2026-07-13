import pytest

from marine_wx_mcp.sources import coast_pilot
from marine_wx_mcp.sources.coast_pilot import (
    CoastPilotError,
    _dms_to_decimal,
    _is_subsection_label,
    extract_coordinates,
    find_chapter,
    parse_pages,
    rebuild_database,
    search,
)


def test_dms_to_decimal_north_east_positive():
    assert _dms_to_decimal("42", "21", "38", "N") == pytest.approx(42.3606, abs=0.001)


def test_dms_to_decimal_south_west_negative():
    assert _dms_to_decimal("87", "48", "48", "W") == pytest.approx(-87.8133, abs=0.001)
    assert _dms_to_decimal("10", "0", None, "S") == -10.0


def test_extract_coordinates_finds_pair():
    text = 'Waukegan Harbor Light (42°21\'38"N., 87°48\'48"W.), 36 feet above the water'
    coords = extract_coordinates(text)
    assert len(coords) == 1
    lat, lon = coords[0]
    assert lat == pytest.approx(42.3606, abs=0.001)
    assert lon == pytest.approx(-87.8133, abs=0.001)


def test_extract_coordinates_missing_seconds():
    text = 'Light (45°19\'18N., 85°17\'54"W.)'
    coords = extract_coordinates(text)
    assert len(coords) == 1


def test_extract_coordinates_none_found():
    assert extract_coordinates("No coordinates here.") == []


def test_find_chapter_either_order():
    assert find_chapter("334    U.S. Coast Pilot 6, Chapter 11   05 JUL 2026") == 11
    assert find_chapter("05 JUL 2026 U.S. Coast Pilot 6, Chapter 11    335") == 11
    assert find_chapter("no header here") is None


def test_is_subsection_label_recognizes_standard_categories():
    assert _is_subsection_label("Channels")
    assert _is_subsection_label("Small-craft facilities")
    assert _is_subsection_label("Weather, Lake Michigan")
    assert _is_subsection_label("Weather, Lake Huron")


def test_is_subsection_label_rejects_harbor_names():
    assert not _is_subsection_label("Waukegan")
    assert not _is_subsection_label("Calumet Harbor to Northerly Island")


def _mock_page(chapter: int, body: str) -> str:
    return f"05 JUL 2026 U.S. Coast Pilot 6, Chapter {chapter}     391\n{body}"


def test_parse_pages_basic_harbor_and_subsection():
    page = _mock_page(
        11,
        "(722) \n"
        "Waukegan\n"
        '(723)  Waukegan, IL, is a city and small commercial harbor.\n'
        '(724)  Waukegan Harbor Light (42°21\'38"N., 87°48\'48"W.), 36 feet above the water.\n'
        "(725) \n"
        "Channels\n"
        "(726)  The harbor is entered through a dredged entrance channel.\n",
    )

    rows = parse_pages([page], volume=6)

    assert len(rows) == 3
    p723, p724, p726 = rows

    assert p723["paragraph_number"] == 723
    assert p723["harbor_heading"] == "Waukegan"
    assert p723["subsection_heading"] is None
    assert p723["lat"] is None

    assert p724["lat"] == pytest.approx(42.3606, abs=0.001)
    assert p724["lon"] == pytest.approx(-87.8133, abs=0.001)

    assert p726["harbor_heading"] == "Waukegan"
    assert p726["subsection_heading"] == "Channels"
    # inherits Waukegan's coordinate from paragraph 724 even under a
    # different subsection heading
    assert p726["lat"] == pytest.approx(42.3606, abs=0.001)
    assert p726["chapter"] == 11


def test_parse_pages_new_harbor_resets_subsection_and_coordinate():
    page = _mock_page(
        11,
        "(700) \n"
        "Portville\n"
        '(701)  Light (10°00\'00"N., 20°00\'00"W.).\n'
        "(702) \n"
        "Channels\n"
        "(703)  Some channel text for Portville.\n"
        "(704) \n"
        "Kenosha\n"
        "(705)  Kenosha, WI, is a small city.\n",
    )

    rows = parse_pages([page], volume=6)
    by_number = {r["paragraph_number"]: r for r in rows}

    assert by_number[703]["harbor_heading"] == "Portville"
    assert by_number[703]["lat"] == pytest.approx(10.0)

    # new harbor with no coordinate of its own must NOT inherit Portville's
    assert by_number[705]["harbor_heading"] == "Kenosha"
    assert by_number[705]["subsection_heading"] is None
    assert by_number[705]["lat"] is None


def test_parse_pages_empty_pages_returns_no_rows():
    assert parse_pages([], volume=6) == []
    assert parse_pages([_mock_page(11, "no paragraph markers here")], volume=6) == []


def test_parse_pages_heading_does_not_leak_across_chapter_boundary():
    # Regression test: the whole volume is one concatenated text stream,
    # so without an explicit reset a heading from one chapter (here
    # "Current", picked up as a harbor-level heading since it isn't in
    # the subsection allowlist) would otherwise keep propagating as the
    # "harbor" for real content in a later chapter's harbor entries.
    chapter_a = _mock_page(2, "(10) \nCurrent\n(11)  Some regulatory text mentioning currents.\n")
    chapter_b = _mock_page(4, "(700) \nEastport\n(701)  Eastport is a customs port of entry.\n")

    rows = parse_pages([chapter_a, chapter_b], volume=1)
    by_number = {r["paragraph_number"]: r for r in rows}

    assert by_number[11]["harbor_heading"] == "Current"
    assert by_number[11]["chapter"] == 2

    assert by_number[701]["harbor_heading"] == "Eastport"
    assert by_number[701]["chapter"] == 4


def test_parse_pages_forward_fills_chapter_across_headerless_pages():
    page_with_header = _mock_page(4, "(700) \nEastport\n(701)  Eastport is a customs port of entry.\n")
    page_without_header = "(702)  More Eastport text with no running header on this page.\n"

    rows = parse_pages([page_with_header, page_without_header], volume=1)
    by_number = {r["paragraph_number"]: r for r in rows}

    assert by_number[702]["chapter"] == 4
    assert by_number[702]["harbor_heading"] == "Eastport"


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    monkeypatch.setattr(coast_pilot, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(coast_pilot, "DB_PATH", tmp_path / "coast_pilot.db")
    return tmp_path / "coast_pilot.db"


def test_search_raises_when_database_missing(temp_db):
    with pytest.raises(CoastPilotError, match="ingest_coast_pilot"):
        search("Waukegan")


def test_rebuild_and_search_roundtrip(temp_db):
    paragraphs = [
        {
            "volume": 6, "chapter": 11, "paragraph_number": 723,
            "harbor_heading": "Waukegan", "subsection_heading": None,
            "text": "Waukegan, IL, is a city and small commercial harbor.",
            "lat": 42.36, "lon": -87.81, "source_page": 58,
        },
        {
            "volume": 1, "chapter": 4, "paragraph_number": 69,
            "harbor_heading": "Eastport", "subsection_heading": None,
            "text": "Eastport is a customs port of entry.",
            "lat": 44.9, "lon": -66.99, "source_page": 161,
        },
    ]
    rebuild_database(paragraphs)

    results = search("Waukegan")
    assert len(results) == 1
    assert results[0]["harbor_heading"] == "Waukegan"

    results = search("harbor OR port")
    assert len(results) == 2

    results = search("port", volume=1)
    assert len(results) == 1
    assert results[0]["volume"] == 1

    results = search("harbor OR port", lat=42.36, lon=-87.81, radius_nm=5)
    assert len(results) == 1
    assert results[0]["harbor_heading"] == "Waukegan"
    assert "_distance_nm" in results[0]
