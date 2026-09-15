# Sentinel-2 L1C Time-Series Pipeline

Imagine you want a **photo album of one place seen from space**, one picture for every day the
Sentinel-2 satellite flew over it. This pipeline builds that album automatically from the
[Copernicus Data Space Ecosystem (CDSE)](https://dataspace.copernicus.eu/).

More precisely, it retrieves every acquisition returned by the CDSE STAC search for the requested AOI
and date range (collection `sentinel-2-l1c`). It keeps at most one acquisition per date, checks pixel
quality, and saves the accepted ones on a common 10 m UTM grid. Every returned granule, saved or not, is
documented in `metadata.csv`.

---

## What happens inside the pipeline

It works in 4 steps.

### 1. Search (discovery)

You give a point (latitude, longitude) and two dates. The pipeline builds a 10.24 km square around the
point. It then asks the CDSE STAC API for every `sentinel-2-l1c` item whose footprint intersects that
square within the date range.

- It follows the result pages (100 items per page) until the server returns no further page.
- The result is exactly what the catalogue returns for that geometry and period. It depends on the
  catalogue content, on the item footprints and on pagination. It is not an independent guarantee that
  every Sentinel-2 overpass is included.

It writes **every** returned item in a notebook (`metadata.csv`), including items it cannot parse.

### 2. Sort (selection)

It applies the following filters, **in this order**:

1. **Deduplication by date and tile**: when the same tile and day exist in several processing baselines,
   only the latest baseline is kept.
2. **Scene cloud cover**: the granule's STAC `eo:cloud_cover` must be ≤ `--max_cloud`.
3. **Geometric AOI coverage**: the STAC footprint must cover at least `--min_coverage` of the square.
4. **Best tile per date**: among the remaining tiles of a date, the pipeline keeps the one with the
   highest AOI coverage, then the same UTM zone as the grid, then the lowest cloud cover.
5. **Optional cap** `--max_images`, applied on the earliest or clearest dates (`--sort`).

> **Warning:** baseline deduplication is performed before cloud-cover and coverage filtering.
> Consequently, an older, clearer acquisition of the same tile and day will not be used as a fallback
> if the more recent baseline is eliminated.

For every item it throws away, it writes **why** in the notebook.

### 3. Check (quality control)

For each selected acquisition, it reads the 13 bands over the square, together with the ESA quality masks
when they can be found.

- In `s3` mode, only the window covering the square is streamed.
- In `https` mode, each band file is downloaded in full, cropped, then deleted.

It then builds a validity mask for each band and computes quality metrics:

- "Is there really data in this pixel, in this band?" (otherwise it is a hole, called *nodata*);
- "Is the pixel flagged as cloud, damaged by the sensor, or saturated?"

By default, an acquisition is **rejected** when any of these exceeds its threshold:

- too few valid pixels;
- too much nodata along the border;
- too much saturation;
- too many sensor artefacts.

Clouds inside the square only lead to rejection if you set `--max_cloud_fraction` below `1.0`.

If the best candidate of a date fails quality control, **the date is rejected**: the pipeline does not
try the next overlapping tile.

### 4. Store (output)

Accepted acquisitions are saved as `.npz` files, one per date. The `metadata.csv` notebook contains one
row per granule returned by the searches run in that output folder. Each row has:

- a status: **saved**, **rejected**, **failed** or **skipped**;
- the stage where the decision was taken;
- an explicit reason (`rejection_reason`).

All saved dates share exactly the **same target grid**:

- the same projection (local UTM CRS);
- the same affine transform;
- the same array shape (1024 × 1024 pixels at 10 m).

Pixel (500, 500) therefore always refers to the same grid cell. The underlying satellite observations can
still be slightly shifted from one date to another (see
[below](#how-spatial-alignment-and-the-changing-orbit-are-handled)).

### How the 13 spectral bands are obtained

- In each Sentinel-2 product, every band (B01 to B12, plus B8A) is a **separate JPEG2000 file**. The bands
  range from about 443 nm (B01, coastal aerosol) to about 2190–2200 nm (B12, short-wave infrared). Exact
  centre wavelengths differ slightly between satellites.
- The bands do not have the same native pixel size:
  - **10 m**: B02, B03, B04, B08;
  - **20 m**: B05, B06, B07, B8A, B11, B12;
  - **60 m**: B01, B09, B10.
- 10 m bands from a tile in the grid's UTM zone are copied pixel-for-pixel (nearest neighbour on an
  aligned lattice).
- 20 m and 60 m bands are resampled to 10 m with **masked bilinear interpolation**. So are all bands
  when the tile is in a neighbouring UTM zone.
  - A target 10 m pixel is marked invalid as soon as the source interpolation kernel contains an invalid
    or nodata sample.
  - Contributions with a weight below 0.001 are tolerated, to absorb floating-point rounding.
  - This avoids interpolating spectral values from nodata pixels at borders.
- The 13 results are stacked in a fixed order into one `(13, 1024, 1024)` array. Validity is decided
  **band by band**: a pixel is never considered valid because another band contains data.

### How spatial alignment and the changing orbit are handled

- **Different passes, different views.** Sentinel-2 does not always fly over the same track. Your area
  can be observed from **different relative orbits**, at different viewing angles, and sometimes near the
  edge of the swath.
- **What ESA provides.** L1C products are orthorectified by ESA and cut into fixed 100 × 100 km **MGRS
  tiles** on a UTM grid.
- **What the pipeline does.**
  - It defines **one fixed target grid**, with its corner snapped to multiples of 60 m. That is the pixel
    lattice of Sentinel-2 tiles within a UTM zone. For tiles in the grid's zone, source and target pixels
    line up without resampling offsets.
  - Tiles from a neighbouring UTM zone are reprojected onto the same grid.
- **Same grid does not mean perfect co-registration.** The underlying observations can show slight
  geometric shifts between dates. These come from the native geolocation accuracy of Sentinel-2
  products, from differences between relative orbit tracks, or from the source MGRS tile used. **This
  pipeline does not perform any additional fine co-registration and does not measure the residual
  shift.** If your analysis is sensitive to small misregistrations, check or co-register the stack
  yourself.
- **Swath edges.** A pass near the edge of the swath only partly covers the square. The missing part
  becomes *nodata*, and the acquisition is rejected if the thresholds are exceeded.
- **Angles.** Viewing and sun angles are not corrected (no BRDF normalisation). They are only recorded in
  `metadata.csv`: `relative_orbit`, `orbit_state`, `view_incidence_angle`, `sun_elevation`,
  `sun_azimuth`.
- **Radiometric offset.** Products with processing baseline ≥ 04.00 carry a **+1000 DN** offset. On CDSE
  this includes the reprocessed archive (Collection-1, baselines 05.xx), not only data acquired since
  2022. See [Radiometry and harmonisation](#radiometry-and-harmonisation).

---

## Tutorial: your first call

### Step 0: install (once)

In a terminal, inside the project folder:

```bash
python -m venv .venv
```

```bash
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

(On Linux / macOS, replace `.venv\Scripts\python.exe` with `.venv/bin/python` everywhere in this tutorial.)

### Step 1: add your Copernicus keys (once)

Searching for pictures is free and anonymous, but **downloading** requires a free CDSE account.

1. Create an account at <https://dataspace.copernicus.eu/>.
2. Generate S3 keys at <https://eodata-s3keysmanager.dataspace.copernicus.eu>.
3. Create a `.env` file at the root of the project containing:

```text
CDSE_S3_ACCESS_KEY=your_access_key
CDSE_S3_SECRET_KEY=your_secret_key
```

The `.env` file is ignored by git, so your keys never end up on GitHub.

For `https` mode, set `CDSE_USERNAME` and `CDSE_PASSWORD` instead. With `--access auto` (the default),
`s3` is used when both S3 keys are set.

### Step 2: look before downloading

A call is one command saying **where**, **when**, and **which folder to store things in**. Add
`--dry_run` to only see the list of selected dates. The search runs, but nothing is downloaded and no file
is written:

```bash
.venv\Scripts\python.exe s2_l1c_pipeline.py --lat 48.8566 --lon 2.3522 --start_date 2023-01-01 --end_date 2023-12-31 --max_cloud 20 --output_dir data/paris_2023 --dry_run
```

You get something like this (the number of rejected granules per stage and reason is logged just before):

```text
date        tile   cloud%  cover  base        crs  item_id
2023-02-07  31UDQ    0.00  1.000  5.10 EPSG:32631  S2A_MSIL1C_20230207T110221_N0510_R094_T31UDQ_...
2023-02-14  31UDQ    0.00  1.000  5.10 EPSG:32631  S2A_MSIL1C_20230214T105141_N0510_R051_T31UDQ_...
...
```

### Step 3: download for real

Same command, **without** `--dry_run`:

```bash
.venv\Scripts\python.exe s2_l1c_pipeline.py --lat 48.8566 --lon 2.3522 --start_date 2023-01-01 --end_date 2023-12-31 --max_cloud 20 --output_dir data/paris_2023
```

The duration per date depends on the access mode and on your network. Each date reads 13 band files plus
up to 27 small quality-mask files.

If it stops halfway (network cut, laptop closed), **run exactly the same command again**: the pipeline
picks up where it left off (see [Resume behaviour](#resume-behaviour)).

### Step 4: open your images

```python
import numpy as np

z = np.load("data/paris_2023/2023-02-07_31UDQ.npz")
image = z["data"]        # the 13 bands: uint16, shape (13, 1024, 1024)
good = z["valid_mask"]   # bool, shape (13, 1024, 1024): True = reliable pixel in that band
bands = z["bands"].tolist()

red = image[bands.index("B04")]
green = image[bands.index("B03")]
blue = image[bands.index("B02")]
```

Always use `valid_mask` to find missing data, not `data == 0` (see
[Radiometry and harmonisation](#radiometry-and-harmonisation)).

To keep only the saved acquisitions that are valid in all bands over at least 99.9 % of the square:

```python
import pandas as pd; meta = pd.read_csv("data/paris_2023/metadata.csv"); good = meta[(meta.status == "saved") & (meta.valid_fraction_all_bands >= 0.999)]
```

To find out why a date is missing, look at the `status` and `rejection_reason` columns.

### More example calls

The 20 clearest images between 2020 and 2024 in Toulouse:

```bash
.venv\Scripts\python.exe s2_l1c_pipeline.py --lat 43.6047 --lon 1.4442 --start_date 2020-01-01 --end_date 2024-12-31 --max_cloud 20 --max_images 20 --sort cloud --output_dir data/toulouse_top20
```

Images with at most 5 % of cloud or cirrus flagged **inside your square** by the ESA mask (rough sort,
then fine sort):

```bash
.venv\Scripts\python.exe s2_l1c_pipeline.py --lat 48.8566 --lon 2.3522 --start_date 2023-01-01 --end_date 2023-12-31 --max_cloud 30 --max_cloud_fraction 0.05 --require_quality_masks --output_dir data/paris_2023_clear
```

---

## The parameters you care about

### The essentials

| Parameter | What it is | Example |
|---|---|---|
| `--lat`, `--lon` | The centre of your area (WGS84) | `--lat 48.8566 --lon 2.3522` (Paris) |
| `--start_date`, `--end_date` | Your 2 dates (inclusive, UTC), format `YYYY-MM-DD` | `--start_date 2023-01-01 --end_date 2023-12-31` |
| `--output_dir` | The folder where files are stored | `--output_dir data/paris_2023` |

### Choosing how many pictures and which ones

| Parameter | Default | What it is |
|---|---|---|
| `--max_cloud` | `100` | Maximum granule cloud cover (%) from STAC `eo:cloud_cover`, computed by ESA over the whole granule (up to 100 × 100 km), not over your square |
| `--min_coverage` | `0.99` | Minimum fraction of your square inside the granule's STAC footprint (geometry only, not pixel validity) |
| `--max_images` | all | Maximum number of dates. Dates already on disk count toward the cap |
| `--sort` | `date` | With `--max_images`: `date` takes the earliest dates, `cloud` the clearest |
| `--dry_run` | off | Only look, do not download or write anything |

### Quality control

| Parameter | Default | What it is |
|---|---|---|
| `--min_valid_fraction` | `0.99` | Minimum fraction of pixels valid in **all 13 bands** |
| `--max_edge_nodata_fraction` | `0.20` | Maximum fraction of the border ring that is nodata in at least one band |
| `--edge_width` | `32` | Width of that border ring, in pixels |
| `--max_saturated_fraction` | `0.01` | Maximum fraction of valid band samples that are saturated |
| `--max_artefact_fraction` | `0.05` | Maximum fraction of valid band samples flagged as artefacts by `MSK_QUALIT`. Only applied when `MSK_QUALIT` was read |
| `--max_cloud_fraction` | `1.0` (off) | Maximum fraction of observed pixels flagged opaque cloud or cirrus by `MSK_CLASSI_B00`. Only applied when that mask was read |
| `--require_quality_masks` | off | Reject the acquisition unless **all** quality masks were read (see [Missing masks](#missing-masks-report-vs-reject)) |
| `--harmonize` | off | Subtract the +1000 DN offset (baseline ≥ 04.00) in the saved file, after quality control |

### Access and runtime

| Parameter | Default | What it is |
|---|---|---|
| `--access` | `auto` | `s3`, `https`, or `auto` (`s3` if S3 keys are set) |
| `--workers` | `4` | Bands read in parallel per acquisition |
| `--retries` | `3` | Attempts per band or mask read |
| `--stac_url`, `--collection` | CDSE STAC v1, `sentinel-2-l1c` | Catalogue endpoint and collection |
| `--log_level` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

To see **all** parameters:

```bash
.venv\Scripts\python.exe s2_l1c_pipeline.py --help
```

Exit codes: `0` success · `1` finished with some failed dates · `2` fatal error · `130` interrupted.

---

## Technical reference

### STAC search

- **Query.** One STAC search with:
  - collection `sentinel-2-l1c`;
  - `intersects` = the 10.24 km square, converted to longitude/latitude with densified edges;
  - datetime from `start_date` 00:00:00Z to `end_date` 23:59:59.999Z;
  - sorted by date, 100 items per page.
- **Pagination.** All pages are followed through `pystac-client`.
- **No server-side cloud filter.** Cloud cover is filtered during selection, so cloudy scenes appear in
  `metadata.csv` as rejected.
- **Acquisition date.** It is the UTC date of the item's sensing `datetime`.

### Selection rules

- **Deduplication.** The key is (date, tile). The kept item has the highest processing baseline; ties are
  broken by the highest `item_id`.
- **Missing cloud cover.** An item without `eo:cloud_cover` is treated as 100 %.
- **Unusable footprint.** A footprint geometry that cannot be projected or intersected gets an AOI
  coverage of 0, so it is rejected as `aoi_coverage_below_threshold`.
- **Ranking per date.** The ranking key is:
  1. AOI coverage, rounded to 3 decimals, highest first;
  2. same EPSG as the grid first;
  3. lowest cloud cover;
  4. `item_id`.

### Pixel validity and resampling

For each band independently, `SpatialCropper.read_band` returns `data`, `valid` and `nodata`. The
following sources are **combined**; they are not exclusive alternatives:

1. the source raster mask (`read_masks()`), when the file declares a per-dataset or alpha mask;
2. the declared nodata value (`src.nodata`) when it exists, otherwise DN `0`. Sentinel-2 L1C JP2 files
   declare neither a mask nor a nodata value, so in practice DN `0` is used;
3. pixels outside the intersection between the square and the raster (plus a 2-pixel read margin);
4. reprojection:
   - `nodata` is the nearest-neighbour projection of the source validity;
   - `valid` additionally requires the warped validity to reach 1 under the same kernel as the data
     (tolerance 0.001), and the warped value to be non-zero.

The ESA masks then refine each band (see below). Pixels outside every detector, or flagged `msi_lost` /
`nodata`, become nodata. Therefore:

- `nodata` means no source sample backs the pixel;
- `valid` means a trustworthy sample;
- `valid` implies not `nodata`, but a pixel can be neither: for example, a 20 m pixel bordering a hole.

The band-level mask sources that were applied are recorded in `mask_source_by_band`, for example
`fallback_nodata_0+MSK_DETFOO+MSK_QUALIT`.

### ESA quality masks (QI_DATA)

CDSE does not list the L1C quality masks as STAC assets. For products where the `QI_DATA` assets are
present (notably processing baseline ≥ 04.00, including the reprocessed Collection-1 archive), the
pipeline looks for and reads these masks.

- **Where they come from.** The paths are derived from the band paths
  (`GRANULE/<granule>/IMG_DATA/…` becomes `GRANULE/<granule>/QI_DATA/…`).
- **When no search is attempted.** For baselines < 04.00 (which only ship GML vector masks), or when the
  band paths are not S3 paths with an `IMG_DATA` folder, no mask is searched for.
- **Resampling.** Masks are always resampled with nearest neighbour.

"Up to 27 quality masks" means:

- 1 classification mask at 60 m (`MSK_CLASSI_B00`);
- 13 detector-footprint masks (`MSK_DETFOO_<band>`);
- 13 quality masks (`MSK_QUALIT_<band>`).

`MSK_CLASSI_B00` is read first. If it cannot be read, the 26 per-band masks are **not attempted** for that
acquisition.

**`MSK_CLASSI_B00`** (60 m, 3 layers, value > 0 = flagged):

| Layer | Name in outputs | Use |
|---|---|---|
| 1 | `opaque_cloud` | `cloud_fraction`, `opaque_cloud_fraction`, `--max_cloud_fraction` |
| 2 | `cirrus` | `cloud_fraction`, `cirrus_fraction`, `--max_cloud_fraction` |
| 3 | `snow_ice` | `snow_ice_fraction` (reported only) |

**`MSK_DETFOO_<band>`** (band resolution, 1 layer): detector index; `0` = no detector imaged the pixel,
which becomes **nodata** in that band.

**`MSK_QUALIT_<band>`** (band resolution, 8 separate 0/1 layers). The pipeline packs them into one bit
field (`quality_bits`, bit *i* = layer *i + 1*):

| File layer | Bit | Name in outputs | Meaning | Effect in the pipeline |
|---|---|---|---|---|
| 1 | 0 | `ancillary_lost` | Ancillary data lost | artefact |
| 2 | 1 | `ancillary_degraded` | Ancillary data degraded | artefact |
| 3 | 2 | `msi_lost` | MSI data lost | **nodata** |
| 4 | 3 | `msi_degraded` | MSI data degraded | artefact |
| 5 | 4 | `defective` | Defective pixel | artefact |
| 6 | 5 | `nodata` | No data | **nodata** |
| 7 | 6 | `crosstalk_partially_corrected` | Partially corrected crosstalk | reported only (`quality_flag_fractions`) |
| 8 | 7 | `saturated_l1a` | Saturated pixel (L1A) | saturation |

The JP2 mask files carry no layer descriptions. The layer order above follows the ESA L1C product
specification and is **assumed** by the pipeline. The layer counts and resolutions were checked on a
CDSE product with baseline 05.10.

#### Missing masks: report vs reject

There is no `--quality_mode` option. The two behaviours are selected with `--require_quality_masks`:

- **Report (default).**
  - A mask that fails to read logs a warning and never rejects the acquisition by itself.
  - When no mask search is attempted (baseline < 04.00), no warning is logged.
  - The metrics that depend on a missing mask are left empty in `metadata.csv` (`None`, not `0`):
    `cloud_fraction`, `opaque_cloud_fraction`, `cirrus_fraction` and `snow_ice_fraction` without
    `MSK_CLASSI_B00`; `artefact_fraction` and `quality_flag_fractions` without any `MSK_QUALIT`.
  - The corresponding thresholds (`--max_cloud_fraction`, `--max_artefact_fraction`) are **skipped**.
  - `saturation_fraction` is still computed from DN 65535.
  - Without `MSK_DETFOO` / `MSK_QUALIT`, validity relies only on the band data.
- **Reject (`--require_quality_masks`).**
  - The acquisition is rejected unless **all** masks were read (`quality_mask_status = available`),
    whichever thresholds are configured.
  - The reason is `quality_masks_unavailable` (no mask read) or `quality_masks_partial`.
- **Partial reads.** With some `MSK_QUALIT` masks missing, `artefact_fraction` and
  `quality_flag_fractions` are computed on the bands whose mask was read. Which bands had which mask is
  visible in `mask_source_by_band`.

### Quality metrics

All metrics are computed on **raw DN, before any radiometric harmonisation**. None of them uses the STAC
footprint coverage.

| Metric | Definition |
|---|---|
| `valid_fraction_any_band` | Pixels valid in at least one band / all 1024 × 1024 pixels |
| `valid_fraction_all_bands` | Pixels valid in all 13 bands / all pixels (compared with `--min_valid_fraction`) |
| `valid_fraction_by_band`, `nodata_fraction_by_band` | Per band, over all pixels |
| `edge_nodata_fraction` | Pixels of the `--edge_width` border ring that are nodata in at least one band / ring pixels |
| `per_band_min`, `per_band_max`, `per_band_p02`, `per_band_p50`, `per_band_p98` | Computed **exclusively on valid (hence non-nodata) samples** of each band; empty for a band without valid samples |
| `saturation_fraction`, `saturation_fraction_by_band` | Valid samples with DN 65535 or the `saturated_l1a` flag / valid samples |
| `artefact_fraction` | Valid samples with an artefact flag / valid samples, over bands with `MSK_QUALIT` |
| `quality_flag_fractions` | Per flag, share of all samples (valid or not) of bands with `MSK_QUALIT` |
| `cloud_fraction` (+ `opaque_cloud_fraction`, `cirrus_fraction`, `snow_ice_fraction`) | Flagged pixels valid in at least one band / pixels valid in at least one band |
| `shadow_fraction` | Always empty: L1C products contain no cloud-shadow mask |

### Radiometry and harmonisation

- **Units.** Values are L1C top-of-atmosphere reflectance digital numbers. For baseline ≥ 04.00,
  `reflectance = (DN - 1000) / 10000`.
- **Default behaviour.** The raw DN are kept.
- **What `--harmonize` does.**
  - It applies `max(DN, 1000) - 1000` to the whole `data` array of acquisitions with baseline ≥ 04.00.
  - It runs **after** quality control and only changes the saved file.
- **Consequences.**
  - Invalid and nodata pixels are already `0` in `data` and stay `0`.
  - Valid samples with DN ≤ 1000 also become `0`, which is why `valid_mask`, not `data == 0`, identifies
    missing data.
  - Saturated samples (65535) become 64535 in the saved file.
- **What is recorded.**
  - `metadata.csv` stores the `harmonized` flag (`1` = 1000 DN subtracted, `0` = not), filled for
    `saved` rows, together with `processing_baseline`.
  - The offset value itself and the flag are **not** stored inside the `.npz`.

### Output: `.npz` files

One file `<YYYY-MM-DD>_<tile_id>.npz` per accepted date, written atomically.

| Key | Type | Shape | Content |
|---|---|---|---|
| `data` | `uint16` | `(13, 1024, 1024)` | Bands in `bands` order; `0` wherever `valid_mask` is false |
| `valid_mask` | `bool` | `(13, 1024, 1024)` | Trustworthy sample, per band |
| `nodata_mask` | `bool` | `(13, 1024, 1024)` | No source sample, per band |
| `valid_any` | `bool` | `(1024, 1024)` | Valid in at least one band |
| `valid_all` | `bool` | `(1024, 1024)` | Valid in all 13 bands |
| `quality_bits` | `uint8` | `(13, 1024, 1024)` | *Optional* (at least one `MSK_QUALIT` read). Bit *i* = `quality_bit_names[i]`; all zeros for bands whose mask was not read |
| `quality_bit_names` | `str` | `(8,)` | *Optional*, with `quality_bits` |
| `classification_mask` | `uint8` | `(3, 1024, 1024)` | *Optional* (`MSK_CLASSI_B00` read). Raw layer values |
| `classification_names` | `str` | `(3,)` | *Optional*, `opaque_cloud, cirrus, snow_ice` |
| `bands` | `str` | `(13,)` | `B01, B02, B03, B04, B05, B06, B07, B08, B8A, B09, B10, B11, B12` |
| `transform` | `float` | `(6,)` | Affine `(a, b, c, d, e, f)` of the grid, in metres |
| `crs` | `str` | `()` | Grid CRS, e.g. `EPSG:32631` |

### Output: `metadata.csv`

One row per granule returned by the STAC searches run with that `--output_dir`, keyed by `item_id`. Rows
from earlier runs are kept and updated. The file is rewritten atomically after every decision.

- **Statuses.**
  - `saved`, `rejected`, `failed`, `skipped`;
  - `selected` only while a run is in progress or after an interruption.
- **Per-band values** are JSON objects keyed by band name.
- **Empty cells** mean "not computed / not available".

| Group | Columns |
|---|---|
| Identification | `filename`, `date`, `tile_id`, `cloud_cover`, `item_id`, `datetime_utc`, `platform`, `processing_baseline`, `source_crs`, `aoi_coverage` |
| Decision | `status`, `stage` (`discovery`, `selection`, `quality_control`, `output`), `rejection_reason` |
| Acquisition geometry | `relative_orbit`, `orbit_state`, `sun_elevation`, `sun_azimuth`, `view_incidence_angle`, `datatake_id`, `processing_datetime` |
| Quality metrics | see [Quality metrics](#quality-metrics) |
| Mask provenance | `mask_source_by_band`, `quality_assets`, `quality_mask_status` (`available` = all masks read, `partial`, `unavailable`) |
| Reproducibility | `qc_config` (JSON of the thresholds used), `harmonized`, `stac_properties` (all STAC properties, access-related keys removed) |

### Rejection reasons

The column is `rejection_reason`.

- Quality-control reasons can be combined with `;`.
- Some codes carry a detail after `:`.
- For `failed` rows, the column holds the exception type and message instead of a code.

| Status | Stage | Exact value | Meaning |
|---|---|---|---|
| `rejected` | `discovery` | `missing_band_assets:<bands>` | The STAC item lacks some of the 13 band assets |
| `rejected` | `discovery` | `tile_id_not_found` | No MGRS tile in `grid:code` or in the item id |
| `rejected` | `discovery` | `datetime_not_found` | No sensing date |
| `rejected` | `discovery` | `parse_error:<Exception>: <message>` | Any other parsing failure |
| `rejected` | `selection` | `older_processing_baseline:<kept item_id>` | Same tile and day exists with a more recent baseline |
| `rejected` | `selection` | `scene_cloud_cover_above_threshold` | `eo:cloud_cover` > `--max_cloud` (or missing while `--max_cloud` < 100) |
| `rejected` | `selection` | `aoi_coverage_below_threshold` | Footprint covers < `--min_coverage` of the square (also for unusable geometries) |
| `rejected` | `selection` | `inferior_overlapping_tile:<kept item_id>` | Another tile was preferred for that date |
| `rejected` | `selection` | `max_images_limit` | Beyond `--max_images` |
| `rejected` | `quality_control` | `no_valid_pixels` | No valid pixel in any band |
| `rejected` | `quality_control` | `valid_fraction_all_bands_below_threshold` | `valid_fraction_all_bands` < `--min_valid_fraction` |
| `rejected` | `quality_control` | `edge_nodata_fraction_above_threshold` | `edge_nodata_fraction` > `--max_edge_nodata_fraction` |
| `rejected` | `quality_control` | `saturation_fraction_above_threshold` | `saturation_fraction` > `--max_saturated_fraction` |
| `rejected` | `quality_control` | `artefact_fraction_above_threshold` | `artefact_fraction` > `--max_artefact_fraction` |
| `rejected` | `quality_control` | `pixel_cloud_fraction_above_threshold` | `cloud_fraction` > `--max_cloud_fraction` |
| `rejected` | `quality_control` | `quality_masks_unavailable`, `quality_masks_partial` | With `--require_quality_masks` |
| `skipped` | `output` | `output_exists_for_date:<filename>` | A file for that date already exists from another granule or an older run |
| `failed` | `quality_control` or `output` | `<ExceptionType>: <message>` | Read or write error (e.g. network or S3 error after all retries) |

Count rejection codes with pandas:

```python
meta.rejection_reason.dropna().str.split(";").explode().str.split(":").str[0].value_counts()
```

### Resume behaviour

Re-running the same command:

- **does not re-read** a date whose file exists and whose row is already `saved` for the same item;
- **does not re-read** an acquisition already rejected by quality control **with the same `qc_config`**.
  Changing a threshold re-evaluates it;
- **retries** `failed` acquisitions;
- **marks as `skipped`** a newly selected granule for a date whose file came from another granule;
- **reads and migrates** a `metadata.csv` written by the previous version of the pipeline (its rows
  become `saved`).

---

## Limitations

- **Selection.**
  - An older baseline is never used as a fallback when the latest baseline of the same tile and day is
    rejected.
  - When the best tile of a date fails quality control, the date is rejected; the next overlapping tile
    is not tried.
- **Geometry.**
  - The same target grid does not imply sub-pixel co-registration of the observations.
  - No fine co-registration is performed, and the residual shift is not measured.
- **Clouds.**
  - Scene cloud cover (`--max_cloud`) is computed by ESA over the whole granule, not over your square.
  - The in-square cloud fraction relies on ESA's 60 m L1C classification.
  - L1C products contain **no cloud-shadow mask**.
- **Quality masks.** They are only searched for on baselines ≥ 04.00. Their layer order is assumed from
  the ESA product specification. With the default report behaviour, missing masks silently disable the
  cloud and artefact thresholds (a warning is logged when a read fails).
- **Radiometry.**
  - Data are top-of-atmosphere (L1C), not surface reflectance.
  - Viewing and illumination differences are not corrected.
  - `--harmonize` sets valid samples ≤ 1000 DN to 0 in the saved `data`.
- **Catalogue.** Results depend on what the CDSE STAC search returns for the geometry and period.
- **Footprint.** The square is 10.24 km wide (1024 px × 10 m).
- **UTM zones** follow the standard 6° definition (latitudes −80° to 84°); the Norway / Svalbard
  exceptions are not applied.
- **`https` mode** downloads full band files (about 0.5–1 GB per acquisition).

---

## Tests

An offline unit and integration test suite (no network, no credentials) lives in `tests/`. It uses
synthetic in-memory rasters and STAC items to check:

- nodata handling and masked resampling;
- quality metrics and thresholds, and the handling of ESA masks;
- selection order and rejection reasons;
- an end-to-end run with resume.

```bash
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

---

## Data source

Contains modified Copernicus Sentinel data, provided by the
[Copernicus Data Space Ecosystem](https://dataspace.copernicus.eu/).
