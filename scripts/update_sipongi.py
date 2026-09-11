from __future__ import annotations

import json
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
# SiPongi MULTI-PT HOTSPOT MONITOR
# ============================================================
#
# Fungsi:
# 1. Membaca semua PT dari data/companies.json
# 2. Mengambil data SiPongi sesuai provinsi
# 3. Menggunakan tanggal WIB (Asia/Jakarta)
# 4. Analisis spasial per boundary HGU
# 5. Filter hotspot di dalam HGU atau <= 5 km
# 6. Menyimpan histori harian permanen per PT
# 7. Membuat current/trend/summary/availability per PT
# 8. Mempertahankan output legacy PT SLS di site/data/
#
# ============================================================


WIB = ZoneInfo("Asia/Jakarta")

ROOT = Path(__file__).resolve().parents[1]

COMPANIES_PATH = ROOT / "data" / "companies.json"

SITE_DATA_ROOT = ROOT / "site" / "data"

HISTORY_ROOT = ROOT / "data" / "history"


# ============================================================
# BASIC JSON
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
# GENERIC HELPERS
# ============================================================

def first_value(
    data: dict[str, Any],
    *keys: str
) -> Any:

    for key in keys:
        value = data.get(key)

        if value is not None and value != "":
            return value

    return None


def to_float(value: Any) -> float | None:

    try:

        if value is None:
            return None

        return float(value)

    except (TypeError, ValueError):
        return None


# ============================================================
# DATE
# ============================================================

def get_monitoring_window(
    days_back: int
) -> tuple[date, date]:

    now_wib = datetime.now(WIB)

    end_date = now_wib.date()

    start_date = (
        end_date
        -
        timedelta(days=days_back - 1)
    )

    return start_date, end_date


def parse_hotspot_date(
    value: Any
) -> str | None:

    if not value:
        return None

    text = str(value).strip()

    # --------------------------------------------------------
    # ISO
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
    # YYYY-MM-DD anywhere
    # --------------------------------------------------------

    import re

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
            return None

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
            return None

    return None


# ============================================================
# COMPANIES
# ============================================================

def load_companies() -> list[dict[str, Any]]:

    if not COMPANIES_PATH.exists():

        raise RuntimeError(
            f"companies.json tidak ditemukan: {COMPANIES_PATH}"
        )

    companies = read_json(
        COMPANIES_PATH
    )

    if not isinstance(companies, list):

        raise RuntimeError(
            "data/companies.json harus berupa array/list."
        )

    enabled = []

    for company in companies:

        if not isinstance(company, dict):
            continue

        if company.get("enabled", True):

            required = [
                "id",
                "name",
                "province_code",
                "boundary"
            ]

            missing = [
                field
                for field in required
                if not company.get(field)
            ]

            if missing:

                raise RuntimeError(
                    f"Konfigurasi PT {company} kurang: {missing}"
                )

            enabled.append(
                company
            )

    if not enabled:

        raise RuntimeError(
            "Tidak ada PT aktif di companies.json."
        )

    return enabled


# ============================================================
# BOUNDARY
# ============================================================

def resolve_path(
    raw_path: str | Path
) -> Path:

    path = Path(
        str(raw_path)
    )

    if path.is_absolute():
        return path

    return ROOT / path


def load_boundary(
    raw_path: str
):

    path = resolve_path(
        raw_path
    )

    if not path.exists():

        raise RuntimeError(
            f"Boundary tidak ditemukan: {path}"
        )

    obj = read_json(
        path
    )

    features = obj.get(
        "features"
    ) or []

    if not features:

        raise RuntimeError(
            f"Boundary kosong: {path}"
        )

    geometries = []

    for feature in features:

        geometry = feature.get(
            "geometry"
        )

        if geometry:

            geometries.append(
                shape(
                    geometry
                )
            )

    if not geometries:

        raise RuntimeError(
            f"Tidak ada geometry pada: {path}"
        )

    boundary = unary_union(
        geometries
    )

    if boundary.is_empty:

        raise RuntimeError(
            f"Boundary kosong setelah union: {path}"
        )

    if not boundary.is_valid:

        boundary = boundary.buffer(0)

    return boundary


# ============================================================
# AUTOMATIC METRIC CRS
# ============================================================

def metric_transformer_for(
    boundary
):

    """
    Menentukan UTM zone otomatis berdasarkan centroid HGU.

    Untuk Indonesia:
    - belahan utara -> EPSG 326xx
    - belahan selatan -> EPSG 327xx
    """

    centroid = boundary.centroid

    lon = centroid.x
    lat = centroid.y

    zone = int(
        (lon + 180) / 6
    ) + 1

    zone = max(
        1,
        min(
            60,
            zone
        )
    )

    epsg = (

        32600 + zone

        if lat >= 0

        else

        32700 + zone

    )

    source_crs = "EPSG:4326"

    target_crs = f"EPSG:{epsg}"

    forward = Transformer.from_crs(
        source_crs,
        target_crs,
        always_xy=True
    ).transform

    return target_crs, transform(
        forward,
        boundary
    ), forward


# ============================================================
# SIPONGI RESPONSE
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
                payload.get("features")
                or []
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
                    if isinstance(
                        item,
                        dict
                    )
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
        "Struktur response SiPongi tidak dikenali."
    )


# ============================================================
# NORMALIZE
# ============================================================

def normalize_item(
    item: dict[str, Any]
) -> dict[str, Any] | None:

    lat = None
    lon = None

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
            geometry.get("type") == "Point"
            and
            isinstance(
                coordinates,
                list
            )
            and
            len(coordinates) >= 2
        ):

            lon = to_float(
                coordinates[0]
            )

            lat = to_float(
                coordinates[1]
            )

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
                "lon",
                "long",
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

    result = {

        "lat":
            lat,

        "lon":
            lon,

        "confidence":
            confidence,

        "confidence_value":
            confidence_value,

        "sumber":
            first_value(
                item,
                "sumber",
                "source",
                "satellite"
            ),

        "date_hotspot":
            date_hotspot,

        "date_hotspot_ori":
            date_hotspot_ori,

        "provinsi":
            first_value(
                item,
                "provinsi",
                "province"
            ),

        "kabkota":
            first_value(
                item,
                "kabkota",
                "kabupaten",
                "kota"
            ),

        "desa":
            first_value(
                item,
                "desa",
                "village"
            )

    }

    # Keep extra scalar fields.
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

    # Backend canonical date.
    result[
        "hotspot_date"
    ] = (

        parse_hotspot_date(
            date_hotspot_ori
        )

        or

        parse_hotspot_date(
            date_hotspot
        )

    )

    return result


# ============================================================
# FETCH BY PROVINCE
# ============================================================

def fetch_sipongi_province(
    config: dict[str, Any],
    province_code: str,
    start_date: date,
    end_date: date
) -> list[dict[str, Any]]:

    endpoint = config[
        "sipongi_endpoint"
    ]

    late_mode = config.get(
        "late_mode",
        "custom"
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

    params.append(
        (
            "provinsi",
            str(
                province_code
            )
        )
    )

    params.append(
        (
            "kabkota",
            ""
        )
    )

    headers = {

        "User-Agent":
            "SiPongi-Multi-PT-Hotspot-Monitor/1.0",

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
        "============================================================"
    )

    print(
        "SIPONGI REQUEST"
    )

    print(
        "Province:",
        province_code
    )

    print(
        "From:",
        start_date
    )

    print(
        "To:",
        end_date
    )

    print(
        "URL:",
        prepared.url
    )

    print(
        "============================================================"
    )

    response = requests.get(
        endpoint,
        params=params,
        headers=headers,
        timeout=180
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

    normalized = []

    for item in raw_items:

        clean = normalize_item(
            item
        )

        if clean:
            normalized.append(
                clean
            )

    print(
        "Raw records:",
        len(raw_items)
    )

    print(
        "Normalized records:",
        len(normalized)
    )

    return normalized


# ============================================================
# GEOJSON FEATURE
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
# DEDUPLICATION
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
            float(
                coordinates[1]
            ),
            5
        ),

        round(
            float(
                coordinates[0]
            ),
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

    unique: dict[
        tuple,
        dict[str, Any]
    ] = {}

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
# SPATIAL PROCESS PER PT
# ============================================================

def process_company_spatial(
    items: list[dict[str, Any]],
    boundary,
    max_distance_km: float
) -> list[dict[str, Any]]:

    target_crs, boundary_metric, to_metric = (
        metric_transformer_for(
            boundary
        )
    )

    print(
        "Metric CRS:",
        target_crs
    )

    bbox = boundary.bounds

    bbox_count = 0
    inside_count = 0

    features = []

    for item in items:

        lat = item["lat"]
        lon = item["lon"]

        point = Point(
            lon,
            lat
        )

        minx, miny, maxx, maxy = bbox

        if (
            minx <= lon <= maxx
            and
            miny <= lat <= maxy
        ):

            bbox_count += 1

        inside = boundary.covers(
            point
        )

        if inside:

            inside_count += 1

        point_metric = transform(
            to_metric,
            point
        )

        distance_km = (

            boundary_metric.distance(
                point_metric
            )

            /

            1000.0

        )

        if (
            not inside
            and
            distance_km > max_distance_km
        ):
            continue

        if inside:

            zone = "Inside HGU"

        elif distance_km <= 1:

            zone = "0–1 km"

        elif distance_km <= 3:

            zone = "1–3 km"

        else:

            zone = "3–5 km"

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

            if inside

            else

            "OUTSIDE HGU"

        )

        features.append(
            feature
        )

    features = deduplicate(
        features
    )

    zone_counter = Counter(

        feature[
            "properties"
        ].get(
            "zone"
        )

        for feature in features

    )

    print("")
    print(
        "SPATIAL RESULT"
    )

    print(
        "Total province response:",
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
        len(features)
    )

    print(
        "Inside HGU:",
        zone_counter.get(
            "Inside HGU",
            0
        )
    )

    print(
        "0–1 km:",
        zone_counter.get(
            "0–1 km",
            0
        )
    )

    print(
        "1–3 km:",
        zone_counter.get(
            "1–3 km",
            0
        )
    )

    print(
        "3–5 km:",
        zone_counter.get(
            "3–5 km",
            0
        )
    )

    return features


# ============================================================
# GROUP BY DATE
# ============================================================

def build_daily_groups(
    features: list[dict[str, Any]]
) -> dict[
    str,
    list[dict[str, Any]]
]:

    groups = defaultdict(list)

    for feature in features:

        properties = feature.get(
            "properties",
            {}
        )

        date_key = properties.get(
            "hotspot_date"
        )

        if not date_key:
            continue

        groups[
            date_key
        ].append(
            feature
        )

    return groups


# ============================================================
# DAILY HISTORY
# ============================================================

def save_daily_history(
    company: dict[str, Any],
    features: list[dict[str, Any]],
    start_date: date,
    end_date: date
) -> None:

    company_id = company[
        "id"
    ]

    history_dir = ROOT / company.get(
        "history_dir",
        f"data/history/{company_id}"
    )

    history_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    groups = build_daily_groups(
        features
    )

    generated_wib = datetime.now(
        WIB
    ).isoformat(
        timespec="seconds"
    )

    current = start_date

    while current <= end_date:

        date_key = current.strftime(
            "%Y-%m-%d"
        )

        daily_features = groups.get(
            date_key,
            []
        )

        obj = {

            "pt_id":
                company_id,

            "pt_name":
                company["name"],

            "date":
                date_key,

            "query_period": {

                "from":
                    start_date.strftime(
                        "%Y-%m-%d"
                    ),

                "to":
                    end_date.strftime(
                        "%Y-%m-%d"
                    )

            },

            "generated_wib":
                generated_wib,

            "hotspot_count":
                len(
                    daily_features
                ),

            "features":
                daily_features

        }

        write_json(

            history_dir /

            f"{date_key}.json",

            obj

        )

        current += timedelta(
            days=1
        )


# ============================================================
# TREND
# ============================================================

def build_trend(
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

        p = feature.get(
            "properties",
            {}
        )

        date_key = p.get(
            "hotspot_date"
        )

        if not date_key:

            records_without_date += 1
            continue

        daily[
            date_key
        ][
            "total"
        ] += 1

        confidence = str(
            p.get(
                "confidence",
                "unknown"
            )
        ).lower()

        if confidence in {
            "high",
            "medium",
            "low"
        }:

            daily[
                date_key
            ][
                confidence
            ] += 1

        else:

            daily[
                date_key
            ][
                "unknown"
            ] += 1

        zone = p.get(
            "zone"
        )

        if zone == "Inside HGU":

            daily[
                date_key
            ][
                "inside_hgu"
            ] += 1

        elif zone == "0–1 km":

            daily[
                date_key
            ][
                "zone_0_1"
            ] += 1

        elif zone == "1–3 km":

            daily[
                date_key
            ][
                "zone_1_3"
            ] += 1

        elif zone == "3–5 km":

            daily[
                date_key
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
                row["date"]
                for row in series
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
# 7 DAY TREND
# ============================================================

def build_7day_trend(
    trend: dict[str, Any]
) -> dict[str, Any]:

    series = trend.get(
        "series",
        []
    )

    last7 = series[
        -7:
    ]

    return {

        "period_start":
            (
                last7[0]["date"]
                if last7
                else None
            ),

        "period_end":
            (
                last7[-1]["date"]
                if last7
                else None
            ),

        "dates":
            [
                row["date"]
                for row in last7
            ],

        "series":
            last7,

        "last_updated_wib":
            datetime.now(
                WIB
            ).isoformat(
                timespec="seconds"
            )

    }


# ============================================================
# SUMMARY
# ============================================================

def build_summary(
    company: dict[str, Any],
    features: list[dict[str, Any]],
    trend: dict[str, Any],
    start_date: date,
    end_date: date,
    source_record_count: int
) -> dict[str, Any]:

    series = trend.get(
        "series",
        []
    )

    confidence = Counter(
        feature[
            "properties"
        ].get(
            "confidence",
            "unknown"
        )

        for feature in features
    )

    zones = Counter(
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
            row.get(
                "total",
                0
            )
        )

        for row in series

    ]

    total = sum(
        daily_totals
    )

    average = (

        total /

        len(
            daily_totals
        )

        if daily_totals

        else 0

    )

    maximum = (

        max(
            daily_totals
        )

        if daily_totals

        else 0

    )

    minimum = (

        min(
            daily_totals
        )

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

        latest

        and

        previous

        and

        Number_or_zero(
            previous.get(
                "total"
            )
        ) != 0

    ):

        vs_previous = round(

            (

                Number_or_zero(
                    latest.get(
                        "total"
                    )
                )

                -

                Number_or_zero(
                    previous.get(
                        "total"
                    )
                )

            )

            /

            Number_or_zero(
                previous.get(
                    "total"
                )
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

        status = "Meningkat"

    elif vs_previous < -20:

        status = "Menurun"

    else:

        status = "Stabil"

    active_days = [

        row

        for row in series

        if Number_or_zero(
            row.get(
                "total"
            )
        ) > 0

    ]

    latest_data_date = (

        active_days[-1]["date"]

        if active_days

        else None

    )

    latest_data_count = (

        Number_or_zero(
            active_days[-1]["total"]
        )

        if active_days

        else 0

    )

    return {

        "pt_id":
            company["id"],

        "pt_name":
            company["name"],

        "province_code":
            str(
                company[
                    "province_code"
                ]
            ),

        "province_name":
            company.get(
                "province_name"
            ),

        "period_start":
            start_date.strftime(
                "%Y-%m-%d"
            ),

        "period_end":
            end_date.strftime(
                "%Y-%m-%d"
            ),

        "timezone":
            "Asia/Jakarta",

        "total_response_sipongi":
            source_record_count,

        "total":
            total,

        "total_within_5km":
            total,

        "inside_hgu":
            zones.get(
                "Inside HGU",
                0
            ),

        "zone_0_1":
            zones.get(
                "0–1 km",
                0
            ),

        "zone_1_3":
            zones.get(
                "1–3 km",
                0
            ),

        "zone_3_5":
            zones.get(
                "3–5 km",
                0
            ),

        "high":
            confidence.get(
                "high",
                0
            ),

        "medium":
            confidence.get(
                "medium",
                0
            ),

        "low":
            confidence.get(
                "low",
                0
            ),

        "unknown":
            confidence.get(
                "unknown",
                0
            ),

        "average_per_day":
            round(
                average,
                1
            ),

        "maximum_per_day":
            maximum,

        "minimum_per_day":
            minimum,

        "latest_day_total":
            (
                Number_or_zero(
                    latest.get(
                        "total"
                    )
                )
                if latest
                else 0
            ),

        "latest_data_date":
            latest_data_date,

        "latest_data_count":
            latest_data_count,

        "vs_previous_pct":
            vs_previous,

        "status":
            status,

        "radius_km":
            5,

        "source":
            "SIPONGI KEMENHUT",

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


def Number_or_zero(
    value: Any
) -> int:

    try:

        return int(
            value or 0
        )

    except (
        TypeError,
        ValueError
    ):

        return 0


# ============================================================
# COMPANY OUTPUT
# ============================================================

def write_company_output(
    company: dict[str, Any],
    features: list[dict[str, Any]],
    trend: dict[str, Any],
    trend7: dict[str, Any],
    summary: dict[str, Any],
    boundary
) -> None:

    company_id = company[
        "id"
    ]

    output_dir = ROOT / company.get(
        "output_dir",
        f"site/data/{company_id}"
    )

    output_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    # --------------------------------------------------------
    # Current hotspot GeoJSON
    # --------------------------------------------------------

    current = {

        "type":
            "FeatureCollection",

        "name":
            f"{company_id}_sipongi_hotspot_30days",

        "properties": {

            "pt_id":
                company_id,

            "pt_name":
                company["name"],

            "period_start":
                summary["period_start"],

            "period_end":
                summary["period_end"],

            "radius_km":
                5

        },

        "features":
            features

    }

    write_json(
        output_dir /
        "current.geojson",
        current
    )

    # --------------------------------------------------------
    # Boundary copy
    # --------------------------------------------------------

    boundary_source = resolve_path(
        company["boundary"]
    )

    boundary_json = read_json(
        boundary_source
    )

    write_json(
        output_dir /
        "pt_boundary.geojson",
        boundary_json
    )

    # --------------------------------------------------------
    # Trend / Summary
    # --------------------------------------------------------

    write_json(
        output_dir /
        "trend_30days.json",
        trend
    )

    write_json(
        output_dir /
        "trend_7days.json",
        trend7
    )

    write_json(
        output_dir /
        "summary.json",
        summary
    )

    # --------------------------------------------------------
    # Availability
    # --------------------------------------------------------

    availability = {

        "pt_id":
            company_id,

        "pt_name":
            company["name"],

        "province_code":
            str(
                company[
                    "province_code"
                ]
            ),

        "latest_data_date":
            summary[
                "latest_data_date"
            ],

        "latest_data_count":
            summary[
                "latest_data_count"
            ],

        "query_start":
            summary[
                "period_start"
            ],

        "query_end":
            summary[
                "period_end"
            ],

        "timezone":
            "Asia/Jakarta",

        "source":
            "SIPONGI KEMENHUT",

        "last_updated_wib":
            summary[
                "last_updated_wib"
            ]

    }

    write_json(
        output_dir /
        "availability.json",
        availability
    )


# ============================================================
# LEGACY SLS OUTPUT
# ============================================================

def write_legacy_sls_output(
    companies: list[dict[str, Any]],
    output_objects: dict[
        str,
        dict[str, Any]
    ]
) -> None:

    """
    Mempertahankan file lama agar dashboard
    PT SLS yang sekarang tidak langsung rusak.

    Setelah frontend multi-PT selesai,
    file legacy ini bisa dihapus.
    """

    sls = None

    for company in companies:

        if company[
            "id"
        ] == "pt_sls":

            sls = company
            break

    if sls is None:
        return

    obj = output_objects.get(
        "pt_sls"
    )

    if not obj:
        return

    write_json(
        SITE_DATA_ROOT /
        "current.geojson",
        obj["current"]
    )

    write_json(
        SITE_DATA_ROOT /
        "trend_30days.json",
        obj["trend"]
    )

    write_json(
        SITE_DATA_ROOT /
        "trend_7days.json",
        obj["trend7"]
    )

    write_json(
        SITE_DATA_ROOT /
        "summary.json",
        obj["summary"]
    )

    write_json(
        SITE_DATA_ROOT /
        "pt_boundary.geojson",
        obj["boundary"]
    )


# ============================================================
# MULTI-PT INDEX FOR FRONTEND
# ============================================================

def write_site_companies(
    companies: list[dict[str, Any]]
) -> None:

    public = []

    for company in companies:

        public.append({

            "id":
                company["id"],

            "name":
                company["name"],

            "province_code":
                str(
                    company[
                        "province_code"
                    ]
                ),

            "province_name":
                company.get(
                    "province_name"
                ),

            "data_path":
                f"data/{company['id']}/",

            "enabled":
                company.get(
                    "enabled",
                    True
                )

        })

    write_json(

        SITE_DATA_ROOT /
        "companies.json",

        public

    )


# ============================================================
# ALL PT SUMMARY
# ============================================================

def write_multi_summary(
    summaries: list[dict[str, Any]]
) -> None:

    write_json(

        SITE_DATA_ROOT /
        "summary_all.json",

        {

            "last_updated_wib":
                datetime.now(
                    WIB
                ).isoformat(
                    timespec="seconds"
                ),

            "companies":
                summaries

        }

    )


# ============================================================
# MAIN
# ============================================================

def main():

    print("")
    print(
        "============================================================"
    )

    print(
        "       SIPONGI MULTI-PT HOTSPOT MONITOR"
    )

    print(
        "       DAILY HISTORY + 30 DAY WINDOW + 5 KM"
    )

    print(
        "============================================================"
    )

    config = read_json(
        ROOT / "config" / "config.json"
    )

    companies = load_companies()

    days_back = int(
        config.get(
            "days_back",
            30
        )
    )

    max_distance_km = float(
        config.get(
            "distance_max_km",
            5
        )
    )

    start_date, end_date = get_monitoring_window(
        days_back
    )

    print(
        "Current WIB:",
        datetime.now(
            WIB
        ).isoformat(
            timespec="seconds"
        )
    )

    print(
        "Window:",
        start_date,
        "to",
        end_date
    )

    print(
        "Days:",
        days_back
    )

    print(
        "Radius:",
        max_distance_km,
        "km"
    )

    print("")
    print(
        "COMPANIES:"
    )

    for company in companies:

        print(
            " -",
            company["id"],
            "|",
            company["name"],
            "| Province",
            company["province_code"]
        )

    # --------------------------------------------------------
    # Group PT by province so one API response
    # can be reused by several PT in the same province.
    # --------------------------------------------------------

    by_province: dict[
        str,
        list[dict[str, Any]]
    ] = defaultdict(list)

    for company in companies:

        by_province[
            str(
                company[
                    "province_code"
                ]
            )
        ].append(
            company
        )

    province_data: dict[
        str,
        list[dict[str, Any]]
    ] = {}

    # --------------------------------------------------------
    # FETCH SIPONGI
    # --------------------------------------------------------

    for province_code in sorted(
        by_province.keys()
    ):

        province_data[
            province_code
        ] = fetch_sipongi_province(

            config,

            province_code,

            start_date,

            end_date

        )

    output_objects = {}

    all_summaries = []

    # --------------------------------------------------------
    # PROCESS EACH PT
    # --------------------------------------------------------

    for company in companies:

        print("")
        print(
            "============================================================"
        )

        print(
            "PROCESSING:",
            company["name"]
        )

        print(
            "ID:",
            company["id"]
        )

        print(
            "Province:",
            company["province_code"]
        )

        print(
            "============================================================"
        )

        boundary = load_boundary(
            company["boundary"]
        )

        items = province_data[
            str(
                company[
                    "province_code"
                ]
            )
        ]

        features = process_company_spatial(

            items,

            boundary,

            max_distance_km

        )

        trend = build_trend(

            features,

            start_date,

            end_date

        )

        trend7 = build_7day_trend(
            trend
        )

        summary = build_summary(

            company,

            features,

            trend,

            start_date,

            end_date,

            len(items)

        )

        save_daily_history(

            company,

            features,

            start_date,

            end_date

        )

        write_company_output(

            company,

            features,

            trend,

            trend7,

            summary,

            boundary

        )

        # Objects for legacy PT SLS.
        output_objects[
            company["id"]
        ] = {

            "current": {

                "type":
                    "FeatureCollection",

                "name":
                    f"{company['id']}_sipongi_hotspot_30days",

                "features":
                    features

            },

            "trend":
                trend,

            "trend7":
                trend7,

            "summary":
                summary,

            "boundary":
                read_json(
                    resolve_path(
                        company[
                            "boundary"
                        ]
                    )
                )

        }

        all_summaries.append(
            summary
        )

        print("")
        print(
            "FINAL:",
            company["name"]
        )

        print(
            "Total response:",
            summary[
                "total_response_sipongi"
            ]
        )

        print(
            "Total <= 5 km:",
            summary[
                "total_within_5km"
            ]
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
            "Average/day:",
            summary[
                "average_per_day"
            ]
        )

        print(
            "Latest date with data:",
            summary[
                "latest_data_date"
            ]
        )

        print(
            "Latest day hotspot:",
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

    # --------------------------------------------------------
    # Public multi-PT index
    # --------------------------------------------------------

    write_site_companies(
        companies
    )

    write_multi_summary(
        all_summaries
    )

    # --------------------------------------------------------
    # Preserve old PT SLS paths
    # --------------------------------------------------------

    write_legacy_sls_output(

        companies,

        output_objects

    )

    print("")
    print(
        "============================================================"
    )

    print(
        "MULTI-PT COMPLETE"
    )

    print(
        "PT processed:",
        len(companies)
    )

    for summary in all_summaries:

        print(
            " -",
            summary["pt_name"],
            ":",
            summary["total_within_5km"],
            "hotspot <= 5 km"
        )

    print(
        "============================================================"
    )


if __name__ == "__main__":
    main()
