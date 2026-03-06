from __future__ import annotations

import unittest

from checkurl.utils import (
    extract_html_labeled_text,
    extract_status_code_from_html,
    find_html_failure_message,
)


class HtmlParsingTests(unittest.TestCase):
    def test_extract_labeled_text_from_table(self) -> None:
        raw_html = """
        <table>
          <tr><th>返回状态码</th><td><span>200</span></td></tr>
          <tr><th>服务器IP</th><td>1.2.3.4</td></tr>
        </table>
        """
        self.assertEqual(extract_html_labeled_text(raw_html, "服务器IP"), "1.2.3.4")

    def test_extract_labeled_text_from_plain_lines(self) -> None:
        raw_html = "<div>返回状态码： 301</div>"
        self.assertEqual(extract_html_labeled_text(raw_html, "返回状态码"), "301")

    def test_extract_status_code_with_multiple_fallbacks(self) -> None:
        raw_html = '<div class="x" data-status-code="404">状态异常</div>'
        self.assertEqual(extract_status_code_from_html(raw_html), 404)

    def test_find_failure_message_from_plain_text(self) -> None:
        raw_html = "<p>网站无法访问，请稍后重试</p>"
        self.assertEqual(find_html_failure_message(raw_html, ["网站无法访问", "验证码"]), "网站无法访问")


if __name__ == "__main__":
    unittest.main()
