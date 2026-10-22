"""
extract_full.py  (Stage A + B of the ingestion pipeline, whole PDF)

Reads EVERY page of the PDF and writes ONE markdown file containing:
  1. each page's text from pymupdf4llm (its built-in picture OCR is stripped out),
  2. OCR text from content images (our own gated OCR), placed after that page's text,
  3. HTML-comment markers (<!-- PAGE ... -->, <!-- IMAGE ... -->) so later stages
     know the page number and the quality status of each piece.

It works in batches and saves progress after each batch, so you can stop it
(Ctrl+C) and run it again later: it resumes where it left off.

Run:  python extract_full.py
Start over from scratch: delete data/full.md and data/full_progress.txt, then run again.
"""
import re
import time
from collections import Counter
from pathlib import Path

import pymupdf
import pymupdf4llm
import pytesseract
from PIL import Image

# ===================== BLOCK 1: SETTINGS (edit these) =====================
PDF_PATH = "data/Prealgebra-OpenStax.pdf"            # your PDF
OUT_PATH = "data/Prealgebra-OpenStax.md"                 # the merged markdown file
PROGRESS_PATH = "data/Prealgebra-OpenStax_progress.txt"  # remembers the next page to process
BATCH_SIZE = 25                           # pages per batch (progress is saved after each)

# Provisional icon rules. Check the "most reused images" list printed at startup.
MIN_SIZE_PT = 20        # images narrower or shorter than this (PDF points) count as icons
MAX_PAGES_REUSED = 10   # an image appearing on this many pages (or more) counts as an icon/logo

# OCR quality gate
DPI_LOW = 300           # first OCR pass resolution
DPI_HIGH = 600          # second OCR pass resolution
MIN_ALNUM = 2           # fewer letters/digits than this = "unreadable"


# ===================== BLOCK 2: SMALL HELPERS =====================
def normalize(text):
    """Collapse all whitespace runs (spaces, newlines) into single spaces."""
    return " ".join(text.split())


# pymupdf4llm may run its OWN Tesseract pass and wrap the result in these markers.
# We cut those blocks out so OCR text is produced once, by our gated step below.
BUILTIN_OCR_BLOCK = re.compile(
    r"<!--\s*Start of picture text\s*-->.*?<!--\s*End of picture text\s*-->", re.S
)


def strip_builtin_ocr(markdown):
    return BUILTIN_OCR_BLOCK.sub("", markdown)


# ===================== BLOCK 3: OCR WITH A QUALITY GATE =====================
def ocr_region(page, bbox):
    """OCR one rectangle of a page at two resolutions.
    Returns (status, text_high_dpi, text_low_dpi), where status is
      'ok'          both resolutions gave the same text
      'disagree'    the two resolutions gave different text (suspect)
      'unreadable'  almost no letters or digits found
    """
    x0, y0, x1, y1 = bbox
    clip = pymupdf.Rect(x0 - 2, y0 - 2, x1 + 2, y1 + 2)  # small margin so edge digits survive
    results = []
    for dpi in (DPI_LOW, DPI_HIGH):
        pix = page.get_pixmap(dpi=dpi, clip=clip)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        results.append(normalize(pytesseract.image_to_string(img, config="--psm 6")))
    low, high = results

    if sum(ch.isalnum() for ch in high) < MIN_ALNUM:
        return "unreadable", high, low
    if low == high:
        return "ok", high, low
    return "disagree", high, low


# ===================== BLOCK 4: TEXT EXTRACTION WITH A FALLBACK =====================
def markdown_for_pages(doc, pages):
    """Returns a list of (page_index, markdown_text_or_None), one entry per page.
    Tries the whole batch at once (fast). If that fails, retries page by page so
    one bad page does not lose the other 24."""
    try:
        chunks = pymupdf4llm.to_markdown(doc, pages=pages, page_chunks=True)
        if len(chunks) == len(pages):
            return [(i, chunk["text"]) for i, chunk in zip(pages, chunks)]
        print(f"   batch returned {len(chunks)} pages for {len(pages)} requested; retrying page by page")
    except Exception as err:
        print(f"   batch text extraction failed ({err}); retrying page by page")

    results = []
    for i in pages:
        try:
            chunk = pymupdf4llm.to_markdown(doc, pages=[i], page_chunks=True)
            results.append((i, chunk[0]["text"]))
        except Exception as err:
            print(f"   page {i}: text extraction failed ({err})")
            results.append((i, None))
    return results


# ===================== BLOCK 5: SETUP AND ICON CENSUS =====================
doc = pymupdf.open(PDF_PATH)
total = len(doc)
print(f"PDF has {total} pages")

# On how many pages does each image (identified by its xref id) appear?
# An image reused across many pages is almost certainly an icon or logo.
print("Counting image reuse across the whole PDF...")
pages_using = Counter()
size_of = {}
for i in range(total):
    ids_on_page = set()
    for info in doc[i].get_image_info(xrefs=True):
        if info["xref"] != 0:
            ids_on_page.add(info["xref"])
            size_of[info["xref"]] = (info["width"], info["height"])
    for xref in ids_on_page:
        pages_using[xref] += 1

print(f"Distinct images: {len(pages_using)}")
print("Most reused images (check these look like icons or logos):")
for xref, n in pages_using.most_common(8):
    print(f"   xref {xref} | on {n} pages | native pixels {size_of[xref]}")

# Resume support: where did the last run stop?
out_file = Path(OUT_PATH)
progress_file = Path(PROGRESS_PATH)
if progress_file.exists() and out_file.exists():
    start = int(progress_file.read_text().strip())
    print(f"Resuming at page {start}")
else:
    start = 0
    out_file.write_text("", encoding="utf-8")

# ===================== BLOCK 6: MAIN LOOP (BATCH BY BATCH) =====================
stats = Counter()
run_started = time.time()

for batch_start in range(start, total, BATCH_SIZE):
    batch = list(range(batch_start, min(batch_start + BATCH_SIZE, total)))
    batch_started = time.time()
    parts = []

    for i, text in markdown_for_pages(doc, batch):
        parts.append(f"<!-- PAGE pdf_index={i} -->")
        if text is None:
            parts.append("<!-- TEXT_EXTRACTION_FAILED -->")
            stats["pages_text_failed"] += 1
        else:
            parts.append(strip_builtin_ocr(text).strip())

        # images on this page, top to bottom, then left to right
        images = sorted(
            doc[i].get_image_info(xrefs=True),
            key=lambda info: (info["bbox"][1], info["bbox"][0]),
        )
        for info in images:
            x0, y0, x1, y1 = info["bbox"]
            if (x1 - x0) < MIN_SIZE_PT or (y1 - y0) < MIN_SIZE_PT:
                stats["skipped_small"] += 1
                continue
            if info["xref"] != 0 and pages_using[info["xref"]] >= MAX_PAGES_REUSED:
                stats["skipped_reused"] += 1
                continue

            try:
                status, high, low = ocr_region(doc[i], info["bbox"])
            except Exception as err:
                print(f"   page {i}: OCR failed on an image ({err})")
                status, high, low = "error", "", ""
            stats["image_" + status] += 1

            box = ",".join(str(round(v)) for v in info["bbox"])
            parts.append(f"<!-- IMAGE page={i} bbox={box} status={status} -->")
            if status == "error":
                parts.append("[Figure: OCR failed]")
            elif status == "unreadable":
                parts.append("[Figure: no readable text extracted]")
            elif status == "ok":
                parts.append(f"[Figure text] {high}")
            else:
                parts.append(f"[Figure text, OCR resolutions disagree] {high}")
                parts.append(f"<!-- alt_300dpi: {low} -->")

    # Save the batch, THEN record progress. If the script dies mid-batch,
    # nothing from that batch was written, so a re-run cannot create duplicates.
    with out_file.open("a", encoding="utf-8") as f:
        f.write("\n\n".join(parts) + "\n\n")
    progress_file.write_text(str(batch[-1] + 1))

    done_now = batch[-1] + 1 - start
    elapsed = time.time() - run_started
    remaining_pages = total - (batch[-1] + 1)
    eta_min = round(elapsed / done_now * remaining_pages / 60, 1)
    print(
        f"Pages {batch[0]}-{batch[-1]} done | batch {round(time.time() - batch_started)}s"
        f" | elapsed {round(elapsed / 60, 1)} min | rough time left {eta_min} min"
    )

# ===================== BLOCK 7: FINAL REPORT =====================
print("\n=== Finished ===")
print("Outcomes this run:", dict(stats))
print(f"Total minutes this run: {round((time.time() - run_started) / 60, 1)}")
print(f"Output: {OUT_PATH} ({round(out_file.stat().st_size / 1_000_000, 1)} MB)")
