"""EPG storage and XMLTV parsing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import contextlib
import gzip
import logging
import re
import sqlite3
import threading
import time

import defusedxml.ElementTree as ET  # Safe XML parsing

from util import safe_urlopen


log = logging.getLogger(__name__)


# =============================================================================
# Data Types
# =============================================================================


@dataclass(slots=True)
class Program:
    channel_id: str
    title: str
    start: datetime
    stop: datetime
    desc: str = ""
    source_id: str = ""


# =============================================================================
# SQLite Storage
# =============================================================================

_DB_PATH: Path | None = None
_local = threading.local()


def init(cache_dir: Path) -> None:
    """Initialize EPG database."""
    global _DB_PATH
    _DB_PATH = cache_dir / "epg.db"
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS channels (
            id TEXT PRIMARY KEY,
            name TEXT,
            source_id TEXT
        );
        CREATE TABLE IF NOT EXISTS icons (
            channel_id TEXT PRIMARY KEY,
            url TEXT
        );
        CREATE TABLE IF NOT EXISTS programs (
            id INTEGER PRIMARY KEY,
            channel_id TEXT,
            title TEXT,
            start_ts REAL,
            stop_ts REAL,
            desc TEXT,
            source_id TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_programs_channel_time
            ON programs(channel_id, start_ts, stop_ts);
        CREATE INDEX IF NOT EXISTS idx_programs_time
            ON programs(start_ts);
    """)
    conn.commit()


def _get_conn() -> sqlite3.Connection:
    """Get thread-local database connection."""
    if not hasattr(_local, "conn") or _local.conn is None:
        if _DB_PATH is None:
            raise RuntimeError("EPG database not initialized")
        _local.conn = sqlite3.connect(_DB_PATH, timeout=30.0)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
    return _local.conn


def clear() -> None:
    """Clear all EPG data."""
    conn = _get_conn()
    conn.executescript("DELETE FROM programs; DELETE FROM channels; DELETE FROM icons;")
    conn.commit()


def clear_source(source_id: str) -> None:
    """Clear EPG data for a specific source."""
    conn = _get_conn()
    conn.execute("DELETE FROM programs WHERE source_id = ?", (source_id,))
    conn.execute("DELETE FROM channels WHERE source_id = ?", (source_id,))
    conn.commit()


def insert_channel(channel_id: str, name: str, source_id: str) -> None:
    """Insert or update a channel."""
    conn = _get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO channels (id, name, source_id) VALUES (?, ?, ?)",
        (channel_id, name, source_id),
    )


def insert_icon(channel_id: str, url: str) -> None:
    """Insert or update a channel icon."""
    conn = _get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO icons (channel_id, url) VALUES (?, ?)",
        (channel_id, url),
    )


def insert_programs(programs: list[tuple[str, str, float, float, str, str]]) -> None:
    """Bulk insert programs. Each tuple: (channel_id, title, start_ts, stop_ts, desc, source_id)."""
    conn = _get_conn()
    conn.executemany(
        "INSERT INTO programs (channel_id, title, start_ts, stop_ts, desc, source_id) VALUES (?, ?, ?, ?, ?, ?)",
        programs,
    )


def commit() -> None:
    """Commit current transaction."""
    _get_conn().commit()


def get_icon(channel_id: str) -> str:
    """Get icon URL for a channel."""
    conn = _get_conn()
    row = conn.execute("SELECT url FROM icons WHERE channel_id = ?", (channel_id,)).fetchone()
    return row["url"] if row else ""


def get_programs_in_range(
    channel_id: str,
    start: datetime,
    end: datetime,
    preferred_source_id: str = "",
) -> list[Program]:
    """Get programs for a channel within a time range."""
    conn = _get_conn()
    start_ts = start.timestamp()
    end_ts = end.timestamp()

    rows = conn.execute(
        """
        SELECT channel_id, title, start_ts, stop_ts, desc, source_id
        FROM programs
        WHERE channel_id = ? AND stop_ts > ? AND start_ts < ?
        ORDER BY start_ts
        """,
        (channel_id, start_ts, end_ts),
    ).fetchall()

    programs = [
        Program(
            channel_id=row["channel_id"],
            title=row["title"],
            start=datetime.fromtimestamp(row["start_ts"], tz=UTC),
            stop=datetime.fromtimestamp(row["stop_ts"], tz=UTC),
            desc=row["desc"] or "",
            source_id=row["source_id"] or "",
        )
        for row in rows
    ]

    if not preferred_source_id or len(programs) <= 1:
        return programs

    # Deduplicate overlapping programs, preferring the preferred source
    result: list[Program] = []
    for p in programs:
        dominated = False
        for i, existing in enumerate(result):
            if p.start < existing.stop and p.stop > existing.start:
                if p.source_id == preferred_source_id and existing.source_id != preferred_source_id:
                    result[i] = p
                dominated = True
                break
        if not dominated:
            result.append(p)
    return sorted(result, key=lambda p: p.start)


_MAX_IN_CLAUSE = 500  # SQLite limit is 999, stay well below


def _dedupe_programs(programs: list[Program], preferred_source_id: str) -> list[Program]:
    """Deduplicate overlapping programs, preferring the preferred source."""
    if not preferred_source_id or len(programs) <= 1:
        return programs
    result: list[Program] = []
    for p in programs:
        dominated = False
        for i, existing in enumerate(result):
            # Check for overlap
            if p.start < existing.stop and p.stop > existing.start:
                # Prefer the preferred source
                if p.source_id == preferred_source_id and existing.source_id != preferred_source_id:
                    result[i] = p
                dominated = True
                break
        if not dominated:
            result.append(p)
    return sorted(result, key=lambda p: p.start)


def get_programs_batch(
    channel_ids: list[str],
    start: datetime,
    end: datetime,
    preferred_sources: dict[str, str] | None = None,
) -> dict[str, list[Program]]:
    """Get programs for multiple channels in a single query.

    Args:
        channel_ids: List of EPG channel IDs to query
        start: Start of time window
        end: End of time window
        preferred_sources: Optional dict mapping channel_id -> preferred source_id
            for deduplication. If provided, overlapping programs from the preferred
            source will be kept over programs from other sources.
    """
    if not channel_ids:
        return {}
    conn = _get_conn()
    start_ts = start.timestamp()
    end_ts = end.timestamp()
    result: dict[str, list[Program]] = {ch: [] for ch in channel_ids}

    # Process in chunks to avoid huge IN clauses
    for i in range(0, len(channel_ids), _MAX_IN_CLAUSE):
        chunk = channel_ids[i : i + _MAX_IN_CLAUSE]
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"""
            SELECT channel_id, title, start_ts, stop_ts, desc, source_id
            FROM programs
            WHERE channel_id IN ({placeholders}) AND stop_ts > ? AND start_ts < ?
            ORDER BY channel_id, start_ts
            """,
            [*chunk, start_ts, end_ts],
        ).fetchall()
        for row in rows:
            result[row["channel_id"]].append(
                Program(
                    channel_id=row["channel_id"],
                    title=row["title"],
                    start=datetime.fromtimestamp(row["start_ts"], tz=UTC),
                    stop=datetime.fromtimestamp(row["stop_ts"], tz=UTC),
                    desc=row["desc"] or "",
                    source_id=row["source_id"] or "",
                )
            )

    # Deduplicate overlapping programs if preferred_sources provided
    if preferred_sources:
        for ch_id in result:
            if ch_id in preferred_sources and result[ch_id]:
                result[ch_id] = _dedupe_programs(result[ch_id], preferred_sources[ch_id])

    channels_with_programs = sum(1 for progs in result.values() if progs)
    log.debug(
        "EPG batch query: requested %d channel IDs, found programs for %d",
        len(channel_ids),
        channels_with_programs,
    )
    return result


def get_icons_batch(channel_ids: list[str]) -> dict[str, str]:
    """Get icons for multiple channels in a single query."""
    if not channel_ids:
        return {}
    conn = _get_conn()
    result: dict[str, str] = {}
    for i in range(0, len(channel_ids), _MAX_IN_CLAUSE):
        chunk = channel_ids[i : i + _MAX_IN_CLAUSE]
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT channel_id, url FROM icons WHERE channel_id IN ({placeholders})",
            chunk,
        ).fetchall()
        for row in rows:
            result[row["channel_id"]] = row["url"]
    return result


def has_programs() -> bool:
    """Check if there are any programs in the database."""
    conn = _get_conn()
    row = conn.execute("SELECT 1 FROM programs LIMIT 1").fetchone()
    return row is not None


def get_program_count() -> int:
    """Get total program count."""
    conn = _get_conn()
    row = conn.execute("SELECT COUNT(*) FROM programs").fetchone()
    return row[0] if row else 0


def get_channel_count() -> int:
    """Get total channel count."""
    conn = _get_conn()
    row = conn.execute("SELECT COUNT(*) FROM channels").fetchone()
    return row[0] if row else 0


def prune_old_programs(before: datetime) -> int:
    """Delete programs that ended before the given time. Returns count deleted."""
    conn = _get_conn()
    cursor = conn.execute("DELETE FROM programs WHERE stop_ts < ?", (before.timestamp(),))
    conn.commit()
    return cursor.rowcount


# =============================================================================
# XMLTV Parsing
# =============================================================================


def _parse_epg_time(s: str) -> datetime:
    """Parse XMLTV time format: 20241130120000 +0000 or 20241130120000+0530."""
    s = s.replace(" ", "")
    if len(s) >= 14:
        dt = datetime.strptime(s[:14], "%Y%m%d%H%M%S")
        if len(s) > 14:
            tz_str = s[14:]
            sign = -1 if tz_str[0] == "-" else 1
            tz_hours = int(tz_str[1:3]) if len(tz_str) >= 3 else 0
            tz_mins = int(tz_str[3:5]) if len(tz_str) >= 5 else 0
            offset = timedelta(hours=tz_hours, minutes=tz_mins)
            dt = dt.replace(tzinfo=timezone(sign * offset))
        return dt
    return datetime.now(UTC)


def _sanitize_epg_xml(xml_str: str) -> str:
    """Try to fix corrupted EPG XML by extracting valid elements."""
    channels = re.findall(r"<channel\s+[^>]*>.*?</channel>", xml_str, re.DOTALL)
    programmes = re.findall(
        r'<programme\s+start="[^"<>]+"\s+stop="[^"<>]+"\s+channel="[^"<>]+"[^>]*>.*?</programme>',
        xml_str,
        re.DOTALL,
    )
    log.info("Sanitized EPG: extracted %d channels, %d programmes", len(channels), len(programmes))
    return '<?xml version="1.0"?>\n<tv>\n' + "\n".join(channels) + "\n".join(programmes) + "\n</tv>"


def fetch_epg(
    epg_url: str,
    cache_dir: Path,
    timeout: int = 120,
    source_id: str = "",
) -> int:
    """Fetch and parse XMLTV EPG data directly into sqlite.

    Returns number of programs inserted.
    """
    with safe_urlopen(epg_url, timeout=timeout) as resp:
        content = resp.read()
        with contextlib.suppress(Exception):
            content = gzip.decompress(content)
        xml_str = content.decode("utf-8")

    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError as e:
        debug_file = cache_dir / f"epg_debug_{int(time.time())}.xml"
        debug_file.write_text(xml_str)
        log.warning("EPG parse failed (%s), attempting sanitization...", e)
        try:
            sanitized = _sanitize_epg_xml(xml_str)
            root = ET.fromstring(sanitized)
            log.info("Sanitized EPG parsed successfully")
        except ET.ParseError as e2:
            log.error("Sanitized EPG also failed: %s", e2)
            raise

    # Parse channels directly into sqlite
    channel_ids: set[str] = set()
    for ch in root.findall("channel"):
        ch_id = ch.get("id", "")
        channel_ids.add(ch_id)
        name_el = ch.find("display-name")
        name = name_el.text if name_el is not None and name_el.text else ch_id
        insert_channel(ch_id, name, source_id)
        icon_el = ch.find("icon")
        if icon_el is not None:
            insert_icon(ch_id, icon_el.get("src", ""))

    # Parse programs in batches
    batch: list[tuple[str, str, float, float, str, str]] = []
    batch_size = 10000
    program_count = 0
    program_channel_ids: set[str] = set()

    for prog in root.findall("programme"):
        ch_id = prog.get("channel", "")
        program_channel_ids.add(ch_id)
        start_str = prog.get("start", "")
        stop_str = prog.get("stop", "")

        title_el = prog.find("title")
        title = title_el.text if title_el is not None and title_el.text else "Unknown"

        desc_el = prog.find("desc")
        desc = desc_el.text if desc_el is not None and desc_el.text else ""

        try:
            start = _parse_epg_time(start_str)
            stop = _parse_epg_time(stop_str)
        except Exception:
            continue

        batch.append((ch_id, title, start.timestamp(), stop.timestamp(), desc, source_id))
        program_count += 1

        if len(batch) >= batch_size:
            insert_programs(batch)
            batch.clear()

    if batch:
        insert_programs(batch)

    commit()
    log.debug(
        "EPG parsed: %d channels, %d unique program channel IDs, %d programs",
        len(channel_ids),
        len(program_channel_ids),
        program_count,
    )
    return program_count
