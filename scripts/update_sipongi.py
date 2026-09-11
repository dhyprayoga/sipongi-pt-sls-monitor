from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from pyproj import Transformer
from shapely.geometry import Point, shape
from shapely.ops import transform, unary_union


# ============================================================
# CONFIGURATION
# ============================================================

WIB = ZoneInfo("Asia/Jakarta")

ROOT = Path(__file__).resolve().parents[1]

CONFIG_PATH = ROOT / "config" / "config.json"
BOUNDARY_PATH = ROOT / "data" / "pt_boundary.geojson"

DATA_DIR = ROOT / "data"
HISTORY_DIR = DATA_DIR / "history"

SITE_DATA = ROOT / "site" / "data"

STATE_PATH = DATA_DIR / "rolling_30days.json"


# ============================================================
# JSON
# ============================================================

def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as f:
        json.dump(
            data,
            f,
            indent=2,
            ensure_ascii=False
        )


# ============================================================
# HELPERS
# ============================================================

def first_value(
    data: dict[str, Any],
    *keys: str
) -> Any:

    for key in keys:
        if (
            key in data
            and data[key] is not None
            and data[key] != ""
        ):
            return data[key]

    return None


def to_float(value: Any) -> float | None:

    try:

        if value is None:
            return None

        if isinstance(value, str):
            value = value.strip()

        return float(value)

    except (TypeError, ValueError):
        return None


# ============================================================
# DATE / TIME
# ============================================================

def monitoring_dates(days_back: int) -> tuple[date, date]:

    """
    Semua tanggal monitoring menggunakan WIB,
    bukan UTC.

    Contoh:
    11 Sep 2026 WIB
    -> start = 13 Aug 2026
    -> end   = 11 Sep 2026
    """

    now_wib = datetime.now(WIB)

    end_date = now_wib.date()

    start_date = (
        end_date
        -
        timedelta(days=days_back - 1)
    )

    return start_date, end_date


def parse_hotspot_date(value: Any) -> str | None:

    if not value:
        return None

    text = str(value).strip()

    # --------------------------------------------------------
    # ISO datetime
    # --------------------------------------------------------

    try:

        parsed = datetime.fromisoformat(
            text.replace("Z", "+00:00")
        )

        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(WIB)

        return parsed.strftime("%Y-%m-%d")

    except ValueError:
        pass

    # --------------------------------------------------------
    # YYYY-MM-DD
    # --------------------------------------------------------

    match = re.search(
        r"(\d{4})-(\d{2})-(\d{2})",
        text
    )

    if match:
        return match.group(0)

    # --------------------------------------------------------
    # DD/MM/YYYY
    # --------------------------------------------------------

    match = re.search(
        r"(\d{1,2})/(\d{1,2})/(\d{4})",
        text
    )

    if match:

        try:

            return date(
                int(match.group(3)),
                int(match.group(2)),
                int(match.group(1))
            ).strftime("%Y-%m-%d")

        except ValueError:
            pass

    # --------------------------------------------------------
    # DD-MM-YYYY
    # --------------------------------------------------------

    match = re.search(
        r"(\d{1,2})-(\d{1,2})-(\d{4})",
        text
    )

    if match:

        try:

            return date(
                int(match.group(3)),
                int(match.group(2)),
                int(match.group(1))
            ).strftime("%Y-%m-%d")

        except ValueError:
            pass

    # --------------------------------------------------------
    # Indonesian date
    #
    # Kamis, 10 September 2026 01:15:00
    # --------------------------------------------------------

    months = {
        "januari": 1,
        "februari": 2,
        "maret": 3,
        "april": 4,
        "mei": 5,
        "juni": 6,
        "juli": 7,
        "agustus": 8,
        "september": 9,
        "oktober": 10,
        "november": 11,
        "desember": 12
    }

    parts = text.replace(",", " ").split()

    for i, part in enumerate(parts):

        month_number = months.get(
            part.lower()
        )

        if month_number is None:
            continue

        if i < 1 or i + 1 >= len(parts):
            continue

        day_text = parts[i - 1]
        year_text = parts[i + 1]

        if not (
            day_text.isdigit()
            and year_text.isdigit()
        ):
            continue

        try:

            return date(
                int(year_text),
                month_number,
                int(day_text)
            ).strftime("%Y-%m-%d")

        except ValueError:
            continue

    return None


# ============================================================
# BOUNDARY
# ============================================================

def load_boundary():

    if not BOUNDARY_PATH.exists():
        raise RuntimeError(
            f"Boundary tidak ditemukan: {BOUNDARY_PATH}"
        )

    obj = read_json(
        BOUNDARY_PATH
    )

    features = obj.get(
        "features"
    ) or []

    if not features:
        raise RuntimeError(
            "pt_boundary.geojson tidak memiliki features."
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
            "Geometry boundary tidak ditemukan."
        )

    boundary = unary_union(
        geometries
    )

    if boundary.is_empty:
        raise RuntimeError(
            "Boundary kosong."
        )

    if not boundary.is_valid:
        print(
            "WARNING: Boundary invalid, memperbaiki..."
        )

        boundary = boundary.buffer(0)

    return boundary


# ============================================================
# METRIC CRS
# ============================================================

def prepare_metric_boundary(
    boundary
):

    """
    PT SLS sekitar 115 BT.
    UTM Zone 50S = EPSG:32750.
    """

    to_metric = Transformer.from_crs(
        "EPSG:4326",
        "EPSG:32750",
        always_xy=True
    ).transform

    boundary_metric = transform(
        to_metric,
        boundary
    )

    return boundary_metric, to_metric


# ============================================================
# SIPONGI RESPONSE EXTRACTION
# ============================================================

def extract_items(
    payload: Any
) -> list[dict[str, Any]]:

    if isinstance(payload, list):

        return [
            item
            for item in payload
            if isinstance(item, dict)
        ]

    if isinstance(payload, dict):

        if payload.get("type") == "FeatureCollection":

            result = []

            for feature in (
                payload.get("features") or []
            ):

                if not isinstance(feature, dict):
                    continue

                properties = (
                    feature.get("properties")
                    or {}
                )

                result.append({

                    **properties,

                    "_geometry":
                        feature.get("geometry")

                })

            return result

        for key in (
            "data",
            "results",
            "items",
            "hotspots"
        ):

            value = payload.get(key)

            if isinstance(value, list):

                return [
                    item
                    for item in value
                    if isinstance(item, dict)
                ]

            if isinstance(value, dict):

                nested = extract_items(
                    value
                )

                if nested:
                    return nested

    raise RuntimeError(
        "Struktur response SiPongi tidak dikenali."
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
    # GeoJSON
    # --------------------------------------------------------

    geometry = item.get(
        "_geometry"
    )

    if isinstance(geometry, dict):

        coordinates = geometry.get(
            "coordinates"
        )

        if (
            geometry.get("type") == "Point"
            and isinstance(coordinates, list)
            and len(coordinates) >= 2
        ):

            lon = to_float(
                coordinates[0]
            )

            lat = to_float(
                coordinates[1]
            )

    # --------------------------------------------------------
    # Coordinates
    # --------------------------------------------------------

    if lat is None:

        lat = to_float(
            first_value(
                item,
                "lat",
                "latitude",
                "LAT",
                "y"
            )
        )

    if lon is None:

        lon = to_float(
            first_value(
                item,
                "long",
                "lon",
                "longitude",
                "LONG",
                "x"
            )
        )

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

    confidence_raw = first_value(
        item,
        "confidence_level",
        "confidenceLevel",
        "confidence"
    )

    confidence_value = to_float(
        first_value(
            item,
            "confidence_value"
        )
    )

    confidence_numeric = to_float(
        first_value(
            item,
            "confidence"
        )
    )

    if confidence_value is None:
        confidence_value = confidence_numeric

    confidence = str(
        confidence_raw or ""
    ).strip().lower()

    if confidence not in {
        "low",
        "medium",
        "high"
    }:

        if confidence_value is not None:

            if confidence_value < 30:
                confidence = "low"

            elif confidence_value < 80:
                confidence = "medium"

            else:
                confidence = "high"

        else:
            confidence = "unknown"

    # --------------------------------------------------------
    # Date
    # --------------------------------------------------------

    date_hotspot = first_value(
        item,
        "date_hotspot",
        "date",
        "tanggal",
        "acq_date"
    )

    date_hotspot_ori = first_value(
        item,
        "date_hotspot_ori",
        "acq_datetime",
        "datetime"
    )

    # --------------------------------------------------------
    # Other
    # --------------------------------------------------------

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

        "lat": lat,
        "lon": lon,

        "confidence":
            confidence,

        "confidence_value":
            confidence_value,

        "sumber":
            source,

        "date_hotspot":
            date_hotspot,

        "date_hotspot_ori":
            date_hotspot_ori,

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
    # Preserve scalar fields
    # --------------------------------------------------------

    for key, value in item.items():

        if key.startswith("_"):
            continue

        if key in result:
            continue

        if (
            isinstance(
                value,
                (
                    str,
                    int,
                    float,
                    bool
                )
            )
            or
            value is None
        ):

            result[key] = value

    return result


# ============================================================
# GEOJSON
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
# FETCH SIPONGI
# ============================================================

def fetch_sipongi(
    config: dict[str, Any],
    start_date: date,
    end_date: date
) -> list[dict[str, Any]]:

    endpoint = config[
        "sipongi_endpoint"
    ]

    province = str(
        config.get(
            "provinsi",
            "12"
        )
    )

    late_mode = str(
        config.get(
            "late_mode",
            "custom"
        )
    )

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
            start_date.strftime(
                "%Y-%m-%d"
            )
        ),

        (
            "to",
            end_date.strftime(
                "%Y-%m-%d"
            )
        ),

        (
            "late",
            late_mode
        )

    ]

    # --------------------------------------------------------
    # Satellites
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

    # --------------------------------------------------------
    # Province
    # --------------------------------------------------------

    params.append(
        (
            "provinsi",
            province
        )
    )

    # --------------------------------------------------------
    # District
    # --------------------------------------------------------

    params.append(
        (
            "kabkota",
            ""
        )
    )

    headers = {

        "User-Agent":
            "SiPongi-PT-SLS-Hotspot-Monitor/6.0",

        "Accept":
            "application/json,text/plain,*/*",

        "Referer":
            "https://sipongi.gakkum.kehutanan.go.id/peta"

    }

    prepared = requests.Request(
        "GET",
        endpoint,
        params=params,
        headers=headers
    ).prepare()

    print("")
    print(
        "=============================================="
    )

    print(
        "             SIPONGI REQUEST"
    )

    print(
        "=============================================="
    )

    print(
        "From WIB:",
        start_date
    )

    print(
        "To WIB:",
        end_date
    )

    print(
        "Late:",
        late_mode
    )

    print(
        "Provinsi:",
        province
    )

    print("")
    print(
        "FULL REQUEST URL:"
    )

    print(
        prepared.url
    )

    print(
        "=============================================="
    )

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

    payload = response.json()

    raw_items = extract_items(
        payload
    )

    items = []

    for raw_item in raw_items:

        clean = normalize_item(
            raw_item
        )

        if clean:
            items.append(
                clean
            )

    print(
        "Raw records:",
        len(raw_items)
    )

    print(
        "Normalized records:",
        len(items)
    )

    return items


# ============================================================
# SPATIAL FILTER
# ============================================================

def classify_zone(
    distance_km: float,
    inside_hgu: bool
) -> str:

    if inside_hgu:
        return "Inside HGU"

    if distance_km <= 1:
        return "0–1 km"

    if distance_km <= 3:
        return "1–3 km"

    if distance_km <= 5:
        return "3–5 km"

    return ">5 km"


def process_spatial(
    items: list[dict[str, Any]],
    boundary,
    max_distance_km: float
) -> list[dict[str, Any]]:

    (
        boundary_metric,
        to_metric
    ) = prepare_metric_boundary(
        boundary
    )

    minx, miny, maxx, maxy = (
        boundary.bounds
    )

    bbox_count = 0
    inside_count = 0

    result = []

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
            minx <= lon <= maxx
            and
            miny <= lat <= maxy
        ):

            bbox_count += 1

        # ----------------------------------------------------
        # Inside HGU
        # ----------------------------------------------------

        inside_hgu = boundary.covers(
            point_wgs84
        )

        if inside_hgu:
            inside_count += 1

        # ----------------------------------------------------
        # Metric
        # ----------------------------------------------------

        point_metric = transform(
            to_metric,
            point_wgs84
        )

        distance_m = boundary_metric.distance(
            point_metric
        )

        distance_km = (
            distance_m / 1000.0
        )

        # ----------------------------------------------------
        # Filter
        # ----------------------------------------------------

        if (
            not inside_hgu
            and
            distance_km > max_distance_km
        ):
            continue

        zone = classify_zone(
            distance_km,
            inside_hgu
        )

        feature = as_feature(
            item
        )

        properties = feature[
            "properties"
        ]

        properties[
            "distance_hgu_km"
        ] = round(
            distance_km,
            3
        )

        properties[
            "zone"
        ] = zone

        properties[
            "location_status"
        ] = (
            "INSIDE HGU"
            if inside_hgu
            else
            "OUTSIDE HGU"
        )

        # Date hasil parsing
        hotspot_date = (
            parse_hotspot_date(
                properties.get(
                    "date_hotspot_ori"
                )
            )
            or
            parse_hotspot_date(
                properties.get(
                    "date_hotspot"
                )
            )
        )

        properties[
            "hotspot_date"
        ] = hotspot_date

        result.append(
            feature
        )

    # --------------------------------------------------------
    # Diagnostics
    # --------------------------------------------------------

    print("")
    print(
        "=============================================="
    )

    print(
        "        SPATIAL + DISTANCE RESULT"
    )

    print(
        "=============================================="
    )

    print(
        "Boundary BBOX:"
    )

    print(
        "  MIN LON:",
        minx
    )

    print(
        "  MIN LAT:",
        miny
    )

    print(
        "  MAX LON:",
        maxx
    )

    print(
        "  MAX LAT:",
        maxy
    )

    print("")

    print(
        "Total hotspot SiPongi:",
        len(items)
    )

    print(
        "Dalam BBOX:",
        bbox_count
    )

    print(
        "Inside HGU:",
        inside_count
    )

    print(
        "Dalam radius <= 5 km:",
        len(result)
    )

    zone_counter = Counter(

        feature[
            "properties"
        ].get(
            "zone",
            "unknown"
        )

        for feature in result

    )

    print("")
    print(
        "DISTRIBUSI ZONA:"
    )

    for zone_name in (
        "Inside HGU",
        "0–1 km",
        "1–3 km",
        "3–5 km"
    ):

        print(
            f"  {zone_name}:",
            zone_counter.get(
                zone_name,
                0
            )
        )

    return result


# ============================================================
# DEDUPLICATE
# ============================================================

def feature_key(
    feature: dict[str, Any]
) -> tuple:

    coordinates = feature[
        "geometry"
    ][
        "coordinates"
    ]

    properties = feature.get(
        "properties",
        {}
    )

    return (

        round(
            float(coordinates[1]),
            5
        ),

        round(
            float(coordinates[0]),
            5
        ),

        str(
            properties.get(
                "hotspot_date",
                ""
            )
        ),

        str(
            properties.get(
                "date_hotspot_ori",
                ""
            )
        ),

        str(
            properties.get(
                "sumber",
                ""
            )
        ),

        str(
            properties.get(
                "confidence",
                ""
            )
        )

    )


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
# DAILY HISTORY
# ============================================================

def build_empty_day(
    date_key: str,
    generated_wib: str,
    query_start: str,
    query_end: str
) -> dict[str, Any]:

    return {

        "date":
            date_key,

        "query_period":
            {
                "from":
                    query_start,

                "to":
                    query_end
            },

        "generated_wib":
            generated_wib,

        "hotspot_count":
            0,

        "features":
            []

    }


def write_daily_history(

    features: list[dict[str, Any]],

    start_date: date,

    end_date: date

) -> dict[str, Any]:

    HISTORY_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    generated_wib = datetime.now(
        WIB
    ).isoformat(
        timespec="seconds"
    )

    # --------------------------------------------------------
    # Group current features by observation date
    # --------------------------------------------------------

    grouped: dict[str, list[dict[str, Any]]] = (
        defaultdict(list)
    )

    for feature in features:

        properties = feature.get(
            "properties",
            {}
        )

        hotspot_date = (
            properties.get(
                "hotspot_date"
            )
        )

        if not hotspot_date:
            continue

        if not (
            start_date.strftime("%Y-%m-%d")
            <=
            hotspot_date
            <=
            end_date.strftime("%Y-%m-%d")
        ):
            continue

        grouped[
            hotspot_date
        ].append(
            feature
        )

    # --------------------------------------------------------
    # Write every date in current 30-day window
    # --------------------------------------------------------

    current = start_date

    while current <= end_date:

        date_key = current.strftime(
            "%Y-%m-%d"
        )

        day_features = grouped.get(
            date_key,
            []
        )

        obj = build_empty_day(

            date_key,

            generated_wib,

            start_date.strftime(
                "%Y-%m-%d"
            ),

            end_date.strftime(
                "%Y-%m-%d"
            )

        )

        obj[
            "hotspot_count"
        ] = len(
            day_features
        )

        obj[
            "features"
        ] = day_features

        write_json(

            HISTORY_DIR /
            f"{date_key}.json",

            obj

        )

        current += timedelta(
            days=1
        )

    print("")
    print(
        "=============================================="
    )

    print(
        "           DAILY HISTORY RESULT"
    )

    print(
        "=============================================="
    )

    active_days = 0
    total_history = 0

    current = start_date

    while current <= end_date:

        date_key = current.strftime(
            "%Y-%m-%d"
        )

        count = len(
            grouped.get(
                date_key,
                []
            )
        )

        print(
            f"  {date_key}: {count}"
        )

        if count > 0:
            active_days += 1

        total_history += count

        current += timedelta(
            days=1
        )

    print("")
    print(
        "Hari dengan hotspot:",
        active_days
    )

    print(
        "Total hotspot history:",
        total_history
    )

    return grouped


# ============================================================
# BUILD TREND
# ============================================================

def build_trend_from_features(

    features: list[dict[str, Any]],

    start_date: date,

    end_date: date

) -> dict[str, Any]:

    daily = defaultdict(

        lambda: {

            "total": 0,

            "high": 0,

            "medium": 0,

            "low": 0,

            "unknown": 0,

            "inside_hgu": 0,

            "zone_0_1": 0,

            "zone_1_3": 0,

            "zone_3_5": 0

        }

    )

    records_without_date = 0

    for feature in features:

        properties = feature.get(
            "properties",
            {}
        )

        hotspot_date = (
            properties.get(
                "hotspot_date"
            )
        )

        if not hotspot_date:

            records_without_date += 1

            continue

        zone = properties.get(
            "zone"
        )

        confidence = str(

            properties.get(
                "confidence",
                "unknown"
            )

        ).lower()

        daily[
            hotspot_date
        ][
            "total"
        ] += 1

        if confidence in {
            "high",
            "medium",
            "low"
        }:

            daily[
                hotspot_date
            ][
                confidence
            ] += 1

        else:

            daily[
                hotspot_date
            ][
                "unknown"
            ] += 1

        if zone == "Inside HGU":

            daily[
                hotspot_date
            ][
                "inside_hgu"
            ] += 1

        elif zone == "0–1 km":

            daily[
                hotspot_date
            ][
                "zone_0_1"
            ] += 1

        elif zone == "1–3 km":

            daily[
                hotspot_date
            ][
                "zone_1_3"
            ] += 1

        elif zone == "3–5 km":

            daily[
                hotspot_date
            ][
                "zone_3_5"
            ] += 1

    series = []

    current = start_date

    while current <= end_date:

        date_key = current.strftime(
            "%Y-%m-%d"
        )

        values = daily.get(

            date_key,

            {

                "total": 0,

                "high": 0,

                "medium": 0,

                "low": 0,

                "unknown": 0,

                "inside_hgu": 0,

                "zone_0_1": 0,

                "zone_1_3": 0,

                "zone_3_5": 0

            }

        )

        series.append({

            "date":
                date_key,

            **values

        })

        current += timedelta(
            days=1
        )

    return {

        "period_start":
            start_date.strftime(
                "%Y-%m-%d"
            ),

        "period_end":
            end_date.strftime(
                "%Y-%m-%d"
            ),

        "dates":
            [
                item["date"]
                for item in series
            ],

        "series":
            series,

        "records_without_date":
            records_without_date,

        "last_updated_wib":
            datetime.now(
                WIB
            ).isoformat(
                timespec="seconds"
            ),

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

    features: list[dict[str, Any]],

    trend: dict[str, Any],

    pt_name: str,

    query_start: date,

    query_end: date

) -> dict[str, Any]:

    series = trend.get(
        "series",
        []
    )

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

    daily_totals = [

        int(
            item.get(
                "total",
                0
            )
        )

        for item in series

    ]

    total = sum(
        daily_totals
    )

    average = (

        total
        /
        len(daily_totals)

        if daily_totals

        else 0

    )

    maximum = (

        max(daily_totals)

        if daily_totals

        else 0

    )

    minimum = (

        min(daily_totals)

        if daily_totals

        else 0

    )

    latest = (
        series[-1]
        if series
        else None
    )

    previous = (
        series[-2]
        if len(series) >= 2
        else None
    )

    vs_previous = None

    if (

        latest is not None
        and
        previous is not None
        and
        previous["total"] != 0

    ):

        vs_previous = round(

            (

                latest["total"]

                -

                previous["total"]

            )

            /

            previous["total"]

            *

            100,

            1

        )

    if vs_previous is None:
        status = "Belum dapat dibandingkan"

    elif vs_previous > 20:
        status = "Meningkat"

    elif vs_previous < -20:
        status = "Menurun"

    else:
        status = "Stabil"

    # --------------------------------------------------------
    # Latest date with data
    # --------------------------------------------------------

    active_days = [

        item
        for item in series
        if item["total"] > 0
    ]

    latest_data_date = (

        active_days[-1]["date"]

        if active_days

        else None

    )

    latest_data_count = (

        active_days[-1]["total"]

        if active_days

        else 0

    )

    return {

        "pt_name":
            pt_name,

        "period_start":
            query_start.strftime(
                "%Y-%m-%d"
            ),

        "period_end":
            query_end.strftime(
                "%Y-%m-%d"
            ),

        "snapshot_date":
            query_end.strftime(
                "%Y-%m-%d"
            ),

        "latest_data_date":
            latest_data_date,

        "latest_data_count":
            latest_data_count,

        "total":
            total,

        "total_30_days":
            total,

        "total_within_5km":
            total,

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

        "average_30_days":
            round(
                average,
                1
            ),

        "max_30_days":
            maximum,

        "min_30_days":
            minimum,

        "latest_day_total":
            (
                latest["total"]
                if latest
                else 0
            ),

        "vs_previous_pct":
            vs_previous,

        "status":
            status,

        "monitoring_radius_km":
            5,

        "source":
            "SIPONGI KEMENHUT",

        "timezone":
            "Asia/Jakarta",

        "last_updated_wib":
            datetime.now(
                WIB
            ).isoformat(
                timespec="seconds"
            ),

        "last_updated_utc":
            datetime.now(
                timezone.utc
            ).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),

        "note":
            (
                "Hotspot merupakan indikasi "
                "anomali suhu/titik panas dan "
                "tidak otomatis berarti "
                "kebakaran aktual. "
                "Verifikasi lapangan tetap "
                "diperlukan."
            )

    }


# ============================================================
# BUILD 7-DAY TREND
# ============================================================

def build_7day_trend(
    trend_30: dict[str, Any]
) -> dict[str, Any]:

    series = trend_30.get(
        "series",
        []
    )

    series_7 = series[
        -7:
    ]

    return {

        "period_start":
            (
                series_7[0]["date"]
                if series_7
                else None
            ),

        "period_end":
            (
                series_7[-1]["date"]
                if series_7
                else None
            ),

        "dates":
            [
                item["date"]
                for item in series_7
            ],

        "series":
            series_7,

        "last_updated_wib":
            datetime.now(
                WIB
            ).isoformat(
                timespec="seconds"
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
        "       SIPONGI PT SLS HOTSPOT MONITOR"
    )

    print(
        "       DAILY HISTORY + 30 DAY WINDOW"
    )

    print(
        "       HGU + RADIUS 5 KM"
    )

    print(
        "=============================================="
    )

    print("")

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

    days_back = int(
        config.get(
            "days_back",
            30
        )
    )

    distance_max_km = float(
        config.get(
            "distance_max_km",
            5
        )
    )

    start_date, end_date = monitoring_dates(
        days_back
    )

    # --------------------------------------------------------
    # CURRENT WIB
    # --------------------------------------------------------

    now_wib = datetime.now(
        WIB
    )

    print(
        "Current WIB:",
        now_wib.isoformat(
            timespec="seconds"
        )
    )

    print(
        "Monitoring from:",
        start_date
    )

    print(
        "Monitoring to:",
        end_date
    )

    print(
        "Window:",
        days_back,
        "hari"
    )

    print(
        "Radius:",
        distance_max_km,
        "km"
    )

    # --------------------------------------------------------
    # BOUNDARY
    # --------------------------------------------------------

    boundary = load_boundary()

    # --------------------------------------------------------
    # FETCH
    # --------------------------------------------------------

    items = fetch_sipongi(

        config,

        start_date,

        end_date

    )

    # --------------------------------------------------------
    # SPATIAL
    # --------------------------------------------------------

    features = process_spatial(

        items,

        boundary,

        distance_max_km

    )

    # --------------------------------------------------------
    # DEDUP
    # --------------------------------------------------------

    features = deduplicate(
        features
    )

    # --------------------------------------------------------
    # SAVE DAILY HISTORY
    # --------------------------------------------------------

    write_daily_history(

        features,

        start_date,

        end_date

    )

    # --------------------------------------------------------
    # TREND
    # --------------------------------------------------------

    trend_30 = build_trend_from_features(

        features,

        start_date,

        end_date

    )

    trend_7 = build_7day_trend(
        trend_30
    )

    # --------------------------------------------------------
    # SUMMARY
    # --------------------------------------------------------

    summary = build_summary(

        features,

        trend_30,

        pt_name,

        start_date,

        end_date

    )

    # --------------------------------------------------------
    # CURRENT GEOJSON
    # --------------------------------------------------------

    current_geojson = {

        "type":
            "FeatureCollection",

        "name":
            "PT_SLS_SIPONGI_30_DAYS_5_KM",

        "features":
            features

    }

    # --------------------------------------------------------
    # SITE DATA
    # --------------------------------------------------------

    write_json(

        SITE_DATA /
        "current.geojson",

        current_geojson

    )

    write_json(

        SITE_DATA /
        "trend_30days.json",

        trend_30

    )

    write_json(

        SITE_DATA /
        "trend_7days.json",

        trend_7

    )

    write_json(

        SITE_DATA /
        "summary.json",

        summary

    )

    write_json(

        SITE_DATA /
        "pt_boundary.geojson",

        read_json(
            BOUNDARY_PATH
        )

    )

    # --------------------------------------------------------
    # AVAILABILITY
    # --------------------------------------------------------

    availability = {

        "pt_name":
            pt_name,

        "timezone":
            "Asia/Jakarta",

        "current_wib":
            now_wib.isoformat(
                timespec="seconds"
            ),

        "query_start":
            start_date.strftime(
                "%Y-%m-%d"
            ),

        "query_end":
            end_date.strftime(
                "%Y-%m-%d"
            ),

        "latest_data_date":
            summary[
                "latest_data_date"
            ],

        "latest_data_count":
            summary[
                "latest_data_count"
            ],

        "total_features_30days":
            len(features),

        "last_updated_wib":
            datetime.now(
                WIB
            ).isoformat(
                timespec="seconds"
            ),

        "source":
            "SIPONGI KEMENHUT"

    }

    write_json(

        SITE_DATA /
        "availability.json",

        availability

    )

    # --------------------------------------------------------
    # STATE
    # --------------------------------------------------------

    state = {

        "period_start":
            start_date.strftime(
                "%Y-%m-%d"
            ),

        "period_end":
            end_date.strftime(
                "%Y-%m-%d"
            ),

        "updated_wib":
            datetime.now(
                WIB
            ).isoformat(
                timespec="seconds"
            ),

        "features":
            features,

        "trend":
            trend_30,

        "summary":
            summary

    }

    write_json(

        STATE_PATH,

        state

    )

    # --------------------------------------------------------
    # FINAL
    # --------------------------------------------------------

    print("")
    print(
        "=============================================="
    )

    print(
        "              FINAL RESULT"
    )

    print(
        "=============================================="
    )

    print(
        "PT:",
        pt_name
    )

    print(
        "Periode:",
        start_date,
        "sampai",
        end_date
    )

    print(
        "Timezone:",
        "WIB"
    )

    print(
        "Total response SiPongi:",
        len(items)
    )

    print(
        "Total hotspot <= 5 km:",
        len(features)
    )

    print(
        "Inside HGU:",
        summary[
            "inside_hgu"
        ]
    )

    print(
        "0–1 km:",
        summary[
            "zone_0_1"
        ]
    )

    print(
        "1–3 km:",
        summary[
            "zone_1_3"
        ]
    )

    print(
        "3–5 km:",
        summary[
            "zone_3_5"
        ]
    )

    print(
        "High:",
        summary[
            "high"
        ]
    )

    print(
        "Medium:",
        summary[
            "medium"
        ]
    )

    print(
        "Low:",
        summary[
            "low"
        ]
    )

    print(
        "Average per day:",
        summary[
            "average_30_days"
        ]
    )

    print(
        "Maximum per day:",
        summary[
            "max_30_days"
        ]
    )

    print(
        "Minimum per day:",
        summary[
            "min_30_days"
        ]
    )

    print(
        "Latest date with data:",
        summary[
            "latest_data_date"
        ]
    )

    print(
        "Latest date hotspot:",
        summary[
            "latest_data_count"
        ]
    )

    print(
        "Status:",
        summary[
            "status"
        ]
    )

    print(
        "=============================================="
    )

    print("")


if __name__ == "__main__":
    main()
