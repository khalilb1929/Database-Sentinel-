#!/usr/bin/env python3
"""
Representativeness analysis of an AOI list (default: the WorldStrat sample).

Compares where the AOIs are against where land actually is, and writes everything the LaTeX
report needs: figures (PDF), LaTeX tables, a macro file with the key numbers, and stats.json.

References used as the "expected" distribution
  * Koppen-Geiger climate classes, Beck et al. (2018), Scientific Data 5:180214, 5-arcmin map
    (downloaded once, ~71 MB) -> climate/biome proxy and the global land mask;
  * Natural Earth 1:110m country polygons -> continents and geodesic land areas.

    python analyze_aoi_bias.py --sites sites/worldstrat_aoi.csv --out report

Outputs under --out: figures/*.pdf, generated/*.tex, generated/stats.json.
"""
from __future__ import annotations

import argparse
import json
import re
import zipfile
from collections import Counter
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import rasterio  # noqa: E402
import requests  # noqa: E402
from pyproj import Geod  # noqa: E402
from scipy.spatial import cKDTree  # noqa: E402
from scipy.stats import chisquare  # noqa: E402
from shapely.geometry import Point, shape  # noqa: E402
from shapely.strtree import STRtree  # noqa: E402

KOPPEN_URL = "https://s3-eu-west-1.amazonaws.com/pfigshare-u-files/12407516/Beck_KG_V1.zip"
KOPPEN_MEMBER = "Beck_KG_V1_present_0p083.tif"  # 5 arcmin, enough for point sampling
NE_URL = ("https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/"
          "ne_110m_admin_0_countries.geojson")
LEGEND_RE = re.compile(r"^\s*(\d+):\s+(\S+)\s+(.+?)\s*\[")
MAIN_GROUPS = {"A": "Tropical", "B": "Arid", "C": "Temperate", "D": "Cold", "E": "Polar"}
LAT_EDGES = [-90, -60, -30, -10, 10, 30, 60, 90]
AOI_SIDE_KM = 10.24  # pipeline footprint; two AOIs closer than this overlap on the ground
EARTH_R_KM = 6371.0
SOURCE_COLORS = {"Landcover": "#4C72B0", "UNHCR": "#DD8452", "ASMSpotter": "#55A868",
                 "Amnesty": "#C44E52", "Other": "#8172B3"}


# ---------------------------------------------------------------------------------------
# inputs
# ---------------------------------------------------------------------------------------
def download(url: str, dest: Path) -> Path:
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"downloading {dest.name} ...", flush=True)
    with requests.get(url, stream=True, timeout=600) as r:
        r.raise_for_status()
        tmp = dest.with_suffix(dest.suffix + ".part")
        with open(tmp, "wb") as fh:
            for chunk in r.iter_content(1 << 20):
                fh.write(chunk)
        tmp.replace(dest)
    return dest


def koppen_assets(cache: Path) -> tuple[Path, dict[int, tuple[str, str]]]:
    zip_path = download(KOPPEN_URL, cache / "Beck_KG_V1.zip")
    tif = cache / KOPPEN_MEMBER
    with zipfile.ZipFile(zip_path) as z:
        if not tif.exists():
            tif.write_bytes(z.read(KOPPEN_MEMBER))
        legend = {}
        for line in z.read("legend.txt").decode("utf-8", "replace").splitlines():
            if m := LEGEND_RE.match(line):
                legend[int(m.group(1))] = (m.group(2), m.group(3).strip())
    return tif, legend


# ---------------------------------------------------------------------------------------
# reference distributions
# ---------------------------------------------------------------------------------------
def land_shares_by_class(tif: Path) -> tuple[pd.Series, np.ndarray, np.ndarray]:
    """Area-weighted share of global land per Koppen class, plus the land mask and its lats."""
    with rasterio.open(tif) as src:
        classes = src.read(1)
        top, res = src.transform.f, -src.transform.e
    lats = top - (np.arange(classes.shape[0]) + 0.5) * res
    weights = np.cos(np.deg2rad(lats))[:, None] * np.ones((1, classes.shape[1]))
    land = classes > 0
    total = weights[land].sum()
    shares = {c: weights[(classes == c)].sum() / total for c in np.unique(classes[land])}
    return pd.Series(shares).sort_index(), land, lats


def land_area_by_continent(geojson: Path) -> tuple[pd.Series, STRtree, list[str]]:
    features = json.loads(geojson.read_text(encoding="utf-8"))["features"]
    geod = Geod(ellps="WGS84")
    geoms, continents, area = [], [], Counter()
    for feat in features:
        geom = shape(feat["geometry"])
        cont = feat["properties"].get("CONTINENT", "Unknown")
        geoms.append(geom)
        continents.append(cont)
        area[cont] += abs(geod.geometry_area_perimeter(geom)[0]) / 1e6  # km²
    total = sum(area.values())
    shares = pd.Series({k: v / total for k, v in area.items()}).sort_values(ascending=False)
    return shares, STRtree(geoms), continents


def land_shares_by_latitude(land: np.ndarray, lats: np.ndarray) -> pd.Series:
    weights = np.cos(np.deg2rad(lats)) * land.sum(axis=1)
    bands = pd.cut(lats, LAT_EDGES, right=False)
    total = weights.sum()
    return pd.Series(weights).groupby(bands, observed=False).sum().div(total)


# ---------------------------------------------------------------------------------------
# annotation
# ---------------------------------------------------------------------------------------
def annotate(df: pd.DataFrame, tif: Path, legend: dict, tree: STRtree,
             continents: list[str]) -> pd.DataFrame:
    with rasterio.open(tif) as src:
        values = [v[0] for v in src.sample(zip(df.lon, df.lat))]
    df = df.copy()
    df["koppen_code"] = [legend.get(int(v), ("Ocean/none", ""))[0] for v in values]
    df["koppen_desc"] = [legend.get(int(v), ("Ocean/none", "outside the land mask"))[1]
                         for v in values]
    df["koppen_value"] = values
    df["koppen_group"] = [MAIN_GROUPS.get(c[0], "Water / unclassified")
                          for c in df["koppen_code"]]

    # Continent of the containing (or nearest) country polygon.
    points = [Point(lon, lat) for lon, lat in zip(df.lon, df.lat)]
    assigned = []
    for pt in points:
        hit = next((i for i in tree.query(pt) if tree.geometries[i].contains(pt)), None)
        if hit is None:
            hit = tree.nearest(pt)
        assigned.append(continents[int(hit)])
    df["continent"] = assigned
    df["lat_band"] = pd.cut(df.lat, LAT_EDGES, right=False)
    return df


def _unit_sphere(df: pd.DataFrame) -> np.ndarray:
    lat, lon = np.deg2rad(df.lat.values), np.deg2rad(df.lon.values)
    return np.column_stack([np.cos(lat) * np.cos(lon), np.cos(lat) * np.sin(lon), np.sin(lat)])


def nearest_neighbour_km(df: pd.DataFrame) -> np.ndarray:
    """Great-circle distance to the closest other AOI (via a 3-D chord-distance KD-tree)."""
    chord, _ = cKDTree(_unit_sphere(df)).query(_unit_sphere(df), k=2)
    return 2 * EARTH_R_KM * np.arcsin(np.clip(chord[:, 1] / 2, 0, 1))


def overlapping_pairs(df: pd.DataFrame, side_km: float = AOI_SIDE_KM) -> dict:
    """AOI pairs whose 10.24 km footprints can overlap, and whether they sit in different
    dataset splits (which would leak imagery between training and evaluation)."""
    radius = 2 * np.sin(side_km / (2 * EARTH_R_KM))  # chord length for a great-circle distance
    pairs = cKDTree(_unit_sphere(df)).query_pairs(radius, output_type="ndarray")
    if len(pairs) == 0:
        return {"n_pairs": 0, "n_sites_involved": 0, "n_cross_split_pairs": 0}
    splits = df["split"].values if "split" in df else np.array(["?"] * len(df))
    cross = splits[pairs[:, 0]] != splits[pairs[:, 1]]
    combos = Counter(tuple(sorted((splits[a], splits[b])))
                     for a, b in pairs[cross]) if cross.any() else Counter()
    return {
        "n_pairs": int(len(pairs)),
        "n_sites_involved": int(len(np.unique(pairs))),
        "n_cross_split_pairs": int(cross.sum()),
        "cross_split_combinations": {f"{a}/{b}": n for (a, b), n in combos.most_common()},
    }


# ---------------------------------------------------------------------------------------
# comparison tables
# ---------------------------------------------------------------------------------------
def compare(observed: pd.Series, expected_share: pd.Series, name: str) -> pd.DataFrame:
    """Observed AOI counts vs the share of global land, with representation ratios."""
    idx = observed.index.union(expected_share.index)
    obs = observed.reindex(idx).fillna(0.0)
    exp_share = expected_share.reindex(idx).fillna(0.0)
    n = obs.sum()
    table = pd.DataFrame({
        "aoi_count": obs.astype(int),
        "aoi_share": obs / n,
        "land_share": exp_share,
        "expected_count": exp_share * n,
    })
    table["ratio"] = table.aoi_share / table.land_share.replace(0, np.nan)
    table = table[(table.aoi_count > 0) | (table.land_share > 0.001)]  # drop empty categories
    table.index.name = name
    return table.sort_values("aoi_count", ascending=False)


def chi2(table: pd.DataFrame) -> dict:
    """Goodness of fit against the land-area distribution (classes with >=5 expected)."""
    keep = table[table.expected_count >= 5]
    if len(keep) < 2:
        return {}
    obs, exp = keep.aoi_count.values.astype(float), keep.expected_count.values.astype(float)
    exp = exp * obs.sum() / exp.sum()
    stat, p = chisquare(obs, exp)
    return {"chi2": float(stat), "dof": int(len(keep) - 1), "p_value": float(p),
            "classes_tested": int(len(keep)),
            "total_variation_distance": float(0.5 * np.abs(keep.aoi_share - keep.land_share).sum())}


# ---------------------------------------------------------------------------------------
# outputs
# ---------------------------------------------------------------------------------------
def latex_table(table: pd.DataFrame, path: Path, label_col: str, caption_cols: dict) -> None:
    rows = []
    for idx, r in table.iterrows():
        ratio = "--" if not np.isfinite(r.ratio) else f"{r.ratio:.2f}"
        rows.append(f"{str(idx).replace('&', r'\&')} & {int(r.aoi_count)} & "
                    f"{100 * r.aoi_share:.1f} & {100 * r.land_share:.1f} & {ratio} \\\\")
    body = "\n".join(rows)
    header = " & ".join(caption_cols.values())
    path.write_text(
        "\\begin{tabular}{lrrrr}\n\\toprule\n"
        f"{label_col} & {header} \\\\\n\\midrule\n{body}\n"
        "\\bottomrule\n\\end{tabular}\n", encoding="utf-8")


def bar_comparison(table: pd.DataFrame, title: str, path: Path, top: int | None = None) -> None:
    t = table.head(top) if top else table
    y = np.arange(len(t))
    fig, ax = plt.subplots(figsize=(7.2, 0.42 * len(t) + 1.5), constrained_layout=True)
    ax.barh(y + 0.2, 100 * t.aoi_share, height=0.4, label="AOI sample", color="#4C72B0")
    ax.barh(y - 0.2, 100 * t.land_share, height=0.4, label="global land area", color="#B0B0B0")
    for i, r in enumerate(t.itertuples()):
        if np.isfinite(r.ratio) and r.land_share > 0:
            ax.text(max(100 * r.aoi_share, 100 * r.land_share) + 0.6, i,
                    f"×{r.ratio:.1f}", va="center", fontsize=8, color="#333333")
    ax.set_yticks(y, [str(i) for i in t.index], fontsize=9)
    ax.invert_yaxis()
    ax.set_xlabel("share (%)")
    ax.set_title(title, fontsize=11)
    ax.legend(fontsize=8, loc="lower right")
    ax.spines[["top", "right"]].set_visible(False)
    fig.savefig(path)
    plt.close(fig)


def world_map(df: pd.DataFrame, geojson: Path, path: Path) -> None:
    fig, ax = plt.subplots(figsize=(11, 5.6), constrained_layout=True)
    for feat in json.loads(geojson.read_text(encoding="utf-8"))["features"]:
        geom = shape(feat["geometry"])
        for poly in (geom.geoms if geom.geom_type == "MultiPolygon" else [geom]):
            x, y = poly.exterior.xy
            ax.fill(x, y, facecolor="#F0F0F0", edgecolor="#BBBBBB", linewidth=0.3, zorder=1)
    for src, sub in df.groupby("source"):
        ax.scatter(sub.lon, sub.lat, s=7, alpha=0.75, linewidths=0, zorder=2,
                   color=SOURCE_COLORS.get(src, "#333333"), label=f"{src} ({len(sub)})")
    ax.set_xlim(-180, 180)
    ax.set_ylim(-90, 90)
    ax.set_aspect("equal")
    ax.set_xlabel("longitude")
    ax.set_ylabel("latitude")
    ax.set_title(f"{len(df)} WorldStrat AOIs by sampling source", fontsize=11)
    ax.legend(fontsize=8, loc="lower left", framealpha=0.9, markerscale=2)
    fig.savefig(path)
    plt.close(fig)


def latitude_figure(df: pd.DataFrame, land: pd.Series, path: Path) -> None:
    obs = df.lat_band.value_counts(normalize=True).reindex(land.index).fillna(0)
    y = np.arange(len(land))
    fig, ax = plt.subplots(figsize=(7.2, 4), constrained_layout=True)
    ax.barh(y + 0.2, 100 * obs.values, height=0.4, label="AOI sample", color="#4C72B0")
    ax.barh(y - 0.2, 100 * land.values, height=0.4, label="global land area", color="#B0B0B0")
    ax.set_yticks(y, [f"{int(i.left)}° to {int(i.right)}°" for i in land.index], fontsize=9)
    ax.invert_yaxis()
    ax.set_xlabel("share (%)")
    ax.set_title("Latitude distribution", fontsize=11)
    ax.legend(fontsize=8)
    ax.spines[["top", "right"]].set_visible(False)
    fig.savefig(path)
    plt.close(fig)


def neighbour_figure(dist: np.ndarray, path: Path) -> None:
    fig, ax = plt.subplots(figsize=(7.2, 4), constrained_layout=True)
    s = np.sort(dist[dist > 0])
    ax.plot(s, 100 * np.arange(1, len(s) + 1) / len(s), color="#4C72B0")
    ax.axvline(AOI_SIDE_KM, color="#C44E52", linestyle="--", linewidth=1)
    ax.text(AOI_SIDE_KM * 1.1, 5, f"{AOI_SIDE_KM:.2f} km\n(footprint side)", fontsize=8,
            color="#C44E52")
    ax.set_xscale("log")
    ax.set_xlabel("distance to the nearest other AOI (km, log scale)")
    ax.set_ylabel("cumulative share of AOIs (%)")
    ax.set_title("Spatial clustering of the AOI sample", fontsize=11)
    ax.grid(alpha=0.3)
    ax.spines[["top", "right"]].set_visible(False)
    fig.savefig(path)
    plt.close(fig)


def composition_figure(df: pd.DataFrame, path: Path) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2), constrained_layout=True)
    for ax, col, title in zip(axes, ("ipcc_class", "smod_class"),
                              ("IPCC land-cover class", "Degree of urbanisation (SMOD)")):
        pivot = (df.groupby([col, "source"], observed=True).size().unstack(fill_value=0)
                 .loc[df[col].value_counts().index])
        bottom = np.zeros(len(pivot))
        for src in pivot.columns:
            ax.barh(np.arange(len(pivot)), pivot[src], left=bottom, height=0.7,
                    color=SOURCE_COLORS.get(src, "#333333"), label=src)
            bottom += pivot[src].values
        ax.set_yticks(np.arange(len(pivot)), pivot.index, fontsize=9)
        ax.invert_yaxis()
        ax.set_xlabel("number of AOIs")
        ax.set_title(title, fontsize=11)
        ax.spines[["top", "right"]].set_visible(False)
    axes[0].legend(fontsize=8, title="source", title_fontsize=8)
    fig.savefig(path)
    plt.close(fig)


def write_macros(path: Path, values: dict) -> None:
    lines = [f"\\newcommand{{\\{k}}}{{{v}}}" for k, v in values.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------------------
def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sites", type=Path, default=Path("sites/worldstrat_aoi.csv"))
    p.add_argument("--out", type=Path, default=Path("report"))
    p.add_argument("--cache", type=Path, default=Path(".cache/reference"))
    args = p.parse_args()

    figures, generated = args.out / "figures", args.out / "generated"
    figures.mkdir(parents=True, exist_ok=True)
    generated.mkdir(parents=True, exist_ok=True)

    df = pd.read_csv(args.sites)
    tif, legend = koppen_assets(args.cache)
    ne = download(NE_URL, args.cache / "ne_110m_admin_0_countries.geojson")

    print("computing reference distributions ...", flush=True)
    koppen_land, land_mask, lats = land_shares_by_class(tif)
    continent_land, tree, continents = land_area_by_continent(ne)
    lat_land = land_shares_by_latitude(land_mask, lats)

    print("annotating AOIs ...", flush=True)
    df = annotate(df, tif, legend, tree, continents)
    df.to_csv(generated / "aoi_annotated.csv", index=False)

    # Koppen main groups (A-E) and detailed classes, restricted to AOIs on the land mask.
    on_land = df[df.koppen_value > 0]
    known = koppen_land[[c in legend for c in koppen_land.index]]
    # Sum the land share of every class sharing a first letter (A, B, C, D, E).
    group_land = known.rename(index=lambda c: MAIN_GROUPS[legend[c][0][0]]).groupby(level=0).sum()
    group_tab = compare(on_land.koppen_group.value_counts(), group_land, "Climate group")
    code_land = known.rename(index=lambda c: legend[c][0])
    code_tab = compare(on_land.koppen_code.value_counts(), code_land, "Koppen class")
    cont_tab = compare(df.continent.value_counts(), continent_land, "Continent")
    lat_tab = compare(df.lat_band.value_counts(), lat_land, "Latitude band")

    dist = nearest_neighbour_km(df)
    clustered = float((dist < AOI_SIDE_KM).mean())
    overlaps = overlapping_pairs(df)

    print("writing figures and tables ...", flush=True)
    world_map(df, ne, figures / "aoi_world_map.pdf")
    bar_comparison(group_tab, "Climate groups: AOI sample vs global land",
                   figures / "koppen_groups.pdf")
    bar_comparison(code_tab, "Koppen-Geiger classes (15 most frequent)",
                   figures / "koppen_classes.pdf", top=15)
    bar_comparison(cont_tab, "Continents: AOI sample vs global land area",
                   figures / "continents.pdf")
    latitude_figure(df, lat_land, figures / "latitude.pdf")
    neighbour_figure(dist, figures / "clustering.pdf")
    composition_figure(df, figures / "composition.pdf")

    cols = {"aoi_count": "AOIs", "aoi_share": "sample (\\%)", "land_share": "land (\\%)",
            "ratio": "ratio"}
    latex_table(group_tab, generated / "table_koppen_groups.tex", "Climate group", cols)
    latex_table(code_tab.head(15), generated / "table_koppen_classes.tex", "K\\\"oppen class", cols)
    latex_table(cont_tab, generated / "table_continents.tex", "Continent", cols)
    latex_table(lat_tab, generated / "table_latitude.tex", "Latitude band", cols)

    stats = {
        "n_aoi": int(len(df)),
        "n_on_land": int(len(on_land)),
        "sources": df.source.value_counts().to_dict(),
        "ipcc": df.ipcc_class.value_counts().to_dict(),
        "smod_urban_share": float(df.smod_class.str.startswith("Urban").mean()),
        "splits": df.split.value_counts().to_dict(),
        "chi2_koppen_groups": chi2(group_tab),
        "chi2_continents": chi2(cont_tab),
        "chi2_latitude": chi2(lat_tab),
        "clustering": {
            "median_nn_km": float(np.median(dist)),
            "share_within_footprint": clustered,
            "share_within_50km": float((dist < 50).mean()),
            "n_unique_clusters_50km": int(len(set(map(tuple, np.round(
                df[["lat", "lon"]].values / 0.5).astype(int))))),
            "overlapping_pairs": overlaps,
        },
        "landcover_subset": {
            "n": int((df.source == "Landcover").sum()),
            "settlement_share": float((df[df.source == "Landcover"].ipcc_class
                                       == "Settlement").mean()),
        },
        "settlement_share_all": float((df.ipcc_class == "Settlement").mean()),
    }
    (generated / "stats.json").write_text(json.dumps(stats, indent=2), encoding="utf-8")

    write_macros(generated / "macros.tex", {
        "NAOI": f"{stats['n_aoi']:,}".replace(",", "\\,"),
        "NLandcover": f"{stats['landcover_subset']['n']:,}".replace(",", "\\,"),
        "SettlementShare": f"{100 * stats['settlement_share_all']:.0f}",
        "SettlementShareLC": f"{100 * stats['landcover_subset']['settlement_share']:.0f}",
        "UrbanShare": f"{100 * stats['smod_urban_share']:.0f}",
        "MedianNN": f"{stats['clustering']['median_nn_km']:.1f}",
        "ClusteredShare": f"{100 * clustered:.0f}",
        # LaTeX control sequences may only contain letters: no digits in these names.
        "WithinFifty": f"{100 * stats['clustering']['share_within_50km']:.0f}",
        "OverlapPairs": f"{overlaps['n_pairs']:,}".replace(",", "\\,"),
        "OverlapSites": f"{overlaps['n_sites_involved']:,}".replace(",", "\\,"),
        "CrossSplitPairs": f"{overlaps['n_cross_split_pairs']:,}".replace(",", "\\,"),
    })

    print(f"\nAOIs: {stats['n_aoi']} ({stats['n_on_land']} on the Koppen land mask)")
    print(f"settlements: {100 * stats['settlement_share_all']:.0f}% of all AOIs, "
          f"{100 * stats['landcover_subset']['settlement_share']:.0f}% of the Landcover stratum")
    print(f"median nearest-neighbour distance: {stats['clustering']['median_nn_km']:.1f} km; "
          f"{100 * clustered:.0f}% closer than one footprint")
    print(f"overlapping pairs: {overlaps['n_pairs']} covering {overlaps['n_sites_involved']} AOIs, "
          f"{overlaps['n_cross_split_pairs']} of them across different splits "
          f"{overlaps.get('cross_split_combinations', {})}")
    for key in ("chi2_koppen_groups", "chi2_continents", "chi2_latitude"):
        if stats[key]:
            s = stats[key]
            print(f"{key}: chi2={s['chi2']:.0f} dof={s['dof']} p={s['p_value']:.2e} "
                  f"TVD={s['total_variation_distance']:.3f}")
    print(f"\nwritten to {args.out}")


if __name__ == "__main__":
    main()
