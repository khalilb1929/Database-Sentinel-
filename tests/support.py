"""Offline fixtures shared by the test modules (no network, no credentials)."""
from contextlib import contextmanager
from datetime import datetime, timezone

import numpy as np
import pystac
from rasterio.io import MemoryFile
from shapely.geometry import mapping

from s2_l1c_pipeline import BANDS, SpatialCropper, parse_args

PARIS = (48.8566, 2.3522)


def small_cropper(size_px: int = 4) -> SpatialCropper:
    return SpatialCropper(*PARIS, size_px=size_px, resolution=10, snap=60)


def cli_args(output_dir, *extra: str):
    """Namespace built by the real CLI parser, with a 1 px edge suited to tiny grids."""
    return parse_args(["--lat", str(PARIS[0]), "--lon", str(PARIS[1]), "--access", "s3",
                       "--output_dir", str(output_dir), "--edge_width", "1", "--retries", "1",
                       *extra])


@contextmanager
def raster(array, transform, crs, nodata=None, mask=None):
    """In-memory GeoTIFF opened for reading."""
    array = np.asarray(array)
    if array.ndim == 2:
        array = array[None]
    profile = dict(driver="GTiff", height=array.shape[1], width=array.shape[2],
                   count=array.shape[0], dtype=str(array.dtype), crs=crs,
                   transform=transform, nodata=nodata)
    with MemoryFile() as memory:
        with memory.open(**profile) as dataset:
            dataset.write(array)
            if mask is not None:
                dataset.write_mask(mask)
        with memory.open() as source:
            yield source


def stac_item(cropper: SpatialCropper, name: str, day: int, cloud: float = 5.0,
              baseline: str = "05.10", tile: str = "31UDQ", epsg: int = 32631,
              bands=tuple(BANDS), geometry=None) -> pystac.Item:
    """A CDSE-shaped sentinel-2-l1c STAC item (band assets only, as on CDSE)."""
    geometry = geometry or mapping(cropper.aoi_lonlat.buffer(0.01))
    item_id = f"S2A_MSIL1C_202301{day:02d}T110401_N{baseline.replace('.', '')}_R094_T{tile}_{name}"
    item = pystac.Item(
        id=item_id, geometry=geometry, bbox=None,
        datetime=datetime(2023, 1, day, 11, 4, 1, tzinfo=timezone.utc),
        properties={
            "eo:cloud_cover": cloud, "grid:code": f"MGRS-{tile}", "processing:version": baseline,
            "platform": "sentinel-2a", "proj:code": f"EPSG:{epsg}", "sat:relative_orbit": 94,
            "sat:orbit_state": "descending", "view:sun_elevation": 20.5,
            "_private": {"product_uuid": "00000000-0000-0000-0000-000000000000"},
        },
    )
    granule = (f"s3://eodata/Sentinel-2/MSI/L1C_N0500/2023/01/{day:02d}/{item_id}.SAFE"
               f"/GRANULE/L1C_T{tile}_A039559_20230101T110358")
    for band in bands:
        item.add_asset(band, pystac.Asset(href=f"{granule}/IMG_DATA/T{tile}_20230101T110401_{band}.jp2",
                                          media_type="image/jp2"))
    return item
