from __future__ import annotations

import json
import math
import os
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from shapely.geometry import Point, shape
from shapely.ops import unary_union, transform
from pyproj import Transformer


# ============================================================
# PATH
# ============================================================

ROOT = Path(__file__).resolve().parents[1]

CONFIG_PATH = ROOT / "config" / "config.json"
BOUNDARY_PATH = ROOT / "data" / "pt_boundary.geojson"
STATE_PATH = ROOT / "data" / "rolling_30days.json"

SITE_DATA = ROOT / "site" / "data"


# ============================================================
# JSON
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
# NUMBER
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
# BOUNDARY
# ============================================================

def load_boundary():

    obj = read_json(
        BOUNDARY_PATH
    )

    features = (
        obj.get("features")
        or []
    )

    if not features:
        raise RuntimeError(
            "Boundary PT belum tersedia."
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
            "Geometry boundary PT tidak ditemukan."
        )

    boundary = unary_union(
        geometries
    )

    if boundary.is_empty:
        raise RuntimeError(
            "Boundary PT kosong."
        )

    if not boundary.is_valid:
        print(
            "WARNING: Boundary PT invalid. "
            "Mencoba repair menggunakan buffer(0)."
        )

        boundary = boundary.buffer(0)

    return boundary


# ============================================================
# COORDINATE SYSTEM
# ============================================================

def prepare_metric_boundary(boundary):

    """
    Boundary awal kita WGS84.
    Untuk perhitungan jarak meter/km,
    gunakan CRS UTM yang sesuai dengan lokasi PT SLS.

    PT SLS sekitar 115 BT dan -2.8 LS:
    UTM Zone 50S = EPSG:32750
    """

    to_metric = Transformer.from_crs(
        "EPSG:4326",
        "EPSG:32750",
        always_xy=True
    ).transform

    from_metric = Transformer.from_crs(
        "EPSG:32750",
        "EPSG:4326",
        always_xy=True
    ).transform

    boundary_metric = transform(
        to_metric,
        boundary
    )

    return (
        boundary_metric,
        to_metric,
        from_metric
    )


# ============================================================
# EXTRACT RESPONSE
# ============================================================

def extract_items(payload: Any) -> list[dict[str, Any]]:

    if isinstance(
        payload,
        list
    ):

        return [
            x
            for x in payload
            if isinstance(x, dict)
        ]


    if isinstance(
        payload,
        dict
    ):

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

                result.append({

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

                })

            return result


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
                    x
                    for x in value
                    if isinstance(x, dict)
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

    lat = None
    lon = None


    # --------------------------------------------------------
    # GeoJSON geometry
    # --------------------------------------------------------

    geometry = item.get(
        "_geometry"
    )

    if isinstance(
        geometry,
        dict
    ):

        coordinates = geometry.get(
            "coordinates"
        )

        if (
            geometry.get("type")
            == "Point"
            and isinstance(
                coordinates,
                list
            )
            and len(coordinates) >= 2
        ):

            lon = num(
                coordinates[0]
            )

            lat = num(
                coordinates[1]
            )


    # --------------------------------------------------------
    # Standard fields
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


    if (
        lat is None
        or
        lon is None
    ):

        return None


    if not (
        -90 <= lat <= 90
        and
        -180 <= lon <= 180
    ):

        return None


    # --------------------------------------------------------
    # CONFIDENCE
    # --------------------------------------------------------

    confidence_number = num(
        first_value(
            item,
            "confidence"
        )
    )


    confidence_level = str(
        first_value(
            item,
            "confidence_level",
            "confidenceLevel"
        )
        or ""
    ).strip().lower()


    if (
        confidence_level
        not in {
            "low",
            "medium",
            "high"
        }
    ):

        if confidence_number is not None:

            if confidence_number < 30:
                confidence_level = "low"

            elif confidence_number < 80:
                confidence_level = "medium"

            else:
                confidence_level = "high"

        else:

            confidence_level = "unknown"


    # --------------------------------------------------------
    # ATTRIBUTES
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


    result = {

        "lat":
            lat,

        "lon":
            lon,

        "confidence":
            confidence_level,

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
    # Extra scalar fields
    # --------------------------------------------------------

    for key, value in item.items():

        if key.startswith("_"):
            continue

        if key in result:
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

            result[key] = value


    return result


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
    # DATE RANGE
    # --------------------------------------------------------

    days_back = int(
        config.get(
            "days_back",
            30
        )
    )


    today = datetime.now(
        timezone.utc
    ).date()


    from_date = (
        today
        -
        timedelta(
            days=days_back - 1
        )
    )


    to_date = today


    print("")
    print("=== SIPONGI REQUEST ===")
    print("Endpoint:", endpoint)
    print("Mode:", config.get(
        "mode"
    ))
    print("From:", from_date)
    print("To:", to_date)
    print("Days:", days_back)
    print("Satelit:", config.get(
        "satelit"
    ))
    print("Confidence:", config.get(
        "confidence"
    ))
    print("=======================")
    print("")


    # --------------------------------------------------------
    # PARAMETER REQUEST
    # --------------------------------------------------------

    params: list[tuple[str, Any]] = [

        (
            "wilayah",
            "IN"
        ),

        (
            "filterperiode",
            "true"
        ),

        (
            "from",
            from_date.strftime(
                "%Y-%m-%d"
            )
        ),

        (
            "to",
            to_date.strftime(
                "%Y-%m-%d"
            )
        )

    ]


    # --------------------------------------------------------
    # SATELLITE
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
    # CONFIDENCE
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


    params.extend([

        (
            "provinsi",
            ""
        ),

        (
            "kabkota",
            ""
        )

    ])


    # --------------------------------------------------------
    # HEADERS
    # --------------------------------------------------------

    headers = {

        "User-Agent":
            "SiPongi-PT-SLS-Hotspot-Monitor/2.0",

        "Accept":
            "application/json,text/plain,*/*",

        "Referer":
            "https://sipongi.gakkum.kehutanan.go.id/peta"

    }


    # --------------------------------------------------------
    # REQUEST
    # --------------------------------------------------------

    response = requests.get(

        endpoint,

        params=params,

        headers=headers,

        timeout=120

    )


    response.raise_for_status()


    print(
        "HTTP:",
        response.status_code
    )


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

            "Response SiPongi bukan JSON.\n"
            f"Content-Type: {content_type}\n"
            f"Response awal: {text[:500]}"

        )


    payload = response.json()


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
# DISTANCE ZONE
# ============================================================

def classify_distance_zone(

    distance_km: float,

    inside_hgu: bool,

    zones: list[dict[str, Any]]

) -> str:

    if inside_hgu:
        return "Inside HGU"


    for zone in zones:

        name = zone.get(
            "name"
        )

        min_km = float(
            zone.get(
                "min_km",
                0
            )
        )

        max_km = float(
            zone.get(
                "max_km",
                0
            )
        )


        if (

            distance_km > min_km

            and

            distance_km <= max_km

        ):

            return name


    return ">5 km"


# ============================================================
# SPATIAL FILTER + DISTANCE
# ============================================================

def process_spatial(

    items: list[dict[str, Any]],

    boundary,

    config

) -> list[dict[str, Any]]:

    (
        boundary_metric,
        to_metric,
        from_metric
    ) = prepare_metric_boundary(
        boundary
    )


    max_distance_km = float(
        config.get(
            "distance_max_km",
            5
        )
    )


    zones = config.get(
        "zones_km",
        []
    )


    result = []


    # --------------------------------------------------------
    # Diagnostic
    # --------------------------------------------------------

    boundary_minx, boundary_miny, boundary_maxx, boundary_maxy = (
        boundary.bounds
    )


    bbox_count = 0
    inside_count = 0


    print("")
    print("==============================================")
    print("          SPATIAL + DISTANCE ANALYSIS")
    print("==============================================")

    print("")
    print("BOUNDARY HGU PT SLS")
    print("----------------------------------------------")

    print(
        "MIN LON:",
        boundary_minx
    )

    print(
        "MIN LAT:",
        boundary_miny
    )

    print(
        "MAX LON:",
        boundary_maxx
    )

    print(
        "MAX LAT:",
        boundary_maxy
    )


    # --------------------------------------------------------
    # Process
    # --------------------------------------------------------

    for item in items:

        lat = item["lat"]
        lon = item["lon"]


        point_wgs84 = Point(
            lon,
            lat
        )


        # ----------------------------------------------------
        # BBOX
        # ----------------------------------------------------

        if (

            boundary_minx <= lon <= boundary_maxx

            and

            boundary_miny <= lat <= boundary_maxy

        ):

            bbox_count += 1


        # ----------------------------------------------------
        # TRANSFORM POINT TO METRIC CRS
        # ----------------------------------------------------

        point_metric = transform(
            to_metric,
            point_wgs84
        )


        # ----------------------------------------------------
        # INSIDE HGU
        # ----------------------------------------------------

        inside_hgu = boundary.covers(
            point_wgs84
        )


        if inside_hgu:

            inside_count += 1


        # ----------------------------------------------------
        # DISTANCE TO HGU BOUNDARY
        # ----------------------------------------------------

        distance_m = (
            boundary_metric.distance(
                point_metric
            )
        )


        distance_km = (
            distance_m
            /
            1000.0
        )


        # ----------------------------------------------------
        # Filter max 5 km
        # ----------------------------------------------------

        if (

            not inside_hgu

            and

            distance_km > max_distance_km

        ):

            continue


        zone = classify_distance_zone(

            distance_km,

            inside_hgu,

            zones

        )


        feature = as_feature(

            item

        )


        feature[
            "properties"
        ][
            "distance_hgu_km"
        ] = round(

            distance_km,

            3

        )


        feature[
            "properties"
        ][
            "zone"
        ] = zone


        feature[
            "properties"
        ][
            "location_status"
        ] = (

            "INSIDE HGU"

            if inside_hgu

            else

            "OUTSIDE HGU"

        )


        result.append(
            feature
        )


    print("")
    print(
        "TOTAL HOTSPOT SIPONGI:",
        len(items)
    )

    print(
        "HOTSPOT DALAM BBOX:",
        bbox_count
    )

    print(
        "HOTSPOT INSIDE HGU:",
        inside_count
    )

    print(
        "HOTSPOT <= 5 KM:",
        len(result)
    )


    print("")
    print(
        "DISTRIBUSI ZONA:"
    )


    zone_counts = Counter(

        feature[
            "properties"
        ].get(
            "zone",
            "unknown"
        )

        for feature in result

    )


    for zone_name, count in zone_counts.items():

        print(
            f"  {zone_name}: {count}"
        )


    print("")
    print(
        "=============================================="
    )


    return result


# ============================================================
# FEATURE
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
# FEATURE KEY
# ============================================================

def feature_key(
    feature: dict[str, Any]
) -> tuple:

    properties = feature.get(
        "properties",
        {}
    )


    lon, lat = feature[
        "geometry"
    ][
        "coordinates"
    ]


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
            or
            ""
        ),

        str(
            properties.get(
                "sumber"
            )
            or
            ""
        ),

        str(
            properties.get(
                "confidence"
            )
            or
            ""
        )

    )


# ============================================================
# DEDUPLICATE
# ============================================================

def deduplicate(
    features: list[dict[str, Any]]
) -> list[dict[str, Any]]:

    unique = {}

    for feature in features:

        unique[
            feature_key(
                feature
            )
        ] = feature


    return list(
        unique.values()
    )


# ============================================================
# SNAPSHOT
# ============================================================

def build_snapshot(

    features: list[dict[str, Any]],

    snapshot_date: str

) -> dict[str, Any]:

    confidence_counter = Counter(

        feature[
            "properties"
        ].get(
            "confidence",
            "unknown"
        )

        for feature in features

    )


    zone_counter = Counter(

        feature[
            "properties"
        ].get(
            "zone",
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
            confidence_counter.get(
                "high",
                0
            ),

        "medium":
            confidence_counter.get(
                "medium",
                0
            ),

        "low":
            confidence_counter.get(
                "low",
                0
            ),

        "unknown":
            confidence_counter.get(
                "unknown",
                0
            ),

        "inside_hgu":
            zone_counter.get(
                "Inside HGU",
                0
            ),

        "zone_0_1":
            zone_counter.get(
                "0–1 km",
                0
            ),

        "zone_1_3":
            zone_counter.get(
                "1–3 km",
                0
            ),

        "zone_3_5":
            zone_counter.get(
                "3–5 km",
                0
            ),

        "features":
            features

    }


# ============================================================
# ROLLING 30 DAYS
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


    return {

        "snapshots":

            [

                by_date[
                    date
                ]

                for date in sorted(
                    dates
                )

            ]

    }


# ============================================================
# TREND 30 DAYS
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


    series = []


    for snapshot in snapshots:

        series.append({

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
                ),

            "inside_hgu":
                snapshot.get(
                    "inside_hgu",
                    0
                ),

            "zone_0_1":
                snapshot.get(
                    "zone_0_1",
                    0
                ),

            "zone_1_3":
                snapshot.get(
                    "zone_1_3",
                    0
                ),

            "zone_3_5":
                snapshot.get(
                    "zone_3_5",
                    0
                )

        })


    return {

        "dates":
            [
                x["date"]
                for x in series
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

            "inside_hgu":
                0,

            "zone_0_1":
                0,

            "zone_1_3":
                0,

            "zone_3_5":
                0,

            "vs_previous_pct":
                None,

            "average_30_days":
                0,

            "min_30_days":
                0,

            "max_30_days":
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


    vs_previous = None


    if (

        previous

        and

        previous.get(
            "total",
            0
        ) != 0

    ):

        vs_previous = round(

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


    if vs_previous is None:

        status = (
            "Belum dapat dibandingkan"
        )

    elif vs_previous > 20:

        status = (
            "Meningkat"
        )

    elif vs_previous < -20:

        status = (
            "Menurun"
        )

    else:

        status = (
            "Stabil"
        )


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

        "inside_hgu":
            latest.get(
                "inside_hgu",
                0
            ),

        "zone_0_1":
            latest.get(
                "zone_0_1",
                0
            ),

        "zone_1_3":
            latest.get(
                "zone_1_3",
                0
            ),

        "zone_3_5":
            latest.get(
                "zone_3_5",
                0
            ),

        "vs_previous_pct":
            vs_previous,

        "average_30_days":
            round(
                average,
                1
            ),

        "min_30_days":
            min(totals),

        "max_30_days":
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
        "       MODE: 30 HARI + BUFFER 5 KM"
    )

    print(
        "=============================================="
    )


    # --------------------------------------------------------
    # CONFIG
    # --------------------------------------------------------

    config = read_json(
        CONFIG_PATH
    )


    pt_name = config.get(
        "pt_name",
        "PT SLS"
    )


    # --------------------------------------------------------
    # BOUNDARY
    # --------------------------------------------------------

    boundary = load_boundary()


    # --------------------------------------------------------
    # SIPONGI
    # --------------------------------------------------------

    items = fetch_sipongi(
        config
    )


    # --------------------------------------------------------
    # SPATIAL PROCESS
    # --------------------------------------------------------

    features = process_spatial(

        items,

        boundary,

        config

    )


    # --------------------------------------------------------
    # DEDUP
    # --------------------------------------------------------

    features = deduplicate(
        features
    )


    # --------------------------------------------------------
    # SNAPSHOT DATE
    # --------------------------------------------------------

    snapshot_date = (
        datetime.now(
            timezone.utc
        ).strftime(
            "%Y-%m-%d"
        )
    )


    snapshot = build_snapshot(

        features,

        snapshot_date

    )


    # --------------------------------------------------------
    # PREVIOUS STATE
    # --------------------------------------------------------

    if STATE_PATH.exists():

        previous_state = read_json(
            STATE_PATH
        )

    else:

        previous_state = {
            "snapshots": []
        }


    # --------------------------------------------------------
    # ROLLING
    # --------------------------------------------------------

    state = update_rolling(

        previous_state,

        snapshot,

        int(
            config.get(
                "keep_days",
                30
            )
        )

    )


    # --------------------------------------------------------
    # TREND
    # --------------------------------------------------------

    trend = build_trend(
        state
    )


    # --------------------------------------------------------
    # SUMMARY
    # --------------------------------------------------------

    summary = build_summary(

        state,

        pt_name

    )


    # --------------------------------------------------------
    # CURRENT GEOJSON
    # --------------------------------------------------------

    current_geojson = {

        "type":
            "FeatureCollection",

        "name":
            "sipongi_pt_sls_5km",

        "features":
            features

    }


    # --------------------------------------------------------
    # WRITE STATE
    # --------------------------------------------------------

    write_json(

        STATE_PATH,

        state

    )


    # --------------------------------------------------------
    # WRITE SITE
    # --------------------------------------------------------

    write_json(

        SITE_DATA /
        "current.geojson",

        current_geojson

    )


    write_json(

        SITE_DATA /
        "trend_30days.json",

        trend

    )


    write_json(

        SITE_DATA /
        "summary.json",

        summary

    )


    # --------------------------------------------------------
    # BACKWARD COMPATIBILITY
    #
    # Dashboard lama masih membaca trend_7days.json.
    # Untuk sementara buat alias data yang sama.
    # Nanti setelah HTML di-upgrade kita bisa ubah
    # menjadi trend_30days.json.
    # --------------------------------------------------------

    write_json(

        SITE_DATA /
        "trend_7days.json",

        trend

    )


    # --------------------------------------------------------
    # COPY BOUNDARY
    # --------------------------------------------------------

    write_json(

        SITE_DATA /
        "pt_boundary.geojson",

        read_json(
            BOUNDARY_PATH
        )

    )


    # --------------------------------------------------------
    # FINAL LOG
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
        "Total dari SiPongi:",
        len(items)
    )

    print(
        "Dalam HGU / <=5km:",
        len(features)
    )

    print(
        "Inside HGU:",
        snapshot[
            "inside_hgu"
        ]
    )

    print(
        "0–1 km:",
        snapshot[
            "zone_0_1"
        ]
    )

    print(
        "1–3 km:",
        snapshot[
            "zone_1_3"
        ]
    )

    print(
        "3–5 km:",
        snapshot[
            "zone_3_5"
        ]
    )

    print(
        "High:",
        snapshot[
            "high"
        ]
    )

    print(
        "Medium:",
        snapshot[
            "medium"
        ]
    )

    print(
        "Low:",
        snapshot[
            "low"
        ]
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
        "=============================================="
    )

    print("")


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    main()
