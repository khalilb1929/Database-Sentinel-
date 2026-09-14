#!/usr/bin/env python3
"""
Sentinel-2 L1C time-series pipeline for the Copernicus Data Space Ecosystem (CDSE).

For a (lat, lon) point it runs four separate stages:
  A. discovery        queries the CDSE STAC API (collection ``sentinel-2-l1c``) and parses every
                      returned granule, including those that will be rejected later;
  B. selection        keeps the latest processing baseline per (day, tile), applies the scene
                      cloud cover and AOI coverage limits, then picks the best tile per date;
  C. quality control  reads all 13 bands onto ONE fixed 1024 x 1024 px, 10 m grid in the local
                      UTM zone (10 m bands copied 1:1, 20 m / 60 m bands bilinearly resampled)
                      with a per-band pixel validity mask and the L1C QI_DATA masks, computes
                      per-acquisition / per-band metrics and applies configurable thresholds;
  D. output           writes ``<YYYY-MM-DD>_<tile>.npz`` for accepted acquisitions only, and one
                      ``metadata.csv`` row per granule (saved / rejected / failed / skipped) with
                      the stage and reason of each decision. Re-running the same command resumes.

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
import json
import logging
import math
import os
import re
import sys
import tempfile
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Iterator, NamedTuple, TypeVar

import numpy as np
import rasterio
import requests
from dotenv import load_dotenv
from pyproj import Transformer
from pystac_client import Client
from rasterio.crs import CRS
from rasterio.enums import MaskFlags, Resampling
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

load_dotenv()

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
# L1C reserves DN 65535 for saturated samples.
SATURATED_DN = 65535
GRID_SIZE_PX = 1024
GRID_RES_M = 10.0
# S2 tile origins are multiples of 60 m, so snapping the AOI corner to 60 m puts it on the
# 10 m, 20 m and 60 m pixel lattices at once.
GRID_SNAP_M = 60.0
# Processing baseline 04.00+ (Jan 2022, and the reprocessed Collection-1 archive) adds a
# +1000 DN radiometric offset.
BASELINE_OFFSET_FROM = 4.0
BOA_OFFSET_DN = 1000

# Pixel quality masks. Since baseline 04.00 every L1C granule ships raster masks in QI_DATA/
# (older baselines only have GML vectors). CDSE does not expose them as STAC assets, so their
# paths are derived from the band hrefs. Layouts checked on CDSE products (baseline 05.10):
#   MSK_CLASSI_B00.jp2   60 m, 3 uint8 layers (0/1): opaque clouds, cirrus, snow/ice
#   MSK_DETFOO_<band>    band resolution, 1 layer: detector index, 0 = no detector
#   MSK_QUALIT_<band>    band resolution, 8 uint8 layers (0/1), in QUALIT_LAYERS order
# L1C has no cloud-shadow mask, so shadow_fraction is always left blank.
QI_RASTER_MASKS_FROM = 4.0
CLASSI_MASK = "MSK_CLASSI_B00"
CLASSI_LAYERS = ("opaque_cloud", "cirrus", "snow_ice")
QUALIT_LAYERS = ("ancillary_lost", "ancillary_degraded", "msi_lost", "msi_degraded",
                 "defective", "nodata", "crosstalk_partially_corrected", "saturated_l1a")


def _bits(*names: str) -> int:
    return sum(1 << QUALIT_LAYERS.index(name) for name in names)


QUALIT_NODATA_BITS = _bits("msi_lost", "nodata")
QUALIT_ARTEFACT_BITS = _bits("ancillary_lost", "ancillary_degraded", "msi_degraded", "defective")
QUALIT_SATURATED_BITS = _bits("saturated_l1a")

OUTPUT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{2}[A-Z]{3})\.npz$")
METADATA_FIELDS = [
    # identification (first columns unchanged for existing readers)
    "filename", "date", "tile_id", "cloud_cover",
    "item_id", "datetime_utc", "platform", "processing_baseline",
    "source_crs", "aoi_coverage",
    # pipeline decision
    "status", "stage", "rejection_reason",
    # acquisition geometry / provenance parsed from STAC
    "relative_orbit", "orbit_state", "sun_elevation", "sun_azimuth", "view_incidence_angle",
    "datatake_id", "processing_datetime",
    # pixel quality control
    "valid_fraction_any_band", "valid_fraction_all_bands",
    "valid_fraction_by_band", "nodata_fraction_by_band", "edge_nodata_fraction",
    "per_band_min", "per_band_max", "per_band_p02", "per_band_p50", "per_band_p98",
    "cloud_fraction", "opaque_cloud_fraction", "cirrus_fraction", "snow_ice_fraction",
    "shadow_fraction", "artefact_fraction", "saturation_fraction", "saturation_fraction_by_band",
    "quality_flag_fractions", "mask_source_by_band", "quality_assets", "quality_mask_status",
    "qc_config", "harmonized", "stac_properties",
]
# STAC properties that describe access, not the acquisition.
_STAC_PROPERTIES_IGNORED = {"_private", "auth:schemes", "storage:schemes"}

T = TypeVar("T")


@dataclass
class Acquisition:
    """One Sentinel-2 L1C granule and the pipeline's decision about it."""

    item_id: str
    datetime_utc: str
    date: str
    tile_id: str
    cloud_cover: float | None
    platform: str
    baseline: float
    source_epsg: int | None
    coverage: float | None
    assets: dict[str, tuple[str | None, str | None]]  # band -> (s3 href, https href)
    product_uuid: str | None = None
    quality_assets: dict[str, tuple[str | None, str | None]] = field(default_factory=dict)
    relative_orbit: int | None = None
    orbit_state: str = ""
    sun_elevation: float | None = None
    sun_azimuth: float | None = None
    view_incidence_angle: float | None = None
    datatake_id: str = ""
    processing_datetime: str = ""
    properties: dict = field(default_factory=dict)
    # discovered -> selected -> saved | rejected | failed | skipped
    status: str = "discovered"
    stage: str = "discovery"
    rejection_reason: str = ""

    @property
    def filename(self) -> str:
        return f"{self.date}_{self.tile_id}.npz"

    def reject(self, stage: str, reason: str) -> None:
        self.status, self.stage, self.rejection_reason = "rejected", stage, reason


class DiscoveryError(ValueError):
    """A STAC item that cannot become a processable Acquisition; the message is the reason."""


class BandRead(NamedTuple):
    data: np.ndarray    # (H, W) uint16, NODATA wherever ``valid`` is False
    valid: np.ndarray   # (H, W) bool, trustworthy sample
    nodata: np.ndarray  # (H, W) bool, no source sample at all
    mask_source: str    # which validity sources were applied, e.g. "fallback_nodata_0"


class MaskRead(NamedTuple):
    layers: np.ndarray   # (L, H, W) uint8, nearest-neighbour
    covered: np.ndarray  # (H, W) bool, inside the mask raster


@dataclass
class AcquisitionData:
    """Everything read for one acquisition, before any radiometric harmonisation."""

    data: np.ndarray                          # (13, H, W) uint16 raw DN, NODATA where invalid
    valid: np.ndarray                         # (13, H, W) bool
    nodata: np.ndarray                        # (13, H, W) bool
    mask_source: dict[str, str] = field(default_factory=dict)
    qualit_bits: np.ndarray | None = None     # (13, H, W) uint8, bit i = QUALIT_LAYERS[i]
    qualit_available: np.ndarray | None = None  # (13,) bool, MSK_QUALIT read for that band
    classi: np.ndarray | None = None          # (3, H, W) uint8, CLASSI_LAYERS
    quality_mask_status: str = "unavailable"  # available | partial | unavailable
    quality_errors: list[str] = field(default_factory=list)


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
    def search(self, aoi_lonlat, start: date, end: date) -> list:
        """Return every granule intersecting the AOI. Cloud cover is deliberately NOT filtered
        server-side: selection rejects cloudy scenes itself so that they appear in metadata.csv."""
        catalog = Client.open(self.stac_url, timeout=self.timeout)
        query = dict(
            collections=[self.collection],
            intersects=mapping(aoi_lonlat),
            datetime=f"{start.isoformat()}T00:00:00Z/{end.isoformat()}T23:59:59.999Z",
            sortby=[{"field": "properties.datetime", "direction": "asc"}],
            limit=100,
        )
        LOG.info("Querying %s (%s, %s -> %s)", self.stac_url, self.collection, start, end)
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

    @staticmethod
    def _hrefs(acq: Acquisition, name: str) -> tuple[str | None, str | None]:
        hrefs = acq.assets.get(name) or acq.quality_assets.get(name)
        if hrefs is None:
            raise LookupError(f"{acq.item_id}: no asset {name}")
        return hrefs

    @contextmanager
    def open_band(self, acq: Acquisition, band: str, workdir: Path) -> Iterator[rasterio.DatasetReader]:
        """Open a band or a QI_DATA mask (``band`` is the asset name)."""
        if self.access == "s3":
            s3_href = self._hrefs(acq, band)[0]
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
        s3_href, https_href = self._hrefs(acq, band)
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

    # A resampled pixel is valid only if its kernel support is (numerically) all valid.
    SUPPORT_TOLERANCE = 1e-3

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
        """Fraction of the AOI inside a granule's STAC footprint (geometry only, not pixel validity)."""
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

    def _source_window(self, src: rasterio.DatasetReader) -> Window | None:
        """Source pixels under the AOI plus a kernel margin, or None if the AOI misses the raster."""
        left, bottom, right, top = transform_bounds(self.crs, src.crs, *self.bounds, densify_pts=21)
        win = from_bounds(left, bottom, right, top, transform=src.transform)
        pad = 2  # extra source pixels so the bilinear kernel has context at the AOI border
        c0, r0 = math.floor(win.col_off) - pad, math.floor(win.row_off) - pad
        c1 = math.ceil(win.col_off + win.width) + pad
        r1 = math.ceil(win.row_off + win.height) + pad
        try:
            win = Window(c0, r0, c1 - c0, r1 - r0).intersection(Window(0, 0, src.width, src.height))
        except WindowError:
            return None
        return win if win.width > 0 and win.height > 0 else None

    def _warp(self, source: np.ndarray, src: rasterio.DatasetReader, win: Window,
              resampling: Resampling, src_nodata: float | None = None) -> np.ndarray:
        """Warp a (…, h, w) window array onto the target grid; uncovered pixels stay 0."""
        out = np.zeros(source.shape[:-2] + (self.size, self.size), dtype=source.dtype)
        reproject(
            source=source, destination=out,
            src_transform=src.window_transform(win), src_crs=src.crs, src_nodata=src_nodata,
            dst_transform=self.transform, dst_crs=self.crs, dst_nodata=src_nodata,
            resampling=resampling,
        )
        return out

    @staticmethod
    def _source_validity(src: rasterio.DatasetReader, source: np.ndarray,
                         win: Window) -> tuple[np.ndarray, str]:
        """Per-sample validity of a source window, from (in priority order) the raster mask,
        the declared nodata value, or the ``NODATA = 0`` fallback."""
        valid = np.ones(source.shape, dtype=bool)
        applied = []
        flags = set(src.mask_flag_enums[0])
        if flags & {MaskFlags.per_dataset, MaskFlags.alpha}:
            valid &= src.read_masks(1, window=win) > 0
            applied.append("raster_mask")
        if src.nodata is not None:
            nodata = src.nodata
            valid &= ~(np.isnan(source) if np.isnan(nodata) else source == nodata)
            applied.append("src_nodata")
        else:
            # Sentinel-2 L1C JP2s declare neither mask nor nodata but reserve DN 0 for no data.
            valid &= source != NODATA
            applied.append("fallback_nodata_0")
        return valid, "+".join(applied)

    def read_band(self, src: rasterio.DatasetReader, native_res: int) -> BandRead:
        """Read one band onto the target grid together with its pixel validity.

        ``nodata``: no source sample backs the target pixel (raster mask, declared nodata or the
        0 fallback, or the pixel lies outside the AOI / raster intersection).
        ``valid``: not nodata AND every source sample in the resampling kernel is valid AND the
        warped value is not nodata. A 20/60 m pixel bordering a hole is therefore invalid (but
        not nodata) instead of being interpolated from partial support. Validity is strictly
        per band: other bands never make a pixel valid.
        """
        win = self._source_window(src)
        if win is None:
            nodata = np.ones((self.size, self.size), dtype=bool)
            return BandRead(np.full(nodata.shape, NODATA, dtype=np.uint16), ~nodata, nodata,
                            "outside_raster")
        source = src.read(1, window=win)
        source_valid, mask_source = self._source_validity(src, source, win)
        source = np.where(source_valid, source, NODATA).astype(np.uint16, copy=False)

        same_grid = (src.crs.to_epsg() == self.epsg and native_res == self.res
                     and self._on_lattice(src.transform))
        resampling = Resampling.nearest if same_grid else Resampling.bilinear
        data = self._warp(source, src, win, resampling, src_nodata=NODATA)
        observed = self._warp(source_valid.astype(np.uint8), src, win, Resampling.nearest) > 0
        support = self._warp(source_valid.astype(np.float32), src, win, resampling)

        valid = observed & (support >= 1.0 - self.SUPPORT_TOLERANCE) & (data != NODATA)
        data[~valid] = NODATA
        return BandRead(data, valid, ~observed, mask_source)

    def read_mask(self, src: rasterio.DatasetReader) -> MaskRead:
        """Nearest-neighbour read of every layer of a categorical / flag mask (0 is a real value)."""
        layers = np.zeros((src.count, self.size, self.size), dtype=np.uint8)
        win = self._source_window(src)
        if win is None:
            return MaskRead(layers, np.zeros((self.size, self.size), dtype=bool))
        source = src.read(window=win).astype(np.uint8, copy=False)
        layers = self._warp(source, src, win, Resampling.nearest)
        covered = self._warp(np.ones(source.shape[1:], dtype=np.uint8), src, win, Resampling.nearest) > 0
        return MaskRead(layers, covered)


def harmonize_to_legacy_scale(data: np.ndarray) -> np.ndarray:
    """Remove the +1000 DN offset of baseline >= 04.00 (values < 1000 clip to 0)."""
    return np.maximum(data, BOA_OFFSET_DN) - BOA_OFFSET_DN


# --------------------------------------------------------------------------------------
# STAC parsing helpers
# --------------------------------------------------------------------------------------
def _float_or_none(value) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _processing_baseline(item_id: str, props: dict) -> float:
    if props.get("processing:version"):
        return float(props["processing:version"])
    match = re.search(r"_N(\d{2})(\d{2})_", item_id)
    return float(f"{match.group(1)}.{match.group(2)}") if match else 0.0


def _asset_hrefs(asset) -> tuple[str | None, str | None]:
    alt = asset.extra_fields.get("alternate", {}).get("https", {}).get("href")
    s3 = asset.href if asset.href.startswith("s3://") else None
    https = alt or (asset.href if asset.href.startswith("https://") else None)
    return s3, https


def quality_mask_assets(assets: dict[str, tuple[str | None, str | None]],
                        baseline: float) -> dict[str, tuple[str | None, str | None]]:
    """Derive the QI_DATA raster mask hrefs from the band hrefs (S3 only; the HTTPS URL is
    rebuilt from the S3 key when needed)."""
    if baseline < QI_RASTER_MASKS_FROM:
        return {}
    masks: dict[str, tuple[str | None, str | None]] = {}
    for band, (s3_href, _) in assets.items():
        if not s3_href or "/IMG_DATA/" not in s3_href:
            continue
        qi_dir = s3_href.rsplit("/IMG_DATA/", 1)[0] + "/QI_DATA/"
        masks.setdefault(CLASSI_MASK, (f"{qi_dir}{CLASSI_MASK}.jp2", None))
        masks[f"MSK_DETFOO_{band}"] = (f"{qi_dir}MSK_DETFOO_{band}.jp2", None)
        masks[f"MSK_QUALIT_{band}"] = (f"{qi_dir}MSK_QUALIT_{band}.jp2", None)
    return masks


def _csv_value(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True, separators=(",", ":")) if value else ""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, float):
        return "" if math.isnan(value) else round(value, 6)
    return value


def _fraction_of(mask: np.ndarray, denominator: np.ndarray | int) -> float | None:
    total = int(denominator.sum()) if isinstance(denominator, np.ndarray) else int(denominator)
    return float(mask.sum()) / total if total else None


# --------------------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------------------
class PipelineRunner:
    QC_CONFIG_KEYS = ("min_valid_fraction", "max_edge_nodata_fraction", "edge_width",
                      "max_saturated_fraction", "max_artefact_fraction", "max_cloud_fraction",
                      "require_quality_masks")

    def __init__(self, args: argparse.Namespace, client: CDSEClient, cropper: SpatialCropper):
        self.args = args
        self.client = client
        self.cropper = cropper
        self.out_dir: Path = args.output_dir
        self.meta_path = self.out_dir / "metadata.csv"
        self.acquisitions: list[Acquisition] = []

    # ---- A. discovery ----------------------------------------------------------------
    def _parse_item(self, item) -> Acquisition:
        props = item.properties
        missing = [b for b in BANDS if b not in item.assets]
        if missing:
            raise DiscoveryError(f"missing_band_assets:{','.join(missing)}")

        tile = (props.get("grid:code") or "").removeprefix("MGRS-")
        if not re.fullmatch(r"\d{2}[A-Z]{3}", tile):
            match = re.search(r"_T(\d{2}[A-Z]{3})_", item.id)
            if not match:
                raise DiscoveryError("tile_id_not_found")
            tile = match.group(1)

        dt = item.datetime or item.common_metadata.start_datetime
        if dt is None:
            raise DiscoveryError("datetime_not_found")

        baseline = _processing_baseline(item.id, props)
        proj_code = item.assets["B02"].extra_fields.get("proj:code") or props.get("proj:code")
        epsg = int(proj_code.split(":")[-1]) if proj_code else props.get("proj:epsg")
        assets = {band: _asset_hrefs(item.assets[band]) for band in BANDS}

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
            quality_assets=quality_mask_assets(assets, baseline),
            relative_orbit=props.get("sat:relative_orbit"),
            orbit_state=props.get("sat:orbit_state", ""),
            sun_elevation=_float_or_none(props.get("view:sun_elevation")),
            sun_azimuth=_float_or_none(props.get("view:sun_azimuth")),
            view_incidence_angle=_float_or_none(props.get("view:incidence_angle")),
            datatake_id=props.get("eopf:datatake_id", "") or props.get("s2:datatake_id", ""),
            processing_datetime=props.get("processing:datetime", ""),
            properties={k: v for k, v in props.items() if k not in _STAC_PROPERTIES_IGNORED},
        )

    def _unparsed_item(self, item, exc: Exception) -> Acquisition:
        """Best-effort record of an item that could not be parsed, so it is not lost."""
        props = getattr(item, "properties", None) or {}
        dt = getattr(item, "datetime", None)  # pystac moves "datetime" out of properties
        stamp = dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else str(props.get("start_datetime") or "")
        tile = re.search(r"_T(\d{2}[A-Z]{3})_", item.id)
        try:
            baseline = _processing_baseline(item.id, props)
        except ValueError:
            baseline = 0.0
        try:
            coverage = self.cropper.coverage(item.geometry) if item.geometry else None
        except Exception:
            coverage = None
        acq = Acquisition(
            item_id=item.id, datetime_utc=stamp, date=stamp[:10],
            tile_id=tile.group(1) if tile else "",
            cloud_cover=_float_or_none(props.get("eo:cloud_cover")),
            platform=props.get("platform", ""), baseline=baseline, source_epsg=None,
            coverage=coverage, assets={},
            properties={k: v for k, v in props.items() if k not in _STAC_PROPERTIES_IGNORED},
        )
        reason = str(exc) if isinstance(exc, DiscoveryError) else f"parse_error:{type(exc).__name__}: {exc}"
        acq.reject("discovery", reason)
        return acq

    def discover(self, items: list) -> list[Acquisition]:
        """A. Parse every STAC item. Unparseable items are kept as rejected acquisitions."""
        self.acquisitions = []
        for item in items:
            try:
                acq = self._parse_item(item)
            except Exception as exc:
                acq = self._unparsed_item(item, exc)
                LOG.warning("Discovery: %s rejected (%s)", item.id, acq.rejection_reason)
            self.acquisitions.append(acq)
        return self.acquisitions

    # ---- B. selection ----------------------------------------------------------------
    def select(self, items: list[Acquisition] | None = None) -> list[Acquisition]:
        """B. Choose at most one granule per date. Every discarded granule is marked rejected
        (stage ``selection``) with a reason; reasons are ``code`` or ``code:detail``."""
        args = self.args
        if items is not None:
            self.acquisitions = items
        candidates = [a for a in self.acquisitions if a.status == "discovered"]

        # 1) Reprocessing duplicates first (same tile & day -> latest processing baseline), so a
        #    later filter can never fall back to an outdated baseline of the same granule.
        groups: dict[tuple[str, str], list[Acquisition]] = defaultdict(list)
        for acq in candidates:
            groups[(acq.date, acq.tile_id)].append(acq)
        latest = []
        for group in groups.values():
            keep = max(group, key=lambda a: (a.baseline, a.item_id))
            for acq in group:
                if acq is not keep:
                    acq.reject("selection", f"older_processing_baseline:{keep.item_id}")
            latest.append(keep)

        # 2) Scene-level cloud cover and 3) geometric AOI coverage of the STAC footprint.
        eligible = []
        for acq in latest:
            if acq.cloud_cover is None or acq.cloud_cover > args.max_cloud:
                acq.reject("selection", "scene_cloud_cover_above_threshold")
            elif acq.coverage is None or acq.coverage < args.min_coverage:
                acq.reject("selection", "aoi_coverage_below_threshold")
            else:
                eligible.append(acq)

        # 4) Overlapping tiles on the same day -> best granule per date.
        def rank(a: Acquisition):
            return (-round(a.coverage, 3), a.source_epsg != self.cropper.epsg, a.cloud_cover, a.item_id)

        per_date: dict[str, list[Acquisition]] = defaultdict(list)
        for acq in eligible:
            per_date[acq.date].append(acq)
        chosen = []
        for group in per_date.values():
            best = min(group, key=rank)
            for acq in group:
                if acq is not best:
                    acq.reject("selection", f"inferior_overlapping_tile:{best.item_id}")
            chosen.append(best)

        # 5) --max_images cap.
        if args.sort == "cloud":
            chosen.sort(key=lambda a: (a.cloud_cover, a.date))
        else:
            chosen.sort(key=lambda a: a.date)
        if args.max_images:
            for acq in chosen[args.max_images:]:
                acq.reject("selection", "max_images_limit")
            chosen = chosen[: args.max_images]
        chosen.sort(key=lambda a: a.date)
        for acq in chosen:
            acq.status, acq.stage, acq.rejection_reason = "selected", "selection", ""
        self._log_rejections()
        LOG.info("%d unique acquisition dates selected", len(chosen))
        return chosen

    def _log_rejections(self) -> None:
        counts = Counter((a.stage, a.rejection_reason.split(":", 1)[0])
                         for a in self.acquisitions if a.status == "rejected")
        for (stage, code), n in sorted(counts.items()):
            LOG.info("  %d granules rejected at %s: %s", n, stage, code)

    # ---- C. quality control ----------------------------------------------------------
    def _read_asset(self, acq: Acquisition, name: str, workdir: Path,
                    reader: Callable[[rasterio.DatasetReader], T]) -> T:
        for attempt in range(1, self.args.retries + 1):
            try:
                with rasterio.Env(**self.client.gdal_options()):
                    with self.client.open_band(acq, name, workdir) as src:
                        return reader(src)
            except (RasterioIOError, requests.RequestException, OSError) as exc:
                if attempt == self.args.retries:
                    raise
                LOG.warning("%s %s: read failed (%s), retry %d/%d",
                            acq.date, name, exc, attempt, self.args.retries)
                time.sleep(5 * attempt)
        raise AssertionError("unreachable")

    def _optional_mask(self, acq: Acquisition, name: str, workdir: Path,
                       errors: list[str]) -> MaskRead | None:
        if name not in acq.quality_assets:
            return None
        try:
            return self._read_asset(acq, name, workdir, self.cropper.read_mask)
        except (LookupError, RasterioIOError, requests.RequestException, OSError) as exc:
            errors.append(f"{name}: {exc}")
            LOG.warning("%s %s: quality mask unavailable (%s)", acq.date, name, exc)
            return None

    def process(self, acq: Acquisition) -> AcquisitionData:
        """C1. Read the 13 bands, their validity masks and the QI_DATA masks (raw DN)."""
        n, size = len(BANDS), self.cropper.size
        data = np.zeros((n, size, size), dtype=np.uint16)
        valid = np.zeros((n, size, size), dtype=bool)
        nodata = np.ones((n, size, size), dtype=bool)
        qualit_bits = np.zeros((n, size, size), dtype=np.uint8)
        qualit_ok = np.zeros(n, dtype=bool)
        detfoo_ok = np.zeros(n, dtype=bool)
        mask_source: dict[str, str] = {}
        errors: list[str] = []

        with tempfile.TemporaryDirectory(prefix="s2l1c_") as tmp:
            workdir = Path(tmp)
            # Probe QI_DATA with the classification mask first: if it is unreachable, the 26
            # per-band masks are not attempted (each would burn its retries).
            classi = self._optional_mask(acq, CLASSI_MASK, workdir, errors)
            use_qi = classi is not None

            def work(job: tuple[int, str]) -> None:
                idx, band = job
                read = self._read_asset(acq, band, workdir,
                                        lambda src: self.cropper.read_band(src, BANDS[band]))
                band_nodata = read.nodata.copy()
                sources = [read.mask_source]
                if use_qi:
                    det = self._optional_mask(acq, f"MSK_DETFOO_{band}", workdir, errors)
                    if det is not None:
                        # Detector index 0: no MSI detector imaged this pixel in this band.
                        band_nodata |= det.covered & (det.layers[0] == 0)
                        detfoo_ok[idx] = True
                        sources.append("MSK_DETFOO")
                    qual = self._optional_mask(acq, f"MSK_QUALIT_{band}", workdir, errors)
                    if qual is not None:
                        for bit in range(min(qual.layers.shape[0], len(QUALIT_LAYERS))):
                            qualit_bits[idx] |= (qual.layers[bit] > 0).astype(np.uint8) << bit
                        band_nodata |= (qualit_bits[idx] & QUALIT_NODATA_BITS) > 0
                        qualit_ok[idx] = True
                        sources.append("MSK_QUALIT")
                band_valid = read.valid & ~band_nodata
                data[idx] = np.where(band_valid, read.data, NODATA)
                valid[idx], nodata[idx] = band_valid, band_nodata
                mask_source[band] = "+".join(sources)

            with ThreadPoolExecutor(max_workers=self.args.workers) as pool:
                list(pool.map(work, enumerate(BANDS)))

        n_read = int(detfoo_ok.sum() + qualit_ok.sum()) + (classi is not None)
        if not acq.quality_assets or n_read == 0:
            status = "unavailable"
        elif n_read == 2 * n + 1:
            status = "available"
        else:
            status = "partial"
        return AcquisitionData(
            data=data, valid=valid, nodata=nodata, mask_source=mask_source,
            qualit_bits=qualit_bits if qualit_ok.any() else None,
            qualit_available=qualit_ok if qualit_ok.any() else None,
            classi=classi.layers if classi is not None else None,
            quality_mask_status=status, quality_errors=errors,
        )

    def qc_config(self) -> str:
        return json.dumps({k: getattr(self.args, k) for k in self.QC_CONFIG_KEYS}, sort_keys=True)

    def _edge_ring(self, shape: tuple[int, int]) -> np.ndarray:
        width = min(self.args.edge_width, (min(shape) + 1) // 2)
        edge = np.zeros(shape, dtype=bool)
        edge[:width, :] = edge[-width:, :] = True
        edge[:, :width] = edge[:, -width:] = True
        return edge

    def quality_control(self, acq: Acquisition | None, result: AcquisitionData) -> dict:
        """C2. Pixel-level metrics and threshold checks, on raw DN (before harmonisation).

        Geometric STAC coverage is never used here: every fraction is computed from the
        per-band pixel masks. Returns a report whose ``rejection_reasons`` is empty when the
        acquisition is accepted.
        """
        args = self.args
        data, valid, nodata = result.data, result.valid, result.nodata
        bands = list(BANDS)
        valid_any, valid_all = valid.any(axis=0), valid.all(axis=0)
        nodata_any = nodata.any(axis=0)

        stats: dict[str, dict[str, float | None]] = {k: {} for k in ("min", "max", "p02", "p50", "p98")}
        for i, band in enumerate(bands):
            values = data[i][valid[i]]
            if values.size:
                p02, p50, p98 = np.percentile(values, [2, 50, 98])
                row = {"min": int(values.min()), "max": int(values.max()),
                       "p02": float(p02), "p50": float(p50), "p98": float(p98)}
            else:
                row = dict.fromkeys(stats)
            for key, value in row.items():
                stats[key][band] = value

        # Saturation: DN 65535 in the valid data, plus the L1A saturation flag when available.
        saturated = data == SATURATED_DN
        artefact_fraction = flag_fractions = None
        if result.qualit_bits is not None:
            bits = result.qualit_bits
            saturated |= (bits & QUALIT_SATURATED_BITS) > 0
            avail = result.qualit_available if result.qualit_available is not None else np.ones(len(bands), bool)
            artefact = ((bits[avail] & QUALIT_ARTEFACT_BITS) > 0) & valid[avail]
            artefact_fraction = _fraction_of(artefact, valid[avail]) or 0.0
            flag_fractions = {name: float(((bits[avail] >> bit) & 1).mean())
                              for bit, name in enumerate(QUALIT_LAYERS)}
        saturated &= valid
        saturation_by_band = {band: _fraction_of(saturated[i], valid[i]) for i, band in enumerate(bands)}

        cloud = {}
        if result.classi is not None:
            layer = {name: result.classi[i] > 0 for i, name in enumerate(CLASSI_LAYERS)
                     if i < result.classi.shape[0]}
            none = np.zeros(valid_any.shape, dtype=bool)
            opaque, cirrus = layer.get("opaque_cloud", none), layer.get("cirrus", none)
            cloud = {
                "cloud_fraction": _fraction_of((opaque | cirrus) & valid_any, valid_any),
                "opaque_cloud_fraction": _fraction_of(opaque & valid_any, valid_any),
                "cirrus_fraction": _fraction_of(cirrus & valid_any, valid_any),
                "snow_ice_fraction": (_fraction_of(layer["snow_ice"] & valid_any, valid_any)
                                      if "snow_ice" in layer else None),
            }

        report = {
            "valid_fraction_any_band": float(valid_any.mean()),
            "valid_fraction_all_bands": float(valid_all.mean()),
            "valid_fraction_by_band": {b: float(valid[i].mean()) for i, b in enumerate(bands)},
            "nodata_fraction_by_band": {b: float(nodata[i].mean()) for i, b in enumerate(bands)},
            "edge_nodata_fraction": float(nodata_any[self._edge_ring(nodata_any.shape)].mean()),
            "per_band_min": stats["min"], "per_band_max": stats["max"],
            "per_band_p02": stats["p02"], "per_band_p50": stats["p50"], "per_band_p98": stats["p98"],
            "cloud_fraction": cloud.get("cloud_fraction"),
            "opaque_cloud_fraction": cloud.get("opaque_cloud_fraction"),
            "cirrus_fraction": cloud.get("cirrus_fraction"),
            "snow_ice_fraction": cloud.get("snow_ice_fraction"),
            "shadow_fraction": None,  # no cloud-shadow mask exists in L1C products
            "artefact_fraction": artefact_fraction,
            "saturation_fraction": _fraction_of(saturated, valid) or 0.0,
            "saturation_fraction_by_band": saturation_by_band,
            "quality_flag_fractions": flag_fractions,
            "mask_source_by_band": result.mask_source,
            "quality_mask_status": result.quality_mask_status,
            "qc_config": self.qc_config(),
        }

        reasons = []
        if args.require_quality_masks and result.quality_mask_status != "available":
            reasons.append(f"quality_masks_{result.quality_mask_status}")
        if not valid_any.any():
            reasons.append("no_valid_pixels")
        if report["valid_fraction_all_bands"] < args.min_valid_fraction:
            reasons.append("valid_fraction_all_bands_below_threshold")
        if report["edge_nodata_fraction"] > args.max_edge_nodata_fraction:
            reasons.append("edge_nodata_fraction_above_threshold")
        if report["saturation_fraction"] > args.max_saturated_fraction:
            reasons.append("saturation_fraction_above_threshold")
        if artefact_fraction is not None and artefact_fraction > args.max_artefact_fraction:
            reasons.append("artefact_fraction_above_threshold")
        if report["cloud_fraction"] is not None and report["cloud_fraction"] > args.max_cloud_fraction:
            reasons.append("pixel_cloud_fraction_above_threshold")
        report["rejection_reasons"] = reasons
        return report

    # ---- D. output -------------------------------------------------------------------
    def _existing_outputs(self) -> dict[str, str]:
        for stale in self.out_dir.glob(".*.part"):
            stale.unlink(missing_ok=True)
        return {m.group(1): p.name for p in self.out_dir.iterdir()
                if (m := OUTPUT_RE.match(p.name))}

    def _load_metadata(self) -> dict[str, dict]:
        if not self.meta_path.exists():
            return {}
        rows = {}
        with open(self.meta_path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if not row.get("status"):  # metadata.csv from the pre-QC pipeline
                    row["status"] = "saved" if row.get("filename") else ""
                    row["stage"] = "output"
                    row.setdefault("valid_fraction_any_band", row.get("valid_fraction", ""))
                rows[self._metadata_key(row)] = row
        return rows

    def _write_metadata(self, rows: dict[str, dict]) -> None:
        tmp = self.meta_path.with_name(".metadata.csv.part")
        with open(tmp, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=METADATA_FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(sorted(rows.values(), key=lambda r: (r.get("date", ""), r.get("item_id", ""))))
        os.replace(tmp, self.meta_path)

    @staticmethod
    def _metadata_key(row: dict[str, str]) -> str:
        return row.get("item_id") or row.get("filename") or f"{row.get('date', '')}:{row.get('tile_id', '')}"

    def _metadata_row(self, acq: Acquisition, filename: str = "", report: dict | None = None) -> dict:
        report = report or {}
        row = {
            "filename": filename,
            "date": acq.date,
            "tile_id": acq.tile_id,
            "cloud_cover": acq.cloud_cover,
            "item_id": acq.item_id,
            "datetime_utc": acq.datetime_utc,
            "platform": acq.platform,
            "processing_baseline": f"{acq.baseline:.2f}" if acq.baseline else "",
            "source_crs": f"EPSG:{acq.source_epsg}" if acq.source_epsg else "",
            "aoi_coverage": None if acq.coverage is None else round(acq.coverage, 4),
            "status": acq.status,
            "stage": acq.stage,
            "rejection_reason": acq.rejection_reason,
            "relative_orbit": acq.relative_orbit,
            "orbit_state": acq.orbit_state,
            "sun_elevation": acq.sun_elevation,
            "sun_azimuth": acq.sun_azimuth,
            "view_incidence_angle": acq.view_incidence_angle,
            "datatake_id": acq.datatake_id,
            "processing_datetime": acq.processing_datetime,
            "quality_assets": ",".join(sorted({n.rsplit("_", 1)[0] for n in acq.quality_assets})),
            "harmonized": (int(self.args.harmonize and acq.baseline >= BASELINE_OFFSET_FROM)
                           if acq.status == "saved" else None),
            "stac_properties": acq.properties,
        }
        for key in METADATA_FIELDS:
            if key not in row and key in report:
                row[key] = report[key]
        return {key: _csv_value(row.get(key)) for key in METADATA_FIELDS}

    def _save(self, acq: Acquisition, result: AcquisitionData) -> None:
        data = result.data
        if self.args.harmonize and acq.baseline >= BASELINE_OFFSET_FROM:
            data = harmonize_to_legacy_scale(data)
        arrays = dict(
            data=data,
            valid_mask=result.valid,
            nodata_mask=result.nodata,
            valid_any=result.valid.any(axis=0),
            valid_all=result.valid.all(axis=0),
            bands=np.array(list(BANDS)),
            transform=np.array(tuple(self.cropper.transform)[:6]),  # Affine (a, b, c, d, e, f)
            crs=np.array(f"EPSG:{self.cropper.epsg}"),
        )
        if result.qualit_bits is not None:
            arrays["quality_bits"] = result.qualit_bits
            arrays["quality_bit_names"] = np.array(QUALIT_LAYERS)
        if result.classi is not None:
            arrays["classification_mask"] = result.classi
            arrays["classification_names"] = np.array(CLASSI_LAYERS)
        dest = self.out_dir / acq.filename
        tmp = self.out_dir / f".{acq.filename}.part"
        with open(tmp, "wb") as fh:  # file handle: numpy won't append a second ".npz"
            np.savez_compressed(fh, **arrays)
        os.replace(tmp, dest)

    # ---- driver ----------------------------------------------------------------------
    def run(self) -> int:
        LOG.info("Target grid: %s", self.cropper.describe())
        items = self.client.search(self.cropper.aoi_lonlat, self.args.start_date, self.args.end_date)
        self.discover(items)
        selection = self.select()

        if self.args.dry_run:
            print(f"{'date':<11} {'tile':<6} {'cloud%':>6} {'cover':>6} {'base':>5} {'crs':>10}  item_id")
            for a in selection:
                print(f"{a.date:<11} {a.tile_id:<6} {a.cloud_cover:>6.2f} {a.coverage:>6.3f} "
                      f"{a.baseline:>5.2f} {('EPSG:' + str(a.source_epsg)):>10}  {a.item_id}")
            return 0

        self.out_dir.mkdir(parents=True, exist_ok=True)
        metadata = self._load_metadata()
        existing = self._existing_outputs()
        for acq in self.acquisitions:
            previous = metadata.get(acq.item_id)
            if acq.status == "rejected":
                # A file saved by an earlier run with other settings stays documented.
                if not (previous and previous.get("status") == "saved"
                        and previous.get("filename") in existing.values()):
                    metadata[acq.item_id] = self._metadata_row(acq)
            elif previous is None:
                metadata[acq.item_id] = self._metadata_row(acq)  # 'selected' until processed
        self._write_metadata(metadata)
        if not selection:
            LOG.warning("Nothing to process.")
            return 0

        self.client.prepare_access()
        qc_config = self.qc_config()
        counts: Counter[str] = Counter()

        for i, acq in enumerate(selection, 1):
            tag = f"[{i}/{len(selection)}] {acq.date} T{acq.tile_id}"
            previous = metadata.get(acq.item_id, {})
            if acq.date in existing:
                filename = existing[acq.date]
                counts["skipped"] += 1
                if previous.get("status") == "saved" and previous.get("filename") == filename:
                    LOG.info("%s already saved (%s), skipping", tag, filename)
                    continue
                acq.status, acq.stage = "skipped", "output"
                acq.rejection_reason = f"output_exists_for_date:{filename}"
                metadata.pop(filename, None)  # legacy row keyed by filename only
                metadata[acq.item_id] = self._metadata_row(acq, filename)
                self._write_metadata(metadata)
                LOG.info("%s: %s already on disk, skipping", tag, filename)
                continue
            if (previous.get("status") == "rejected" and previous.get("stage") == "quality_control"
                    and previous.get("qc_config") == qc_config):
                counts["rejected"] += 1
                LOG.info("%s already rejected by quality control with the same thresholds (%s)",
                         tag, previous.get("rejection_reason"))
                continue

            t0 = time.time()
            stage = "quality_control"
            try:
                result = self.process(acq)
                report = self.quality_control(acq, result)
                if report["rejection_reasons"]:
                    acq.reject("quality_control", ";".join(report["rejection_reasons"]))
                    metadata[acq.item_id] = self._metadata_row(acq, report=report)
                    self._write_metadata(metadata)
                    counts["rejected"] += 1
                    LOG.warning("%s rejected by quality control: %s", tag, acq.rejection_reason)
                    continue
                stage = "output"
                self._save(acq, result)
                acq.status, acq.stage, acq.rejection_reason = "saved", "output", ""
                metadata[acq.item_id] = self._metadata_row(acq, acq.filename, report)
                self._write_metadata(metadata)
                counts["saved"] += 1
                LOG.info("%s saved (cloud %.1f%%, valid all bands %.1f%%, masks %s, %.0fs)",
                         tag, acq.cloud_cover, 100 * report["valid_fraction_all_bands"],
                         result.quality_mask_status, time.time() - t0)
            except Exception as exc:  # keep the time series going; failures are retried on re-run
                acq.status, acq.stage = "failed", stage
                acq.rejection_reason = f"{type(exc).__name__}: {exc}"
                metadata[acq.item_id] = self._metadata_row(acq)
                self._write_metadata(metadata)
                counts["failed"] += 1
                LOG.error("%s failed: %s", tag, exc, exc_info=LOG.isEnabledFor(logging.DEBUG))

        LOG.info("Finished: %d saved, %d rejected by quality control, %d already present, "
                 "%d failed -> %s", counts["saved"], counts["rejected"], counts["skipped"],
                 counts["failed"], self.out_dir)
        return 1 if counts["failed"] else 0


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
    g = p.add_argument_group("query / selection")
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
                   help="min fraction of the AOI inside the granule's STAC footprint (geometry only)")

    g = p.add_argument_group("quality control")
    g.add_argument("--min_valid_fraction", type=_fraction, default=0.99,
                   help="min fraction of AOI pixels valid in all 13 bands")
    g.add_argument("--max_edge_nodata_fraction", type=_fraction, default=0.20,
                   help="max fraction of the AOI border (--edge_width px) that is nodata in any band")
    g.add_argument("--edge_width", type=_positive_int, default=32,
                   help="border width in pixels used for edge nodata QC")
    g.add_argument("--max_saturated_fraction", type=_fraction, default=0.01,
                   help="max fraction of valid band samples that are saturated")
    g.add_argument("--max_artefact_fraction", type=_fraction, default=0.05,
                   help="max fraction of valid band samples flagged defective / degraded by MSK_QUALIT "
                        "(ignored when the mask is unavailable)")
    g.add_argument("--max_cloud_fraction", type=_fraction, default=1.0,
                   help="max fraction of observed pixels flagged opaque cloud or cirrus by MSK_CLASSI "
                        "(ignored when the mask is unavailable)")
    g.add_argument("--require_quality_masks", action="store_true",
                   help="reject acquisitions whose QI_DATA masks could not all be read")

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
