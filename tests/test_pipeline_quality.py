import unittest

import numpy as np

from s2_l1c_pipeline import (BANDS, CLASSI_MASK, QUALIT_LAYERS, SATURATED_DN, AcquisitionData,
                             BandRead, CDSEClient, MaskRead, PipelineRunner)
from support import cli_args, small_cropper, stac_item

N = len(BANDS)
BIT = {name: 1 << i for i, name in enumerate(QUALIT_LAYERS)}


def acquisition_data(**overrides) -> AcquisitionData:
    shape = (N, 4, 4)
    valid = overrides.pop("valid", np.ones(shape, dtype=bool))
    fields = dict(data=np.full(shape, 1500, dtype=np.uint16), valid=valid, nodata=~valid)
    fields.update(overrides)
    return AcquisitionData(**fields)


class QualityControlTests(unittest.TestCase):
    def setUp(self):
        self.runner = PipelineRunner(cli_args(".", "--min_valid_fraction", "0"),
                                     CDSEClient("s3"), small_cropper())

    def qc(self, result, **thresholds):
        for key, value in thresholds.items():
            setattr(self.runner.args, key, value)
        return self.runner.quality_control(None, result)

    def test_any_band_and_all_bands_validity_differ(self):
        valid = np.ones((N, 4, 4), dtype=bool)
        valid[0, 0, 0] = False  # B01
        valid[1, 0, 1] = False  # B02
        report = self.qc(acquisition_data(valid=valid))

        self.assertEqual(report["valid_fraction_any_band"], 1.0)
        self.assertAlmostEqual(report["valid_fraction_all_bands"], 14 / 16)
        self.assertAlmostEqual(report["valid_fraction_by_band"]["B01"], 15 / 16)
        self.assertEqual(report["valid_fraction_by_band"]["B03"], 1.0)
        self.assertAlmostEqual(report["nodata_fraction_by_band"]["B02"], 1 / 16)
        self.assertEqual(report["quality_mask_status"], "unavailable")
        self.assertIsNone(report["cloud_fraction"])
        self.assertIsNone(report["shadow_fraction"])
        self.assertEqual(report["rejection_reasons"], [])

    def test_min_valid_fraction_uses_all_bands(self):
        valid = np.ones((N, 4, 4), dtype=bool)
        valid[3, 1:3, 1:3] = False
        report = self.qc(acquisition_data(valid=valid), min_valid_fraction=0.9)
        self.assertEqual(report["rejection_reasons"], ["valid_fraction_all_bands_below_threshold"])

    def test_no_valid_pixels(self):
        report = self.qc(acquisition_data(valid=np.zeros((N, 4, 4), dtype=bool)))
        self.assertIn("no_valid_pixels", report["rejection_reasons"])
        self.assertIsNone(report["per_band_min"]["B01"])

    def test_edge_nodata_fraction_counts_only_the_border(self):
        valid = np.ones((N, 4, 4), dtype=bool)
        valid[5, 1, 1] = False  # interior
        self.assertEqual(self.qc(acquisition_data(valid=valid))["edge_nodata_fraction"], 0.0)
        valid[5, 0, 0] = False  # border ring of a 4x4 grid holds 12 pixels
        report = self.qc(acquisition_data(valid=valid), max_edge_nodata_fraction=0.05)
        self.assertAlmostEqual(report["edge_nodata_fraction"], 1 / 12)
        self.assertEqual(report["rejection_reasons"], ["edge_nodata_fraction_above_threshold"])

    def test_band_statistics_ignore_invalid_samples(self):
        data = np.full((N, 4, 4), 1500, dtype=np.uint16)
        valid = np.ones((N, 4, 4), dtype=bool)
        data[0, 0, 0], valid[0, 0, 0] = 9, False
        data[0, 3, 3] = 4000
        report = self.qc(acquisition_data(data=data, valid=valid))
        self.assertEqual(report["per_band_min"]["B01"], 1500)
        self.assertEqual(report["per_band_max"]["B01"], 4000)
        self.assertEqual(report["per_band_p50"]["B01"], 1500.0)

    def test_saturation_is_measured_on_valid_raw_samples(self):
        data = np.full((N, 4, 4), 1500, dtype=np.uint16)
        valid = np.ones((N, 4, 4), dtype=bool)
        data[2, 1, 1] = SATURATED_DN
        data[2, 2, 2], valid[2, 2, 2] = SATURATED_DN, False  # invalid: not counted
        report = self.qc(acquisition_data(data=data, valid=valid), max_saturated_fraction=0.001)
        self.assertAlmostEqual(report["saturation_fraction"], 1 / (N * 16 - 1))
        self.assertAlmostEqual(report["saturation_fraction_by_band"]["B03"], 1 / 15)
        self.assertEqual(report["rejection_reasons"], ["saturation_fraction_above_threshold"])

    def test_cloud_fractions_from_classification_mask(self):
        classi = np.zeros((3, 4, 4), dtype=np.uint8)
        classi[0, 0, :2] = 1  # opaque
        classi[1, 3, 3] = 1   # cirrus
        classi[2, 2, 2] = 1   # snow
        result = acquisition_data(classi=classi, quality_mask_status="partial")
        report = self.qc(result, max_cloud_fraction=0.1)
        self.assertAlmostEqual(report["cloud_fraction"], 3 / 16)
        self.assertAlmostEqual(report["opaque_cloud_fraction"], 2 / 16)
        self.assertAlmostEqual(report["cirrus_fraction"], 1 / 16)
        self.assertAlmostEqual(report["snow_ice_fraction"], 1 / 16)
        self.assertEqual(report["rejection_reasons"], ["pixel_cloud_fraction_above_threshold"])

    def test_quality_flags_drive_artefact_and_saturation(self):
        bits = np.zeros((N, 4, 4), dtype=np.uint8)
        bits[4, 0, :2] = BIT["defective"]
        bits[4, 1, 0] = BIT["saturated_l1a"]
        result = acquisition_data(qualit_bits=bits, qualit_available=np.ones(N, dtype=bool))
        report = self.qc(result, max_artefact_fraction=0.001)
        self.assertAlmostEqual(report["artefact_fraction"], 2 / (N * 16))
        self.assertAlmostEqual(report["saturation_fraction"], 1 / (N * 16))
        self.assertAlmostEqual(report["quality_flag_fractions"]["defective"], 2 / (N * 16))
        self.assertEqual(report["rejection_reasons"], ["artefact_fraction_above_threshold"])

    def test_strict_mode_rejects_missing_or_partial_masks(self):
        self.runner.args.require_quality_masks = True
        report = self.qc(acquisition_data())
        self.assertEqual(report["rejection_reasons"], ["quality_masks_unavailable"])
        report = self.qc(acquisition_data(quality_mask_status="partial"))
        self.assertEqual(report["rejection_reasons"], ["quality_masks_partial"])


class ProcessQualityMaskTests(unittest.TestCase):
    """process(): QI_DATA masks refine the per-band validity (reads are faked)."""

    def setUp(self):
        self.cropper = small_cropper()
        self.runner = PipelineRunner(cli_args(".", "--workers", "3"), CDSEClient("s3"), self.cropper)
        [self.acq] = self.runner.discover([stac_item(self.cropper, "OK", 1)])
        self.calls = []

    def fake_read(self, acq, name, workdir, reader):
        self.calls.append(name)
        full = np.ones((4, 4), dtype=bool)
        if name in BANDS:
            return BandRead(np.full((4, 4), 2000, dtype=np.uint16), full, ~full, "fallback_nodata_0")
        if name == CLASSI_MASK:
            layers = np.zeros((3, 4, 4), dtype=np.uint8)
            layers[0, 0, 0] = 1
            return MaskRead(layers, full)
        if name.startswith("MSK_DETFOO"):
            layers = np.full((1, 4, 4), 3, dtype=np.uint8)
            if name.endswith("B01"):
                layers[0, 3, 3] = 0  # outside every detector
            return MaskRead(layers, full)
        layers = np.zeros((len(QUALIT_LAYERS), 4, 4), dtype=np.uint8)
        if name.endswith("B02"):
            layers[QUALIT_LAYERS.index("nodata"), 2, 2] = 1
            layers[QUALIT_LAYERS.index("defective"), 1, 1] = 1
        return MaskRead(layers, full)

    def test_masks_update_validity_and_status(self):
        del self.acq.quality_assets["MSK_DETFOO_B12"]
        self.runner._read_asset = self.fake_read
        result = self.runner.process(self.acq)

        self.assertEqual(result.quality_mask_status, "partial")
        self.assertTrue(result.nodata[0, 3, 3])
        self.assertFalse(result.valid[0, 3, 3])
        self.assertEqual(result.data[0, 3, 3], 0)
        self.assertTrue(result.nodata[1, 2, 2])
        self.assertTrue(result.valid[1, 1, 1])  # defective is an artefact, not nodata
        self.assertTrue(result.qualit_bits[1, 1, 1] & BIT["defective"])
        self.assertEqual(result.mask_source["B01"], "fallback_nodata_0+MSK_DETFOO+MSK_QUALIT")
        self.assertEqual(result.mask_source["B12"], "fallback_nodata_0+MSK_QUALIT")
        self.assertEqual(int(result.valid.sum()), N * 16 - 2)

        report = self.runner.quality_control(self.acq, result)
        self.assertAlmostEqual(report["valid_fraction_all_bands"], 14 / 16)
        self.assertAlmostEqual(report["artefact_fraction"], 1 / (N * 16 - 2))
        self.assertAlmostEqual(report["cloud_fraction"], 1 / 16)

    def test_unreachable_qi_data_skips_per_band_masks(self):
        def read(acq, name, workdir, reader):
            if name == CLASSI_MASK:
                self.calls.append(name)
                raise LookupError("no such key")
            return self.fake_read(acq, name, workdir, reader)

        self.runner._read_asset = read
        result = self.runner.process(self.acq)

        self.assertEqual(result.quality_mask_status, "unavailable")
        self.assertEqual(sorted(self.calls), sorted([CLASSI_MASK, *BANDS]))
        self.assertIsNone(result.qualit_bits)
        self.assertEqual(len(result.quality_errors), 1)


if __name__ == "__main__":
    unittest.main()
