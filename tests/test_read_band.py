import unittest

import numpy as np
from rasterio.transform import Affine

from s2_l1c_pipeline import NODATA
from support import raster, small_cropper


class ReadBandValidityTests(unittest.TestCase):
    """SpatialCropper.read_band: per-band validity, independent of the other bands."""

    def setUp(self):
        self.cropper = small_cropper(size_px=12)
        self.ulx, self.uly = self.cropper.ulx, self.cropper.uly

    def aligned(self, array, **kwargs):
        return raster(array, self.cropper.transform, self.cropper.crs, **kwargs)

    def test_raster_mask_and_declared_nodata_are_both_applied(self):
        array = np.arange(1, 145, dtype=np.uint16).reshape(12, 12)
        array[0, 0] = 0
        mask = np.full((12, 12), 255, dtype=np.uint8)
        mask[1, 1] = 0
        with self.aligned(array, nodata=0, mask=mask) as src:
            read = self.cropper.read_band(src, 10)

        self.assertIn("raster_mask", read.mask_source)
        self.assertFalse(read.valid[0, 0])
        self.assertTrue(read.nodata[0, 0])
        self.assertFalse(read.valid[1, 1])
        self.assertTrue(read.nodata[1, 1])
        self.assertEqual(read.data[1, 1], NODATA)
        # 10 m on the grid lattice: valid samples are copied bit-for-bit.
        np.testing.assert_array_equal(read.data[read.valid], array[read.valid])
        self.assertEqual(int(read.valid.sum()), 142)

    def test_zero_is_nodata_when_source_declares_nothing(self):
        array = np.full((12, 12), 1234, dtype=np.uint16)
        array[3, 4] = 0
        with self.aligned(array) as src:
            read = self.cropper.read_band(src, 10)

        self.assertEqual(read.mask_source, "fallback_nodata_0")
        self.assertTrue(read.nodata[3, 4])
        self.assertFalse(read.valid[3, 4])
        self.assertEqual(int(read.valid.sum()), 143)

    def test_declared_nodata_value_replaces_the_zero_fallback(self):
        array = np.full((12, 12), 1234, dtype=np.uint16)
        array[5, 5] = 7
        with self.aligned(array, nodata=7) as src:
            read = self.cropper.read_band(src, 10)

        self.assertEqual(read.mask_source, "src_nodata")
        self.assertTrue(read.nodata[5, 5])
        self.assertEqual(int(read.valid.sum()), 143)

    def test_pixels_outside_the_raster_are_nodata(self):
        # Raster covers grid columns -2..5 only.
        transform = Affine(10, 0, self.ulx - 20, 0, -10, self.uly)
        with raster(np.full((12, 8), 500, dtype=np.uint16), transform, self.cropper.crs) as src:
            read = self.cropper.read_band(src, 10)

        self.assertTrue(read.valid[:, :6].all())
        self.assertTrue(read.nodata[:, 6:].all())
        self.assertFalse(read.valid[:, 6:].any())

    def test_aoi_entirely_outside_the_raster(self):
        transform = Affine(10, 0, self.ulx + 50_000, 0, -10, self.uly)
        with raster(np.full((12, 12), 500, dtype=np.uint16), transform, self.cropper.crs) as src:
            read = self.cropper.read_band(src, 10)

        self.assertEqual(read.mask_source, "outside_raster")
        self.assertTrue(read.nodata.all())
        self.assertFalse(read.valid.any())

    def test_resampled_pixel_with_partial_kernel_support_is_invalid(self):
        # 20 m band starting 2 source pixels before the grid, with one nodata sample at (5, 5),
        # i.e. under target rows/cols 6-7.
        array = (1000 + np.arange(100, dtype=np.uint16).reshape(10, 10))
        array[5, 5] = 0
        transform = Affine(20, 0, self.ulx - 40, 0, -20, self.uly + 40)
        with raster(array, transform, self.cropper.crs) as src:
            read = self.cropper.read_band(src, 20)

        self.assertTrue(read.nodata[6:8, 6:8].all())
        # Bilinear kernel of (6, 5) includes the hole: invalid, yet a source sample exists.
        self.assertFalse(read.valid[6, 5])
        self.assertFalse(read.nodata[6, 5])
        self.assertEqual(read.data[6, 5], NODATA)
        # Kernel of (6, 4) does not touch the hole.
        self.assertTrue(read.valid[6, 4])
        self.assertTrue(read.valid[2, 2])
        self.assertEqual(int(read.nodata.sum()), 4)


class ReadMaskTests(unittest.TestCase):
    def test_categorical_mask_is_resampled_nearest_with_all_layers(self):
        cropper = small_cropper(size_px=12)
        layers = np.zeros((3, 4, 4), dtype=np.uint8)
        layers[0, 1, 1] = 1
        layers[1, 2, 2] = 9
        transform = Affine(60, 0, cropper.ulx - 60, 0, -60, cropper.uly + 60)
        with raster(layers, transform, cropper.crs) as src:
            read = cropper.read_mask(src)

        self.assertEqual(read.layers.shape, (3, 12, 12))
        self.assertTrue(read.covered.all())
        self.assertEqual(set(np.unique(read.layers)), {0, 1, 9})
        self.assertTrue((read.layers[0, :6, :6] == 1).all())
        self.assertTrue((read.layers[1, 6:, 6:] == 9).all())
        self.assertFalse(read.layers[2].any())


if __name__ == "__main__":
    unittest.main()
