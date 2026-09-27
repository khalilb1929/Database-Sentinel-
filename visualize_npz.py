#!/usr/bin/env python3
"""
Quick-look figures for the pipeline outputs (``<date>_<tile>.npz``).

For each file, writes next to it (or in --out_dir):
  <stem>_overview.png  true colour, false colour (NIR), NDVI, and a 1.28 km zoom comparing
                       a native 10 m band with bands resampled from 20 m and 60 m
  <stem>_bands.png     the 13 spectral bands, each with its own contrast stretch

Usage:
  python visualize_npz.py data/test/2023-06-04_31UDQ.npz
  python visualize_npz.py data/paris_2023            # every .npz in the folder
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

NATIVE_RES_M = {"B01": 60, "B02": 10, "B03": 10, "B04": 10, "B05": 20, "B06": 20, "B07": 20,
                "B08": 10, "B8A": 20, "B09": 60, "B10": 60, "B11": 20, "B12": 20}
WAVELENGTH_NM = {"B01": 443, "B02": 490, "B03": 560, "B04": 665, "B05": 705, "B06": 740,
                 "B07": 783, "B08": 842, "B8A": 865, "B09": 945, "B10": 1375, "B11": 1610,
                 "B12": 2190}
BASELINE_OFFSET_DN = 1000  # added by ESA from processing baseline 04.00 onwards
ZOOM_PX = 128


def metadata_row(npz_path: Path) -> dict:
    meta = npz_path.parent / "metadata.csv"
    if not meta.exists():
        return {}
    with open(meta, newline="") as fh:
        return next((row for row in csv.DictReader(fh) if row["filename"] == npz_path.name), {})


def dn_offset(row: dict, choice: str) -> int:
    if choice != "auto":
        return int(choice)
    if not row:
        print("  metadata.csv row not found: assuming no radiometric offset (use --offset 1000 if needed)")
        return 0
    baseline = float(row.get("processing_baseline") or 0)
    harmonized = str(row.get("harmonized", "0")).strip() in ("1", "True", "true")
    return BASELINE_OFFSET_DN if baseline >= 4.0 and not harmonized else 0


def stretch(img: np.ndarray, lo: float = 2, hi: float = 98) -> np.ndarray:
    """Percentile stretch to [0, 1] (joint over all channels, so colours stay balanced)."""
    finite = img[np.isfinite(img)]
    if finite.size == 0:
        return np.zeros_like(img)
    p_lo, p_hi = np.percentile(finite, [lo, hi])
    return np.clip((img - p_lo) / max(p_hi - p_lo, 1e-9), 0, 1)


def composite(refl: np.ndarray, bands: list[str], names: tuple[str, str, str]) -> np.ndarray:
    rgb = np.stack([refl[bands.index(n)] for n in names], axis=-1)
    return np.nan_to_num(stretch(rgb), nan=0.0)


def render(npz_path: Path, out_dir: Path, offset_choice: str) -> list[Path]:
    z = np.load(npz_path)
    data, bands = z["data"], z["bands"].tolist()
    row = metadata_row(npz_path)
    offset = dn_offset(row, offset_choice)

    # Top-of-atmosphere reflectance; DN 0 = no data.
    refl = (data.astype(np.float32) - offset) / 10000.0
    refl[data == 0] = np.nan

    title = f"{npz_path.name}  ·  {z['crs']}  ·  10 m grid, 1024×1024 px (10.24 km)"
    if row:
        title += f"\n{row.get('platform', '')}  {row.get('datetime_utc', '')}  ·  cloud cover {row.get('cloud_cover', '?')}%"
    title += f"  ·  reflectance = (DN − {offset}) / 10000"

    # ---- overview ----------------------------------------------------------------------
    fig, axes = plt.subplots(2, 3, figsize=(15, 10.5), constrained_layout=True)
    fig.suptitle(title, fontsize=11)

    axes[0, 0].imshow(composite(refl, bands, ("B04", "B03", "B02")))
    axes[0, 0].set_title("True colour (B04, B03, B02)")
    axes[0, 1].imshow(composite(refl, bands, ("B08", "B04", "B03")))
    axes[0, 1].set_title("False colour (B08 NIR, B04, B03)\nvegetation appears red")

    nir, red = refl[bands.index("B08")], refl[bands.index("B04")]
    with np.errstate(invalid="ignore", divide="ignore"):
        ndvi = (nir - red) / (nir + red)
    im = axes[0, 2].imshow(ndvi, cmap="RdYlGn", vmin=-0.2, vmax=0.9)
    axes[0, 2].set_title(f"NDVI = (B08 − B04) / (B08 + B04)\nmedian {np.nanmedian(ndvi):.2f}")
    fig.colorbar(im, ax=axes[0, 2], fraction=0.046, pad=0.02)

    size = data.shape[-1]
    r0 = c0 = (size - ZOOM_PX) // 2
    crop = np.s_[r0:r0 + ZOOM_PX, c0:c0 + ZOOM_PX]
    for ax, band in zip(axes[1], ("B04", "B05", "B01")):
        res = NATIVE_RES_M[band]
        how = "native, copied 1:1" if res == 10 else f"native {res} m → bilinear to 10 m"
        ax.imshow(stretch(refl[bands.index(band)][crop]), cmap="gray", interpolation="nearest")
        ax.set_title(f"Zoom {ZOOM_PX * 10 / 1000:.2f} km · {band} ({how})")

    for ax in axes.flat:
        ax.set_xticks([])
        ax.set_yticks([])
    for ax in axes[0]:
        ax.add_patch(plt.Rectangle((c0, r0), ZOOM_PX, ZOOM_PX, fill=False, ec="cyan", lw=1.2))

    overview = out_dir / f"{npz_path.stem}_overview.png"
    fig.savefig(overview, dpi=110)
    plt.close(fig)

    # ---- all bands ---------------------------------------------------------------------
    fig, axes = plt.subplots(3, 5, figsize=(17, 11), constrained_layout=True)
    fig.suptitle(title + "\nEach band is stretched independently (2–98 % percentiles)", fontsize=11)
    for ax in axes.flat:
        ax.axis("off")
    for ax, band in zip(axes.flat, bands):
        img = refl[bands.index(band)]
        ax.imshow(stretch(img), cmap="gray")
        ax.set_title(f"{band} · {WAVELENGTH_NM[band]} nm · native {NATIVE_RES_M[band]} m\n"
                     f"median reflectance {np.nanmedian(img):.3f}", fontsize=9)

    band_sheet = out_dir / f"{npz_path.stem}_bands.png"
    fig.savefig(band_sheet, dpi=90)
    plt.close(fig)
    return [overview, band_sheet]


def main() -> None:
    p = argparse.ArgumentParser(description="Quick-look PNGs for pipeline .npz outputs.")
    p.add_argument("paths", nargs="+", type=Path, help=".npz files or folders containing them")
    p.add_argument("--out_dir", type=Path, default=None, help="default: next to each .npz")
    p.add_argument("--offset", choices=("auto", "0", "1000"), default="auto",
                   help="DN offset to remove; auto reads processing_baseline/harmonized from metadata.csv")
    args = p.parse_args()

    files = []
    for path in args.paths:
        files += sorted(path.glob("*.npz")) if path.is_dir() else [path]
    if not files:
        raise SystemExit("No .npz file found.")
    for f in files:
        out_dir = args.out_dir or f.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        print(f"{f.name}:")
        for png in render(f, out_dir, args.offset):
            print(f"  -> {png}")


if __name__ == "__main__":
    main()
