import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

import numpy as np
from shapely.geometry import box, mapping

from s2_l1c_pipeline import (BANDS, CLASSI_MASK, AcquisitionData, CDSEClient, PipelineRunner,
                             parse_args)
from support import cli_args, small_cropper, stac_item


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.cropper = small_cropper()
        self.runner = PipelineRunner(cli_args("."), CDSEClient("s3"), self.cropper)

    def test_item_metadata_and_quality_mask_paths_are_parsed(self):
        [acq] = self.runner.discover([stac_item(self.cropper, "OK", 1)])

        self.assertEqual(acq.status, "discovered")
        self.assertEqual((acq.tile_id, acq.baseline, acq.source_epsg), ("31UDQ", 5.1, 32631))
        self.assertEqual(acq.relative_orbit, 94)
        self.assertNotIn("_private", acq.properties)
        self.assertEqual(len(acq.quality_assets), 2 * len(BANDS) + 1)
        s3_href, https_href = acq.quality_assets["MSK_QUALIT_B8A"]
        self.assertTrue(s3_href.endswith("/GRANULE/L1C_T31UDQ_A039559_20230101T110358/QI_DATA/MSK_QUALIT_B8A.jp2"))
        self.assertIsNone(https_href)
        self.assertTrue(acq.quality_assets[CLASSI_MASK][0].endswith("/QI_DATA/MSK_CLASSI_B00.jp2"))

    def test_old_baselines_have_no_raster_quality_masks(self):
        [acq] = self.runner.discover([stac_item(self.cropper, "OLD", 1, baseline="02.04")])
        self.assertEqual(acq.quality_assets, {})

    def test_unparseable_item_is_kept_as_rejected(self):
        item = stac_item(self.cropper, "MISSING", 1, bands=[b for b in BANDS if b != "B10"])
        [acq] = self.runner.discover([item])

        self.assertEqual((acq.status, acq.stage), ("rejected", "discovery"))
        self.assertEqual(acq.rejection_reason, "missing_band_assets:B10")
        self.assertEqual((acq.date, acq.tile_id), ("2023-01-01", "31UDQ"))
        self.assertEqual(self.runner.select(), [])


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.cropper = small_cropper()

    def select(self, items, *extra):
        runner = PipelineRunner(cli_args(".", *extra), CDSEClient("s3"), self.cropper)
        runner.discover(items)
        chosen = runner.select()
        return chosen, {a.item_id.rsplit("_", 1)[-1]: a for a in runner.acquisitions}

    def test_latest_baseline_is_chosen_before_cloud_filtering(self):
        items = [stac_item(self.cropper, "OLD", 1, cloud=5, baseline="05.00"),
                 stac_item(self.cropper, "NEW", 1, cloud=50, baseline="05.10")]
        chosen, acqs = self.select(items, "--max_cloud", "20")

        self.assertEqual(chosen, [])
        self.assertTrue(acqs["OLD"].rejection_reason.startswith("older_processing_baseline:"))
        self.assertIn(acqs["NEW"].item_id, acqs["OLD"].rejection_reason)
        self.assertEqual(acqs["NEW"].rejection_reason, "scene_cloud_cover_above_threshold")

    def test_coverage_overlap_and_max_images_reasons(self):
        far = mapping(box(10.0, 10.0, 10.1, 10.1))
        items = [stac_item(self.cropper, "FAR", 1, geometry=far),
                 stac_item(self.cropper, "BEST", 2, cloud=10),
                 stac_item(self.cropper, "OTHERZONE", 2, cloud=1, tile="30UYV", epsg=32630),
                 stac_item(self.cropper, "CLEAR", 3, cloud=0),
                 stac_item(self.cropper, "HAZY", 4, cloud=30)]
        chosen, acqs = self.select(items, "--max_images", "2", "--sort", "cloud")

        self.assertEqual([a.item_id for a in chosen], [acqs["BEST"].item_id, acqs["CLEAR"].item_id])
        self.assertTrue(all(a.status == "selected" for a in chosen))
        self.assertEqual(acqs["FAR"].rejection_reason, "aoi_coverage_below_threshold")
        self.assertEqual(acqs["OTHERZONE"].rejection_reason,
                         f"inferior_overlapping_tile:{acqs['BEST'].item_id}")
        self.assertEqual(acqs["HAZY"].rejection_reason, "max_images_limit")
        self.assertTrue(all(a.stage == "selection" for a in acqs.values()))


class FakeClient(CDSEClient):
    def __init__(self, items):
        super().__init__("s3")
        self.items = items

    def search(self, aoi_lonlat, start, end):
        return list(self.items)

    def prepare_access(self):
        pass


class RunMetadataTests(unittest.TestCase):
    """End-to-end run with fake STAC items and fake band reads."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)
        self.cropper = small_cropper()
        self.items = [
            stac_item(self.cropper, "GOOD", 1),
            stac_item(self.cropper, "BAD", 2),
            stac_item(self.cropper, "FAIL", 3),
            stac_item(self.cropper, "EXIST", 4),
            stac_item(self.cropper, "CLOUDY", 5, cloud=80),
            stac_item(self.cropper, "MISSING", 6, bands=["B02"]),
        ]
        (self.out / "2023-01-04_31UDQ.npz").write_bytes(b"")

    def tearDown(self):
        self.tmp.cleanup()

    def run_pipeline(self):
        calls = []

        def fake_process(acq):
            name = acq.item_id.rsplit("_", 1)[-1]
            calls.append(name)
            if name == "FAIL":
                raise RuntimeError("network down")
            shape = (len(BANDS), 4, 4)
            data = np.full(shape, 1500, dtype=np.uint16)
            valid = np.ones(shape, dtype=bool)
            if name == "BAD":
                valid[:, :2, :] = False
                data[~valid] = 0
            return AcquisitionData(data=data, valid=valid, nodata=~valid,
                                   mask_source=dict.fromkeys(BANDS, "fallback_nodata_0"))

        args = cli_args(self.out, "--max_cloud", "50", "--harmonize")
        runner = PipelineRunner(args, FakeClient(self.items), self.cropper)
        runner.process = fake_process
        code = runner.run()
        with open(self.out / "metadata.csv", newline="", encoding="utf-8") as fh:
            rows = {r["item_id"].rsplit("_", 1)[-1]: r for r in csv.DictReader(fh)}
        return code, calls, rows

    def test_every_acquisition_is_documented_and_resume_is_idempotent(self):
        code, calls, rows = self.run_pipeline()

        self.assertEqual(code, 1)
        self.assertEqual(sorted(calls), ["BAD", "FAIL", "GOOD"])
        statuses = {name: (r["status"], r["stage"]) for name, r in rows.items()}
        self.assertEqual(statuses, {
            "GOOD": ("saved", "output"),
            "BAD": ("rejected", "quality_control"),
            "FAIL": ("failed", "quality_control"),
            "EXIST": ("skipped", "output"),
            "CLOUDY": ("rejected", "selection"),
            "MISSING": ("rejected", "discovery"),
        })
        self.assertEqual(rows["BAD"]["rejection_reason"].split(";"),
                         ["valid_fraction_all_bands_below_threshold", "edge_nodata_fraction_above_threshold"])
        self.assertEqual(rows["BAD"]["valid_fraction_all_bands"], "0.5")
        self.assertEqual(rows["FAIL"]["rejection_reason"], "RuntimeError: network down")
        self.assertEqual(rows["EXIST"]["rejection_reason"], "output_exists_for_date:2023-01-04_31UDQ.npz")
        self.assertEqual(rows["GOOD"]["filename"], "2023-01-01_31UDQ.npz")
        self.assertEqual(rows["GOOD"]["harmonized"], "1")
        self.assertEqual(json.loads(rows["GOOD"]["valid_fraction_by_band"])["B8A"], 1.0)
        self.assertEqual(rows["GOOD"]["shadow_fraction"], "")
        self.assertFalse((self.out / "2023-01-02_31UDQ.npz").exists())

        with np.load(self.out / "2023-01-01_31UDQ.npz") as z:
            self.assertEqual(z["data"].max(), 500)  # harmonised at output, after QC
            self.assertEqual(z["valid_mask"].shape, (len(BANDS), 4, 4))
            self.assertTrue(z["valid_all"].all())
            self.assertFalse(z["nodata_mask"].any())

        # Second run: saved and QC-rejected acquisitions are not read again; failures are retried.
        code, calls, rows_again = self.run_pipeline()
        self.assertEqual(calls, ["FAIL"])
        self.assertEqual(rows_again["GOOD"], rows["GOOD"])
        self.assertEqual(rows_again["BAD"], rows["BAD"])


class CliTests(unittest.TestCase):
    def test_quality_options_and_defaults(self):
        args = parse_args(["--lat", "1", "--lon", "2", "--output_dir", "x", "--access", "s3"])
        self.assertEqual(args.min_valid_fraction, 0.99)
        self.assertEqual(args.edge_width, 32)
        self.assertFalse(args.require_quality_masks)
        args = parse_args(["--lat", "1", "--lon", "2", "--output_dir", "x", "--access", "s3",
                           "--min_valid_fraction", "0.8"])
        self.assertEqual(args.min_valid_fraction, 0.8)
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            parse_args(["--lat", "1", "--lon", "2", "--output_dir", "x", "--min_valid_fraction", "1.5"])


if __name__ == "__main__":
    unittest.main()
