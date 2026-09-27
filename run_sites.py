#!/usr/bin/env python3
"""
Run the Sentinel-2 L1C pipeline over a list of coordinates (batch driver).

Reads a CSV with at least ``lat`` and ``lon`` columns (plus an optional ``site_id``) and calls
the pipeline once per site, writing each time series to ``<output_root>/<site_id>/``. Every
pipeline option not consumed here is forwarded unchanged, so the per-site behaviour (cloud
limit, date range, quality control, access mode) is exactly that of ``s2_l1c_pipeline.py``.

    # dry run: how many acquisitions would the first 5 sites yield?
    python run_sites.py --sites sites/worldstrat_aoi.csv --output_root data/worldstrat \
        --limit 5 --start_date 2023-01-01 --end_date 2023-12-31 --max_cloud 20 --dry_run

    # real run, resumable, 2 clearest scenes per year per site
    python run_sites.py --sites sites/worldstrat_aoi.csv --output_root data/worldstrat \
        --start_date 2023-01-01 --end_date 2023-12-31 --max_cloud 20 --max_images 2 --sort cloud

Progress is appended to ``<output_root>/sites_progress.csv`` (one row per site and attempt).
Re-running the same command skips sites already marked ``done`` unless ``--redo`` is given;
within a site, the pipeline itself skips dates already on disk.
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
import re
import sys
import time
from pathlib import Path

import s2_l1c_pipeline as pipeline

LOG = logging.getLogger("s2sites")
PROGRESS_FIELDS = ["site_id", "lat", "lon", "exit_code", "status", "n_files", "seconds",
                   "finished_utc"]
SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_id(raw: str) -> str:
    """Filesystem-safe folder name (WorldStrat ids contain spaces, e.g. 'Amnesty POI-10-1-1')."""
    return SAFE_ID_RE.sub("_", raw.strip()) or "site"


def read_sites(path: Path, limit: int | None, skip: int) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise SystemExit(f"{path}: no rows")
    missing = {"lat", "lon"} - set(rows[0])
    if missing:
        raise SystemExit(f"{path}: missing column(s) {sorted(missing)}")

    sites = []
    for i, row in enumerate(rows):
        try:
            lat, lon = float(row["lat"]), float(row["lon"])
        except (TypeError, ValueError):
            LOG.warning("row %d: unreadable coordinates, skipped", i + 2)
            continue
        sites.append({"site_id": safe_id(row.get("site_id") or f"site_{i:05d}"),
                      "lat": lat, "lon": lon})
    return sites[skip: None if limit is None else skip + limit]


def done_sites(progress: Path) -> set[str]:
    if not progress.exists():
        return set()
    with open(progress, newline="", encoding="utf-8") as fh:
        return {r["site_id"] for r in csv.DictReader(fh) if r.get("status") == "done"}


def append_progress(progress: Path, row: dict) -> None:
    new = not progress.exists()
    with open(progress, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=PROGRESS_FIELDS, extrasaction="ignore")
        if new:
            writer.writeheader()
        writer.writerow(row)


def parse_args(argv: list[str] | None = None) -> tuple[argparse.Namespace, list[str]]:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Any other option is passed through to s2_l1c_pipeline.py "
               "(--start_date, --max_cloud, --max_images, --access, --harmonize, ...).")
    p.add_argument("--sites", type=Path, required=True, help="CSV with lat, lon [, site_id]")
    p.add_argument("--output_root", type=Path, required=True,
                   help="one sub-folder per site is created here")
    p.add_argument("--limit", type=int, default=None, help="process at most N sites")
    p.add_argument("--skip", type=int, default=0, help="skip the first N sites (sharding)")
    p.add_argument("--redo", action="store_true", help="re-run sites already marked done")
    p.add_argument("--sleep", type=float, default=2.0,
                   help="pause between sites; the CDSE catalogue rate-limits bursts of queries")
    p.add_argument("--site_retries", type=int, default=3,
                   help="attempts per site when the pipeline fails (rate limit, transient I/O)")
    p.add_argument("--retry_wait", type=float, default=60.0,
                   help="seconds before the first retry, doubled at each further attempt")
    p.add_argument("--continue_on_error", action="store_true", default=True,
                   help="keep going when a site fails (default)")
    p.add_argument("--stop_on_error", dest="continue_on_error", action="store_false")
    return p.parse_known_args(argv)


def main(argv: list[str] | None = None) -> int:
    args, passthrough = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    for flag in ("--lat", "--lon", "--output_dir"):
        if any(a == flag or a.startswith(flag + "=") for a in passthrough):
            raise SystemExit(f"{flag} is set per site by this script; remove it")

    # A dry run downloads nothing, so it must neither read nor write the progress log.
    dry_run = any(a == "--dry_run" for a in passthrough)
    sites = read_sites(args.sites, args.limit, args.skip)
    args.output_root.mkdir(parents=True, exist_ok=True)
    progress = args.output_root / "sites_progress.csv"
    already = set() if args.redo or dry_run else done_sites(progress)

    LOG.info("%d site(s) from %s, %d already done, output -> %s",
             len(sites), args.sites, len(already), args.output_root)

    failures = 0
    for i, site in enumerate(sites, 1):
        tag = f"[{i}/{len(sites)}] {site['site_id']}"
        if site["site_id"] in already:
            LOG.info("%s already done, skipping", tag)
            continue

        out_dir = args.output_root / site["site_id"]
        argv_site = ["--lat", repr(site["lat"]), "--lon", repr(site["lon"]),
                     "--output_dir", str(out_dir), *passthrough]
        LOG.info("%s lat=%.5f lon=%.5f", tag, site["lat"], site["lon"])
        start = time.time()
        for attempt in range(1, args.site_retries + 1):
            if args.sleep and (i > 1 or attempt > 1):
                time.sleep(args.sleep)
            try:
                code = pipeline.main(argv_site)
            except KeyboardInterrupt:
                LOG.warning("Interrupted. Re-run the same command to resume.")
                return 130
            except SystemExit as exc:  # argparse error: the whole run is misconfigured
                return int(exc.code or 2)
            except Exception as exc:
                LOG.error("%s crashed: %s", tag, exc)
                code = 2
            # Exit code 1 means some dates failed inside the site; the pipeline itself retries
            # those on the next run, so only a fatal error (2) is worth another attempt here.
            if code != 2 or attempt == args.site_retries:
                break
            wait = args.retry_wait * 2 ** (attempt - 1)
            LOG.warning("%s failed, retrying in %.0fs (attempt %d/%d)",
                        tag, wait, attempt + 1, args.site_retries)
            time.sleep(wait)

        n_files = len(list(out_dir.glob("*.npz"))) if out_dir.exists() else 0
        status = "done" if code == 0 else "partial" if code == 1 else "failed"
        failures += code != 0
        if not dry_run:
            append_progress(progress, {
                "site_id": site["site_id"], "lat": site["lat"], "lon": site["lon"],
                "exit_code": code, "status": status, "n_files": n_files,
                "seconds": round(time.time() - start, 1),
                "finished_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            })
        LOG.info("%s %s (%d file(s), %.0fs)", tag, status, n_files, time.time() - start)
        if code != 0 and not args.continue_on_error:
            return code

    LOG.info("Finished: %d site(s), %d with failures. Progress log: %s",
             len(sites), failures, progress)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
