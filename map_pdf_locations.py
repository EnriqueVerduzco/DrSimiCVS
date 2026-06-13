#!/usr/bin/env python3

from __future__ import annotations

import csv
import html as html_lib
import json
import re
import time
import urllib.parse
import urllib.request
from urllib.error import HTTPError, URLError
from dataclasses import dataclass, asdict
from pathlib import Path

from PyPDF2 import PdfReader


PDF_PATH = Path("/tmp/CVSDrSimifutbol.pdf")
OUT_DIR = Path("/Users/enriqueverduzco/Documents/ConsumeTracker/artifacts/dr-simi-stores")
PAGES_DIR = Path("/Users/enriqueverduzco/Documents/ConsumeTracker/docs")
CSV_PATH = OUT_DIR / "store_locations.csv"
GEOJSON_PATH = OUT_DIR / "store_locations.geojson"
HTML_PATH = OUT_DIR / "store_locations_map.html"
SUMMARY_PATH = OUT_DIR / "summary.json"
CACHE_PATH = OUT_DIR / "geocode_cache.json"
PAGES_INDEX_PATH = PAGES_DIR / "index.html"
PAGES_NOJEKYLL_PATH = PAGES_DIR / ".nojekyll"

STATE_NAMES = [
    "CALIFORNIA",
    "TEXAS",
    "NEVADA",
    "ARIZONA",
    "OKLAHOMA",
]


@dataclass
class StoreRecord:
    store_id: str
    raw_address: str
    state: str
    zip_code: str
    query: str
    matched_address: str = ""
    latitude: str = ""
    longitude: str = ""
    geocode_status: str = "pending"


def extract_lines(pdf_path: Path) -> list[str]:
    reader = PdfReader(str(pdf_path))
    lines: list[str] = []
    for page in reader.pages:
        text = page.extract_text() or ""
        for raw_line in text.splitlines():
            line = " ".join(raw_line.replace("\xa0", " ").split())
            if not line:
                continue
            if line == "CVS con productos Dr. Simi Futbol":
                continue
            if line == "STORES ADDRESS CITY State ZIP CODE":
                continue
            if re.match(r"^--- PAGE \d+ ---$", line):
                continue
            if re.match(r"^\d+\s", line):
                lines.append(line)
    return lines


def parse_store_line(line: str) -> StoreRecord:
    match = re.match(r"^(\d+)\s+(.*)$", line)
    if not match:
        raise ValueError(f"Could not parse line: {line}")

    store_id, remainder = match.groups()

    state = next((name for name in STATE_NAMES if f" {name} " in f" {remainder} "), "")
    if not state:
        raise ValueError(f"Missing state in line: {line}")

    before_state, _, after_state = remainder.partition(f" {state} ")
    zip_candidate = after_state.strip()
    zip_code = zip_candidate if re.fullmatch(r"\d{5}", zip_candidate) else ""
    query = f"{before_state}, {state}" + (f" {zip_code}" if zip_code else "")

    return StoreRecord(
        store_id=store_id,
        raw_address=before_state,
        state=state.title(),
        zip_code=zip_code,
        query=query,
    )


def geocode(query: str) -> tuple[str, str, str, str]:
    encoded = urllib.parse.quote(query)
    url = (
        "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
        f"?address={encoded}&benchmark=Public_AR_Current&format=json"
    )
    with urllib.request.urlopen(url, timeout=30) as response:
        payload = json.load(response)

    matches = payload.get("result", {}).get("addressMatches", [])
    if not matches:
        return "", "", "", "not_found"

    top = matches[0]
    coords = top.get("coordinates", {})
    longitude = coords.get("x", "")
    latitude = coords.get("y", "")
    matched_address = top.get("matchedAddress", "")
    if latitude == "" or longitude == "":
        return "", "", matched_address, "missing_coordinates"
    return str(latitude), str(longitude), matched_address, "ok"


def geocode_nominatim(query: str) -> tuple[str, str, str, str]:
    encoded = urllib.parse.quote(query)
    url = (
        "https://nominatim.openstreetmap.org/search"
        f"?q={encoded}&format=jsonv2&limit=1&countrycodes=us"
    )
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "ConsumeTracker DrSimi store mapper/1.0"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)

    if not payload:
        return "", "", "", "not_found"

    top = payload[0]
    latitude = top.get("lat", "")
    longitude = top.get("lon", "")
    matched_address = top.get("display_name", "")
    if latitude == "" or longitude == "":
        return "", "", matched_address, "missing_coordinates"
    return str(latitude), str(longitude), matched_address, "ok"


def geocode_with_retries(query: str, retries: int = 4) -> tuple[str, str, str, str]:
    for attempt in range(1, retries + 1):
        try:
            latitude, longitude, matched_address, status = geocode(query)
            if status == "ok":
                return latitude, longitude, matched_address, status
            return geocode_nominatim(query)
        except (HTTPError, URLError, TimeoutError, OSError):
            if attempt == retries:
                return "", "", "", "request_failed"
            time.sleep(attempt)


def load_cache() -> dict[str, dict[str, str]]:
    if not CACHE_PATH.exists():
        return {}
    return json.loads(CACHE_PATH.read_text(encoding="utf-8"))


def save_cache(cache: dict[str, dict[str, str]]) -> None:
    CACHE_PATH.write_text(json.dumps(cache, indent=2), encoding="utf-8")


def write_csv(records: list[StoreRecord]) -> None:
    with CSV_PATH.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(asdict(records[0]).keys()))
        writer.writeheader()
        for record in records:
            writer.writerow(asdict(record))


def write_geojson(records: list[StoreRecord]) -> None:
    features = []
    for record in records:
        if not record.latitude or not record.longitude:
            continue
        properties = asdict(record)
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [float(record.longitude), float(record.latitude)],
                },
                "properties": properties,
            }
        )
    collection = {"type": "FeatureCollection", "features": features}
    GEOJSON_PATH.write_text(json.dumps(collection, indent=2), encoding="utf-8")


def write_html(records: list[StoreRecord]) -> None:
    geojson = json.loads(GEOJSON_PATH.read_text(encoding="utf-8"))
    mapped_records = [record for record in records if record.geocode_status == "ok"]
    list_items = "".join(
        (
            '<div class="item">'
            f"<strong>Store {html_lib.escape(record.store_id)}</strong>"
            '<a class="address-link" '
            f'data-address="{html_lib.escape(record.matched_address or record.query, quote=True)}" '
            'href="#" target="_blank" rel="noopener noreferrer">'
            f"{html_lib.escape(record.matched_address or record.query)}"
            "</a>"
            "</div>"
        )
        for record in mapped_records
    )
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Dr. Simi Store Locations</title>
  <link
    rel="stylesheet"
    href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
    integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY="
    crossorigin=""
  />
  <style>
    :root {{
      color-scheme: light;
      --bg: #f3efe4;
      --panel: rgba(255, 252, 245, 0.92);
      --ink: #112011;
      --accent: #146b3a;
      --muted: #5d6a5d;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Georgia, "Times New Roman", serif;
      background:
        radial-gradient(circle at top left, rgba(20, 107, 58, 0.12), transparent 30%),
        linear-gradient(180deg, #fbf8f1, #efe8d6);
      color: var(--ink);
    }}
    .shell {{
      max-width: 1180px;
      margin: 0 auto;
      padding: 24px;
    }}
    .panel {{
      background: var(--panel);
      backdrop-filter: blur(8px);
      border: 1px solid rgba(17, 32, 17, 0.08);
      border-radius: 24px;
      box-shadow: 0 18px 48px rgba(17, 32, 17, 0.08);
      overflow: hidden;
    }}
    .hero {{
      padding: 24px 24px 18px;
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: 28px;
      line-height: 1.05;
    }}
    p {{
      margin: 0 0 14px;
      color: var(--muted);
      line-height: 1.45;
    }}
    .stat {{
      margin: 18px 0;
      padding: 14px 16px;
      background: rgba(20, 107, 58, 0.06);
      border-left: 4px solid var(--accent);
    }}
    .map-wrap {{
      padding: 0 24px 24px;
    }}
    .map-frame {{
      border-radius: 18px;
      overflow: hidden;
      border: 1px solid rgba(17, 32, 17, 0.12);
      background: rgba(20, 107, 58, 0.05);
    }}
    .list-section {{
      border-top: 1px solid rgba(17, 32, 17, 0.08);
      background: rgba(255, 255, 255, 0.32);
    }}
    .list-toggle {{
      list-style: none;
      cursor: pointer;
      padding: 18px 24px;
      font-size: 16px;
      font-weight: 700;
      display: flex;
      align-items: center;
      justify-content: space-between;
    }}
    .list-toggle::-webkit-details-marker {{
      display: none;
    }}
    .list-toggle::after {{
      content: "+";
      font-size: 22px;
      line-height: 1;
      color: var(--accent);
    }}
    .list-section[open] .list-toggle::after {{
      content: "−";
    }}
    .list {{
      margin: 0;
      padding: 0 24px 20px;
      max-height: 420px;
      overflow: auto;
      padding-right: 6px;
    }}
    .item {{
      padding: 10px 0;
      border-bottom: 1px solid rgba(17, 32, 17, 0.08);
    }}
    .item strong {{
      display: block;
      font-size: 14px;
    }}
    .address-link {{
      display: block;
      font-size: 13px;
      color: var(--muted);
      line-height: 1.4;
      text-decoration: none;
    }}
    .address-link:hover,
    .address-link:focus-visible {{
      color: var(--accent);
      text-decoration: underline;
    }}
    #map {{
      min-height: 62vh;
      width: 100%;
    }}
    @media (max-width: 900px) {{
      .shell {{
        padding: 14px;
      }}
      .hero,
      .map-wrap,
      .list-toggle,
      .list {{
        padding-left: 16px;
        padding-right: 16px;
      }}
      #map {{ min-height: 52vh; }}
      .list {{ max-height: none; }}
    }}
  </style>
</head>
<body>
  <div class="shell">
    <main class="panel">
      <section class="hero">
        <h1>Dr. Simi Store Map</h1>
        <div class="stat">
          <strong>{sum(1 for record in records if record.geocode_status == "ok")} mapped locations</strong>
        </div>
      </section>
      <section class="map-wrap">
        <div class="map-frame">
          <div id="map"></div>
        </div>
      </section>
      <details class="list-section">
        <summary class="list-toggle">Store List</summary>
        <div class="list">
          {list_items}
        </div>
      </details>
    </main>
  </div>
  <script
    src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
    integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo="
    crossorigin=""
  ></script>
  <script>
    const data = {json.dumps(geojson)};
    const isAppleDevice = /iPad|iPhone|iPod|Mac/.test(navigator.userAgent);

    function mapsUrlFor(address) {{
      const encoded = encodeURIComponent(address);
      return isAppleDevice
        ? `https://maps.apple.com/?q=${{encoded}}`
        : `https://www.google.com/maps/search/?api=1&query=${{encoded}}`;
    }}

    function configureMapLinks(root = document) {{
      root.querySelectorAll(".address-link").forEach((link) => {{
        const address = link.dataset.address;
        if (!address) {{
          return;
        }}
        link.href = mapsUrlFor(address);
      }});
    }}

    configureMapLinks();

    const map = L.map("map", {{ scrollWheelZoom: true }});
    L.tileLayer("https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png", {{
      maxZoom: 19,
      attribution: "&copy; OpenStreetMap contributors"
    }}).addTo(map);

    const markers = L.geoJSON(data, {{
      pointToLayer: (feature, latlng) => L.circleMarker(latlng, {{
        radius: 5,
        color: "#0d4c28",
        weight: 1,
        fillColor: "#1f8f50",
        fillOpacity: 0.82
      }}),
      onEachFeature: (feature, layer) => {{
        const p = feature.properties;
        const address = p.matched_address || p.query;
        const popup = document.createElement("div");
        const title = document.createElement("strong");
        title.textContent = `Store ${{p.store_id}}`;

        const link = document.createElement("a");
        link.className = "address-link";
        link.dataset.address = address;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = address;

        popup.appendChild(title);
        popup.appendChild(document.createElement("br"));
        popup.appendChild(link);
        configureMapLinks(popup);
        layer.bindPopup(popup);
      }}
    }}).addTo(map);

    const bounds = markers.getBounds();
    if (bounds.isValid()) {{
      map.fitBounds(bounds.pad(0.08));
    }} else {{
      map.setView([34.05, -118.24], 6);
    }}
  </script>
</body>
</html>
"""
    HTML_PATH.write_text(html, encoding="utf-8")


def write_summary(records: list[StoreRecord]) -> None:
    counts_by_state: dict[str, int] = {}
    for record in records:
        counts_by_state[record.state] = counts_by_state.get(record.state, 0) + 1
    payload = {
        "source_pdf": str(PDF_PATH),
        "total_rows": len(records),
        "mapped_rows": sum(1 for record in records if record.geocode_status == "ok"),
        "counts_by_state": counts_by_state,
    }
    SUMMARY_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def write_pages_site() -> None:
    PAGES_DIR.mkdir(parents=True, exist_ok=True)
    PAGES_INDEX_PATH.write_text(HTML_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    PAGES_NOJEKYLL_PATH.write_text("", encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    records = [parse_store_line(line) for line in extract_lines(PDF_PATH)]
    cache = load_cache()
    for index, record in enumerate(records, start=1):
        cached = cache.get(record.query)
        if cached and cached.get("geocode_status") == "ok":
            latitude = cached.get("latitude", "")
            longitude = cached.get("longitude", "")
            matched_address = cached.get("matched_address", "")
            status = cached.get("geocode_status", "cached")
        else:
            latitude, longitude, matched_address, status = geocode_with_retries(record.query)
            cache[record.query] = {
                "latitude": latitude,
                "longitude": longitude,
                "matched_address": matched_address,
                "geocode_status": status,
            }
        record.latitude = latitude
        record.longitude = longitude
        record.matched_address = matched_address
        record.geocode_status = status
        if index % 10 == 0:
            save_cache(cache)
            print(f"Geocoded {index}/{len(records)}", flush=True)
        time.sleep(0.05)
    save_cache(cache)
    write_csv(records)
    write_geojson(records)
    write_html(records)
    write_summary(records)
    write_pages_site()
    print(f"Wrote {CSV_PATH}", flush=True)
    print(f"Wrote {GEOJSON_PATH}", flush=True)
    print(f"Wrote {HTML_PATH}", flush=True)
    print(f"Wrote {SUMMARY_PATH}", flush=True)
    print(f"Wrote {PAGES_INDEX_PATH}", flush=True)


if __name__ == "__main__":
    main()
