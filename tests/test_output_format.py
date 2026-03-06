from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from checkurl.models import CheckResult, UrlItem
from checkurl.output import write_output


class OutputFormatTests(unittest.TestCase):
    def test_output_contains_provider_latency_and_detail(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "result.txt"
            items = [UrlItem(index=0, raw_url="example.com", normalized_url="http://example.com")]
            result_map = {
                "http://example.com": CheckResult(
                    normalized_url="http://example.com",
                    status_code=200,
                    provider="xiarou",
                    detail="ok",
                    checked_at="2026-01-01T00:00:00+00:00",
                    latency_ms=123.45,
                )
            }

            write_output(items, result_map, output_path)

            content = output_path.read_text(encoding="utf-8").strip()
            fields = content.split("\t")
            self.assertEqual(len(fields), 5)
            self.assertEqual(fields[0], "200")
            self.assertEqual(fields[1], "example.com")
            self.assertEqual(fields[2], "xiarou")
            self.assertEqual(fields[3], "123.45")
            self.assertEqual(fields[4], "ok")


if __name__ == "__main__":
    unittest.main()
