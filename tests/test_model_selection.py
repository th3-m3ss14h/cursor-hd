import tempfile
import unittest
from pathlib import Path

import model_utils


class LocalModelDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cursor-hd-weights-")
        self.weights = Path(self.temp.name) / "weights"
        self.weights.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def add_model(self, filename):
        path = self.weights / filename
        path.write_bytes(b"test fixture, not a real checkpoint")
        return path.resolve()

    def test_default_weights_directory_is_relative_to_the_project_not_cwd(self):
        expected = Path(model_utils.__file__).resolve().parent / "weights"
        self.assertEqual(Path(model_utils.weights_directory()), expected)

    def test_lanczos_choice_ignores_any_installed_checkpoint(self):
        self.add_model("RealESRGAN_x4plus.pth")
        backend, model_path = model_utils.resolve_backend_choice("lanczos", self.weights)
        self.assertEqual(backend, "lanczos")
        self.assertIsNone(model_path)

    def test_realesrgan_choice_without_checkpoint_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "select Lanczos"):
            model_utils.resolve_backend_choice("realesrgan", self.weights)

    def test_unknown_backend_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unsupported upscale method"):
            model_utils.resolve_backend_choice("automatic", self.weights)

    def test_unknown_pth_is_ignored(self):
        self.add_model("unrecognized_model.pth")
        self.assertEqual(model_utils.resolve_backend_choice("lanczos", self.weights), ("lanczos", None))
        with self.assertRaisesRegex(ValueError, "no supported checkpoint"):
            model_utils.resolve_backend_choice("realesrgan", self.weights)
        self.assertEqual(model_utils.available_models(self.weights), [])

    def test_preferred_x4_model_is_selected_when_gpu_mode_is_chosen(self):
        preferred = self.add_model("RealESRGAN_x4plus.pth")
        self.add_model("RealESRGAN_x4plus_anime_6B.pth")
        backend, model_path = model_utils.resolve_backend_choice("realesrgan", self.weights)
        self.assertEqual(backend, "realesrgan")
        self.assertEqual(model_path, str(preferred))

    def test_fallback_model_is_selected_in_documented_order(self):
        anime = self.add_model("RealESRGAN_x4plus_anime_6B.pth")
        self.add_model("RealESRGAN_x2plus.pth")
        backend, model_path = model_utils.resolve_backend_choice("realesrgan", self.weights)
        self.assertEqual(backend, "realesrgan")
        self.assertEqual(model_path, str(anime))

    def test_model_api_list_contains_only_supported_checkpoints(self):
        self.add_model("RealESRGAN_x4plus.pth")
        self.add_model("ignore-this.pth")
        models = model_utils.available_models(self.weights)
        self.assertEqual([model["name"] for model in models], ["RealESRGAN_x4plus.pth"])
        self.assertEqual(models[0]["known_config"]["netscale"], 4)


if __name__ == "__main__":
    unittest.main()
