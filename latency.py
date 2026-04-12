"""Latency check: measure TCP connect time from the user's machine to each
Runpod S3 datacenter, so the user can pick a region with good latency when
setting up their network volume.

The region list is scraped from Runpod's docs page at run time (no SDK
function exists to list them, no JSON API). Regex targets the endpoint
hostname pattern which is stable even if the page HTML changes. Cached for
24h under the plugin's cache dir so repeated runs don't hammer the docs site.

Measurement: 20 HTTPS GET requests per region to ``https://<host>/``,
sequential within a region with a 100ms gap between samples. Each sample
opens a fresh TCP+TLS connection (force_close=True on the connector) so we
don't benefit from HTTP/2 multiplexing or keep-alive, and so every sample
reflects the full round-trip cost.

We use HTTP GET rather than a bare TCP connect because ``*.runpod.io`` is
fronted by Cloudflare — a TCP/TLS probe terminates at the nearest
Cloudflare edge and reports a flat ~4ms for every region regardless of
where the datacenter actually is. An HTTP GET to the S3 API returns an
S3-style XML error (InvalidRequest / MissingSecurityHeader), which the CDN
has to proxy to origin because S3 API responses aren't cacheable. That
forces the edge → origin backhaul onto the wire, which is where the real
regional latency lives.

Drop the first sample (warm-up, DNS + TLS setup), report median, min, max,
and population stdev of the remaining 19. Regions run concurrently.

Results stream back to the caller via an ``on_progress`` callback so the
UI can render the table incrementally as each region finishes, instead of
the user staring at a blank popup for the whole run.
"""

import asyncio
import json
import os
import re
import statistics
import time
import urllib.error
import urllib.request
from typing import Optional

import aiohttp

_PREFIX = "[RunOnRunpod]"

_REGIONS_DOCS_URL = "https://docs.runpod.io/storage/s3-api"
_REGIONS_CACHE_TTL = 24 * 60 * 60

_SAMPLES = 20
_INTER_SAMPLE_DELAY = 0.1  # 100 ms
_REQUEST_TIMEOUT = 10.0

# Regex for an S3 region host. Real Runpod region codes follow a strict
# pattern: 2-3 lowercase letters, dash, 2-3 letters, dash, 1-2 digits
# (e.g. eu-cz-1, eur-is-1, us-ks-2). The tight pattern excludes the
# placeholder "s3api-DATACENTER.runpod.io" that appears as an example on
# the docs page. Stable across docs-page HTML redesigns since the URLs
# themselves can't change — every customer has them in their S3 configs.
_HOST_RE = re.compile(
    r"s3api-([a-z]{2,3}-[a-z]{2,3}-\d{1,2})\.runpod\.io",
    re.IGNORECASE,
)


def _cache_dir() -> str:
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
    os.makedirs(d, exist_ok=True)
    return d


def _regions_cache_path() -> str:
    return os.path.join(_cache_dir(), "regions.json")


def _is_valid_region_host(host: str) -> bool:
    """Guard against stale caches / bad scrapes leaking junk region codes
    like the ``s3api-datacenter.runpod.io`` placeholder through.
    """
    m = _HOST_RE.fullmatch(host)
    return m is not None


def _filter_regions(regions: list[dict]) -> list[dict]:
    return [r for r in regions if _is_valid_region_host(r.get("host", ""))]


def _fetch_docs_page(timeout: int = 15) -> Optional[str]:
    try:
        req = urllib.request.Request(
            _REGIONS_DOCS_URL,
            headers={"User-Agent": "ComfyUI-RunOnRunpod"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        print(f"{_PREFIX} Failed to fetch regions page: {e}")
        return None


def fetch_regions() -> list[dict]:
    """Return the list of RunPod S3 regions, scraping the docs page if the
    cache is stale or missing. Each entry is ``{"region": "<code>",
    "host": "s3api-<code>.runpod.io"}``.

    Raises RuntimeError if neither fresh scrape nor cached data is available.
    """
    cache_path = _regions_cache_path()
    if os.path.exists(cache_path):
        age = time.time() - os.path.getmtime(cache_path)
        if age < _REGIONS_CACHE_TTL:
            try:
                with open(cache_path, "r") as f:
                    cached = _filter_regions(json.load(f))
                if cached:
                    return cached
                # Cache exists but every entry is invalid — fall through
                # to a fresh scrape (e.g. older cache from a looser regex).
            except (json.JSONDecodeError, OSError):
                pass  # fall through to re-fetch

    html = _fetch_docs_page()
    if html is None:
        # Last-ditch: use stale cache if it exists.
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r") as f:
                    cached = _filter_regions(json.load(f))
                if cached:
                    print(f"{_PREFIX} Using stale regions cache")
                    return cached
            except (json.JSONDecodeError, OSError):
                pass
        raise RuntimeError(
            "Could not fetch the Runpod S3 regions list from "
            f"{_REGIONS_DOCS_URL} and no cached copy is available"
        )

    # Extract unique regions, preserving first-seen order.
    seen: set[str] = set()
    regions: list[dict] = []
    for m in _HOST_RE.finditer(html):
        code = m.group(1).lower()
        if code in seen:
            continue
        seen.add(code)
        regions.append({"region": code, "host": f"s3api-{code}.runpod.io"})

    if not regions:
        raise RuntimeError(
            f"Scraped {_REGIONS_DOCS_URL} but found no s3api-*.runpod.io URLs — "
            "docs page format may have changed"
        )

    try:
        with open(cache_path, "w") as f:
            json.dump(regions, f)
    except OSError as e:
        print(f"{_PREFIX} Failed to cache regions list: {e}")

    return regions


async def _measure_one_request(session: aiohttp.ClientSession, url: str, timeout: float) -> Optional[float]:
    """Return HTTPS GET round-trip time in ms, or None on failure.

    Any HTTP status counts as success — we just want the timing. A 400
    from the S3 endpoint is the expected response for an unauthenticated
    GET and still exercises the full edge → origin round-trip.
    """
    start = time.monotonic()
    try:
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=timeout),
            allow_redirects=False,
        ) as resp:
            await resp.read()
    except Exception:
        return None
    return (time.monotonic() - start) * 1000.0


async def measure_region(
    host: str,
    samples: int = _SAMPLES,
    delay: float = _INTER_SAMPLE_DELAY,
    timeout: float = _REQUEST_TIMEOUT,
) -> dict:
    """Measure HTTPS GET round-trip latency to ``host``. Runs samples
    sequentially with ``delay`` seconds between them. Each sample uses a
    fresh TCP+TLS connection (force_close=True) so connection reuse can't
    hide the real round-trip cost. Drops the first sample as warm-up.
    """
    url = f"https://{host}/"
    # force_close on the connector ensures each request gets its own TCP
    # and TLS handshake rather than reusing a kept-alive connection.
    connector = aiohttp.TCPConnector(force_close=True)
    timings: list[float] = []
    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            for i in range(samples):
                t = await _measure_one_request(session, url, timeout)
                if t is not None:
                    timings.append(t)
                if i < samples - 1:
                    await asyncio.sleep(delay)
    finally:
        await connector.close()

    # Drop the first sample (warm-up) if we have enough.
    effective = timings[1:] if len(timings) > 1 else timings

    if not effective:
        return {
            "host": host,
            "median_ms": None,
            "min_ms": None,
            "max_ms": None,
            "stdev_ms": None,
            "samples": 0,
            "error": "unreachable",
        }

    # Population stdev requires at least 2 data points.
    stdev = statistics.pstdev(effective) if len(effective) >= 2 else 0.0

    return {
        "host": host,
        "median_ms": round(statistics.median(effective), 1),
        "min_ms": round(min(effective), 1),
        "max_ms": round(max(effective), 1),
        "stdev_ms": round(stdev, 1),
        "samples": len(effective),
    }


def _result_sort_key(r: dict):
    """Sort by median ascending, then stdev ascending."""
    return (r.get("median_ms") or 0.0, r.get("stdev_ms") or 0.0)


async def check_all_regions(on_progress=None, on_start=None) -> list[dict]:
    """Run latency measurements against every known region concurrently.

    - ``on_start(total)`` is called once with the total region count before
      any measurements begin, so the UI can initialize a progress counter.
    - ``on_progress(result)`` is called after each region completes with
      that region's result dict (reachable or not), so the UI can advance
      its counter. Unreachable regions are passed through with
      ``median_ms == None`` so callers can decide what to do with them.

    Returns the final list of **reachable** regions sorted by median
    ascending, then stdev ascending. Unreachable regions are dropped from
    the return value entirely — the user just won't see them.
    """
    regions = await asyncio.to_thread(fetch_regions)
    if on_start:
        try:
            on_start(len(regions))
        except Exception as cb_exc:
            print(f"{_PREFIX} on_start callback error: {cb_exc}")

    async def _one(region: dict) -> dict:
        m = await measure_region(region["host"])
        result = {"region": region["region"], **m}
        if on_progress:
            try:
                on_progress(result)
            except Exception as cb_exc:
                print(f"{_PREFIX} on_progress callback error: {cb_exc}")
        return result

    tasks = [asyncio.create_task(_one(r)) for r in regions]
    all_results = await asyncio.gather(*tasks)
    # Drop unreachable regions from the final list.
    reachable = [r for r in all_results if r.get("median_ms") is not None]
    reachable.sort(key=_result_sort_key)
    return reachable
