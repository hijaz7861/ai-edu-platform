import sys
import os
import unittest
import glob
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import init_db  # noqa: E402
from services import core, ingestion  # noqa: E402

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
if not os.path.exists(FONT_PATH):
    candidates = glob.glob("/usr/share/fonts/truetype/dejavu/DejaVuSans*.ttf")
    FONT_PATH = candidates[0] if candidates else None

TMP_DIR = os.path.join(os.path.dirname(__file__), "_tmp_images")


def make_page_image(path, lines, font_size=28):
    img = Image.new("RGB", (900, 500), color="white")
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(FONT_PATH, font_size) if FONT_PATH else ImageFont.load_default()
    y = 30
    for line in lines:
        draw.text((40, y), line, fill="black", font=font)
        y += font_size + 14
    img.save(path)


class TestIngestionOCR(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.makedirs(TMP_DIR, exist_ok=True)

    def setUp(self):
        self.conn = init_db(":memory:")
        self.inst = core.create_institution(self.conn, "Springfield Academy", "SPR")
        self.book = ingestion.create_book(self.conn, self.inst, "General Science Grade 8", "teacher-1")

    def tearDown(self):
        self.conn.close()

    def test_real_ocr_extracts_known_text(self):
        img_path = os.path.join(TMP_DIR, "page1.png")
        make_page_image(img_path, [
            "Chapter 3: Photosynthesis",
            "Plants convert sunlight into chemical energy.",
            "This process occurs mainly in the leaves.",
        ])

        bf = ingestion.add_book_file(self.conn, self.book, "image", img_path, "page1.png", "abc123", "teacher-1")
        page_id = ingestion.add_book_page(self.conn, bf, 1, img_path)

        result = ingestion.run_ocr(self.conn, page_id, img_path)

        # This is real Tesseract output being asserted against, not a mock
        self.assertIn("Photosynthesis", result["raw_text"])
        self.assertIn("sunlight", result["raw_text"].lower())
        self.assertGreater(result["confidence"], 0)
        self.assertNotIn("blank_page", result["flags"])

        page = self.conn.execute("SELECT * FROM book_pages WHERE id = ?", (page_id,)).fetchone()
        self.assertEqual(page["status"], "ocr_done")

    def test_blank_page_is_flagged_not_hidden(self):
        img_path = os.path.join(TMP_DIR, "blank.png")
        blank = Image.new("RGB", (900, 500), color="white")
        blank.save(img_path)

        bf = ingestion.add_book_file(self.conn, self.book, "image", img_path, "blank.png", "def456", "teacher-1")
        page_id = ingestion.add_book_page(self.conn, bf, 2, img_path)

        result = ingestion.run_ocr(self.conn, page_id, img_path)

        self.assertIn("blank_page", result["flags"])
        page = self.conn.execute("SELECT * FROM book_pages WHERE id = ?", (page_id,)).fetchone()
        self.assertEqual(page["status"], "flagged")

    def test_chapter_and_topic_detection_from_real_ocr_text(self):
        img_path = os.path.join(TMP_DIR, "page3.png")
        make_page_image(img_path, [
            "Chapter 3: Photosynthesis",
            "Light Reactions",
            "The light reactions occur in the thylakoid.",
        ])
        bf = ingestion.add_book_file(self.conn, self.book, "image", img_path, "page3.png", "ghi789", "teacher-1")
        page_id = ingestion.add_book_page(self.conn, bf, 3, img_path)
        result = ingestion.run_ocr(self.conn, page_id, img_path)

        elements = ingestion.detect_structure(self.conn, self.book, page_id, result["raw_text"], page_number=3)
        types_found = {e["type"] for e in elements}

        self.assertIn("chapter", types_found, f"expected a detected chapter in: {result['raw_text']!r}")


if __name__ == "__main__":
    unittest.main()
