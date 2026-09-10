from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
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
# JSON HELPERS
# ============================================================

def read_json(path: Path) -> Any:

    with path.open(
        "r",
        encoding="utf-8"
    ) as f:

        return json.load(f)


def write_json(
    path: Path,
    data: Any
) -> None:

    path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    with path.open(
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            data,
            f,
            indent=2,
            ensure_ascii=False
        )


# ============================================================
# VALUE HELPERS
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


def to_float(
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


# ============================================================
# LOAD HGU BOUNDARY
# ============================================================

def load_boundary():

    if not BOUNDARY_PATH.exists():

        raise RuntimeError(
            f"Boundary tidak ditemukan: {BOUNDARY_PATH}"
        )


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
            "WARNING: Boundary HGU invalid."
        )

        print(
            "Mencoba memperbaiki geometry..."
        )


        boundary = boundary.buffer(
            0
        )


    if boundary.is_empty:

        raise RuntimeError(
            "Boundary tetap kosong setelah repair."
        )


    return boundary


# ============================================================
# PREPARE METRIC CRS
# ============================================================

def prepare_metric_boundary(
    boundary
):

    """
    PT SLS berada sekitar 115 BT dan 2-3 LS.

    UTM Zone 50S:
    EPSG:32750

    Digunakan agar jarak dihitung dalam meter/km.
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


    return (
        boundary_metric,
        to_metric
    )


# ============================================================
# EXTRACT API RESPONSE
# ============================================================

def extract_items(
    payload: Any
) -> list[dict[str, Any]]:

    # --------------------------------------------------------
    # Direct list
    # --------------------------------------------------------

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


    # --------------------------------------------------------
    # Dictionary
    # --------------------------------------------------------

    if isinstance(
        payload,
        dict
    ):

        # ----------------------------------------------------
        # GeoJSON
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


                properties = (

                    feature.get(
                        "properties"
                    )

                    or {}

                )


                result.append({

                    **properties,

                    "_geometry":
                        feature.get(
                            "geometry"
                        )

                })


            return result


        # ----------------------------------------------------
        # Common API wrappers
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

            lon = to_float(
                coordinates[0]
            )


            lat = to_float(
                coordinates[1]
            )


    # --------------------------------------------------------
    # Standard coordinate fields
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


    # --------------------------------------------------------
    # Invalid coordinate
    # --------------------------------------------------------

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


    # Coba confidence numerik dari field confidence
    confidence_numeric = to_float(

        first_value(

            item,

            "confidence"

        )

    )


    if confidence_value is None:

        confidence_value = confidence_numeric


    confidence = str(

        confidence_raw

        or ""

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
    # DATES
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
    # OTHER ATTRIBUTES
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
    # Preserve other scalar fields
    # --------------------------------------------------------

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
# DATE PARSER
# ============================================================

INDONESIAN_MONTHS = {

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


def parse_hotspot_date(
    value: Any
) -> str | None:

    if not value:

        return None


    text = str(
        value
    ).strip()


    # --------------------------------------------------------
    # ISO datetime
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
    # YYYY-MM-DD
    # --------------------------------------------------------

    match = re.search(

        r"(\d{4})-(\d{2})-(\d{2})",

        text

    )


    if match:

        return match.group(
            0
        )


    # --------------------------------------------------------
    # Indonesian date
    #
    # Kamis, 10 September 2026 01:15:00
    # --------------------------------------------------------

    parts = text.replace(
        ",",
        " "
    ).split()


    for index, part in enumerate(
        parts
    ):

        month_number = (

            INDONESIAN_MONTHS.get(

                part.lower()

            )

        )


        if month_number is None:

            continue


        if index < 1:

            continue


        if index + 1 >= len(parts):

            continue


        day_text = parts[
            index - 1
        ]


        year_text = parts[
            index + 1
        ]


        if not (

            day_text.isdigit()

            and

            year_text.isdigit()

        ):

            continue


        try:

            parsed = date(

                int(
                    year_text
                ),

                month_number,

                int(
                    day_text
                )

            )


            return parsed.strftime(
                "%Y-%m-%d"
            )


        except ValueError:

            continue


    return None


# ============================================================
# FETCH SIPONGI
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


    # ========================================================
    # DEFAULTS
    #
    # Berdasarkan request asli SiPongi yang kita tangkap:
    #
    # filterperiode=true
    # late=custom
    # provinsi=12
    # ========================================================

    late_mode = str(

        config.get(

            "late_mode",

            "custom"

        )

    )


    province = str(

        config.get(

            "provinsi",

            "12"

        )

    )


    # ========================================================
    # REQUEST PARAMETERS
    # ========================================================

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
        ),

        (
            "late",
            late_mode
        )

    ]


    # --------------------------------------------------------
    # Satellite
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


    # ========================================================
    # HEADERS
    # ========================================================

    headers = {

        "User-Agent":
            "SiPongi-PT-SLS-Hotspot-Monitor/5.0",

        "Accept":
            "application/json,text/plain,*/*",

        "Referer":
            "https://sipongi.gakkum.kehutanan.go.id/peta"

    }


    # ========================================================
    # PRINT REQUEST
    # ========================================================

    print("")
    print(
        "=============================================="
    )

    print(
        "              SIPONGI REQUEST"
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
            "mode",
            "date_range"
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
        "Late:",
        late_mode
    )


    print(
        "Provinsi:",
        province
    )


    print(
        "Satelit:",
        config.get(
            "satelit",
            []
        )
    )


    print(
        "Confidence:",
        config.get(
            "confidence",
            []
        )
    )


    print("")
    print(
        "Request parameters:"
    )


    for key, value in params:

        print(
            f"  {key} = {value}"
        )


    # --------------------------------------------------------
    # Prepare exact URL for diagnostic
    # --------------------------------------------------------

    prepared_request = requests.Request(

        "GET",

        endpoint,

        params=params,

        headers=headers

    ).prepare()


    print("")
    print(
        "FULL REQUEST URL:"
    )

    print(
        prepared_request.url
    )


    print(
        "=============================================="
    )

    print("")


    # ========================================================
    # REQUEST
    # ========================================================

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


    # ========================================================
    # RESPONSE
    # ========================================================

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


    try:

        payload = response.json()


    except ValueError as exc:

        raise RuntimeError(

            "Response SiPongi bukan JSON valid.\n"

            f"Response awal: {text[:500]}"

        ) from exc


    # ========================================================
    # EXTRACT
    # ========================================================

    raw_items = extract_items(
        payload
    )


    normalized = []


    for raw_item in raw_items:

        item = normalize_item(
            raw_item
        )


        if item:

            normalized.append(
                item
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
# SPATIAL PROCESSING
# ============================================================

def process_spatial(

    items: list[dict[str, Any]],

    boundary,

    config: dict[str, Any]

) -> list[dict[str, Any]]:

    (

        boundary_metric,

        to_metric

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


    minx, miny, maxx, maxy = (
        boundary.bounds
    )


    bbox_count = 0

    inside_count = 0

    result = []


    # ========================================================
    # PROCESS
    # ========================================================

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
        # BBOX
        # ----------------------------------------------------

        in_bbox = (

            minx <= lon <= maxx

            and

            miny <= lat <= maxy

        )


        if in_bbox:

            bbox_count += 1


        # ----------------------------------------------------
        # INSIDE HGU
        # ----------------------------------------------------

        inside_hgu = boundary.covers(

            point_wgs84

        )


        if inside_hgu:

            inside_count += 1


        # ----------------------------------------------------
        # METRIC POINT
        # ----------------------------------------------------

        point_metric = transform(

            to_metric,

            point_wgs84

        )


        # ----------------------------------------------------
        # DISTANCE TO HGU
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
        # FILTER 5 KM
        # ----------------------------------------------------

        if (

            not inside_hgu

            and

            distance_km > max_distance_km

        ):

            continue


        # ----------------------------------------------------
        # ZONE
        # ----------------------------------------------------

        zone = ">5 km"


        if inside_hgu:

            zone = "Inside HGU"

        else:

            for zone_config in zones:

                zone_name = zone_config.get(
                    "name"
                )


                zone_min = float(

                    zone_config.get(

                        "min_km",

                        0

                    )

                )


                zone_max = float(

                    zone_config.get(

                        "max_km",

                        0

                    )

                )


                if (

                    distance_km > zone_min

                    and

                    distance_km <= zone_max

                ):

                    zone = zone_name

                    break


        # ----------------------------------------------------
        # CREATE FEATURE
        # ----------------------------------------------------

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


        result.append(
            feature
        )


    # ========================================================
    # DIAGNOSTIC
    # ========================================================

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
        "Hotspot dalam BBOX:",
        bbox_count
    )


    print(
        "Hotspot inside HGU:",
        inside_count
    )


    print(
        "Hotspot <= 5 km:",
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


    print("")
    print(
        "=============================================="
    )
    print("")


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
# DAILY TREND
# ============================================================

def build_daily_trend(

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


    # --------------------------------------------------------
    # Aggregate
    # --------------------------------------------------------

    for feature in features:

        properties = feature.get(

            "properties",

            {}

        )


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


        if not hotspot_date:

            records_without_date += 1

            continue


        confidence = str(

            properties.get(

                "confidence",

                "unknown"

            )

        ).lower()


        zone = properties.get(
            "zone"
        )


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


    # --------------------------------------------------------
    # Complete calendar
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


        current_date += timedelta(
            days=1
        )


    # --------------------------------------------------------
    # LOG
    # --------------------------------------------------------

    print("")
    print(
        "=============================================="
    )

    print(
        "             DAILY TREND RESULT"
    )

    print(
        "=============================================="
    )


    active_days = [

        item

        for item in series

        if item[
            "total"
        ] > 0

    ]


    print(
        "Tanggal dengan hotspot:",
        len(active_days)
    )


    print(
        "Total hotspot seluruh hari:",
        sum(

            item[
                "total"
            ]

            for item in series

        )
    )


    print(
        "Record tanpa tanggal:",
        records_without_date
    )


    print("")


    if active_days:

        print(
            "Tanggal yang memiliki hotspot:"
        )


        for item in active_days:

            print(

                " ",

                item[
                    "date"
                ],

                "Total=",

                item[
                    "total"
                ],

                "Inside=",

                item[
                    "inside_hgu"
                ],

                "0–1=",

                item[
                    "zone_0_1"
                ],

                "1–3=",

                item[
                    "zone_1_3"
                ],

                "3–5=",

                item[
                    "zone_3_5"
                ]

            )

    else:

        print(
            "Tidak ada hotspot yang memiliki tanggal "
            "dan lolos filter <=5 km."
        )


    print("")
    print(
        "=============================================="
    )
    print("")


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

    pt_name: str

) -> dict[str, Any]:

    series = trend.get(

        "series",

        []

    )


    # --------------------------------------------------------
    # Confidence
    # --------------------------------------------------------

    confidence_counter = Counter(

        feature[
            "properties"
        ].get(

            "confidence",

            "unknown"

        )

        for feature in features

    )


    # --------------------------------------------------------
    # Zone
    # --------------------------------------------------------

    zone_counter = Counter(

        feature[
            "properties"
        ].get(

            "zone",

            "unknown"

        )

        for feature in features

    )


    # --------------------------------------------------------
    # Daily total
    # --------------------------------------------------------

    daily_totals = [

        int(

            item.get(

                "total",

                0

            )

        )

        for item in series

    ]


    total_30 = sum(
        daily_totals
    )


    average_30 = (

        total_30

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
    # Latest / previous
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

        latest is not None

        and

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


    # --------------------------------------------------------
    # Return
    # --------------------------------------------------------

    return {

        "pt_name":
            pt_name,

        "period_start":
            trend.get(
                "period_start"
            ),

        "period_end":
            trend.get(
                "period_end"
            ),

        "snapshot_date":
            trend.get(
                "period_end"
            ),

        "total":
            total_30,

        "total_30_days":
            total_30,

        "total_within_5km":
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

        "average_30_days":
            round(
                average_30,
                1
            ),

        "min_30_days":
            min_30,

        "max_30_days":
            max_30,

        "latest_day_total":
            (
                latest[
                    "total"
                ]

                if latest

                else 0

            ),

        "vs_previous_pct":
            vs_previous_pct,

        "status":
            status,

        "monitoring_radius_km":
            5,

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
                "tidak otomatis berarti "
                "kebakaran aktual. "
                "Verifikasi lapangan "
                "tetap diperlukan."
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
    # Configuration
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
    # Boundary
    # --------------------------------------------------------

    boundary = load_boundary()


    # --------------------------------------------------------
    # Fetch
    # --------------------------------------------------------

    items = fetch_sipongi(
        config
    )


    # --------------------------------------------------------
    # Spatial
    # --------------------------------------------------------

    features = process_spatial(

        items,

        boundary,

        config

    )


    # --------------------------------------------------------
    # Deduplicate
    # --------------------------------------------------------

    features = deduplicate(
        features
    )


    # --------------------------------------------------------
    # Daily trend
    # --------------------------------------------------------

    trend = build_daily_trend(

        features,

        start_date,

        end_date

    )


    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    summary = build_summary(

        features,

        trend,

        pt_name

    )


    # --------------------------------------------------------
    # Current GeoJSON
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
    # Save current GeoJSON
    # --------------------------------------------------------

    write_json(

        SITE_DATA /
        "current.geojson",

        current_geojson

    )


    # --------------------------------------------------------
    # Save trend
    # --------------------------------------------------------

    write_json(

        SITE_DATA /
        "trend_30days.json",

        trend

    )


    # --------------------------------------------------------
    # Compatibility with old HTML
    # --------------------------------------------------------

    write_json(

        SITE_DATA /
        "trend_7days.json",

        trend

    )


    # --------------------------------------------------------
    # Save summary
    # --------------------------------------------------------

    write_json(

        SITE_DATA /
        "summary.json",

        summary

    )


    # --------------------------------------------------------
    # Copy HGU
    # --------------------------------------------------------

    write_json(

        SITE_DATA /
        "pt_boundary.geojson",

        read_json(
            BOUNDARY_PATH
        )

    )


    # --------------------------------------------------------
    # Rolling state
    #
    # State selalu ditimpa dengan window 30 hari terbaru.
    # Tidak menumpuk tanpa batas.
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

        "updated_utc":
            datetime.now(
                timezone.utc
            ).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
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


    # ========================================================
    # FINAL RESULT
    # ========================================================

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
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    main()
