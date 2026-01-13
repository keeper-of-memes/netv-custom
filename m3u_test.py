"""Tests for m3u.py."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def m3u_module(tmp_path: Path):
    """Import m3u module with mocked cache."""
    import cache

    cache.SERVER_SETTINGS_FILE = tmp_path / "server_settings.json"
    cache.USERS_DIR = tmp_path / "users"
    cache.USERS_DIR.mkdir(exist_ok=True)
    cache.CACHE_DIR = tmp_path / "cache"
    cache.CACHE_DIR.mkdir(exist_ok=True)
    cache.get_cache().clear()

    import m3u

    yield m3u

    cache.get_cache().clear()


class TestParseM3u:
    def test_parse_basic_m3u(self, m3u_module):
        content = """#EXTM3U
#EXTINF:-1 tvg-id="ch1" tvg-logo="http://logo.png" group-title="News",Channel One
http://stream.example.com/ch1.m3u8
#EXTINF:-1 tvg-id="ch2" group-title="Sports",Channel Two
http://stream.example.com/ch2.m3u8
"""
        cats, streams, _ = m3u_module.parse_m3u(content, "src1")

        assert len(cats) == 2
        assert any(c["category_name"] == "News" for c in cats)
        assert any(c["category_name"] == "Sports" for c in cats)

        assert len(streams) == 2
        assert streams[0]["name"] == "Channel One"
        assert streams[0]["epg_channel_id"] == "ch1"
        assert streams[0]["stream_icon"] == "http://logo.png"
        assert streams[0]["direct_url"] == "http://stream.example.com/ch1.m3u8"
        assert streams[0]["source_id"] == "src1"

    def test_parse_m3u_with_epg_url(self, m3u_module):
        content = """#EXTM3U url-tvg="http://epg.example.com/guide.xml"
#EXTINF:-1,Test Channel
http://test.stream
"""
        _, _, epg_url = m3u_module.parse_m3u(content, "src1")
        assert epg_url == "http://epg.example.com/guide.xml"

    def test_parse_m3u_x_tvg_url(self, m3u_module):
        content = """#EXTM3U x-tvg-url="http://alt.epg.com/guide.xml"
#EXTINF:-1,Test Channel
http://test.stream
"""
        _, _, epg_url = m3u_module.parse_m3u(content, "src1")
        assert epg_url == "http://alt.epg.com/guide.xml"

    def test_parse_m3u_uncategorized(self, m3u_module):
        content = """#EXTM3U
#EXTINF:-1,No Group Channel
http://stream.example.com/nogroupch.m3u8
"""
        cats, streams, _ = m3u_module.parse_m3u(content, "src1")
        assert len(cats) == 1
        assert cats[0]["category_name"] == "Uncategorized"
        assert streams[0]["category_ids"][0].endswith("_uncategorized")

    def test_parse_m3u_category_ids_prefixed(self, m3u_module):
        content = """#EXTM3U
#EXTINF:-1 group-title="Movies",Test
http://test
"""
        cats, streams, _ = m3u_module.parse_m3u(content, "mysource")
        assert cats[0]["category_id"].startswith("mysource_")
        assert streams[0]["category_ids"][0].startswith("mysource_")

    def test_parse_m3u_empty(self, m3u_module):
        cats, streams, epg_url = m3u_module.parse_m3u("", "src1")
        assert cats == []
        assert streams == []
        assert epg_url == ""


class TestParseEpgUrls:
    def test_parse_tuple_list(self, m3u_module):
        raw = [["http://epg1.com", 120, "src1"], ["http://epg2.com", 60, "src2"]]
        result = m3u_module.parse_epg_urls(raw)
        assert len(result) == 2
        assert result[0] == ("http://epg1.com", 120, "src1")
        assert result[1] == ("http://epg2.com", 60, "src2")

    def test_parse_tuple_passthrough(self, m3u_module):
        raw = [("http://epg.com", 100, "s1")]
        result = m3u_module.parse_epg_urls(raw)
        assert result[0] == ("http://epg.com", 100, "s1")

    def test_parse_empty(self, m3u_module):
        assert m3u_module.parse_epg_urls([]) == []

    def test_parse_skips_malformed(self, m3u_module):
        raw = [["http://epg.com", 90], "plain_string", ["http://valid.com", 60, "src"]]
        result = m3u_module.parse_epg_urls(raw)
        assert len(result) == 1
        assert result[0] == ("http://valid.com", 60, "src")


class TestFetchLocks:
    def test_get_fetch_lock(self, m3u_module):
        lock = m3u_module.get_fetch_lock("live")
        assert lock is not None

    def test_get_refresh_in_progress(self, m3u_module):
        rip = m3u_module.get_refresh_in_progress()
        assert isinstance(rip, set)


if __name__ == "__main__":
    from testing import run_tests

    run_tests(__file__)
