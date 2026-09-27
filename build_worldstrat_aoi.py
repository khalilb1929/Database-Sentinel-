#!/usr/bin/env python3
"""
Build the area-of-interest (AOI) list from the WorldStrat dataset metadata.

WorldStrat (Cornebise et al., NeurIPS 2021 Datasets & Benchmarks; CC-BY-4.0, Zenodo record
6810792) ships a stratified worldwide sample of AOIs. This script downloads its two small
metadata files (~15 MB, the imagery archives are NOT needed), reduces them to one row per
AOI centre, and writes a CSV that ``run_sites.py`` can feed to the Sentinel-2 pipeline.

    python build_worldstrat_aoi.py                     # -> sites/worldstrat_aoi.csv
    python build_worldstrat_aoi.py --out sites/my.csv --cache .cache

Output columns: site_id, lat, lon, source, ipcc_class, lccs_class, smod_class, split.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pandas as pd
import requests

ZENODO_RECORD = "6810792"
ZENODO_FILES = {
    "metadata.csv": f"https://zenodo.org/api/records/{ZENODO_RECORD}/files/metadata.csv/content",
    "stratified_train_val_test_split.csv":
        f"https://zenodo.org/api/records/{ZENODO_RECORD}/files/"
        f"stratified_train_val_test_split.csv/content",
}
# AOI identifiers look like "Landcover-743-0", "UNHCR-NGAs003476", "ASMSpotter-1-1-1",
# "Amnesty POI-10-1-1"; the leading token is the sampling source.
SOURCES = ("Landcover", "UNHCR", "ASMSpotter", "Amnesty")
SOURCE_RE = re.compile(rf"^({'|'.join(SOURCES)})", re.IGNORECASE)
CANONICAL_SOURCE = {s.lower(): s for s in SOURCES}


def fetch(cache: Path) -> dict[str, Path]:
    cache.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name, url in ZENODO_FILES.items():
        dest = cache / name
        if not dest.exists():
            print(f"downloading {name} ...", flush=True)
            with requests.get(url, stream=True, timeout=300) as r:
                r.raise_for_status()
                tmp = dest.with_suffix(dest.suffix + ".part")
                with open(tmp, "wb") as fh:
                    for chunk in r.iter_content(1 << 20):
                        fh.write(chunk)
                tmp.replace(dest)
        paths[name] = dest
    return paths


def build(cache: Path) -> pd.DataFrame:
    files = fetch(cache)
    # One row per (AOI, low-resolution acquisition) -> keep one row per AOI.
    meta = pd.read_csv(files["metadata.csv"], index_col=0)
    aoi = meta.groupby(level=0).first()

    split = pd.read_csv(files["stratified_train_val_test_split.csv"], index_col=0)
    split = split.drop_duplicates(subset="tile").set_index("tile")

    out = pd.DataFrame({
        "site_id": aoi.index,
        "lat": aoi["lat"].round(6),
        "lon": aoi["lon"].round(6),
        "source": [CANONICAL_SOURCE[m.group(1).lower()] if (m := SOURCE_RE.match(str(i)))
                   else "Other" for i in aoi.index],
        "ipcc_class": aoi["IPCC Class"],
        "lccs_class": aoi["LCCS class"],
        "smod_class": aoi["SMOD Class"],
        "split": split["split"].reindex(aoi.index).fillna("unassigned").values,
    })
    out = out.dropna(subset=["lat", "lon"])
    bad = out[(out.lat.abs() > 90) | (out.lon.abs() > 180)]
    if len(bad):
        print(f"dropping {len(bad)} AOIs with out-of-range coordinates", file=sys.stderr)
        out = out.drop(bad.index)
    return out.sort_values("site_id").reset_index(drop=True)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", type=Path, default=Path("sites/worldstrat_aoi.csv"))
    p.add_argument("--cache", type=Path, default=Path(".cache/worldstrat"))
    args = p.parse_args()

    df = build(args.cache)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(args.out, index=False)

    print(f"\n{len(df)} AOIs -> {args.out}")
    print(f"latitude  {df.lat.min():.2f} .. {df.lat.max():.2f}")
    print(f"longitude {df.lon.min():.2f} .. {df.lon.max():.2f}\n")
    for col in ("source", "ipcc_class", "split"):
        counts = df[col].value_counts()
        print(f"{col}: " + ", ".join(f"{k} {v}" for k, v in counts.items()))


if __name__ == "__main__":
    main()
