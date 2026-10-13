import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import xcur_upscale


class RealESRGANPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cursor-hd-preflight-")
        self.model = Path(self.temp.name) / "RealESRGAN_x4plus.pth"
        self.model.write_bytes(b"checkpoint fixture")

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_model_fails_before_attempting_to_initialize_runtime(self):
        missing = str(Path(self.temp.name) / "missing.pth")
        with patch("xcur_upscale._ensure_realesrgan_upsampler") as ensure:
            with self.assertRaisesRegex(RuntimeError, "No themes were changed"):
                xcur_upscale.preflight_realesrgan(missing)
        ensure.assert_not_called()

    def test_preflight_initializes_model_and_executes_gpu_probe(self):
        fake_upsampler = Mock()
        fake_upsampler.enhance.return_value = (np.ones((128, 128, 3), dtype=np.uint8), None)
        fake_torch = SimpleNamespace(cuda=SimpleNamespace(synchronize=Mock()))
        with patch("xcur_upscale._ensure_realesrgan_upsampler", return_value=fake_upsampler) as ensure, \
             patch.dict(sys.modules, {"torch": fake_torch}):
            result = xcur_upscale.preflight_realesrgan(str(self.model))

        ensure.assert_called_once_with(str(self.model))
        fake_upsampler.enhance.assert_called_once()
        probe, kwargs = fake_upsampler.enhance.call_args
        self.assertEqual(probe[0].shape, (32, 32, 3))
        self.assertEqual(kwargs["outscale"], 4)
        fake_torch.cuda.synchronize.assert_called_once_with()
        self.assertEqual(result["backend"], "realesrgan")
        self.assertEqual(result["cuda_test"], "passed")

    def test_failed_gpu_probe_is_reported_before_modification(self):
        fake_upsampler = Mock()
        fake_upsampler.enhance.side_effect = RuntimeError("synthetic CUDA kernel failure")
        fake_torch = SimpleNamespace(cuda=SimpleNamespace(synchronize=Mock()))
        with patch("xcur_upscale._ensure_realesrgan_upsampler", return_value=fake_upsampler), \
             patch.dict(sys.modules, {"torch": fake_torch}):
            with self.assertRaisesRegex(RuntimeError, "before any themes were changed") as caught:
                xcur_upscale.preflight_realesrgan(str(self.model))
        self.assertIn("synthetic CUDA kernel failure", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
