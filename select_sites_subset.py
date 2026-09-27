#!/usr/bin/env python3
"""
Pick a small, well-spread subset of the AOI list for a first dataset build.

Taking the first N rows of ``sites/worldstrat_aoi.csv`` would give N neighbouring mining sites
in Suriname. This script instead allocates the N sites across Köppen climate groups in
proportion to global land area (correcting the sampling biases measured in the
representativeness report), and inside each group picks sites by farthest-point sampling, so
they are spread over continents and never overlap.

Requires the annotated CSV produced by ``analyze_aoi_bias.py``.

    python analyze_aoi_bias.py                      # once, writes the annotated CSV
    python select_sites_subset.py --n 100           # -> sites/worldstrat_aoi_100.csv
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from analyze_aoi_bias import MAIN_GROUPS, koppen_assets, land_shares_by_class

EARTH_R_KM = 6371.0
MIN_SEPARATION_KM = 50.0  # keep footprints (10.24 km) well apart


def unit_sphere(df: pd.DataFrame) -> np.ndarray:
    lat, lon = np.deg2rad(df.lat.values), np.deg2rad(df.lon.values)
    return np.column_stack([np.cos(lat) * np.cos(lon), np.cos(lat) * np.sin(lon), np.sin(lat)])


def great_circle_km(xyz: np.ndarray, point: np.ndarray) -> np.ndarray:
    chord = np.linalg.norm(xyz - point, axis=1)
    return 2 * EARTH_R_KM * np.arcsin(np.clip(chord / 2, 0, 1))


def allocate(land_share: pd.Series, available: pd.Series, n: int) -> dict[str, int]:
    """Largest-remainder allocation of n sites over climate groups, capped by availability."""
    groups = [g for g in land_share.index if available.get(g, 0) > 0]
    exact = {g: n * land_share[g] / land_share[groups].sum() for g in groups}
    alloc = {g: min(int(np.floor(v)), available[g]) for g, v in exact.items()}
    while sum(alloc.values()) < n:
        spare = {g: exact[g] - alloc[g] for g in groups if alloc[g] < available[g]}
        if not spare:
            break
        alloc[max(spare, key=spare.get)] += 1
    return alloc


def farthest_point(candidates: pd.DataFrame, k: int, chosen_xyz: list[np.ndarray],
                   seed: int) -> pd.DataFrame:
    """Greedily take the candidate farthest from everything already selected."""
    if k <= 0 or candidates.empty:
        return candidates.iloc[:0]
    xyz = unit_sphere(candidates)
    rng = np.random.default_rng(seed)
    picked: list[int] = []
    # Distance of every candidate to the closest already-selected site.
    if chosen_xyz:
        dist = np.min([great_circle_km(xyz, p) for p in chosen_xyz], axis=0)
    else:
        dist = np.full(len(candidates), np.inf)
        first = int(rng.integers(len(candidates)))  # reproducible seed point
        picked.append(first)
        dist = great_circle_km(xyz, xyz[first])

    while len(picked) < min(k, len(candidates)):
        idx = int(np.argmax(dist))
        if dist[idx] < MIN_SEPARATION_KM:
            break  # nothing left far enough from the current selection
        picked.append(idx)
        dist = np.minimum(dist, great_circle_km(xyz, xyz[idx]))
    return candidates.iloc[picked]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--annotated", type=Path, default=Path("report/generated/aoi_annotated.csv"))
    p.add_argument("--n", type=int, default=100, help="number of sites to select")
    p.add_argument("--out", type=Path, default=None, help="default: sites/worldstrat_aoi_<n>.csv")
    p.add_argument("--cache", type=Path, default=Path(".cache/reference"))
    p.add_argument("--seed", type=int, default=0)
    args = p.parse_args()
    out = args.out or Path(f"sites/worldstrat_aoi_{args.n}.csv")

    if not args.annotated.exists():
        raise SystemExit(f"{args.annotated} not found — run analyze_aoi_bias.py first")
    df = pd.read_csv(args.annotated)
    df = df[df.koppen_value > 0]  # drop the few AOIs off the land mask

    tif, legend = koppen_assets(args.cache)
    koppen_land, _, _ = land_shares_by_class(tif)
    known = koppen_land[[c in legend for c in koppen_land.index]]
    land_share = known.rename(index=lambda c: MAIN_GROUPS[legend[c][0][0]]).groupby(level=0).sum()

    available = df.koppen_group.value_counts()
    alloc = allocate(land_share, available, args.n)

    chosen_xyz: list[np.ndarray] = []
    parts = []
    # Rarest strata first: they constrain the selection most.
    for group in sorted(alloc, key=lambda g: available[g]):
        sub = farthest_point(df[df.koppen_group == group], alloc[group], chosen_xyz, args.seed)
        parts.append(sub)
        chosen_xyz.extend(unit_sphere(sub))
    selection = pd.concat(parts).sort_values("site_id")

    keep = ["site_id", "lat", "lon", "source", "ipcc_class", "smod_class", "split",
            "koppen_code", "koppen_group", "continent"]
    out.parent.mkdir(parents=True, exist_ok=True)
    selection[keep].to_csv(out, index=False)

    print(f"{len(selection)} sites -> {out}\n")
    summary = pd.DataFrame({
        "selected": selection.koppen_group.value_counts(),
        "share_%": (100 * selection.koppen_group.value_counts() / len(selection)).round(1),
        "land_%": (100 * land_share).round(1),
    }).fillna(0)
    print(summary.to_string())
    print("\ncontinents: " + ", ".join(f"{k} {v}" for k, v in
                                       selection.continent.value_counts().items()))
    print("land cover: " + ", ".join(f"{k} {v}" for k, v in
                                     selection.ipcc_class.value_counts().items()))
    xyz = unit_sphere(selection)
    nn = [great_circle_km(np.delete(xyz, i, axis=0), xyz[i]).min() for i in range(len(xyz))]
    print(f"closest pair: {min(nn):.0f} km (footprint is 10.24 km)")


if __name__ == "__main__":
    main()
