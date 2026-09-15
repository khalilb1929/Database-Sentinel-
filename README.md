# Sentinel-2 L1C Time-Series Pipeline

Imagine you want a **photo album of one place seen from space**, one picture for every day the
Sentinel-2 satellite flew over it. This pipeline builds that album automatically from the
[Copernicus Data Space Ecosystem (CDSE)](https://dataspace.copernicus.eu/).

---

## What happens inside the pipeline

It works in 4 steps.

### 1. Search (discovery)

You give a point (latitude, longitude) and two dates. The pipeline asks the Copernicus catalogue:
"Which Sentinel-2 pictures exist here between these two dates?"

It writes **every** answer in a notebook (`metadata.csv`), even the ones it will throw away later.

### 2. Sort (selection)

It removes what is not useful:

- **duplicates**: the same picture reprocessed several times by ESA, it keeps the most recent version;
- pictures that are **too cloudy**;
- pictures that **do not cover your area** well enough;
- if there are several pictures on the same day, it keeps **the best one**.

For every picture it throws away, it writes **why** in the notebook.

### 3. Check (quality control)

For the pictures it keeps, it downloads a **10 km × 10 km** square (1024 × 1024 pixels, 10 m per
pixel), in the satellite's **13 colours** (bands). Then it looks at every pixel:

- "Is there really data here?" (otherwise it is a hole, called *nodata*);
- "Is it a cloud? A damaged pixel? Too bright (saturated)?"

If the picture has too many holes, clouds or defects, it is **rejected**.

### 4. Store (output)

Pictures that pass the check are saved as `.npz` files, one per date. The `metadata.csv` notebook says
for every picture whether it was **saved**, **rejected**, **failed** or **skipped** (already there).

All pictures sit on **exactly the same grid**: pixel (500, 500) is the same spot on the ground on
every date.

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

### Step 2: look before downloading

A call is one command saying **where**, **when**, and **which folder to store things in**. Add
`--dry_run` to only see the list of dates, without downloading anything:

```bash
.venv\Scripts\python.exe s2_l1c_pipeline.py --lat 48.8566 --lon 2.3522 --start_date 2023-01-01 --end_date 2023-12-31 --max_cloud 20 --output_dir data/paris_2023 --dry_run
```

You get something like:

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

Expect about 30 seconds per date. If it stops halfway (network cut, laptop closed), **run exactly the
same command again**: the pipeline picks up where it left off.

### Step 4: open your images

```python
import numpy as np

z = np.load("data/paris_2023/2023-02-07_31UDQ.npz")
image = z["data"]        # the 13 bands: shape (13, 1024, 1024)
good = z["valid_mask"]   # True = reliable pixel, False = hole or doubtful pixel
bands = z["bands"].tolist()

red = image[bands.index("B04")]
green = image[bands.index("B03")]
blue = image[bands.index("B02")]
```

To find out why a date is missing, open `data/paris_2023/metadata.csv` (in Excel, for example) and look
at the `status` and `rejection_reason` columns.

### More example calls

The 20 clearest images between 2020 and 2024 in Toulouse:

```bash
.venv\Scripts\python.exe s2_l1c_pipeline.py --lat 43.6047 --lon 1.4442 --start_date 2020-01-01 --end_date 2024-12-31 --max_cloud 20 --max_images 20 --sort cloud --output_dir data/toulouse_top20
```

Images that are really cloud-free **over your area** (rough sort, then fine sort):

```bash
.venv\Scripts\python.exe s2_l1c_pipeline.py --lat 48.8566 --lon 2.3522 --start_date 2023-01-01 --end_date 2023-12-31 --max_cloud 30 --max_cloud_fraction 0.05 --output_dir data/paris_2023_clear
```

---

## The parameters you care about

### The essentials

| Parameter | What it is | Example |
|---|---|---|
| `--lat`, `--lon` | The centre of your area | `--lat 48.8566 --lon 2.3522` (Paris) |
| `--start_date`, `--end_date` | Your 2 dates (inclusive), format `YYYY-MM-DD` | `--start_date 2023-01-01 --end_date 2023-12-31` |
| `--output_dir` | The folder where files are stored | `--output_dir data/paris_2023` |

### Choosing how many pictures and which ones

| Parameter | What it is | Tip |
|---|---|---|
| `--max_cloud` | Maximum cloud % over the whole satellite tile (100 × 100 km) | `20` for fairly clear images |
| `--max_images` | Maximum number of dates | `--max_images 20` |
| `--sort` | With `--max_images`: `date` takes the earliest dates, `cloud` the clearest | `--sort cloud` |
| `--dry_run` | Only look, do not download | Always do this first |

### Quality (already set by default, change only if needed)

| Parameter | Default | What it is |
|---|---|---|
| `--min_valid_fraction` | `0.99` | At least 99 % of pixels must be good in all 13 bands |
| `--max_cloud_fraction` | `1.0` (off) | Maximum cloud % **inside your 10 km square**, more precise than `--max_cloud`. Example: `0.05` = 5 % |
| `--max_artefact_fraction` | `0.05` | Maximum % of pixels damaged by the sensor |
| `--max_saturated_fraction` | `0.01` | Maximum % of pixels that are too bright |
| `--harmonize` | off | Removes the +1000 offset ESA has added to the values since 2022. Useful to compare with older data |

**Tip:** for images that are really cloud-free over your area, combine `--max_cloud 30` (rough, fast
sort) with `--max_cloud_fraction 0.05` (fine sort on your square).

To see **all** parameters:

```bash
.venv\Scripts\python.exe s2_l1c_pipeline.py --help
```

---

## Data source

Contains modified Copernicus Sentinel data, provided by the
[Copernicus Data Space Ecosystem](https://dataspace.copernicus.eu/).
