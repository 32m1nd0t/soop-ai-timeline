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

    def test_readme_identifies_app_as_unofficial(self) -> None:
        readme = (self.root / "README.md").read_text(encoding="utf-8")

        self.assertIn("SOOP이 제작·승인·후원한 공식 앱이 아니며", readme)
        self.assertIn("SOOP과 제휴 관계가 없습니다", readme)


if __name__ == "__main__":
    unittest.main()
