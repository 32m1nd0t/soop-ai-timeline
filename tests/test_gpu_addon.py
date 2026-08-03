import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from soop_timeline.services import transcription


class GPUAddonTests(unittest.TestCase):
    def tearDown(self) -> None:
        transcription._NVIDIA_RUNTIME_PATHS_CONFIGURED = False
        transcription._NVIDIA_DLL_DIRECTORY_HANDLES.clear()

    def test_addon_requires_all_runtime_dlls(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {transcription.GPU_ADDON_DIR_ENVIRONMENT: directory},
        ):
            runtime_dir = Path(directory)
            for filename in transcription.GPU_RUNTIME_DLLS[:-1]:
                (runtime_dir / filename).touch()
            self.assertFalse(transcription.gpu_addon_installed())
            (runtime_dir / transcription.GPU_RUNTIME_DLLS[-1]).touch()
            self.assertTrue(transcription.gpu_addon_installed())

    def test_external_addon_directory_is_added_before_python_packages(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {transcription.GPU_ADDON_DIR_ENVIRONMENT: directory},
        ), patch.object(transcription.os, "name", "nt"), patch.object(
            transcription.importlib.util,
            "find_spec",
            return_value=None,
        ), patch.object(transcription.os, "add_dll_directory") as add_directory:
            transcription._NVIDIA_RUNTIME_PATHS_CONFIGURED = False
            discovered = transcription.configure_nvidia_runtime_paths()

        self.assertEqual(discovered, (Path(directory).resolve(),))
        add_directory.assert_called_once_with(str(Path(directory).resolve()))


if __name__ == "__main__":
    unittest.main()
