from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from shapely.geometry import Point, shape
from shapely.ops import unary_union


# ============================================================
# PATH
# ============================================================

ROOT = Path(__file__).resolve().parents[1]

CONFIG_PATH = ROOT / "config" / "config.json"
BOUNDARY_PATH = ROOT / "data" / "pt_boundary.geojson"
STATE_PATH = ROOT / "data" / "rolling_7days.json"

SITE_DATA = ROOT / "site" / "data"


# ============================================================
# JSON HELPERS
# ============================================================

def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    path.write_text(
        json.dumps(
            obj,
            indent=2,
            ensure_ascii=False
        ),
        encoding="utf-8"
    )


# ============================================================
# NUMBER HELPERS
# ============================================================

def num(value: Any) -> float | None:

    try:
        if value is None:
            return None

        if isinstance(value, str):
            value = value.strip()

        return float(value)

    except (TypeError, ValueError):
        return None


def first_value(
    data: dict[str, Any],
    *keys: str
) -> Any:

    for key in keys:

        if (
            key in data
            and data[key] not in (None, "")
        ):
            return data[key]

    return None


# ============================================================
# LOAD PT BOUNDARY
# ============================================================

def load_boundary():

    obj = read_json(
        BOUNDARY_PATH
    )

    features = obj.get(
        "features"
    ) or []

    if not features:

        raise RuntimeError(
            "Boundary PT belum diisi. "
            "Isi data/pt_boundary.geojson "
            "dengan Polygon/MultiPolygon WGS84."
        )

    geometries = []

    for feature in features:

        geometry = feature.get(
            "geometry"
        )

        if geometry:

            geometries.append(
                shape(geometry)
            )

    if not geometries:

        raise RuntimeError(
            "Tidak ada geometry valid "
            "di boundary PT."
        )

    union = unary_union(
        geometries
    )

    if union.is_empty:

        raise RuntimeError(
            "Boundary PT kosong."
        )

    # --------------------------------------------------------
    # Repair geometry apabila invalid
    # --------------------------------------------------------

    if not union.is_valid:

        print(
            "WARNING: Boundary PT invalid. "
            "Mencoba memperbaiki geometry..."
        )

        union = union.buffer(0)

    if union.is_empty:

        raise RuntimeError(
            "Boundary PT tetap kosong "
            "setelah repair."
        )

    return union


# ============================================================
# EXTRACT API ITEMS
# ============================================================

def extract_items(
    payload: Any
) -> list[dict[str, Any]]:

    """
    Menangani beberapa bentuk response API umum:
    - list [...]
    - FeatureCollection
    - {"data": [...]}
    - {"results": [...]}
    - {"items": [...]}
    - {"hotspots": [...]}
    """

    # --------------------------------------------------------
    # Response berupa list
    # --------------------------------------------------------

    if isinstance(
        payload,
        list
    ):

        return [
            item
            for item in payload
            if isinstance(item, dict)
        ]


    # --------------------------------------------------------
    # Response berupa dict
    # --------------------------------------------------------

    if isinstance(
        payload,
        dict
    ):

        # ----------------------------------------------------
        # GeoJSON FeatureCollection
        # ----------------------------------------------------

        if payload.get(
            "type"
        ) == "FeatureCollection":

            features = (
                payload.get(
                    "features"
                )
                or []
            )

            result = []

            for feature in features:

                if not isinstance(
                    feature,
                    dict
                ):
                    continue

                item = {
                    **(
                        feature.get(
                            "properties"
                        )
                        or {}
                    ),

                    "_geometry":
                        feature.get(
                            "geometry"
                        )
                }

                result.append(
                    item
                )

            return result


        # ----------------------------------------------------
        # Data wrapper
        # ----------------------------------------------------

        for key in (
            "data",
            "results",
            "items",
            "hotspots"
        ):

            value = payload.get(
                key
            )

            if isinstance(
                value,
                list
            ):

                return [
                    item
                    for item in value
                    if isinstance(item, dict)
                ]


            if isinstance(
                value,
                dict
            ):

                nested = extract_items(
                    value
                )

                if nested:
                    return nested


    raise RuntimeError(
        "Struktur response SiPongi "
        "tidak dikenali."
    )


# ============================================================
# NORMALIZE HOTSPOT
# ============================================================

def normalize_item(
    item: dict[str, Any]
) -> dict[str, Any] | None:

    geometry = item.get(
        "_geometry"
    )

    lat = None
    lon = None


    # --------------------------------------------------------
    # Kalau response GeoJSON
    # --------------------------------------------------------

    if isinstance(
        geometry,
        dict
    ):

        coords = geometry.get(
            "coordinates"
        )

        if (
            geometry.get("type")
            == "Point"
            and isinstance(coords, list)
            and len(coords) >= 2
        ):

            lon = num(
                coords[0]
            )

            lat = num(
                coords[1]
            )


    # --------------------------------------------------------
    # Kalau response biasa
    # --------------------------------------------------------

    if lat is None:

        lat = num(
            first_value(
                item,
                "lat",
                "latitude",
                "LAT",
                "y"
            )
        )


    if lon is None:

        lon = num(
            first_value(
                item,
                "long",
                "lon",
                "longitude",
                "LONG",
                "x"
            )
        )


    # --------------------------------------------------------
    # Koordinat tidak valid
    # --------------------------------------------------------

    if lat is None or lon is None:

        return None


    if not (
        -90 <= lat <= 90
        and
        -180 <= lon <= 180
    ):

        return None


    # --------------------------------------------------------
    # Confidence
    # --------------------------------------------------------

    raw_confidence = first_value(
        item,
        "confidence_level",
        "confidenceLevel",
        "confidence"
    )


    confidence = str(
        raw_confidence
        or ""
    ).strip().lower()


    # --------------------------------------------------------
    # Confidence numerik
    # --------------------------------------------------------

    confidence_number = num(
        first_value(
            item,
            "confidence"
        )
    )


    if (
        confidence
        not in {
            "low",
            "medium",
            "high"
        }
        and confidence_number is not None
    ):

        if confidence_number < 30:

            confidence = "low"

        elif confidence_number < 80:

            confidence = "medium"

        else:

            confidence = "high"


    if confidence not in {
        "low",
        "medium",
        "high"
    }:

        confidence = "unknown"


    # --------------------------------------------------------
    # Fields utama SiPongi
    # --------------------------------------------------------

    date_hotspot = first_value(
        item,
        "date_hotspot",
        "date",
        "tanggal",
        "acq_date"
    )


    date_original = first_value(
        item,
        "date_hotspot_ori",
        "acq_datetime",
        "datetime"
    )


    source = first_value(
        item,
        "sumber",
        "source",
        "satellite"
    )


    province = first_value(
        item,
        "provinsi",
        "province"
    )


    district = first_value(
        item,
        "kabkota",
        "kabupaten",
        "kota"
    )


    village = first_value(
        item,
        "desa",
        "village"
    )


    kawasan = first_value(
        item,
        "kawasan"
    )


    # --------------------------------------------------------
    # Normalized object
    # --------------------------------------------------------

    clean = {

        "lat":
            lat,

        "lon":
            lon,

        "confidence":
            confidence,

        "confidence_value":
            confidence_number,

        "sumber":
            source,

        "date_hotspot":
            date_hotspot,

        "date_hotspot_ori":
            date_original,

        "provinsi":
            province,

        "kabkota":
            district,

        "desa":
            village,

        "kawasan":
            kawasan
    }


    # --------------------------------------------------------
    # Pertahankan field tambahan
    # --------------------------------------------------------

    for key, value in item.items():

        if key.startswith("_"):
            continue

        if key in clean:
            continue

        if isinstance(
            value,
            (
                str,
                int,
                float,
                bool
            )
        ) or value is None:

            clean[key] = value


    return clean


# ============================================================
# CONVERT TO GEOJSON FEATURE
# ============================================================

def as_feature(
    item: dict[str, Any]
) -> dict[str, Any]:

    properties = dict(
        item
    )

    lat = properties.pop(
        "lat"
    )

    lon = properties.pop(
        "lon"
    )


    return {

        "type":
            "Feature",

        "geometry": {

            "type":
                "Point",

            "coordinates":
                [
                    lon,
                    lat
                ]
        },

        "properties":
            properties
    }


# ============================================================
# FEATURE UNIQUE KEY
# ============================================================

def feature_key(
    feature: dict[str, Any]
) -> tuple:

    properties = (
        feature.get(
            "properties"
        )
        or {}
    )

    lon, lat = (
        feature[
            "geometry"
        ][
            "coordinates"
        ]
    )


    return (

        round(
            float(lat),
            5
        ),

        round(
            float(lon),
            5
        ),

        str(
            properties.get(
                "date_hotspot_ori"
            )
            or
            properties.get(
                "date_hotspot"
            )
            or ""
        ),

        str(
            properties.get(
                "sumber"
            )
            or ""
        ),

        str(
            properties.get(
                "confidence"
            )
            or ""
        )
    )


# ============================================================
# FETCH SIPONGI
# ============================================================

def fetch_sipongi(
    config: dict[str, Any]
) -> list[dict[str, Any]]:

    endpoint = config[
        "sipongi_endpoint"
    ]


    # --------------------------------------------------------
    # Parameter dibuat sama seperti request yang kamu tangkap
    # --------------------------------------------------------

    params: list[tuple[str, Any]] = [

        (
            "wilayah",
            "IN"
        ),

        (
            "filterperiode",
            "false"
        ),

        (
            "from",
            ""
        ),

        (
            "to",
            ""
        ),

        (
            "late",
            str(
                config.get(
                    "late_hours",
                    24
                )
            )
        )
    ]


    # --------------------------------------------------------
    # Satelit
    # --------------------------------------------------------

    for satellite in config.get(
        "satelit",
        []
    ):

        params.append(
            (
                "satelit[]",
                satellite
            )
        )


    # --------------------------------------------------------
    # Confidence
    # --------------------------------------------------------

    for confidence in config.get(
        "confidence",
        []
    ):

        params.append(
            (
                "confidence[]",
                confidence
            )
        )


    params.extend(

        [
            (
                "provinsi",
                ""
            ),

            (
                "kabkota",
                ""
            )
        ]

    )


    headers = {

        "User-Agent":
            "SiPongi-PT-Hotspot-Monitor/1.0",

        "Accept":
            "application/json,text/plain,*/*",

        "Referer":
            "https://sipongi.gakkum.kehutanan.go.id/peta"

    }


    print("")
    print("=== SIPONGI REQUEST ===")
    print("Endpoint:", endpoint)
    print("Late:", config.get(
        "late_hours",
        24
    ))
    print("Satelit:", config.get(
        "satelit",
        []
    ))
    print("Confidence:", config.get(
        "confidence",
        []
    ))
    print("=======================")
    print("")


    response = requests.get(

        endpoint,

        params=params,

        headers=headers,

        timeout=90
    )


    response.raise_for_status()


    content_type = (
        response.headers.get(
            "content-type"
        )
        or ""
    ).lower()


    text = response.text.strip()


    if (
        "json"
        not in content_type
        and
        not text.startswith(
            (
                "{",
                "["
            )
        )
    ):

        raise RuntimeError(

            "Endpoint tidak "
            "mengembalikan JSON.\n"
            f"Content-Type: {content_type}\n"
            f"Response awal: {text[:500]}"

        )


    try:

        payload = response.json()

    except ValueError as exc:

        raise RuntimeError(

            "Response SiPongi bukan "
            "JSON valid.\n"
            f"Response awal: {text[:500]}"

        ) from exc


    items = extract_items(
        payload
    )


    normalized = []


    for item in items:

        clean = normalize_item(
            item
        )

        if clean:

            normalized.append(
                clean
            )


    return normalized


# ============================================================
# SNAPSHOT DATE
# ============================================================

def make_snapshot_date() -> str:

    return datetime.now(
        timezone.utc
    ).strftime(
        "%Y-%m-%d"
    )


# ============================================================
# BUILD SNAPSHOT
# ============================================================

def build_snapshot(

    features: list[dict[str, Any]],

    snapshot_date: str

) -> dict[str, Any]:

    counts = Counter(

        feature[
            "properties"
        ].get(
            "confidence",
            "unknown"
        )

        for feature in features

    )


    return {

        "snapshot_date":
            snapshot_date,

        "total":
            len(features),

        "high":
            counts.get(
                "high",
                0
            ),

        "medium":
            counts.get(
                "medium",
                0
            ),

        "low":
            counts.get(
                "low",
                0
            ),

        "unknown":
            counts.get(
                "unknown",
                0
            ),

        "features":
            features
    }


# ============================================================
# UPDATE ROLLING 7 DAYS
# ============================================================

def update_rolling(

    previous_state: dict[str, Any],

    today_snapshot: dict[str, Any],

    keep_days: int

) -> dict[str, Any]:

    snapshots = (
        previous_state.get(
            "snapshots"
        )
        or []
    )


    by_date = {}


    for snapshot in snapshots:

        date = snapshot.get(
            "snapshot_date"
        )

        if date:

            by_date[
                date
            ] = snapshot


    by_date[
        today_snapshot[
            "snapshot_date"
        ]
    ] = today_snapshot


    dates = sorted(

        by_date.keys(),

        reverse=True

    )[
        :keep_days
    ]


    new_snapshots = [

        by_date[date]

        for date in sorted(
            dates
        )

    ]


    return {

        "snapshots":
            new_snapshots

    }


# ============================================================
# TREND
# ============================================================

def build_trend(
    state: dict[str, Any]
) -> dict[str, Any]:

    snapshots = sorted(

        state.get(
            "snapshots"
        )
        or [],

        key=lambda x:
            x[
                "snapshot_date"
            ]

    )


    series = [

        {

            "date":
                snapshot[
                    "snapshot_date"
                ],

            "total":
                snapshot.get(
                    "total",
                    0
                ),

            "high":
                snapshot.get(
                    "high",
                    0
                ),

            "medium":
                snapshot.get(
                    "medium",
                    0
                ),

            "low":
                snapshot.get(
                    "low",
                    0
                )

        }

        for snapshot in snapshots

    ]


    return {

        "dates":
            [
                item["date"]
                for item in series
            ],

        "series":
            series,

        "last_updated_utc":
            datetime.now(
                timezone.utc
            ).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )

    }


# ============================================================
# SUMMARY
# ============================================================

def build_summary(

    state: dict[str, Any],

    pt_name: str

) -> dict[str, Any]:

    snapshots = sorted(

        state.get(
            "snapshots"
        )
        or [],

        key=lambda x:
            x[
                "snapshot_date"
            ]

    )


    if not snapshots:

        return {

            "pt_name":
                pt_name,

            "snapshot_date":
                None,

            "total":
                0,

            "high":
                0,

            "medium":
                0,

            "low":
                0,

            "vs_previous_pct":
                None,

            "average_7_days":
                0,

            "min_7_days":
                0,

            "max_7_days":
                0,

            "status":
                "Belum ada data",

            "last_updated_utc":
                None

        }


    latest = snapshots[-1]


    previous = (
        snapshots[-2]
        if len(snapshots) >= 2
        else None
    )


    totals = [

        snapshot.get(
            "total",
            0
        )

        for snapshot in snapshots

    ]


    average = (
        sum(totals)
        /
        len(totals)
    )


    previous_percentage = None


    if (

        previous

        and
        previous.get(
            "total",
            0
        ) != 0

    ):

        previous_percentage = round(

            (

                latest.get(
                    "total",
                    0
                )

                -

                previous.get(
                    "total",
                    0
                )

            )

            /

            previous.get(
                "total",
                0
            )

            *

            100,

            1

        )


    if previous_percentage is None:

        status = (
            "Belum dapat dibandingkan"
        )

    elif previous_percentage > 20:

        status = "Meningkat"

    elif previous_percentage < -20:

        status = "Menurun"

    else:

        status = "Stabil"


    return {

        "pt_name":
            pt_name,

        "snapshot_date":
            latest[
                "snapshot_date"
            ],

        "total":
            latest.get(
                "total",
                0
            ),

        "high":
            latest.get(
                "high",
                0
            ),

        "medium":
            latest.get(
                "medium",
                0
            ),

        "low":
            latest.get(
                "low",
                0
            ),

        "unknown":
            latest.get(
                "unknown",
                0
            ),

        "vs_previous_pct":
            previous_percentage,

        "average_7_days":
            round(
                average,
                1
            ),

        "min_7_days":
            min(totals),

        "max_7_days":
            max(totals),

        "status":
            status,

        "last_updated_utc":
            datetime.now(
                timezone.utc
            ).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),

        "source":
            "SIPONGI KEMENHUT",

        "note":
            (
                "Hotspot merupakan "
                "indikasi anomali suhu/"
                "titik panas dan "
                "memerlukan verifikasi "
                "lapangan."
            )

    }


# ============================================================
# SPATIAL DIAGNOSTIC
# ============================================================

def spatial_diagnostic(

    items: list[dict[str, Any]],

    boundary

) -> tuple[list[dict[str, Any]], int]:

    min_lon, min_lat, max_lon, max_lat = (
        boundary.bounds
    )


    bbox_items = []

    polygon_features = []


    # --------------------------------------------------------
    # Semua koordinat hotspot
    # --------------------------------------------------------

    all_lons = []
    all_lats = []


    for item in items:

        lon = item.get(
            "lon"
        )

        lat = item.get(
            "lat"
        )


        if lon is None or lat is None:
            continue


        all_lons.append(
            lon
        )

        all_lats.append(
            lat
        )


        point = Point(
            lon,
            lat
        )


        # ----------------------------------------------------
        # BBOX
        # ----------------------------------------------------

        if (

            min_lon <= lon <= max_lon

            and

            min_lat <= lat <= max_lat

        ):

            bbox_items.append(
                item
            )


        # ----------------------------------------------------
        # Polygon
        # ----------------------------------------------------

        if boundary.covers(
            point
        ):

            polygon_features.append(
                as_feature(
                    item
                )
            )


    # --------------------------------------------------------
    # Diagnostic coordinates
    # --------------------------------------------------------

    print("")
    print("==============================================")
    print("          SPATIAL DIAGNOSTIC")
    print("==============================================")

    print("")
    print("BOUNDARY HGU PT SLS")
    print("----------------------------------------------")

    print(
        "MIN LON :",
        min_lon
    )

    print(
        "MIN LAT :",
        min_lat
    )

    print(
        "MAX LON :",
        max_lon
    )

    print(
        "MAX LAT :",
        max_lat
    )


    if all_lons and all_lats:

        print("")
        print("RENTANG KOORDINAT SEMUA HOTSPOT SIPONGI")
        print("----------------------------------------------")

        print(
            "MIN LON :",
            min(all_lons)
        )

        print(
            "MAX LON :",
            max(all_lons)
        )

        print(
            "MIN LAT :",
            min(all_lats)
        )

        print(
            "MAX LAT :",
            max(all_lats)
        )


    print("")
    print(
        "TOTAL HOTSPOT DARI SIPONGI :",
        len(items)
    )

    print(
        "HOTSPOT DALAM BBOX HGU     :",
        len(bbox_items)
    )

    print(
        "HOTSPOT DALAM POLYGON HGU  :",
        len(polygon_features)
    )


    # --------------------------------------------------------
    # Sample hotspot BBOX
    # --------------------------------------------------------

    if bbox_items:

        print("")
        print(
            "CONTOH HOTSPOT DALAM BBOX"
        )

        print(
            "----------------------------------------------"
        )


        for item in bbox_items[:20]:

            print(

                "LAT=",
                item.get(
                    "lat"
                ),

                "LON=",
                item.get(
                    "lon"
                ),

                "CONF=",
                item.get(
                    "confidence"
                ),

                "SRC=",
                item.get(
                    "sumber"
                ),

                "DATE=",
                item.get(
                    "date_hotspot"
                )

            )


    # --------------------------------------------------------
    # Kalau BBOX ada tapi polygon 0
    # --------------------------------------------------------

    if (

        len(bbox_items) > 0

        and

        len(polygon_features) == 0

    ):

        print("")
        print(
            "WARNING:"
        )

        print(
            "Ada hotspot di sekitar BBOX HGU "
            "tetapi tidak ada yang masuk polygon."
        )

        print(
            "Kemungkinan perlu diperiksa:"
        )

        print(
            "1. geometry boundary,"
        )

        print(
            "2. koordinat,"
        )

        print(
            "3. posisi hotspot terhadap HGU."
        )


    # --------------------------------------------------------
    # Hotspot terdekat apabila polygon = 0
    # --------------------------------------------------------

    if (

        len(polygon_features) == 0

        and

        items

    ):

        nearest = []

        for item in items:

            lon = item.get(
                "lon"
            )

            lat = item.get(
                "lat"
            )

            if lon is None or lat is None:
                continue


            point = Point(
                lon,
                lat
            )


            distance = boundary.distance(
                point
            )


            nearest.append(

                (
                    distance,
                    item
                )

            )


        nearest.sort(
            key=lambda x:
                x[0]
        )


        print("")
        print(
            "10 HOTSPOT TERDEKAT DENGAN BOUNDARY"
        )

        print(
            "----------------------------------------------"
        )


        for distance, item in nearest[:10]:

            print(

                "DIST_DEG=",
                round(
                    distance,
                    6
                ),

                "LAT=",
                item.get(
                    "lat"
                ),

                "LON=",
                item.get(
                    "lon"
                ),

                "CONF=",
                item.get(
                    "confidence"
                ),

                "SRC=",
                item.get(
                    "sumber"
                ),

                "DATE=",
                item.get(
                    "date_hotspot"
                )

            )


    print("")
    print(
        "=============================================="
    )

    return (
        polygon_features,
        len(bbox_items)
    )


# ============================================================
# MAIN
# ============================================================

def main():

    print("")
    print(
        "=============================================="
    )

    print(
        "       SiPongi PT SLS Hotspot Monitor"
    )

    print(
        "=============================================="
    )

    # --------------------------------------------------------
    # Load configuration
    # --------------------------------------------------------

    config = read_json(
        CONFIG_PATH
    )


    pt_name = config.get(
        "pt_name",
        "NAMA PT"
    )


    # --------------------------------------------------------
    # Load boundary
    # --------------------------------------------------------

    boundary = load_boundary()


    # --------------------------------------------------------
    # Fetch SiPongi
    # --------------------------------------------------------

    items = fetch_sipongi(
        config
    )


    # --------------------------------------------------------
    # Spatial diagnostic
    # --------------------------------------------------------

    features, bbox_count = spatial_diagnostic(

        items,

        boundary

    )


    # --------------------------------------------------------
    # Deduplicate
    # --------------------------------------------------------

    unique = {}

    for feature in features:

        unique[
            feature_key(
                feature
            )
        ] = feature


    features = list(
        unique.values()
    )


    # --------------------------------------------------------
    # Snapshot
    # --------------------------------------------------------

    snapshot_date = (
        make_snapshot_date()
    )


    snapshot = build_snapshot(

        features,

        snapshot_date

    )


    # --------------------------------------------------------
    # Load previous rolling state
    # --------------------------------------------------------

    previous_state = read_json(
        STATE_PATH
    )


    # --------------------------------------------------------
    # Rolling 7 days
    # --------------------------------------------------------

    state = update_rolling(

        previous_state,

        snapshot,

        int(
            config.get(
                "keep_days",
                7
            )
        )

    )


    # --------------------------------------------------------
    # Trend & summary
    # --------------------------------------------------------

    trend = build_trend(
        state
    )


    summary = build_summary(

        state,

        pt_name

    )


    # --------------------------------------------------------
    # Current GeoJSON
    # --------------------------------------------------------

    current_geojson = {

        "type":
            "FeatureCollection",

        "name":
            "sipongi_hotspots_pt_sls",

        "features":
            features

    }


    # --------------------------------------------------------
    # Write output
    # --------------------------------------------------------

    write_json(

        STATE_PATH,

        state

    )


    write_json(

        SITE_DATA /
        "current.geojson",

        current_geojson

    )


    write_json(

        SITE_DATA /
        "trend_7days.json",

        trend

    )


    write_json(

        SITE_DATA /
        "summary.json",

        summary

    )


    # --------------------------------------------------------
    # Copy boundary to GitHub Pages
    # --------------------------------------------------------

    boundary_for_site = (
        SITE_DATA /
        "pt_boundary.geojson"
    )


    write_json(

        boundary_for_site,

        read_json(
            BOUNDARY_PATH
        )

    )


    # --------------------------------------------------------
    # Final output
    # --------------------------------------------------------

    print("")
    print(
        "=============================================="
    )

    print(
        "FINAL RESULT"
    )

    print(
        "=============================================="
    )

    print(
        "PT:",
        pt_name
    )

    print(
        "Response hotspot:",
        len(items)
    )

    print(
        "Hotspot dalam BBOX:",
        bbox_count
    )

    print(
        "Hotspot inside PT:",
        len(features)
    )

    print(
        "High:",
        snapshot["high"]
    )

    print(
        "Medium:",
        snapshot["medium"]
    )

    print(
        "Low:",
        snapshot["low"]
    )

    print(
        "Rolling snapshots:",
        len(
            state[
                "snapshots"
            ]
        )
    )

    print(
        "Run UTC:",
        snapshot_date
    )

    print(
        "=============================================="
    )
    print("")


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    main()
