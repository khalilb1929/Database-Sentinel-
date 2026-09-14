#!/usr/bin/env python3
"""
Sentinel-2 L1C time-series pipeline for the Copernicus Data Space Ecosystem (CDSE).

For a (lat, lon) point it:
  1. queries the CDSE STAC API (collection ``sentinel-2-l1c``) for a date range / cloud limit,
  2. keeps one best acquisition per day (full AOI coverage, native UTM zone, latest baseline),
  3. reads all 13 bands onto ONE fixed 1024 x 1024 px, 10 m grid in the local UTM zone
     (10 m bands copied 1:1, 20 m / 60 m bands bilinearly resampled),
  4. writes ``<YYYY-MM-DD>_<tile>.npz`` (key ``data``: uint16, shape (13, 1024, 1024))
     and ``metadata.csv``; re-running the same command resumes where it stopped.

Data access modes (``--access``)
  s3     Recommended. Windowed streaming reads of the band JP2s straight from the CDSE
         object store through GDAL /vsis3/ (only the AOI region is decoded).
         export CDSE_S3_ACCESS_KEY=...  CDSE_S3_SECRET_KEY=...
         (generate keys at https://eodata-s3keysmanager.dataspace.copernicus.eu)
  https  Downloads each band JP2 through the OData "Nodes" endpoint with an OAuth2 token,
         crops it, deletes it. Heavier (~0.5-1 GB transferred per acquisition).
         export CDSE_USERNAME=...  CDSE_PASSWORD=...
  auto   s3 if the S3 keys are set, https otherwise.
"""
from __future__ import annotations

import argparse
import csv
import logging
import math
import os
import re
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterator

import numpy as np
import rasterio
import requests
from pyproj import Transformer
from pystac_client import Client
from rasterio.crs import CRS
from rasterio.enums import Resampling
from rasterio.errors import RasterioIOError, WindowError
from rasterio.transform import Affine
from rasterio.warp import reproject, transform_bounds
from rasterio.windows import Window, from_bounds
from requests.adapters import HTTPAdapter
from shapely import make_valid
from shapely.geometry import box, mapping, shape
from shapely.ops import transform as shp_transform
from urllib3.util.retry import Retry

LOG = logging.getLogger("s2l1c")

STAC_URL = "https://stac.dataspace.copernicus.eu/v1"
STAC_COLLECTION = "sentinel-2-l1c"
TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
ODATA_CATALOGUE = "https://catalogue.dataspace.copernicus.eu/odata/v1"
ODATA_DOWNLOAD = "https://download.dataspace.copernicus.eu/odata/v1"
S3_ENDPOINT = "eodata.dataspace.copernicus.eu"

S2_FIRST_DATE = "2015-06-23"
DEFAULT_END_DATE = "2026-08-31"

# Output band order (tensor axis 0) -> native ground sampling distance in metres.
BANDS: dict[str, int] = {
    "B01": 60, "B02": 10, "B03": 10, "B04": 10, "B05": 20, "B06": 20, "B07": 20,
    "B08": 10, "B8A": 20, "B09": 60, "B10": 60, "B11": 20, "B12": 20,
}
NODATA = 0
GRID_SIZE_PX = 1024
GRID_RES_M = 10.0
# S2 tile origins are multiples of 60 m, so snapping the AOI corner to 60 m puts it on the
# 10 m, 20 m and 60 m pixel lattices at once.
GRID_SNAP_M = 60.0
# Processing baseline 04.00+ (Jan 2022, and the reprocessed Collection-1 archive) adds a
# +1000 DN radiometric offset.
BASELINE_OFFSET_FROM = 4.0
BOA_OFFSET_DN = 1000

OUTPUT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{2}[A-Z]{3})\.npz$")
METADATA_FIELDS = [
    "filename", "date", "tile_id", "cloud_cover",
    "item_id", "datetime_utc", "platform", "processing_baseline",
    "source_crs", "aoi_coverage", "valid_fraction", "harmonized",
]


@dataclass
class Acquisition:
    """One Sentinel-2 L1C granule, reduced to what the pipeline needs."""

    item_id: str
    datetime_utc: str
    date: str
    tile_id: str
    cloud_cover: float
    platform: str
    baseline: float
    source_epsg: int | None
    coverage: float
    assets: dict[str, tuple[str | None, str | None]]  # band -> (s3 href, https href)
    product_uuid: str | None = None

    @property
    def filename(self) -> str:
        return f"{self.date}_{self.tile_id}.npz"


# --------------------------------------------------------------------------------------
# CDSE access
# --------------------------------------------------------------------------------------
class CDSEClient:
    """Catalogue search (STAC) and authenticated band access (S3 or OData over HTTPS)."""

    def __init__(self, access: str, stac_url: str = STAC_URL, collection: str = STAC_COLLECTION,
                 timeout: float = 120.0, max_retries: int = 5):
        self.access = access
        self.stac_url = stac_url
        self.collection = collection
        self.timeout = timeout
        self.max_retries = max_retries

        self.session = requests.Session()
        retry = Retry(total=max_retries, backoff_factor=2,
                      status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods=frozenset({"GET", "POST"}))
        self.session.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=32))

        self._lock = threading.Lock()
        self._token: str | None = None
        self._token_expiry = 0.0
        self._refresh_token: str | None = None
        self._refresh_expiry = 0.0

    # ---- credentials -----------------------------------------------------------------
    @staticmethod
    def _env(name: str) -> str:
        value = os.environ.get(name)
        if not value:
            raise RuntimeError(f"Environment variable {name} is required for --access "
                               f"{'s3' if 'S3' in name else 'https'}")
        return value

    def prepare_access(self) -> None:
        """Validate credentials and configure GDAL before any data is read."""
        if self.access == "s3":
            # rasterio.Env refuses AWS credentials as config options, but GDAL reads them
            # from the process environment. Set once here, before any worker thread starts.
            os.environ.update({
                "AWS_ACCESS_KEY_ID": self._env("CDSE_S3_ACCESS_KEY"),
                "AWS_SECRET_ACCESS_KEY": self._env("CDSE_S3_SECRET_KEY"),
                "AWS_S3_ENDPOINT": S3_ENDPOINT,
                "AWS_HTTPS": "YES",
                "AWS_VIRTUAL_HOSTING": "FALSE",
            })
        else:
            self._get_token()
        LOG.info("Data access mode: %s", self.access)

    def _get_token(self) -> str:
        """Return a valid OAuth2 access token (CDSE tokens live 10 min; refresh or re-login)."""
        with self._lock:
            now = time.time()
            if self._token and now < self._token_expiry - 60:
                return self._token
            grants = []
            if self._refresh_token and now < self._refresh_expiry - 60:
                grants.append({"grant_type": "refresh_token", "refresh_token": self._refresh_token})
            grants.append({"grant_type": "password",
                           "username": self._env("CDSE_USERNAME"),
                           "password": self._env("CDSE_PASSWORD")})
            for grant in grants:
                resp = self.session.post(TOKEN_URL, data={**grant, "client_id": "cdse-public"},
                                         timeout=self.timeout)
                if resp.ok:
                    break
            if not resp.ok:
                raise RuntimeError(f"CDSE authentication failed (HTTP {resp.status_code}): "
                                   f"{resp.text[:200]}")
            payload = resp.json()
            self._token = payload["access_token"]
            self._token_expiry = now + float(payload.get("expires_in", 600))
            self._refresh_token = payload.get("refresh_token")
            self._refresh_expiry = now + float(payload.get("refresh_expires_in", 3600))
            return self._token

    def _invalidate_token(self) -> None:
        with self._lock:
            self._token = None

    # ---- catalogue -------------------------------------------------------------------
    def search(self, aoi_lonlat, start: date, end: date, max_cloud: float) -> list:
        catalog = Client.open(self.stac_url, timeout=self.timeout)
        query = dict(
            collections=[self.collection],
            intersects=mapping(aoi_lonlat),
            datetime=f"{start.isoformat()}T00:00:00Z/{end.isoformat()}T23:59:59.999Z",
            sortby=[{"field": "properties.datetime", "direction": "asc"}],
            limit=100,
        )
        if max_cloud < 100:
            query["filter"] = {"op": "<=", "args": [{"property": "eo:cloud_cover"}, max_cloud]}
            query["filter_lang"] = "cql2-json"
        LOG.info("Querying %s (%s, %s -> %s, cloud <= %g%%)",
                 self.stac_url, self.collection, start, end, max_cloud)
        items = []
        for item in catalog.search(**query).items():
            items.append(item)
            if len(items) % 500 == 0:
                LOG.info("  ... %d items fetched", len(items))
        LOG.info("STAC search returned %d granules", len(items))
        return items

    # ---- data access -----------------------------------------------------------------
    @staticmethod
    def gdal_options() -> dict[str, str]:
        return {
            "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
            "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".jp2",
            "GDAL_HTTP_MAX_RETRY": "5",
            "GDAL_HTTP_RETRY_DELAY": "3",
            "VSI_CACHE": "TRUE",
            "GDAL_NUM_THREADS": "ALL_CPUS",
        }

    @contextmanager
    def open_band(self, acq: Acquisition, band: str, workdir: Path) -> Iterator[rasterio.DatasetReader]:
        if self.access == "s3":
            s3_href = acq.assets[band][0]
            if not s3_href:
                raise LookupError(f"{acq.item_id}: no S3 href for {band}")
            with rasterio.open("/vsis3/" + s3_href.removeprefix("s3://")) as src:
                yield src
        else:
            local = workdir / f"{acq.item_id}_{band}.jp2"
            self.download(self.https_url(acq, band), local)
            try:
                with rasterio.open(local) as src:
                    yield src
            finally:
                local.unlink(missing_ok=True)

    def https_url(self, acq: Acquisition, band: str) -> str:
        s3_href, https_href = acq.assets[band]
        if https_href:
            return https_href
        # Fallback: rebuild the OData Nodes URL from the S3 object key.
        match = re.search(r"/([^/]+\.SAFE)/(.+)$", s3_href or "")
        if not match:
            raise LookupError(f"{acq.item_id}: cannot derive an HTTPS URL for {band}")
        if acq.product_uuid is None:
            acq.product_uuid = self._odata_product_id(match.group(1))
        nodes = "/".join(f"Nodes({part})" for part in [match.group(1), *match.group(2).split("/")])
        return f"{ODATA_DOWNLOAD}/Products({acq.product_uuid})/{nodes}/$value"

    def _odata_product_id(self, safe_name: str) -> str:
        resp = self.session.get(f"{ODATA_CATALOGUE}/Products",
                                params={"$filter": f"Name eq '{safe_name}'", "$top": 1},
                                timeout=self.timeout)
        resp.raise_for_status()
        values = resp.json().get("value", [])
        if not values:
            raise LookupError(f"OData: product {safe_name} not found")
        return values[0]["Id"]

    def _authorized_get(self, url: str) -> requests.Response:
        """Streamed GET with a bearer token. Redirects are followed manually because CDSE
        redirects across hosts and requests strips the Authorization header when it does."""
        for _ in range(10):
            resp = self.session.get(url, headers={"Authorization": f"Bearer {self._get_token()}"},
                                    stream=True, allow_redirects=False, timeout=self.timeout)
            if resp.is_redirect:
                url = requests.compat.urljoin(url, resp.headers["Location"])
                resp.close()
                continue
            if resp.status_code == 401:
                resp.close()
                self._invalidate_token()
                raise requests.HTTPError("401 Unauthorized (token invalidated)", response=resp)
            resp.raise_for_status()
            return resp
        raise requests.TooManyRedirects(url)

    def download(self, url: str, dest: Path, chunk_size: int = 8 << 20) -> Path:
        part = dest.with_name(dest.name + ".part")
        for attempt in range(1, self.max_retries + 1):
            try:
                with self._authorized_get(url) as resp:
                    expected = int(resp.headers.get("Content-Length", 0))
                    written = 0
                    with open(part, "wb") as fh:
                        for block in resp.iter_content(chunk_size):
                            fh.write(block)
                            written += len(block)
                if expected and written != expected:
                    raise OSError(f"truncated download ({written}/{expected} bytes)")
                os.replace(part, dest)
                return dest
            except (requests.RequestException, OSError) as exc:
                part.unlink(missing_ok=True)
                if attempt == self.max_retries:
                    raise
                wait = min(60, 2 ** attempt)
                LOG.warning("Download failed (%s), retry %d/%d in %ds",
                            exc, attempt, self.max_retries, wait)
                time.sleep(wait)
        raise AssertionError("unreachable")


# --------------------------------------------------------------------------------------
# Spatial framing
# --------------------------------------------------------------------------------------
class SpatialCropper:
    """Owns the fixed UTM target grid shared by every acquisition and resamples bands onto it."""

    def __init__(self, lat: float, lon: float, size_px: int = GRID_SIZE_PX,
                 resolution: float = GRID_RES_M, snap: float = GRID_SNAP_M):
        if not -80.0 <= lat <= 84.0:
            raise ValueError("Latitude must be within [-80, 84] (UTM domain)")
        if not -180.0 <= lon <= 180.0:
            raise ValueError("Longitude must be within [-180, 180]")
        self.size = size_px
        self.res = resolution

        zone = min(int((lon + 180.0) // 6.0) + 1, 60)
        self.epsg = (32600 if lat >= 0 else 32700) + zone
        self.crs = CRS.from_epsg(self.epsg)
        self._to_utm = Transformer.from_crs("EPSG:4326", self.epsg, always_xy=True)
        to_lonlat = Transformer.from_crs(self.epsg, "EPSG:4326", always_xy=True)

        cx, cy = self._to_utm.transform(lon, lat)
        extent = size_px * resolution
        self.ulx = round((cx - extent / 2) / snap) * snap
        self.uly = round((cy + extent / 2) / snap) * snap
        self.transform = Affine(resolution, 0.0, self.ulx, 0.0, -resolution, self.uly)
        self.bounds = (self.ulx, self.uly - extent, self.ulx + extent, self.uly)
        self.aoi_utm = box(*self.bounds)
        # Densified edges so the lon/lat polygon follows the projected square exactly.
        self.aoi_lonlat = shp_transform(to_lonlat.transform, self.aoi_utm.segmentize(resolution * 64))
        self.center_shift_m = math.hypot(self.ulx + extent / 2 - cx, self.uly - extent / 2 - cy)

    def describe(self) -> str:
        return (f"EPSG:{self.epsg}, {self.size}x{self.size} px @ {self.res:g} m "
                f"({self.size * self.res / 1000:.2f} km), bounds={tuple(round(b) for b in self.bounds)}, "
                f"grid centre {self.center_shift_m:.1f} m from requested point")

    def coverage(self, geometry_lonlat: dict) -> float:
        """Fraction of the AOI inside a granule's valid-data footprint."""
        try:
            footprint = make_valid(shp_transform(self._to_utm.transform, shape(geometry_lonlat)))
            return footprint.intersection(self.aoi_utm).area / self.aoi_utm.area
        except Exception:  # degenerate / antimeridian geometries
            return 0.0

    def _on_lattice(self, t: Affine) -> bool:
        def integral(v: float) -> bool:
            return abs(v - round(v)) < 1e-6
        return (abs(t.a - self.res) < 1e-6 and abs(t.e + self.res) < 1e-6
                and integral((self.ulx - t.c) / self.res) and integral((self.uly - t.f) / self.res))

    def read_band(self, src: rasterio.DatasetReader, native_res: int) -> np.ndarray:
        """Read only the source pixels under the AOI and warp them onto the target grid."""
        out = np.full((self.size, self.size), NODATA, dtype=np.uint16)
        left, bottom, right, top = transform_bounds(self.crs, src.crs, *self.bounds, densify_pts=21)
        win = from_bounds(left, bottom, right, top, transform=src.transform)
        pad = 2  # extra source pixels so the bilinear kernel has context at the AOI border
        c0, r0 = math.floor(win.col_off) - pad, math.floor(win.row_off) - pad
        c1 = math.ceil(win.col_off + win.width) + pad
        r1 = math.ceil(win.row_off + win.height) + pad
        try:
            win = Window(c0, r0, c1 - c0, r1 - r0).intersection(Window(0, 0, src.width, src.height))
        except WindowError:
            return out  # AOI entirely outside this raster
        source = src.read(1, window=win)

        same_grid = (src.crs.to_epsg() == self.epsg and native_res == self.res
                     and self._on_lattice(src.transform))
        reproject(
            source=source, destination=out,
            src_transform=src.window_transform(win), src_crs=src.crs, src_nodata=NODATA,
            dst_transform=self.transform, dst_crs=self.crs, dst_nodata=NODATA,
            resampling=Resampling.nearest if same_grid else Resampling.bilinear,
        )
        return out


def harmonize_to_legacy_scale(data: np.ndarray) -> np.ndarray:
    """Remove the +1000 DN offset of baseline >= 04.00 (values < 1000 clip to 0)."""
    return np.maximum(data, BOA_OFFSET_DN) - BOA_OFFSET_DN


# --------------------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------------------
class PipelineRunner:
    def __init__(self, args: argparse.Namespace, client: CDSEClient, cropper: SpatialCropper):
        self.args = args
        self.client = client
        self.cropper = cropper
        self.out_dir: Path = args.output_dir
        self.meta_path = self.out_dir / "metadata.csv"

    # ---- selection -------------------------------------------------------------------
    def _parse_item(self, item) -> Acquisition | None:
        props = item.properties
        missing = [b for b in BANDS if b not in item.assets]
        if missing:
            LOG.warning("Skipping %s: missing assets %s", item.id, missing)
            return None

        tile = (props.get("grid:code") or "").removeprefix("MGRS-")
        if not re.fullmatch(r"\d{2}[A-Z]{3}", tile):
            match = re.search(r"_T(\d{2}[A-Z]{3})_", item.id)
            if not match:
                LOG.warning("Skipping %s: tile id not found", item.id)
                return None
            tile = match.group(1)

        if props.get("processing:version"):
            baseline = float(props["processing:version"])
        else:
            match = re.search(r"_N(\d{2})(\d{2})_", item.id)
            baseline = float(f"{match.group(1)}.{match.group(2)}") if match else 0.0

        proj_code = item.assets["B02"].extra_fields.get("proj:code") or props.get("proj:code")
        epsg = int(proj_code.split(":")[-1]) if proj_code else props.get("proj:epsg")

        assets = {}
        for band in BANDS:
            asset = item.assets[band]
            alt = asset.extra_fields.get("alternate", {}).get("https", {}).get("href")
            s3 = asset.href if asset.href.startswith("s3://") else None
            https = alt or (asset.href if asset.href.startswith("https://") else None)
            assets[band] = (s3, https)

        dt = item.datetime or item.common_metadata.start_datetime
        return Acquisition(
            item_id=item.id,
            datetime_utc=dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            date=dt.strftime("%Y-%m-%d"),
            tile_id=tile,
            cloud_cover=float(props.get("eo:cloud_cover", 100.0)),
            platform=props.get("platform", ""),
            baseline=baseline,
            source_epsg=epsg,
            coverage=self.cropper.coverage(item.geometry),
            assets=assets,
            product_uuid=props.get("_private", {}).get("product_uuid"),
        )

    def select(self, items: list) -> list[Acquisition]:
        args = self.args
        # 1) Reprocessing duplicates: same tile & day -> keep the latest processing baseline.
        latest: dict[tuple[str, str], Acquisition] = {}
        for item in items:
            acq = self._parse_item(item)
            if acq is None or acq.cloud_cover > args.max_cloud:
                continue
            key = (acq.date, acq.tile_id)
            if key not in latest or (acq.baseline, acq.item_id) > (latest[key].baseline, latest[key].item_id):
                latest[key] = acq

        # 2) Overlapping tiles on the same day -> one granule per date.
        def rank(a: Acquisition):
            return (-round(a.coverage, 3), a.source_epsg != self.cropper.epsg, a.cloud_cover, a.item_id)

        per_date: dict[str, Acquisition] = {}
        rejected = 0
        for acq in latest.values():
            if acq.coverage < args.min_coverage:
                rejected += 1
                continue
            if acq.date not in per_date or rank(acq) < rank(per_date[acq.date]):
                per_date[acq.date] = acq
        if rejected:
            LOG.info("%d granules dropped: AOI coverage < %.0f%%", rejected, args.min_coverage * 100)

        chosen = list(per_date.values())
        if args.sort == "cloud":
            chosen.sort(key=lambda a: (a.cloud_cover, a.date))
        else:
            chosen.sort(key=lambda a: a.date)
        if args.max_images:
            chosen = chosen[: args.max_images]
        chosen.sort(key=lambda a: a.date)
        LOG.info("%d unique acquisition dates selected", len(chosen))
        return chosen

    # ---- disk state ------------------------------------------------------------------
    def _existing_outputs(self) -> dict[str, str]:
        for stale in self.out_dir.glob(".*.part"):
            stale.unlink(missing_ok=True)
        return {m.group(1): p.name for p in self.out_dir.iterdir()
                if (m := OUTPUT_RE.match(p.name))}

    def _load_metadata(self) -> dict[str, dict]:
        if not self.meta_path.exists():
            return {}
        with open(self.meta_path, newline="") as fh:
            return {row["filename"]: row for row in csv.DictReader(fh)}

    def _write_metadata(self, rows: dict[str, dict]) -> None:
        tmp = self.meta_path.with_name(".metadata.csv.part")
        with open(tmp, "w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=METADATA_FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(sorted(rows.values(), key=lambda r: r["filename"]))
        os.replace(tmp, self.meta_path)

    def _metadata_row(self, acq: Acquisition, filename: str, valid_fraction: float | None) -> dict:
        same_granule = filename == acq.filename
        return {
            "filename": filename,
            "date": acq.date,
            "tile_id": filename[11:16],
            "cloud_cover": acq.cloud_cover if same_granule else "",
            "item_id": acq.item_id if same_granule else "",
            "datetime_utc": acq.datetime_utc if same_granule else "",
            "platform": acq.platform if same_granule else "",
            "processing_baseline": f"{acq.baseline:.2f}" if same_granule else "",
            "source_crs": f"EPSG:{acq.source_epsg}" if same_granule and acq.source_epsg else "",
            "aoi_coverage": f"{acq.coverage:.4f}" if same_granule else "",
            "valid_fraction": "" if valid_fraction is None else f"{valid_fraction:.4f}",
            "harmonized": int(self.args.harmonize and acq.baseline >= BASELINE_OFFSET_FROM) if same_granule else "",
        }

    def _save(self, acq: Acquisition, data: np.ndarray) -> None:
        dest = self.out_dir / acq.filename
        tmp = self.out_dir / f".{acq.filename}.part"
        with open(tmp, "wb") as fh:  # file handle: numpy won't append a second ".npz"
            np.savez_compressed(
                fh,
                data=data,
                bands=np.array(list(BANDS)),
                transform=np.array(tuple(self.cropper.transform)[:6]),  # Affine (a, b, c, d, e, f)
                crs=np.array(f"EPSG:{self.cropper.epsg}"),
            )
        os.replace(tmp, dest)

    # ---- processing ------------------------------------------------------------------
    def _read_band(self, acq: Acquisition, band: str, workdir: Path) -> np.ndarray:
        for attempt in range(1, self.args.retries + 1):
            try:
                with rasterio.Env(**self.client.gdal_options()):
                    with self.client.open_band(acq, band, workdir) as src:
                        return self.cropper.read_band(src, BANDS[band])
            except (RasterioIOError, requests.RequestException, OSError) as exc:
                if attempt == self.args.retries:
                    raise
                LOG.warning("%s %s: read failed (%s), retry %d/%d",
                            acq.date, band, exc, attempt, self.args.retries)
                time.sleep(5 * attempt)
        raise AssertionError("unreachable")

    def process(self, acq: Acquisition) -> np.ndarray:
        data = np.empty((len(BANDS), self.cropper.size, self.cropper.size), dtype=np.uint16)
        with tempfile.TemporaryDirectory(prefix="s2l1c_") as tmp:
            def work(job: tuple[int, str]) -> None:
                idx, band = job
                data[idx] = self._read_band(acq, band, Path(tmp))

            with ThreadPoolExecutor(max_workers=self.args.workers) as pool:
                list(pool.map(work, enumerate(BANDS)))
        if self.args.harmonize and acq.baseline >= BASELINE_OFFSET_FROM:
            data = harmonize_to_legacy_scale(data)
        return data

    def run(self) -> int:
        LOG.info("Target grid: %s", self.cropper.describe())
        items = self.client.search(self.cropper.aoi_lonlat, self.args.start_date,
                                   self.args.end_date, self.args.max_cloud)
        selection = self.select(items)

        if self.args.dry_run:
            print(f"{'date':<11} {'tile':<6} {'cloud%':>6} {'cover':>6} {'base':>5} {'crs':>10}  item_id")
            for a in selection:
                print(f"{a.date:<11} {a.tile_id:<6} {a.cloud_cover:>6.2f} {a.coverage:>6.3f} "
                      f"{a.baseline:>5.2f} {('EPSG:' + str(a.source_epsg)):>10}  {a.item_id}")
            return 0
        if not selection:
            LOG.warning("Nothing to process.")
            return 0

        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.client.prepare_access()
        existing = self._existing_outputs()
        metadata = self._load_metadata()
        done = skipped = failed = 0

        for i, acq in enumerate(selection, 1):
            tag = f"[{i}/{len(selection)}] {acq.date} T{acq.tile_id}"
            if acq.date in existing:
                filename = existing[acq.date]
                if filename not in metadata:
                    metadata[filename] = self._metadata_row(acq, filename, None)
                    self._write_metadata(metadata)
                skipped += 1
                LOG.info("%s already on disk (%s), skipping", tag, filename)
                continue

            t0 = time.time()
            try:
                data = self.process(acq)
                valid = float(np.any(data != NODATA, axis=0).mean())
                self._save(acq, data)
                metadata[acq.filename] = self._metadata_row(acq, acq.filename, valid)
                self._write_metadata(metadata)
                done += 1
                LOG.info("%s saved (cloud %.1f%%, valid %.1f%%, %.0fs)",
                         tag, acq.cloud_cover, 100 * valid, time.time() - t0)
                if valid < 0.99:
                    LOG.warning("%s: only %.1f%% of the AOI holds data", tag, 100 * valid)
            except Exception as exc:  # keep the time series going; failures are retried on re-run
                failed += 1
                LOG.error("%s failed: %s", tag, exc, exc_info=LOG.isEnabledFor(logging.DEBUG))

        LOG.info("Finished: %d downloaded, %d already present, %d failed -> %s",
                 done, skipped, failed, self.out_dir)
        return 1 if failed else 0


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------
def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid date '{value}', expected YYYY-MM-DD")


def _positive_int(value: str) -> int:
    ivalue = int(value)
    if ivalue < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return ivalue


def _fraction(value: str) -> float:
    fvalue = float(value)
    if not 0.0 <= fvalue <= 1.0:
        raise argparse.ArgumentTypeError("must be within [0, 1]")
    return fvalue


def _percent(value: str) -> float:
    fvalue = float(value)
    if not 0.0 <= fvalue <= 100.0:
        raise argparse.ArgumentTypeError("must be within [0, 100]")
    return fvalue


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Download a Sentinel-2 L1C (13-band, 1024x1024 px @ 10 m, UTM-aligned) "
                    "time series from the Copernicus Data Space Ecosystem.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    g = p.add_argument_group("query")
    g.add_argument("--lat", type=float, required=True, help="AOI centre latitude (WGS84)")
    g.add_argument("--lon", type=float, required=True, help="AOI centre longitude (WGS84)")
    g.add_argument("--start_date", type=_iso_date, default=S2_FIRST_DATE, help="first day (inclusive)")
    g.add_argument("--end_date", type=_iso_date, default=DEFAULT_END_DATE, help="last day (inclusive)")
    g.add_argument("--max_images", type=_positive_int, default=None,
                   help="cap on acquisition dates (default: all). Already-downloaded dates count toward it")
    g.add_argument("--max_cloud", type=_percent, default=100.0, help="max granule cloud cover (%%)")
    g.add_argument("--sort", choices=("date", "cloud"), default="date",
                   help="how --max_images picks dates: earliest first, or clearest first")
    g.add_argument("--min_coverage", type=_fraction, default=0.99,
                   help="min fraction of the AOI inside the granule's data footprint")

    g = p.add_argument_group("output / processing")
    g.add_argument("--output_dir", type=Path, required=True, help="directory for .npz files and metadata.csv")
    g.add_argument("--harmonize", action="store_true",
                   help="subtract the +1000 DN offset of processing baseline >= 04.00")
    g.add_argument("--dry_run", action="store_true", help="list the selected acquisitions and exit")

    g = p.add_argument_group("access / runtime")
    g.add_argument("--access", choices=("auto", "s3", "https"), default="auto",
                   help="s3: CDSE_S3_ACCESS_KEY/CDSE_S3_SECRET_KEY; https: CDSE_USERNAME/CDSE_PASSWORD")
    g.add_argument("--workers", type=_positive_int, default=4, help="bands read in parallel per acquisition")
    g.add_argument("--retries", type=_positive_int, default=3, help="attempts per band read")
    g.add_argument("--stac_url", default=STAC_URL)
    g.add_argument("--collection", default=STAC_COLLECTION)
    g.add_argument("--log_level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))

    args = p.parse_args(argv)
    if args.start_date > args.end_date:
        p.error("--start_date must be <= --end_date")
    if args.access == "auto":
        has_s3 = os.environ.get("CDSE_S3_ACCESS_KEY") and os.environ.get("CDSE_S3_SECRET_KEY")
        args.access = "s3" if has_s3 else "https"
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    try:
        cropper = SpatialCropper(args.lat, args.lon)
        client = CDSEClient(access=args.access, stac_url=args.stac_url, collection=args.collection)
        return PipelineRunner(args, client, cropper).run()
    except KeyboardInterrupt:
        LOG.warning("Interrupted. Re-run the same command to resume.")
        return 130
    except Exception as exc:
        LOG.error("Fatal: %s", exc, exc_info=args.log_level == "DEBUG")
        return 2


if __name__ == "__main__":
    sys.exit(main())
