import io
import unittest


def _pdf_fixture() -> bytes:
    """Build a tiny three-page PDF with a blank middle page."""
    objects = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog = add(b"<< /Type /Catalog /Pages 2 0 R >>")
    pages = add(b"<< /Type /Pages /Kids [3 0 R 4 0 R 5 0 R] /Count 3 >>")
    for content_id in (6, 7, 8):
        add(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 9 0 R >> >> /Contents "
            + str(content_id).encode("ascii")
            + b" 0 R >>"
        )
    for text in (b"Page one", b"", b"Page three"):
        stream = b"BT /F1 18 Tf 72 720 Td (" + text + b") Tj ET"
        add(b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream")
    add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    result = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(f"{number} 0 obj\n".encode("ascii"))
        result.extend(body)
        result.extend(b"\nendobj\n")
    xref = len(result)
    result.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    result.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        result.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    result.extend(
        b"trailer\n<< /Size 10 /Root 1 0 R >>\nstartxref\n"
        + str(xref).encode("ascii")
        + b"\n%%EOF\n"
    )
    return bytes(result)


class ExtractTests(unittest.TestCase):
    def test_html_slide_units_keep_empty_numbers_notes_and_unique_anchors(self):
        from tools.artifact_search.extract import Unit, extract

        html = b"""
        <html><head><title>Deck</title><style>ignore</style></head><body>
          <div class='slides'>
            <section class='slide' id='intro'><h1>Intro</h1><p>Hello <em>world</em>.</p>
              <aside class='notes'>Remember this.</aside>
              <section class='slide' id='nested'>Duplicate me never.</section>
            </section>
            <section class='slide'><script>ignore()</script></section>
            <section class='slide' id='intro'><h1>Last</h1><nav>ignore nav</nav><p>Visible</p></section>
          </div>
        </body></html>
        """
        units = extract(html, ".html", "deck.html")

        self.assertEqual([unit.kind for unit in units], ["slide", "slide", "slide"])
        self.assertEqual([unit.number for unit in units], [1, 2, 3])
        self.assertEqual([unit.anchor for unit in units], [None, None, None])
        self.assertEqual(units[0].title, "Intro")
        self.assertIn("Hello world.", units[0].text)
        self.assertIn("Remember this.", units[0].text)
        self.assertEqual(units[1].text, "")
        self.assertIn("Last", units[2].text)
        self.assertNotIn("ignore", units[0].text)
        self.assertNotIn("Duplicate me", units[0].text)

    def test_reveal_leaf_sections_get_actual_unique_id_anchor(self):
        from tools.artifact_search.extract import extract

        html = b"<div class='reveal'><div class='slides'><section id='one'><h2>One</h2></section><section><h2>Two</h2></section></div></div>"
        units = extract(html, "html", "reveal.html")
        self.assertEqual([unit.anchor for unit in units], ["one", None])
        self.assertEqual([unit.title for unit in units], ["One", "Two"])

    def test_inline_letter_styling_preserves_words_and_blocks_separate(self):
        from tools.artifact_search.extract import extract
        html = b"<section class='slide' id='letters'><h2>Counting</h2><p>st<span>r</span>awbe<span>rr</span>y</p><p>Navier-Stokes</p></section>"
        units = extract(html, '.html', 'letters.html')
        self.assertEqual(units[0].text, 'Counting strawberry Navier-Stokes')
        self.assertEqual(units[0].anchor, 'letters')

    def test_excessive_html_nesting_is_a_sanitized_extraction_failure(self):
        from tools.artifact_search.extract import extract, ExtractionError
        with self.assertRaises(ExtractionError):
            extract(b'<div>' * 2000 + b'content' + b'</div>' * 2000, '.html', 'nested.html')

    def test_non_slide_html_falls_back_to_sections(self):
        from tools.artifact_search.extract import extract

        units = extract(b"<main><section><h1>A</h1><p>Alpha</p></section><section>Beta</section></main>", ".html", "x.html")
        self.assertEqual([(u.kind, u.number, u.title) for u in units], [("section", 1, "A"), ("section", 2, "")])
        self.assertIn("Alpha", units[0].text)

    def test_pdf_pages_keep_original_numbers_while_omitting_blank_page(self):
        from tools.artifact_search.extract import extract

        units = extract(_pdf_fixture(), ".pdf", "fixture.pdf")
        self.assertEqual([(unit.kind, unit.number) for unit in units], [("page", 1), ("page", 3)])
        self.assertIn("Page one", units[0].text)
        self.assertIn("Page three", units[1].text)

    def test_pdf_failure_is_sanitized(self):
        from tools.artifact_search.extract import ExtractionError, extract

        with self.assertRaises(ExtractionError) as raised:
            extract(b"%PDF-1.7\nthis is corrupt", ".pdf", "secret-document.pdf")
        self.assertNotIn("secret-document", str(raised.exception))
        self.assertNotIn("this is corrupt", str(raised.exception))

    def test_text_and_unsupported_extensions(self):
        from tools.artifact_search.extract import ExtractionError, extract

        self.assertEqual(extract(b"plain text", ".txt", "x.txt")[0].text, "plain text")
        with self.assertRaises(ExtractionError):
            extract(b"data", ".docx", "x.docx")


if __name__ == "__main__":
    unittest.main()
