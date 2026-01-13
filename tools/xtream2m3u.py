#!/usr/bin/env python3
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportImplicitStringConcatenation=false, reportUnknownParameterType=false

# M3U Details:
#   https://github.com/HamzaBhf00/m3u-tags-iptv
# Xtream Codes:
#   https://github.com/engenex/xtream-codes-api-v2/blob/main/%5BHow-To%5D%20Player%20API%20v2%20-%20Tutorials%20-%20Xtream%20Codes.pdf
from __future__ import annotations

from typing import Any, Protocol

import collections
import concurrent.futures
import functools
import gzip  # lzma(80%), bz2(78%), gzip(75%) but gzip was fastest.
import json
import math
import pathlib
import pickle
import shutil
import threading
import time
import urllib
import urllib.error
import urllib.parse
import urllib.request


class RetryableError(Exception):
    pass


TOOLS_DIR = pathlib.Path(__file__).parent.resolve()
CONFIG_FILE = TOOLS_DIR / "xtream.json"
TEMPDIR = TOOLS_DIR
DESTDIR = TOOLS_DIR


def _load_config() -> dict:
    """Load config from xtream.json, creating template if missing."""
    if not CONFIG_FILE.exists():
        template = {
            "url": "https://your-provider.com",
            "username": "your_username",
            "password": "your_password",
            "live_filter": {},
            "locals_group": "",
            "locals_filter": [],
        }
        CONFIG_FILE.write_text(json.dumps(template, indent=2))
        raise SystemExit(f"Created {CONFIG_FILE} - edit with your credentials and re-run")
    return json.loads(CONFIG_FILE.read_text())


def _get_urls() -> tuple[str, str, str]:
    """Return (api_url, get_url, epg_url) from config."""
    cfg = _load_config()
    base = cfg["url"].rstrip("/")
    user, passwd = cfg["username"], cfg["password"]
    api = f"{base}/player_api.php?username={user}&password={passwd}"
    get = f"{base}/get.php?username={user}&password={passwd}"
    epg = f"{base}/xmltv.php?username={user}&password={passwd}"
    return api, get, epg


def _get_filters() -> tuple[dict[int, str], str, set[str]]:
    """Return (live_filter, locals_group, locals_filter) from config."""
    cfg = _load_config()
    live_filter = {int(k): v for k, v in cfg.get("live_filter", {}).items()}
    locals_group = cfg.get("locals_group", "")
    locals_filter = set(cfg.get("locals_filter", []))
    return live_filter, locals_group, locals_filter


def main(cached_only: bool = False) -> None:
    api_url, _, epg_url = _get_urls()
    live_filter, locals_group, locals_filter = _get_filters()

    if not cached_only:
        fetch_all_data(api_url)

    auth = load_dict("authentication.json")
    iptv_url = process_iptv_url(auth)

    live, live_categories = process(
        load_list("get_live_stream.json"),
        load_list("get_live_categories.json"),
        iptv_url,
    )
    del live_categories
    live = filter_live(live, live_filter, locals_group, locals_filter)
    write_m3u_live(live, auth, epg_url)

    vod_url = list(iptv_url)
    vod_url.insert(2, "movie")
    vod, vod_categories = process(
        load_list("get_vod_streams.json"),
        load_list("get_vod_categories.json"),
        vod_url,
    )
    del vod_categories
    write_m3u_vod(vod, auth)

    series_url = list(iptv_url)
    series_url.insert(2, "series")
    series, series_categories = process(
        load_list("get_series.json"),
        load_list("get_series_categories.json"),
        series_url,
    )
    del series_categories
    series_info = fetch_series_info(series, api_url, cached_only=cached_only)
    write_m3u_series(series, series_info, auth, series_url)


###############################################################################
#  ____            __                       _                                 #
# |  _ \    ___   / _|  _ __    ___   ___  | |__                              #
# | |_) |  / _ \ | |_  | '__|  / _ \ / __| | '_ \                             #
# |  _ <  |  __/ |  _| | |    |  __/ \__ \ | | | |                            #
# |_| \_\  \___| |_|   |_|     \___| |___/ |_| |_|                            #
#                                                                             #
###############################################################################


def fetch_all_data(api_url: str) -> None:
    if False:  # Intentionally disabled debug code
        r = fetch_text(api_url + "&type=m3u_plus").encode("utf-8")  # pyright: ignore[reportUnreachable]
        with gzip.open(TEMPDIR / "xtream.m3u.gz", "wb") as f:
            f.write(r)

    print("Fetching authentication...", end=" ", flush=True)
    t0 = time.perf_counter()
    r = fetch_text(api_url)
    with open(TEMPDIR / "authentication.json", "w") as f:
        f.write(r)
    print(f"({time.perf_counter() - t0:.1f}s)")

    print("Fetching live streams...", end=" ", flush=True)
    t0 = time.perf_counter()
    r = fetch_text(api_url + "&action=get_live_streams", timeout=120)
    with open(TEMPDIR / "get_live_stream.json", "w") as f:
        f.write(r)
    print(f"({time.perf_counter() - t0:.1f}s)")

    print("Fetching live categories...", end=" ", flush=True)
    t0 = time.perf_counter()
    r = fetch_text(api_url + "&action=get_live_categories")
    with open(TEMPDIR / "get_live_categories.json", "w") as f:
        f.write(r)
    print(f"({time.perf_counter() - t0:.1f}s)")

    print("Fetching series...", end=" ", flush=True)
    t0 = time.perf_counter()
    r = fetch_text(api_url + "&action=get_series", timeout=120)
    with open(TEMPDIR / "get_series.json", "w") as f:
        f.write(r)
    print(f"({time.perf_counter() - t0:.1f}s)")

    print("Fetching series categories...", end=" ", flush=True)
    t0 = time.perf_counter()
    r = fetch_text(api_url + "&action=get_series_categories")
    with open(TEMPDIR / "get_series_categories.json", "w") as f:
        f.write(r)
    print(f"({time.perf_counter() - t0:.1f}s)")

    print("Fetching VOD streams...", end=" ", flush=True)
    t0 = time.perf_counter()
    r = fetch_text(api_url + "&action=get_vod_streams", timeout=120)
    with open(TEMPDIR / "get_vod_streams.json", "w") as f:
        f.write(r)
    print(f"({time.perf_counter() - t0:.1f}s)")

    print("Fetching VOD categories...", end=" ", flush=True)
    t0 = time.perf_counter()
    r = fetch_text(api_url + "&action=get_vod_categories")
    with open(TEMPDIR / "get_vod_categories.json", "w") as f:
        f.write(r)
    print(f"({time.perf_counter() - t0:.1f}s)")


def fetch_text(url: str, timeout: int = 5) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Unsupported URL scheme: {parsed.scheme}")

    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RetryableError(f"Unable to get {url}; http error {e.code}.") from e
        raise ValueError(f"Unable to get {url}; http error {e.code}.") from e
    except (urllib.error.URLError, TimeoutError) as e:
        reason = e.reason if isinstance(e, urllib.error.URLError) else str(e)
        raise RetryableError(f"Unable to get {url}; timeout {reason}.") from e


def fetch_series_info(
    series: dict[int, dict[str, Any]],
    api_url: str,
    cached_only: bool = False,
) -> dict[int, Any]:
    series_info: dict[int, None | Any] = {}
    try:
        with gzip.open(TEMPDIR / "series_info.pickle.gz", "rb") as f:
            series_info = pickle.load(f)
    except Exception as e:
        print(f"Cache miss: {e}")
        series_info = dict.fromkeys(series.keys())

    if cached_only:
        return series_info

    changed = False
    refetch_count = 0

    for k in series:
        series_info.setdefault(k, None)
        try:
            t = int(series_info[k]["info"]["last_modified"])  # pyright: ignore[reportOptionalSubscript]
        except (KeyError, TypeError, ValueError):
            t = -1
        if series[k]["last_modified"] > t:
            refetch_count += 1
            series_info[k] = None

    print(f"Marked {refetch_count}/{len(series)} series for re/fetch.")

    for k in tuple(series_info.keys()):
        if k in series:
            continue
        changed = True
        del series_info[k]

    progress_lock = threading.Lock()
    progress_count = sum(v is not None for v in series_info.values())
    limiter = SlidingRateLimiter(max_calls=4, per_seconds=1)
    task_ = functools.partial(
        _task,
        limiter=limiter,
        series_info=series_info,
        api_url=api_url,
        progress_lock=progress_lock,
        progress_count_ref=[progress_count],
    )

    retries = -1
    max_retries = 3
    max_workers = math.ceil(1.5 * limiter.max_calls)

    while (retries := retries + 1) < max_retries and (
        ids := [k for k, v in series_info.items() if v is None]
    ):
        changed = True
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
        try:
            list(executor.map(task_, ids))
            executor.shutdown(wait=True)
        except KeyboardInterrupt:
            print("\nCancelling...")
            executor.shutdown(wait=False, cancel_futures=True)
            raise

    if changed:
        pickle_filename = TEMPDIR / "series_info.pickle.gz"
        with gzip.open(pickle_filename.with_suffix(".tmp"), "wb") as f:
            pickle.dump(series_info, f)
        shutil.move(pickle_filename.with_suffix(".tmp"), pickle_filename)

    return series_info


class RateLimiter(Protocol):
    def __init__(self, max_calls: int, per_seconds: float = 1): ...

    def acquire(self) -> None: ...


def _task(
    id_: int,
    limiter: RateLimiter,
    series_info: dict[int, Any],
    api_url: str,
    progress_lock: threading.Lock,
    progress_count_ref: list[int],
) -> None:
    try:
        limiter.acquire()
        result = json.loads(
            fetch_text(
                url=f"{api_url}&action=get_series_info&series_id={id_}",
                timeout=60,
            )
        )
        series_info[id_] = result
        with progress_lock:
            progress_count_ref[0] += 1
            print_progress_bar(
                iteration=progress_count_ref[0],
                total=len(series_info),
            )
    except RetryableError as e:
        print(e)
    except (json.JSONDecodeError, ValueError, KeyError) as e:
        print(e)
        series_info[id_] = {}


def filter_live(
    live: dict[int, dict[str, Any]],
    live_filter: dict[int, str],
    locals_group: str,
    locals_filter: set[str],
) -> dict[int, dict[str, Any]]:
    if live_filter:
        live_ = collections.defaultdict(dict)
        for k, v in live.items():
            if not any(c in live_filter for c in v["category_ids"]):
                continue
            if len(v["group-title"]) != 1:
                raise ValueError(f"Expected single group-title, got {v['group-title']}")
            live_[v["group-title"][0]][k] = v
        live = {}
        for v in live_filter.values():
            live.update(live_[v])

    if locals_group and locals_filter:
        live = {
            k: v
            for k, v in live.items()
            if (
                locals_group not in v["group-title"]
                or any(c in v["tvg-name"] for c in locals_filter)
            )
        }

    return live


###############################################################################
#  ____                                                                       #
# |  _ \    __ _   _ __   ___    ___                                          #
# | |_) |  / _` | | '__| / __|  / _ \                                         #
# |  __/  | (_| | | |    \__ \ |  __/                                         #
# |_|      \__,_| |_|    |___/  \___|                                         #
#                                                                             #
###############################################################################


def process(
    elements: list[dict[str, Any]],
    categories: list[dict[str, Any]],
    iptv_url: list[str],
) -> tuple[dict[int, dict[str, Any]], dict[int, Any]]:
    categories_dict: dict[int, str] = {
        int(c["category_id"]): c["category_name"] for c in categories
    }
    elements_dict: dict[int, dict[str, None | int | str | list[str]]] = {}
    for s in elements:
        stream_type = s.get("stream_type")
        if stream_type in ("live", "radio_streams"):
            id_ = int(s["stream_id"])
            attr = {
                "tvg-name": s["name"] or s["title"],
                "tvg-logo": s["stream_icon"],
                "group-title": [categories_dict[c] for c in s["category_ids"]],
                "tvg-id": "" if s["epg_channel_id"] is None else s["epg_channel_id"],
                "url": "/".join([*iptv_url, str(id_)]),
                "category_ids": s["category_ids"],
                "year": None,
                "rating": None,
                "num": s["num"],
                "last_modified": None,
                # 'timeshift': None, ???
            }
            if s["tv_archive"] not in (0, 1):
                raise ValueError(f"Invalid tv_archive value: {s}")
            # assert not s["direct_source"], s
        elif stream_type == "series" or s.get("series_id") is not None:
            id_ = int(s["series_id"])
            attr = {
                "tvg-name": s["name"] or s["title"],
                "tvg-logo": s["cover"],
                "group-title": [categories_dict[c] for c in s["category_ids"]],
                "tvg-id": None,
                "url": None,
                "category_ids": s["category_ids"],
                "year": toint(s.get("year")),
                "rating": tofloat(s.get("rating")),
                "num": s["num"],
                "last_modified": int(s["last_modified"]),
            }
        elif stream_type == "movie":
            id_ = int(s["stream_id"])
            attr = {
                "tvg-name": s["name"] or s["title"],
                "tvg-logo": s["stream_icon"],
                "group-title": [categories_dict[c] for c in s["category_ids"]],
                "tvg-id": None,
                "url": "/".join([*iptv_url, f"{id_}.{s['container_extension']}"]),
                "category_ids": s["category_ids"],
                "year": toint(s.get("year")),
                "rating": tofloat(s.get("rating")),
                "num": s["num"],
                "last_modified": None,
            }
        else:
            print(f"Unrecognized {stream_type=}: {s}")
            continue
        if id_ in elements_dict:
            raise ValueError(f"Duplicate id {id_}: {attr}")
        elements_dict[id_] = attr

    return elements_dict, categories_dict


def process_iptv_url(auth: dict[str, dict[str, Any]]) -> list[str]:
    if (status := auth["user_info"]["status"]) != "Active":
        raise ValueError(f"Unsupported {status=}.")
    if (max_connections := int(auth["user_info"]["max_connections"])) < 1:
        raise ValueError(f"Insufficient {max_connections=}.")
    if (server_protocol := auth["server_info"]["server_protocol"]) not in (
        "http",
        "https",
    ):
        raise ValueError(f"Unrecognized {server_protocol=}.")
    # We used to respect server protocol but now we just force HTTPS.
    server_protocol = "https"
    port_key = server_protocol + "_port"
    port = auth["server_info"].get(port_key, auth["server_info"][port_key])
    return [
        f"{server_protocol}:/",  # We'll join everything with slashes later.
        f"{auth['server_info']['url']}:{port}",
        auth["user_info"]["username"],
        auth["user_info"]["password"],
    ]


def toint(x: str | None) -> int | None:
    return int(x) if x else None


def tofloat(x: str | None) -> float | None:
    return float(x) if x else None


def load(filename: str) -> Any:
    with open(TEMPDIR / filename) as f:
        return json.load(f)


def load_dict(filename: str) -> dict[str, Any]:
    result = load(filename)
    if not isinstance(result, dict):
        raise TypeError(f"Expected dict from {filename}, got {type(result)}")
    return result


def load_list(filename: str) -> list[dict[str, Any]]:
    result = load(filename)
    if not isinstance(result, list):
        raise TypeError(f"Expected list from {filename}, got {type(result)}")
    return result


class SlidingRateLimiter:
    def __init__(self, max_calls: int, per_seconds: float = 1):
        self.max_calls = max_calls
        self.per_seconds = per_seconds
        self.lock = threading.Lock()
        self.requests = collections.deque()

    def acquire(self) -> None:
        while True:
            with self.lock:
                cutoff = time.perf_counter() - self.per_seconds
                while self.requests and self.requests[0] <= cutoff:
                    self.requests.popleft()
                if len(self.requests) < self.max_calls:
                    self.requests.append(time.perf_counter())
                    return
                sleep_time = max(0, self.requests[0] - cutoff)
            time.sleep(sleep_time)


class ChunkingRateLimiter:
    def __init__(self, max_calls: int, per_seconds: float = 1):
        self.max_calls = max_calls
        self.per_seconds = per_seconds
        self.condition = threading.Condition()
        self.calls = 0
        self.last_reset = time.perf_counter()

    def acquire(self) -> None:
        with self.condition:
            if self.calls >= self.max_calls:
                now = time.perf_counter()
                elapsed = now - self.last_reset
                if elapsed < self.per_seconds:
                    sleep_time = self.per_seconds - elapsed
                    self.condition.wait(timeout=sleep_time)
                    # Basically just,
                    # self.lock.release()
                    # time.sleep(sleep_time)
                    # self.lock.acquire()
                self.calls = 0
                self.last_reset = time.perf_counter()
            self.calls += 1


def print_progress_bar(
    iteration: int,
    total: int,
    prefix: str = "",
    suffix: str = "",
    decimals: int = 1,
    length: int = 50,
    fill: str = "█",
    printEnd: str = "\r",
) -> None:
    r"""Call in a loop to create terminal progress bar
    @params:
        iteration   - Required  : current iteration (Int)
        total       - Required  : total iterations (Int)
        prefix      - Optional  : prefix string (Str)
        suffix      - Optional  : suffix string (Str)
        decimals    - Optional  : positive number of decimals in percent complete (Int)
        length      - Optional  : character length of bar (Int)
        fill        - Optional  : bar fill character (Str)
        printEnd    - Optional  : end character (e.g. "\r", "\r\n") (Str)
    """
    if total == 0:
        return
    percent = ("{0:." + str(decimals) + "f}").format(100 * (iteration / float(total)))
    filledLength = int(length * iteration // total)
    bar = fill * filledLength + "-" * (length - filledLength)
    print(f"\r{prefix} |{bar}| {percent}% {suffix}", end=printEnd)
    # Print New Line on Complete
    if iteration == total:
        print()


def write_m3u_live(
    live: dict[int, dict[str, Any]],
    auth: dict[str, dict[str, Any]],
    epg_url: str,
) -> None:
    with open(DESTDIR / "live.m3u", "w") as f:
        print(f'#EXTM3U url-tvg="{epg_url}" x-tvg-url="{epg_url}"', file=f)
        if auth["server_info"].get("xui") is not None:
            version = auth["server_info"]["version"]
            print(f'#EXT-X-SESSION-DATA:DATA-ID="com.xui.{version}"', file=f)
        for v in live.values():
            tvg_name = v["tvg-name"]
            tvg_logo = v["tvg-logo"]
            group_title = "TV | " + v["group-title"][0]
            url = v["url"]
            tvg_id = v["tvg-id"]
            print(
                f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-name="{tvg_name}" '
                f'tvg-logo="{tvg_logo}" group-title="{group_title}",{tvg_name}',
                file=f,
            )
            print(url, file=f)


def write_m3u_vod(
    vod: dict[int, dict[str, Any]],
    auth: dict[str, dict[str, Any]],
) -> None:
    with open(DESTDIR / "vod.m3u", "w") as f:
        print("#EXTM3U", file=f)
        if auth["server_info"].get("xui") is not None:
            version = auth["server_info"]["version"]
            print(f'#EXT-X-SESSION-DATA:DATA-ID="com.xui.{version}"', file=f)
        for v in vod.values():
            tvg_name = v["tvg-name"]
            tvg_logo = v["tvg-logo"]
            group_title = "VOD | " + v["group-title"][0]
            url = v["url"]
            print(
                f'#EXTINF:-1 tvg-name="{tvg_name}" tvg-logo="{tvg_logo}" '
                f'group-title="{group_title}",{tvg_name}',
                file=f,
            )
            print(url, file=f)


def write_m3u_series(
    series: dict[int, dict[str, Any]],
    series_info: dict[int, None | Any],
    auth: dict[str, dict[str, Any]],
    series_url: list[str],
) -> None:
    series_episodes = {}
    for k in series:
        info = series_info.get(k)
        if not info or "episodes" not in info:
            continue
        try:
            series_episodes[k] = list(_descend(info["episodes"]))
        except Exception as e:
            print(f"Series {k}: {e}")

    with open(DESTDIR / "series.m3u", "w") as f:
        print("#EXTM3U", file=f)
        if auth["server_info"].get("xui") is not None:
            version = auth["server_info"]["version"]
            print(f'#EXT-X-SESSION-DATA:DATA-ID="com.xui.{version}"', file=f)
        for k, vv in series_episodes.items():
            v = series[k]
            tvg_logo = v["tvg-logo"]
            group_title = "Series | " + v["group-title"][0]
            for e in vv:
                tvg_name = e["title"]
                url = "/".join([*series_url, f"{e['id']}.{e['container_extension']}"])
                print(
                    f'#EXTINF:-1 tvg-name="{tvg_name}" tvg-logo="{tvg_logo}" '
                    f'group-title="{group_title}",{tvg_name}',
                    file=f,
                )
                print(url, file=f)


def _descend(x: Any):
    if isinstance(x, dict):
        if "id" in x:
            yield x
        else:
            for x_ in x.values():
                yield from _descend(x_)
    elif isinstance(x, list):
        for x_ in x:
            yield from _descend(x_)


if __name__ == "__main__":
    main(cached_only=False)
