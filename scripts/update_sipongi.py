from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from pyproj import Transformer
from shapely.geometry import Point, shape
from shapely.ops import transform, unary_union


# ============================================================
# PATH
# ============================================================

ROOT = Path(__file__).resolve().parents[1]

CONFIG_PATH = ROOT / "config" / "config.json"
BOUNDARY_PATH = ROOT / "data" / "pt_boundary.geojson"

SITE_DATA = ROOT / "site" / "data"

STATE_PATH = ROOT / "data" / "rolling_30days.json"


# ============================================================
# JSON
# ============================================================

def read_json(path: Path) -> Any:

    with path.open(
        "r",
        encoding="utf-8"
    ) as f:

        return json.load(f)


def write_json(
    path: Path,
    obj: Any
) -> None:

    path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    path.write_text(

        json.dumps(
            obj,
            indent=2,
            ensure_ascii=False
        ),

        encoding="utf-8"

    )


# ============================================================
# HELPERS
# ============================================================

def num(
    value: Any
) -> float | None:

    try:

        if value is None:
            return None

        if isinstance(
            value,
            str
        ):

            value = value.strip()

        return float(value)

    except (
        TypeError,
        ValueError
    ):

        return None


def first_value(
    data: dict[str, Any],
    *keys: str
) -> Any:

    for key in keys:

        if (
            key in data
            and
            data[key] not in (
                None,
                ""
            )
        ):

            return data[key]

    return None


# ============================================================
# LOAD BOUNDARY
# ============================================================

def load_boundary():

    obj = read_json(
        BOUNDARY_PATH
    )

    features = (
        obj.get(
            "features"
        )
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
            "WARNING: Boundary PT invalid."
        )

        boundary = boundary.buffer(
            0
        )

    if boundary.is_empty:

        raise RuntimeError(
            "Boundary PT tetap kosong setelah repair."
        )

    return boundary


# ============================================================
# METRIC CRS
# ============================================================

def prepare_metric_boundary(
    boundary
):

    """
    PT SLS berada sekitar 115 BT dan 2-3 LS.

    UTM Zone 50S:
    EPSG:32750

    Digunakan agar jarak dihitung dalam meter.
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

def extract_items(
    payload: Any
) -> list[dict[str, Any]]:

    if isinstance(
        payload,
        list
    ):

        return [

            item

            for item in payload

            if isinstance(
                item,
                dict
            )

        ]


    if isinstance(
        payload,
        dict
    ):

        # GeoJSON
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


        # Common wrappers
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
# NORMALIZE HOTSPOT
# ============================================================

def normalize_item(
    item: dict[str, Any]
) -> dict[str, Any] | None:

    lat = None
    lon = None


    # --------------------------------------------------------
    # GeoJSON Point
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

            geometry.get(
                "type"
            ) == "Point"

            and

            isinstance(
                coordinates,
                list
            )

            and

            len(coordinates) >= 2

        ):

            lon = num(
                coordinates[0]
            )

            lat = num(
                coordinates[1]
            )


    # --------------------------------------------------------
    # Normal coordinates
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
    # Confidence
    # --------------------------------------------------------

    confidence_value = num(

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


    if confidence_level not in {

        "low",

        "medium",

        "high"

    }:

        if confidence_value is not None:

            if confidence_value < 30:

                confidence_level = "low"

            elif confidence_value < 80:

                confidence_level = "medium"

            else:

                confidence_level = "high"

        else:

            confidence_level = "unknown"


    # --------------------------------------------------------
    # Dates
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


    # --------------------------------------------------------
    # Other attributes
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

        "lat":
            lat,

        "lon":
            lon,

        "confidence":
            confidence_level,

        "confidence_value":
            confidence_value,

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


    # Extra scalar fields
    for key, value in item.items():

        if key.startswith(
            "_"
        ):

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
# SIPONGI REQUEST
# ============================================================

def fetch_sipongi(
    config: dict[str, Any]
) -> list[dict[str, Any]]:

    endpoint = config[
        "sipongi_endpoint"
    ]


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

            days=
                days_back - 1

        )

    )


    to_date = today


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
        "Endpoint:",
        endpoint
    )

    print(
        "Mode:",
        config.get(
            "mode"
        )
    )

    print(
        "From:",
        from_date
    )

    print(
        "To:",
        to_date
    )

    print(
        "Days:",
        days_back
    )

    print(
        "Satelit:",
        config.get(
            "satelit"
        )
    )

    print(
        "Confidence:",
        config.get(
            "confidence"
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


    headers = {

        "User-Agent":
            "SiPongi-PT-SLS-Hotspot-Monitor/3.0",

        "Accept":
            "application/json,text/plain,*/*",

        "Referer":
            "https://sipongi.gakkum.kehutanan.go.id/peta"

    }


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


    text = response.text.strip()


    content_type = (

        response.headers.get(
            "content-type"
        )
        or ""

    ).lower()


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


    return items


# ============================================================
# DISTANCE ZONE
# ============================================================

def classify_zone(

    distance_km: float,

    inside_hgu: bool,

    config: dict[str, Any]

) -> str:

    if inside_hgu:

        return "Inside HGU"


    for zone in config.get(
        "zones_km",
        []
    ):

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
# SPATIAL PROCESSING
# ============================================================

def process_spatial(

    items: list[dict[str, Any]],

    boundary,

    config: dict[str, Any]

) -> list[dict[str, Any]]:

    (
        boundary_metric,
        to_metric,
        _from_metric
    ) = prepare_metric_boundary(
        boundary
    )


    max_distance_km = float(

        config.get(

            "distance_max_km",

            5

        )

    )


    boundary_minx, boundary_miny, boundary_maxx, boundary_maxy = (

        boundary.bounds

    )


    bbox_count = 0

    inside_count = 0

    result = []


    for item in items:

        lat = item[
            "lat"
        ]

        lon = item[
            "lon"
        ]


        point_wgs84 = Point(

            lon,

            lat

        )


        # ----------------------------------------------------
        # BBOX diagnostic
        # ----------------------------------------------------

        if (

            boundary_minx <= lon <= boundary_maxx

            and

            boundary_miny <= lat <= boundary_maxy

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
        # Metric point
        # ----------------------------------------------------

        point_metric = transform(

            to_metric,

            point_wgs84

        )


        # ----------------------------------------------------
        # Distance to HGU
        # ----------------------------------------------------

        distance_m = boundary_metric.distance(

            point_metric

        )


        distance_km = (

            distance_m

            /

            1000.0

        )


        # ----------------------------------------------------
        # Filter 5 KM
        # ----------------------------------------------------

        if (

            not inside_hgu

            and

            distance_km > max_distance_km

        ):

            continue


        zone = classify_zone(

            distance_km,

            inside_hgu,

            config

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
        boundary_minx
    )

    print(
        "  MIN LAT:",
        boundary_miny
    )

    print(
        "  MAX LON:",
        boundary_maxx
    )

    print(
        "  MAX LAT:",
        boundary_maxy
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


    for zone_name in [

        "Inside HGU",

        "0–1 km",

        "1–3 km",

        "3–5 km"

    ]:

        print(

            f"  {zone_name}:",

            zone_counter.get(
                zone_name,
                0
            )

        )


    print("")
    print(
        "=============================================="
    )


    return result


# ============================================================
# HOTSPOT DATE
# ============================================================

def extract_hotspot_date(
    properties: dict[str, Any]
) -> str | None:

    raw = (

        properties.get(
            "date_hotspot_ori"
        )

        or

        properties.get(
            "date_hotspot"
        )

    )


    if not raw:

        return None


    text = str(
        raw
    ).strip()


    # --------------------------------------------------------
    # Try ISO formats
    # --------------------------------------------------------

    try:

        parsed = datetime.fromisoformat(

            text.replace(
                "Z",
                "+00:00"
            )

        )

        return parsed.strftime(
            "%Y-%m-%d"
        )

    except ValueError:

        pass


    # --------------------------------------------------------
    # SiPongi Indonesian date example:
    #
    # Kamis, 10 September 2026 01:15:00
    # --------------------------------------------------------

    months = {

        "januari": "01",
        "februari": "02",
        "maret": "03",
        "april": "04",
        "mei": "05",
        "juni": "06",
        "juli": "07",
        "agustus": "08",
        "september": "09",
        "oktober": "10",
        "november": "11",
        "desember": "12"

    }


    parts = text.replace(
        ",",
        " "
    ).split()


    for i, part in enumerate(
        parts
    ):

        month_number = months.get(
            part.lower()
        )


        if (

            month_number
            and
            i >= 1
            and
            i + 1 < len(parts)

        ):

            day = parts[
                i - 1
            ]

            year = parts[
                i + 1
            ]


            if (

                day.isdigit()

                and

                year.isdigit()

            ):

                return (

                    f"{year}-"
                    f"{month_number}-"
                    f"{int(day):02d}"

                )


    # --------------------------------------------------------
    # Simple YYYY-MM-DD regex
    # --------------------------------------------------------

    import re

    match = re.search(

        r"(\d{4})-(\d{2})-(\d{2})",

        text

    )


    if match:

        return match.group(
            0
        )


    return None


# ============================================================
# BUILD DAILY TREND
# ============================================================

def build_daily_trend(

    features: list[dict[str, Any]],

    start_date,

    end_date

) -> dict[str, Any]:

    daily = defaultdict(

        lambda: {

            "total": 0,

            "high": 0,

            "medium": 0,

            "low": 0,

            "inside_hgu": 0,

            "zone_0_1": 0,

            "zone_1_3": 0,

            "zone_3_5": 0

        }

    )


    for feature in features:

        properties = (
            feature.get(
                "properties"
            )
            or {}
        )


        date = extract_hotspot_date(
            properties
        )


        if not date:

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


        daily[date][
            "total"
        ] += 1


        if confidence in {
            "high",
            "medium",
            "low"
        }:

            daily[date][
                confidence
            ] += 1


        if zone == "Inside HGU":

            daily[date][
                "inside_hgu"
            ] += 1

        elif zone == "0–1 km":

            daily[date][
                "zone_0_1"
            ] += 1

        elif zone == "1–3 km":

            daily[date][
                "zone_1_3"
            ] += 1

        elif zone == "3–5 km":

            daily[date][
                "zone_3_5"
            ] += 1


    # --------------------------------------------------------
    # Create complete 30-day series
    # --------------------------------------------------------

    series = []

    current_date = start_date


    while current_date <= end_date:

        date_key = current_date.strftime(
            "%Y-%m-%d"
        )


        values = daily.get(

            date_key,

            {

                "total": 0,

                "high": 0,

                "medium": 0,

                "low": 0,

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


        current_date += timedelta(
            days=1
        )


    return {

        "dates": [

            item[
                "date"
            ]

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

    features: list[dict[str, Any]],

    trend: dict[str, Any],

    pt_name: str,

    start_date,

    end_date

) -> dict[str, Any]:

    series = trend.get(
        "series",
        []
    )


    total_30 = len(
        features
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

        item[
            "total"
        ]

        for item in series

    ]


    average_30 = (

        sum(
            daily_totals
        )

        /

        len(
            daily_totals
        )

        if daily_totals

        else 0

    )


    max_30 = (

        max(
            daily_totals
        )

        if daily_totals

        else 0

    )


    min_30 = (

        min(
            daily_totals
        )

        if daily_totals

        else 0

    )


    # --------------------------------------------------------
    # Latest vs previous day
    # --------------------------------------------------------

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


    vs_previous_pct = None


    if (

        previous is not None

        and

        previous[
            "total"
        ] != 0

    ):

        vs_previous_pct = round(

            (

                latest[
                    "total"
                ]

                -

                previous[
                    "total"
                ]

            )

            /

            previous[
                "total"
            ]

            *

            100,

            1

        )


    # --------------------------------------------------------
    # Status
    # --------------------------------------------------------

    if vs_previous_pct is None:

        status = (
            "Belum dapat dibandingkan"
        )

    elif vs_previous_pct > 20:

        status = "Meningkat"

    elif vs_previous_pct < -20:

        status = "Menurun"

    else:

        status = "Stabil"


    return {

        "pt_name":
            pt_name,

        "period_start":
            start_date.strftime(
                "%Y-%m-%d"
            ),

        "period_end":
            end_date.strftime(
                "%Y-%m-%d"
            ),

        "snapshot_date":
            end_date.strftime(
                "%Y-%m-%d"
            ),

        "total":
            total_30,

        "total_30_days":
            total_30,

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

        "total_within_5km":
            total_30,

        "vs_previous_pct":
            vs_previous_pct,

        "average_30_days":
            round(
                average_30,
                1
            ),

        "min_30_days":
            min_30,

        "max_30_days":
            max_30,

        "status":
            status,

        "latest_day_total":
            latest[
                "total"
            ]
            if latest
            else 0,

        "source":
            "SIPONGI KEMENHUT",

        "last_updated_utc":
            datetime.now(
                timezone.utc
            ).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),

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
        "       SIPONGI PT SLS HOTSPOT MONITOR"
    )
    print(
        "       30 HARI + RADIUS 5 KM"
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


    today = datetime.now(
        timezone.utc
    ).date()


    start_date = (

        today

        -

        timedelta(
            days=
                days_back - 1
        )

    )


    end_date = today


    # --------------------------------------------------------
    # BOUNDARY
    # --------------------------------------------------------

    boundary = load_boundary()


    # --------------------------------------------------------
    # FETCH SIPONGI
    # --------------------------------------------------------

    items = fetch_sipongi(
        config
    )


    # --------------------------------------------------------
    # SPATIAL FILTER
    # --------------------------------------------------------

    features = process_spatial(

        items,

        boundary,

        config

    )


    # --------------------------------------------------------
    # DEDUPLICATE
    #
    # Keep separate satellite detections if date/source
    # differs, because they are separate SiPongi records.
    # --------------------------------------------------------

    unique = {}

    for feature in features:

        properties = feature.get(
            "properties",
            {}
        )


        coordinates = feature[
            "geometry"
        ][
            "coordinates"
        ]


        key = (

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


        unique[
            key
        ] = feature


    features = list(
        unique.values()
    )


    # --------------------------------------------------------
    # TREND
    # --------------------------------------------------------

    trend = build_daily_trend(

        features,

        start_date,

        end_date

    )


    # --------------------------------------------------------
    # SUMMARY
    # --------------------------------------------------------

    summary = build_summary(

        features,

        trend,

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
            "sipongi_pt_sls_hotspots_30days_5km",

        "features":
            features

    }


    # --------------------------------------------------------
    # SAVE CURRENT DATA
    # --------------------------------------------------------

    write_json(

        SITE_DATA /
        "current.geojson",

        current_geojson

    )


    # --------------------------------------------------------
    # SAVE TREND 30 DAYS
    # --------------------------------------------------------

    write_json(

        SITE_DATA /
        "trend_30days.json",

        trend

    )


    # Compatibility with current HTML
    write_json(

        SITE_DATA /
        "trend_7days.json",

        trend

    )


    # --------------------------------------------------------
    # SAVE SUMMARY
    # --------------------------------------------------------

    write_json(

        SITE_DATA /
        "summary.json",

        summary

    )


    # --------------------------------------------------------
    # SAVE BOUNDARY
    # --------------------------------------------------------

    write_json(

        SITE_DATA /
        "pt_boundary.geojson",

        read_json(
            BOUNDARY_PATH
        )

    )


    # --------------------------------------------------------
    # SAVE CURRENT 30-DAY STATE
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

        "features":
            features,

        "trend":
            trend,

        "summary":
            summary

    }


    write_json(

        STATE_PATH,

        state

    )


    # --------------------------------------------------------
    # FINAL LOG
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
        "Status:",
        summary[
            "status"
        ]
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
