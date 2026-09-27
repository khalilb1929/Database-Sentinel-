#!/usr/bin/env python3
"""
Acquisition-level characteristics of the planned dataset, measured from the catalogue only.

The representativeness report asks *where* the AOIs are. This script asks what the time series
at those AOIs will actually look like: how many dates, in which years and seasons, under which
cloud cover and sun elevation. It queries the CDSE STAC API (no credentials, no download) for a
sample of sites and writes figures, a summary JSON and the raw per-granule table.

    python analyze_acquisitions.py --sites sites/worldstrat_aoi_100.csv --sample 20

Outputs: report/figures/acq_*.pdf, report/generated/acquisitions_*.{csv,json}
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import requests  # noqa: E402

STAC_SEARCH = "https://stac.dataspace.copernicus.eu/v1/search"
COLLECTION = "sentinel-2-l1c"
DEFAULT_START, DEFAULT_END = "2015-06-23", "2026-08-31"
FIELDS = ["properties.datetime", "properties.eo:cloud_cover", "properties.platform",
          "properties.processing:version", "properties.grid:code",
          "properties.view:sun_elevation", "properties.view:incidence_angle",
          "properties.sat:relative_orbit"]
GROUP_COLORS = {"Tropical": "#C44E52", "Arid": "#DD8452", "Temperate": "#4C72B0",
                "Cold": "#55A868", "Polar": "#8172B3"}


def search_site(lat: float, lon: float, start: str, end: str, pause: float,
                timeout: float = 120) -> list[dict]:
    body = {
        "collections": [COLLECTION],
        "intersects": {"type": "Point", "coordinates": [lon, lat]},
        "datetime": f"{start}T00:00:00Z/{end}T23:59:59.999Z",
        "fields": {"include": FIELDS, "exclude": ["assets", "geometry", "links", "bbox"]},
        "limit": 100,
    }
    out: list[dict] = []
    while True:
        for attempt in range(5):  # the catalogue rate-limits bursts (HTTP 429)
            resp = requests.post(STAC_SEARCH, json=body, timeout=timeout)
            if resp.status_code != 429:
                break
            time.sleep(5 * (attempt + 1))
        resp.raise_for_status()
        page = resp.json()
        for feat in page.get("features", []):
            props = feat.get("properties", {})
            out.append({"item_id": feat.get("id", ""), **props})
        nxt = next((l for l in page.get("links", []) if l.get("rel") == "next"), None)
        if not nxt:
            return out
        body = nxt.get("body", body)
        time.sleep(pause)


def dedupe(df: pd.DataFrame) -> pd.DataFrame:
    """Mirror the pipeline: newest processing baseline per (date, tile), one granule per date."""
    df = df.sort_values(["date", "tile", "baseline", "item_id"])
    df = df.drop_duplicates(subset=["date", "tile"], keep="last")
    return df.drop_duplicates(subset=["date"], keep="last")


def collect(sites: pd.DataFrame, start: str, end: str, pause: float) -> pd.DataFrame:
    rows = []
    for i, site in enumerate(sites.itertuples(), 1):
        t0 = time.time()
        granules = search_site(site.lat, site.lon, start, end, pause)
        df = pd.DataFrame(granules)
        if df.empty:
            print(f"[{i}/{len(sites)}] {site.site_id}: no granule", flush=True)
            continue
        df["datetime"] = pd.to_datetime(df["datetime"], format="mixed", utc=True)
        df["date"] = df["datetime"].dt.date
        df["tile"] = df.get("grid:code", pd.Series([""] * len(df))).astype(str)
        df["baseline"] = pd.to_numeric(df.get("processing:version"), errors="coerce").fillna(0)
        df = dedupe(df)
        df["site_id"] = site.site_id
        df["lat"] = site.lat
        df["koppen_group"] = getattr(site, "koppen_group", "")
        df["continent"] = getattr(site, "continent", "")
        rows.append(df)
        print(f"[{i}/{len(sites)}] {site.site_id} ({site.koppen_group}, lat {site.lat:.1f}): "
              f"{len(df)} dates, {time.time() - t0:.0f}s", flush=True)
        time.sleep(pause)
    return pd.concat(rows, ignore_index=True)


def figures(df: pd.DataFrame, figdir: Path) -> None:
    cloud = pd.to_numeric(df["eo:cloud_cover"], errors="coerce")
    sun = pd.to_numeric(df["view:sun_elevation"], errors="coerce")

    # 1. acquisitions per year, stacked by platform
    fig, ax = plt.subplots(figsize=(7.2, 3.6), constrained_layout=True)
    per_year = (df.assign(year=df.datetime.dt.year)
                  .groupby(["year", "platform"], observed=True).size().unstack(fill_value=0))
    bottom = np.zeros(len(per_year))
    for plat in per_year.columns:
        ax.bar(per_year.index, per_year[plat], bottom=bottom, label=plat, width=0.8)
        bottom += per_year[plat].values
    ax.set_xlabel("year")
    ax.set_ylabel("acquisitions (sampled sites)")
    ax.set_title("Acquisitions per year and platform", fontsize=11)
    ax.legend(fontsize=8)
    ax.spines[["top", "right"]].set_visible(False)
    fig.savefig(figdir / "acq_per_year.pdf")
    plt.close(fig)

    # 2. cloud cover CDF per climate group
    fig, ax = plt.subplots(figsize=(7.2, 3.8), constrained_layout=True)
    for group, sub in df.groupby("koppen_group", observed=True):
        c = pd.to_numeric(sub["eo:cloud_cover"], errors="coerce").dropna().sort_values()
        if len(c) < 20:
            continue
        ax.plot(c.values, 100 * np.arange(1, len(c) + 1) / len(c),
                label=f"{group} (n={len(c)})", color=GROUP_COLORS.get(group))
    ax.axvline(20, color="#888888", linestyle="--", linewidth=1)
    ax.text(21, 5, "--max_cloud 20", fontsize=8, color="#666666")
    ax.set_xlabel("scene cloud cover (%)")
    ax.set_ylabel("cumulative share of acquisitions (%)")
    ax.set_title("Cloud cover by climate group", fontsize=11)
    ax.legend(fontsize=8, loc="lower right")
    ax.spines[["top", "right"]].set_visible(False)
    fig.savefig(figdir / "acq_cloud_cdf.pdf")
    plt.close(fig)

    # 3. seasonal availability: all acquisitions vs those under 20 % cloud
    fig, axes = plt.subplots(1, 2, figsize=(11, 3.6), constrained_layout=True)
    month = df.datetime.dt.month
    for ax, mask, title in ((axes[0], slice(None), "All acquisitions"),
                            (axes[1], cloud <= 20, "Cloud cover $\\leq$ 20 %")):
        sub = df[mask] if not isinstance(mask, slice) else df
        m = sub.datetime.dt.month
        for group, g in sub.groupby("koppen_group", observed=True):
            counts = g.datetime.dt.month.value_counts().reindex(range(1, 13), fill_value=0)
            ax.plot(range(1, 13), 100 * counts / max(counts.sum(), 1), marker="o", ms=3,
                    label=group, color=GROUP_COLORS.get(group))
        ax.set_xticks(range(1, 13))
        ax.set_xlabel("month")
        ax.set_ylabel("share of acquisitions (%)")
        ax.set_title(title, fontsize=11)
        ax.spines[["top", "right"]].set_visible(False)
    axes[1].legend(fontsize=8)
    fig.savefig(figdir / "acq_seasonality.pdf")
    plt.close(fig)

    # 4. sun elevation against latitude
    fig, ax = plt.subplots(figsize=(7.2, 3.8), constrained_layout=True)
    sc = ax.scatter(df.lat, sun, s=4, alpha=0.25, c=cloud, cmap="Blues_r", vmin=0, vmax=100,
                    linewidths=0)
    fig.colorbar(sc, ax=ax, label="cloud cover (%)")
    ax.set_xlabel("site latitude (°)")
    ax.set_ylabel("sun elevation (°)")
    ax.set_title("Illumination geometry across the sample", fontsize=11)
    ax.spines[["top", "right"]].set_visible(False)
    fig.savefig(figdir / "acq_sun_elevation.pdf")
    plt.close(fig)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sites", type=Path, default=Path("sites/worldstrat_aoi_100.csv"))
    p.add_argument("--sample", type=int, default=20, help="sites to query (0 = all)")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--start_date", default=DEFAULT_START)
    p.add_argument("--end_date", default=DEFAULT_END)
    p.add_argument("--pause", type=float, default=1.0, help="seconds between catalogue requests")
    p.add_argument("--out", type=Path, default=Path("report"))
    args = p.parse_args()

    figdir, gendir = args.out / "figures", args.out / "generated"
    figdir.mkdir(parents=True, exist_ok=True)
    gendir.mkdir(parents=True, exist_ok=True)

    sites = pd.read_csv(args.sites)
    if args.sample and args.sample < len(sites):
        # Stratify the sample by climate so every group is represented. (Built with an explicit
        # loop: in pandas 3 groupby.apply drops the grouping column from the frames it yields.)
        strata = [g.sample(max(1, round(args.sample * len(g) / len(sites))),
                           random_state=args.seed)
                  for _, g in sites.groupby("koppen_group", observed=True)]
        sites = pd.concat(strata).sort_values("site_id")
    print(f"querying {len(sites)} site(s), {args.start_date} to {args.end_date}\n", flush=True)

    df = collect(sites, args.start_date, args.end_date, args.pause)
    df.to_csv(gendir / "acquisitions_sample.csv", index=False)
    figures(df, figdir)

    cloud = pd.to_numeric(df["eo:cloud_cover"], errors="coerce")
    per_site = df.groupby("site_id").size()
    gaps = (df.sort_values(["site_id", "datetime"]).groupby("site_id")["datetime"]
              .diff().dt.total_seconds() / 86400).dropna()
    by_group = df.assign(cloud=cloud).groupby("koppen_group", observed=True).agg(
        sites=("site_id", "nunique"), dates=("date", "size"),
        median_cloud=("cloud", "median"), clear_share=("cloud", lambda c: float((c <= 20).mean())))

    stats = {
        "sites_queried": int(df.site_id.nunique()),
        "date_range": [args.start_date, args.end_date],
        "dates_per_site": {"min": int(per_site.min()), "median": float(per_site.median()),
                           "max": int(per_site.max()), "mean": float(per_site.mean())},
        "revisit_days": {"median": float(gaps.median()),
                         "p90": float(gaps.quantile(0.9)), "max": float(gaps.max())},
        "cloud": {"median": float(cloud.median()),
                  "share_under_10": float((cloud <= 10).mean()),
                  "share_under_20": float((cloud <= 20).mean()),
                  "share_over_80": float((cloud >= 80).mean())},
        "platforms": df.platform.value_counts().to_dict(),
        "baselines": df["processing:version"].astype(str).value_counts().head(6).to_dict(),
        "acquisitions_per_year": df.assign(y=df.datetime.dt.year).y.value_counts().sort_index()
                                   .to_dict(),
        "by_climate": json.loads(by_group.to_json(orient="index")),
    }
    (gendir / "acquisitions_stats.json").write_text(json.dumps(stats, indent=2), encoding="utf-8")

    print("\n=== summary")
    print(f"{stats['sites_queried']} sites, {len(df)} acquisition dates")
    print(f"dates per site: median {stats['dates_per_site']['median']:.0f} "
          f"(min {stats['dates_per_site']['min']}, max {stats['dates_per_site']['max']})")
    print(f"revisit gap: median {stats['revisit_days']['median']:.1f} d, "
          f"p90 {stats['revisit_days']['p90']:.1f} d, max {stats['revisit_days']['max']:.0f} d")
    print(f"cloud: median {stats['cloud']['median']:.0f}%, "
          f"{100 * stats['cloud']['share_under_20']:.0f}% of scenes under 20%, "
          f"{100 * stats['cloud']['share_over_80']:.0f}% above 80%")
    print(f"platforms: {stats['platforms']}")
    print(by_group.to_string())


if __name__ == "__main__":
    main()
