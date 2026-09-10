# SiPongi PT Hotspot Monitor

Web map otomatis untuk memantau hotspot SiPongi di dalam batas PT.

## Konsep

- Sumber data: endpoint yang dipanggil halaman peta SiPongi.
- Mode data: `late=24` (hotspot periode terakhir 24 jam sesuai request aplikasi SiPongi).
- Frekuensi pengambilan: 1 kali per hari.
- Sumber satelit yang diminta: NASA-MODIS, NASA-SNPP, NASA-NOAA20.
- Confidence: low, medium, high.
- Filter spasial: hanya titik yang masuk polygon PT.
- Histori aktif: rolling 7 hari.
- Data lama > 7 hari dikeluarkan dari data aktif.
- Output: GitHub Pages (peta + summary + trend).

## Struktur

```text
sipongi_pt_hotspot_monitor/
├── .github/workflows/
│   └── update-sipongi.yml
├── config/
│   └── config.json
├── data/
│   ├── pt_boundary.geojson
│   └── rolling_7days.json
├── scripts/
│   └── update_sipongi.py
├── site/
│   ├── index.html
│   └── data/
│       ├── current.geojson
│       ├── trend_7days.json
│       └── summary.json
└── requirements.txt
```

## 1. Masukkan boundary PT

Ganti:

`data/pt_boundary.geojson`

dengan boundary PT dalam GeoJSON WGS84 / EPSG:4326.

Format boleh Polygon atau MultiPolygon.

Contoh minimum:

```json
{
  "type": "FeatureCollection",
  "features": [
    {
      "type": "Feature",
      "properties": {"name": "PT CONTOH"},
      "geometry": {
        "type": "Polygon",
        "coordinates": [[[110,-1],[111,-1],[111,0],[110,0],[110,-1]]]
      }
    }
  ]
}
```

## 2. Konfigurasi

Edit `config/config.json`.

Default:

```json
{
  "pt_name": "NAMA PT",
  "sipongi_endpoint": "https://opsroom.sipongidata.my.id/api/opsroom/indoHotspot",
  "late_hours": 24,
  "satelit": ["NASA-MODIS", "NASA-SNPP", "NASA-NOAA20"],
  "confidence": ["low", "medium", "high"],
  "keep_days": 7,
  "timezone_label": "WIB"
}
```

## 3. GitHub Pages

Repository:
Settings → Pages → Build and deployment → Source: GitHub Actions.

Workflow akan menjalankan build + deploy.

## 4. Jadwal

Workflow berjalan 1 kali sehari pada:

`07:15 WIB` = `00:15 UTC`

Jika ingin jam lain, ubah cron pada `.github/workflows/update-sipongi.yml`.

## 5. Apa yang terjadi setiap hari?

1. GitHub Actions mengambil data SiPongi dengan `late=24`.
2. Response diproses.
3. Titik hotspot diubah menjadi GeoJSON.
4. Spatial filter dilakukan terhadap polygon PT.
5. Snapshot hari itu disimpan ke `rolling_7days.json`.
6. Snapshot yang lebih tua dari 7 hari dibuang dari data aktif.
7. Summary dan trend 7 hari dihitung ulang.
8. `current.geojson`, `summary.json`, dan `trend_7days.json` diperbarui.
9. Perubahan data di-commit ke repository.
10. Boundary PT disalin ke artifact Pages.
11. GitHub Pages dideploy.

## 6. Output dashboard

Dashboard menampilkan:

- Total hotspot 24 jam
- High / Medium / Low
- Perubahan terhadap hari sebelumnya
- Rata-rata 7 hari
- Maksimum 7 hari
- Status meningkat / stabil / menurun
- Grafik tren 7 hari
- Peta titik hotspot
- Popup detail titik
- Waktu update terakhir
- Sumber: SIPONGI KEMENHUT

## 7. Catatan tentang "data 7 hari"

Data aktif hanya menyimpan rolling 7 hari dalam `rolling_7days.json`, sehingga file data lama tidak menumpuk di folder aktif.

Namun Git sendiri menyimpan riwayat commit. Artinya versi lama masih dapat ada di Git history. Untuk monitoring biasa ini tidak mengganggu ukuran data aktif. Bila suatu saat diperlukan repository tanpa histori data harian, kita dapat memindahkan state ke branch khusus yang ditulis ulang.

## 8. Catatan teknis

Endpoint diambil dari request yang terlihat pada browser saat membuka halaman SiPongi `/peta`.

Contoh request:

```text
https://opsroom.sipongidata.my.id/api/opsroom/indoHotspot
```

dengan parameter:

```text
wilayah=IN
filterperiode=false
late=24
satelit[]=NASA-MODIS
satelit[]=NASA-SNPP
satelit[]=NASA-NOAA20
confidence[]=low
confidence[]=medium
confidence[]=high
provinsi=
kabkota=
```

Karena endpoint tersebut adalah endpoint aplikasi dan tidak memiliki dokumentasi API publik di repository ini, parser dibuat fleksibel untuk beberapa bentuk response umum (GeoJSON, list, atau object dengan key `data`/`features`).

Jika struktur response SiPongi berubah, script perlu disesuaikan.

## 9. Interpretasi

Hotspot adalah indikasi anomali suhu/indikasi titik panas, bukan bukti otomatis kebakaran aktual. Gunakan untuk screening dan pemantauan awal serta verifikasi lapangan.

## 10. Validasi endpoint SiPongi

Jika workflow gagal pada langkah "Fetch SiPongi", buka log Actions.
Kemungkinan penyebab utama:

- struktur response endpoint berubah;
- endpoint membutuhkan header/cookie tambahan;
- parameter request berubah;
- response bukan JSON.

Parser utama ada di `scripts/update_sipongi.py`, terutama fungsi:
`fetch_sipongi()`, `extract_items()`, dan `normalize_item()`.

Jangan memasukkan credential/cookie browser ke repository.

## Boundary PT SLS yang sudah dipasang

- Sumber file: `HGU_PT_SLS.shp` yang diberikan.
- Boundary terdiri dari 13 polygon.
- Geometry dinyatakan valid pada pemeriksaan awal.
- CRS sumber: EPSG:32750 (UTM zone 50S).
- Boundary sudah direproyeksikan menjadi WGS84 / EPSG:4326 untuk kebutuhan web map dan spatial filtering.
- Perkiraan luas geometri total dari shapefile: sekitar 10.007,07 ha.
