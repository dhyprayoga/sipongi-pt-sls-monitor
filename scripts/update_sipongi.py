from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from shapely.geometry import Point, shape
from shapely.ops import unary_union


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "config.json"
BOUNDARY_PATH = ROOT / "data" / "pt_boundary.geojson"
STATE_PATH = ROOT / "data" / "rolling_7days.json"
SITE_DATA = ROOT / "site" / "data"


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(obj, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def first_value(d: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def load_boundary():
    obj = read_json(BOUNDARY_PATH)
    features = obj.get("features") or []
    if not features:
        raise RuntimeError(
            "Boundary PT belum diisi. Isi data/pt_boundary.geojson "
            "dengan Polygon/MultiPolygon WGS84."
        )
    geoms = []
    for feat in features:
        geom = feat.get("geometry")
        if geom:
            geoms.append(shape(geom))
    if not geoms:
        raise RuntimeError("Tidak ada geometry valid di boundary PT.")
    union = unary_union(geoms)
    if union.is_empty:
        raise RuntimeError("Boundary PT kosong.")
    return union


def extract_items(payload: Any) -> list[dict[str, Any]]:
    """Handle common API response shapes."""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]

    if isinstance(payload, dict):
        if payload.get("type") == "FeatureCollection":
            features = payload.get("features") or []
            return [
                {
                    **(f.get("properties") or {}),
                    "_geometry": f.get("geometry"),
                }
                for f in features
                if isinstance(f, dict)
            ]

        for key in ("data", "results", "items", "hotspots"):
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
            if isinstance(value, dict):
                nested = extract_items(value)
                if nested:
                    return nested

    raise RuntimeError(
        "Struktur response SiPongi tidak dikenali. "
        "Cek response endpoint dan sesuaikan extract_items()."
    )


def normalize_item(item: dict[str, Any]) -> dict[str, Any] | None:
    geom = item.get("_geometry")
    lat = lon = None

    if isinstance(geom, dict):
        coords = geom.get("coordinates")
        if geom.get("type") == "Point" and isinstance(coords, list) and len(coords) >= 2:
            lon, lat = num(coords[0]), num(coords[1])

    if lat is None:
        lat = num(first_value(item, "lat", "latitude", "LAT", "y"))
    if lon is None:
        lon = num(first_value(item, "long", "lon", "longitude", "LONG", "x"))

    if lat is None or lon is None:
        return None

    confidence = str(
        first_value(item, "confidence_level", "confidenceLevel", "confidence") or ""
    ).strip().lower()

    # If confidence_level is absent but confidence is numeric, map:
    cnum = num(first_value(item, "confidence"))
    if confidence not in {"low", "medium", "high"} and cnum is not None:
        if cnum < 30:
            confidence = "low"
        elif cnum < 80:
            confidence = "medium"
        else:
            confidence = "high"

    if confidence not in {"low", "medium", "high"}:
        confidence = "unknown"

    # Normalize date/time fields while preserving raw values.
    date_hotspot = first_value(item, "date_hotspot", "date", "tanggal", "acq_date")
    date_ori = first_value(item, "date_hotspot_ori", "acq_datetime", "datetime")
    source = first_value(item, "sumber", "source", "satellite")
    provinsi = first_value(item, "provinsi", "province")
    kabkota = first_value(item, "kabkota", "kabupaten", "kota")
    desa = first_value(item, "desa", "village")
    kawasan = first_value(item, "kawasan")

    clean = {
        "lat": lat,
        "lon": lon,
        "confidence": confidence,
        "confidence_value": cnum,
        "sumber": source,
        "date_hotspot": date_hotspot,
        "date_hotspot_ori": date_ori,
        "provinsi": provinsi,
        "kabkota": kabkota,
        "desa": desa,
        "kawasan": kawasan,
    }

    # Keep useful extra scalar fields from the API.
    for key, value in item.items():
        if key.startswith("_") or key in clean:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            clean[key] = value

    return clean


def as_feature(item: dict[str, Any]) -> dict[str, Any]:
    props = dict(item)
    lat = props.pop("lat")
    lon = props.pop("lon")
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": props,
    }


def feature_key(feature: dict[str, Any]) -> tuple:
    p = feature.get("properties", {})
    lon, lat = feature["geometry"]["coordinates"]
    return (
        round(float(lat), 5),
        round(float(lon), 5),
        str(p.get("date_hotspot_ori") or p.get("date_hotspot") or ""),
        str(p.get("sumber") or ""),
        str(p.get("confidence") or ""),
    )


def fetch_sipongi(config: dict[str, Any]) -> list[dict[str, Any]]:
    endpoint = config["sipongi_endpoint"]

    params: list[tuple[str, Any]] = [
        ("wilayah", "IN"),
        ("filterperiode", "false"),
        ("from", ""),
        ("to", ""),
        ("late", str(config.get("late_hours", 24))),
    ]

    for s in config.get("satelit", []):
        params.append(("satelit[]", s))
    for c in config.get("confidence", []):
        params.append(("confidence[]", c))

    params.extend([
        ("provinsi", ""),
        ("kabkota", ""),
    ])

    headers = {
        "User-Agent": "SiPongi-PT-Hotspot-Monitor/1.0",
        "Accept": "application/json,text/plain,*/*",
        "Referer": "https://sipongi.gakkum.kehutanan.go.id/peta",
    }

    r = requests.get(endpoint, params=params, headers=headers, timeout=90)
    r.raise_for_status()

    content_type = (r.headers.get("content-type") or "").lower()
    text = r.text.strip()

    if "json" not in content_type and not text.startswith(("{", "[")):
        raise RuntimeError(
            f"Endpoint tidak mengembalikan JSON. Content-Type={content_type}. "
            f"Response awal={text[:300]!r}"
        )

    try:
        payload = r.json()
    except ValueError as exc:
        raise RuntimeError(
            f"Response bukan JSON valid. Awal response: {text[:500]}"
        ) from exc

    items = extract_items(payload)
    normalized = []
    for item in items:
        clean = normalize_item(item)
        if clean:
            normalized.append(clean)

    return normalized


def make_snapshot_date() -> str:
    # Workflow is intended to run once daily at 07:15 WIB = 00:15 UTC.
    # Use UTC date as the stable run key.
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def build_snapshot(features: list[dict[str, Any]], snapshot_date: str) -> dict[str, Any]:
    counts = Counter(
        f["properties"].get("confidence", "unknown") for f in features
    )

    return {
        "snapshot_date": snapshot_date,
        "total": len(features),
        "high": counts.get("high", 0),
        "medium": counts.get("medium", 0),
        "low": counts.get("low", 0),
        "unknown": counts.get("unknown", 0),
        "features": features,
    }


def update_rolling(
    previous_state: dict[str, Any],
    today_snapshot: dict[str, Any],
    keep_days: int,
) -> dict[str, Any]:
    snapshots = previous_state.get("snapshots") or []

    by_date = {}
    for snap in snapshots:
        d = snap.get("snapshot_date")
        if d:
            by_date[d] = snap

    by_date[today_snapshot["snapshot_date"]] = today_snapshot

    dates = sorted(by_date.keys(), reverse=True)[:keep_days]
    new_snaps = [by_date[d] for d in sorted(dates)]

    return {"snapshots": new_snaps}


def build_trend(state: dict[str, Any]) -> dict[str, Any]:
    snaps = sorted(state.get("snapshots") or [], key=lambda x: x["snapshot_date"])

    series = [
        {
            "date": s["snapshot_date"],
            "total": s.get("total", 0),
            "high": s.get("high", 0),
            "medium": s.get("medium", 0),
            "low": s.get("low", 0),
        }
        for s in snaps
    ]

    return {
        "dates": [x["date"] for x in series],
        "series": series,
        "last_updated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def build_summary(state: dict[str, Any], pt_name: str) -> dict[str, Any]:
    snaps = sorted(state.get("snapshots") or [], key=lambda x: x["snapshot_date"])
    if not snaps:
        return {
            "pt_name": pt_name,
            "snapshot_date": None,
            "total": 0,
            "high": 0,
            "medium": 0,
            "low": 0,
            "vs_previous_pct": None,
            "average_7_days": 0,
            "min_7_days": 0,
            "max_7_days": 0,
            "status": "Belum ada data",
            "last_updated_utc": None,
        }

    latest = snaps[-1]
    previous = snaps[-2] if len(snaps) >= 2 else None

    totals = [s.get("total", 0) for s in snaps]
    avg = sum(totals) / len(totals)

    vs_prev = None
    if previous and previous.get("total", 0) != 0:
        vs_prev = round(
            (latest.get("total", 0) - previous.get("total", 0))
            / previous.get("total", 0)
            * 100,
            1,
        )

    if vs_prev is None:
        status = "Belum dapat dibandingkan"
    elif vs_prev > 20:
        status = "Meningkat"
    elif vs_prev < -20:
        status = "Menurun"
    else:
        status = "Stabil"

    return {
        "pt_name": pt_name,
        "snapshot_date": latest["snapshot_date"],
        "total": latest.get("total", 0),
        "high": latest.get("high", 0),
        "medium": latest.get("medium", 0),
        "low": latest.get("low", 0),
        "unknown": latest.get("unknown", 0),
        "vs_previous_pct": vs_prev,
        "average_7_days": round(avg, 1),
        "min_7_days": min(totals),
        "max_7_days": max(totals),
        "status": status,
        "last_updated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "SIPONGI KEMENHUT",
        "note": (
            "Hotspot merupakan indikasi anomali suhu/titik panas dan "
            "memerlukan verifikasi lapangan."
        ),
    }


def main():
    config = read_json(CONFIG_PATH)
    boundary = load_boundary()

    items = fetch_sipongi(config)

    features = []
    for item in items:
        point = Point(item["lon"], item["lat"])
        if boundary.covers(point):
            features.append(as_feature(item))

    # Deduplicate points within the snapshot.
    unique = {}
    for f in features:
        unique[feature_key(f)] = f
    features = list(unique.values())

    snapshot_date = make_snapshot_date()
    snapshot = build_snapshot(features, snapshot_date)

    previous_state = read_json(STATE_PATH)
    state = update_rolling(
        previous_state,
        snapshot,
        int(config.get("keep_days", 7)),
    )

    trend = build_trend(state)
    summary = build_summary(state, config.get("pt_name", "NAMA PT"))

    current_geojson = {
        "type": "FeatureCollection",
        "name": "sipongi_hotspots_pt",
        "features": features,
    }

    write_json(STATE_PATH, state)
    write_json(SITE_DATA / "current.geojson", current_geojson)
    write_json(SITE_DATA / "trend_7days.json", trend)
    write_json(SITE_DATA / "summary.json", summary)

    # GitHub Pages publishes only the `site/` directory.
    # Copy the PT boundary there so the browser can display it.
    boundary_for_site = SITE_DATA / "pt_boundary.geojson"
    write_json(boundary_for_site, read_json(BOUNDARY_PATH))

    print("=== SiPongi PT Hotspot Monitor ===")
    print("PT:", config.get("pt_name"))
    print("Response hotspot:", len(items))
    print("Hotspot inside PT:", len(features))
    print("High:", snapshot["high"])
    print("Medium:", snapshot["medium"])
    print("Low:", snapshot["low"])
    print("Rolling snapshots:", len(state["snapshots"]))
    print("Run UTC:", snapshot_date)


if __name__ == "__main__":
    main()
