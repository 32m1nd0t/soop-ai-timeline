from pathlib import Path
import unittest


class DistributionDocumentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]

    def test_required_distribution_documents_exist(self) -> None:
        privacy = (self.root / "PRIVACY.md").read_text(encoding="utf-8")
        notices = (self.root / "THIRD_PARTY_NOTICES.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("Gemini", privacy)
        self.assertIn("제3자 소프트웨어 고지", notices)
        self.assertIn("PySide6", notices)
        self.assertIn("NVIDIA cuDNN", notices)

    def test_build_spec_bundles_distribution_documents_and_licenses(self) -> None:
        spec = (self.root / "SOOPTimeline.spec").read_text(encoding="utf-8")

        self.assertIn('("PRIVACY.md", "THIRD_PARTY_NOTICES.md")', spec)
        self.assertIn("third_party_components.txt", spec)
        self.assertIn("third_party_licenses", spec)
        self.assertIn('build_mode == "installed"', spec)
        self.assertIn("bundle_gpu_runtime", spec)
        self.assertIn("COLLECT(", spec)

    def test_readme_identifies_app_as_unofficial(self) -> None:
        readme = (self.root / "README.md").read_text(encoding="utf-8")

        self.assertIn("SOOP이 제작·승인·후원한 공식 앱이 아니며", readme)
        self.assertIn("SOOP과 제휴 관계가 없습니다", readme)

    def test_build_script_rejects_native_failures_and_stale_outputs(self) -> None:
        script = (self.root / "build_exe.ps1").read_text(encoding="utf-8")

        self.assertGreaterEqual(script.count("$LASTEXITCODE -ne 0"), 5)
        self.assertIn("Remove-Item -LiteralPath $exe -Force", script)
        self.assertIn("Remove-Item -LiteralPath $manifestPath -Force", script)

    def test_release_builds_use_pinned_dependencies(self) -> None:
        constraints = (self.root / "build-constraints.txt").read_text(encoding="utf-8")
        pins = {
            line.split("==", 1)[0].lower()
            for line in constraints.splitlines()
            if "==" in line and not line.startswith("#")
        }

        for package in ("huggingface-hub", "pyside6", "ctranslate2", "av"):
            self.assertIn(package, pins)
        for path in (
            ".github/workflows/release.yml",
            "build_exe.ps1",
            "build_installed_app.ps1",
        ):
            script = (self.root / path).read_text(encoding="utf-8")
            self.assertIn("pip install -c build-constraints.txt", script, path)

    def test_installer_replaces_previous_runtime_files(self) -> None:
        installer = (self.root / "installer" / "SOOPTimeline.iss").read_text(
            encoding="utf-8"
        )

        self.assertIn("[InstallDelete]", installer)
        self.assertIn('Name: "{app}\\_internal"', installer)

    def test_relaunched_app_does_not_inherit_redirection_guard(self) -> None:
        installer = (self.root / "installer" / "SOOPTimeline.iss").read_text(
            encoding="utf-8"
        )

        self.assertIn("RedirectionGuard=no", installer)

    def test_installer_and_release_workflow_include_verified_update_assets(self) -> None:
        installer = (self.root / "installer" / "SOOPTimeline.iss").read_text(
            encoding="utf-8"
        )
        gpu_installer = (
            self.root / "installer" / "SOOPTimeline-GPU-Addon.iss"
        ).read_text(encoding="utf-8")
        build = (self.root / "build_installer.ps1").read_text(encoding="utf-8")
        workflow = (self.root / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("PrivilegesRequired=lowest", installer)
        self.assertIn("CloseApplications=yes", installer)
        self.assertIn("dist\\SOOPTimeline\\*", installer)
        self.assertIn("gpu-runtime", gpu_installer)
        self.assertIn("SOOPTimeline-Setup.exe", build)
        self.assertIn("SOOPTimeline-GPU-Addon.exe", build)
        self.assertIn("installer_sha256", build)
        self.assertIn("gpu_addon_sha256", build)
        self.assertIn('Split-Path -Leaf $pythonDirectory', build)
        self.assertIn('$pythonRoot = $pythonDirectory', build)
        self.assertIn("Smoke-test silent installation", workflow)
        self.assertGreaterEqual(workflow.count("SOOPTimeline-Setup.exe"), 4)
        self.assertGreaterEqual(workflow.count("SOOPTimeline-GPU-Addon.exe"), 4)


if __name__ == "__main__":
    unittest.main()
