"""
chunk_md_v5.py  (Step 2, v5: structure from bookmarks + known part titles, not heading levels)

Why v3: the converter picked heading levels from font size, so levels are unreliable
(the same "Solution" title shows up at levels 3 to 6, section titles sit at ## or ###,
ⓐ/ⓑ labels look like headings, and a garbled graphic became a fake heading).
Titles are consistent, though, and the PDF bookmarks are authoritative.

So the structure now comes from two places:
  1. CHAPTER and SECTION: from the PDF bookmarks, looked up by page.
  2. PART (Learning Objectives, 2.1 EXERCISES, Self Check, ...): from headings whose
     TITLE matches a known list, whatever their '#' level.
Everything else (EXAMPLE, Solution, TRY IT, ...) stays inside the text.

Then, as decided: split by structure first, then by size with overlap, and put
"chapter > section > part" at the top of every chunk.

Also: removes leaked page headers, the repeated OpenStax footer line, and the
"[Figure: ...]" placeholder lines (a flag is kept instead); tracks the page range of
each chunk by dropping a tiny page tag into the text and reading it back after cutting.

Output: one JSON object per line in data/chunks.jsonl.
Every line has a comment saying what it does.

Run:  python chunk_md_v5.py
"""
import bisect                                 # bisect = fast "which bracket does this number fall in" lookup
import json                                   # json = write Python data as JSON text
import random                                 # random = pick random chunks to show you
import re                                     # re = regular expressions: pattern matching for text
import statistics                             # statistics = median and percentile functions
from collections import Counter               # Counter = a dictionary that counts how often things appear
from pathlib import Path                      # Path = a safe way to work with file locations

import pymupdf                                # pymupdf = reads the PDF (we only use it for the bookmarks)

# ===================== SETTINGS (edit these) =====================
PDF_PATH = "data/Prealgebra-OpenStax.pdf"     # the PDF (for its bookmarks)
MD_PATH = "data/Prealgebra-OpenStax.md"       # the markdown file made by extract_full.py
OUT_PATH = "data/chunks.jsonl"                # where the chunks are saved
HEADER_ZONE_LINES = 2                         # leaked headers sit in the first 2 non-blank lines of a page
MAX_CHARS = 1200                              # biggest chunk body; the model reads about 512 tokens (~1,500-2,000 chars)
OVERLAP_CHARS = 150                           # how much of the previous chunk to repeat at the start of the next
SEPARATORS = ["\n\n", "\n", " "]              # where we prefer to cut: paragraph break, then line break, then space
DROP_DISAGREE_FIGURES = True                  # remove figure text where the 300 and 600 DPI OCR passes disagreed (a flag is kept)
DROP_PLACEHOLDERS = True                      # remove "[Figure: ...]" placeholder lines (a flag is kept); False keeps them
MAX_CHUNK_PAGE_SPAN = 3                       # the report flags chunks that stretch over more pages than this

# ===================== PATTERNS =====================
PAGE_RE = re.compile(r"^<!-- PAGE pdf_index=(\d+) -->\s*$")    # a page marker line; captures the page number
COMMENT_LINE_RE = re.compile(r"^<!--.*-->\s*$")                 # any other line that is only an HTML comment
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)")                    # a heading: 1-6 '#' characters, a space, then the title
HEADER_RE = re.compile(r"^(Chapter\s+\d+\b.*|Answer Key|\d{1,4})$", re.I)   # leaked header: "Chapter 1 ...", "Answer Key", or a page number
FOOTER_RE = re.compile(r"^This OpenStax book is available for free at\s+\S+$", re.I)   # the repeated footer line
DISAGREE_RE = re.compile(r"^\[Figure text, OCR resolutions disagree\]")   # figure text lines where the two OCR passes disagreed
PLACEHOLDER_RE = re.compile(r"^\[Figure: (no readable text extracted|OCR failed)\]$")  # placeholder lines from extract_full.py
PART_RE = re.compile(                                           # known part titles (lowercase, digits already turned into '#')
    r"^(practice makes perfect|writing exercises|everyday math|self check"
    r"|#\.# exercises|chapter # review|key concepts|review exercises|practice test"
    r"|chapter outline|introduction)$"
)
TAG_OPEN = "⟦p"                               # start of the private page tag we drop into the text
TAG_CLOSE = "⟧"                               # end of the private page tag
PAGE_TAG_RE = re.compile(r"⟦p(\d+)⟧\n?")      # finds a page tag (and its line break) and captures the page number


# ===================== FUNCTION: CLEAN A TITLE =====================
def clean_title(title):
    """Tidy a title taken from a heading or a bookmark."""
    title = title.replace("\xa0", " ")                      # turn non-breaking spaces into normal spaces
    title = title.strip().strip("*").strip()                # remove spaces and bold/footnote '*' marks at both ends
    return " ".join(title.split())                          # collapse any run of spaces into one space


# ===================== FUNCTION: COMPARABLE TITLE =====================
def norm_title(title):
    """Reduce a title to lowercase letters and digits, without its leading number ('2.1 Use ...' becomes 'use...')."""
    title = re.sub(r"^[\d.\s]+", "", title.lower())         # cut leading digits, dots and spaces, e.g. "2.1. "
    return re.sub(r"[^a-z0-9]", "", title)                  # keep only letters and digits


# ===================== FUNCTION: CLEAN HTML LEFTOVERS =====================
def clean_html(line):
    """Replace the converter's HTML bits with plain text."""
    line = re.sub(r"<br\s*/?>", " ", line)                  # a <br> (forced line break inside a table cell) becomes a space
    line = re.sub(r"<sup>(.*?)</sup>", r"^\1", line)        # 3<sup>5</sup> becomes 3^5 (a superscript, i.e. an exponent)
    line = re.sub(r"</?(?:mark|sub|sup|i|b|u)>", "", line)  # remove any other leftover tags such as <mark> or <sub>
    return line                                             # hand the cleaned line back


# ===================== FUNCTION: STANDARD PART LABEL =====================
def canonical_part(title):
    """Turn a part title like 'REVIEW EXERCISES' or '2.1 EXERCISES' into one standard label."""
    key = re.sub(r"\d+", "#", title.lower())                # lowercase, and turn every number into '#'
    if key == "#.# exercises":                              # '1.1 EXERCISES', '2.3 EXERCISES', ...
        return "Exercises"                                  # one label for all of them
    if key == "chapter # review":                           # 'CHAPTER 1 REVIEW', 'CHAPTER 2 REVIEW', ...
        return "Chapter Review"                             # one label for all of them
    return title.title()                                    # otherwise use the title with standard capitalisation


# ===================== FUNCTION: RECURSIVE SPLIT BY SIZE =====================
def split_text(text, max_chars, seps):
    """Cut text into pieces no longer than max_chars, trying the separators in order."""
    if len(text) <= max_chars:                              # already small enough
        return [text]                                       # nothing to cut
    if not seps:                                            # no separators left to try (a huge word with no spaces)
        return [text[i:i + max_chars] for i in range(0, len(text), max_chars)]  # cut blindly every max_chars characters
    sep = seps[0]                                           # the separator to try first (the most natural boundary)
    pieces = text.split(sep)                                # break the text at every occurrence of that separator
    chunks = []                                             # finished chunks go here
    current = ""                                            # the chunk we are filling right now
    for piece in pieces:                                    # go through the small pieces one by one
        candidate = current + sep + piece if current else piece   # what the chunk would look like if we add this piece
        if len(candidate) <= max_chars:                     # it still fits
            current = candidate                             # so keep it
        else:                                               # it does not fit
            if current:                                     # if we have a partly filled chunk
                chunks.append(current)                      # close it and store it
            if len(piece) > max_chars:                      # this single piece is itself too big
                chunks.extend(split_text(piece, max_chars, seps[1:]))  # cut it again using the next separator (recursion)
                current = ""                                # nothing is left over to fill
            else:                                           # the piece fits on its own
                current = piece                             # start a new chunk with it
    if current:                                             # after the loop, a partly filled chunk may remain
        chunks.append(current)                              # store it too
    return chunks                                           # hand back the list of chunks


# ===================== FUNCTION: ADD OVERLAP =====================
def add_overlap(pieces, overlap):
    """Prefix each piece (except the first) with the tail of the piece before it."""
    pieces = [p.strip() for p in pieces if p.strip()]       # trim whitespace and drop empty pieces
    result = []                                             # pieces with overlap added
    for k, piece in enumerate(pieces):                      # k = position in the list, piece = the text
        if k > 0 and overlap > 0:                           # not the first piece, and overlap is switched on
            tail = pieces[k - 1][-overlap:]                 # the last `overlap` characters of the previous piece
            if "\n" in tail:                                # if the tail contains a line break
                tail = tail.split("\n", 1)[1]               # start the overlap after the first line break (a clean line start)
            elif " " in tail:                               # otherwise, if it contains a space
                tail = tail.split(" ", 1)[1]                # start after the first space (drop the cut-off word)
            piece = tail + "\n" + piece                     # put the tail in front of this piece
        result.append(piece)                                # store the finished piece
    return result                                           # hand back the list


# ===================== STEP 0: CHAPTERS AND SECTIONS FROM THE PDF BOOKMARKS =====================
doc = pymupdf.open(PDF_PATH)                                # open the PDF
chapters = []                                               # (start page index, chapter title) for each top-level bookmark
sections_bm = []                                            # (start page index, chapter title, section title) for each level-2 bookmark
current_chapter = "(front matter)"                          # the chapter we are inside while walking the bookmark list
for level, title, page in doc.get_toc():                    # every bookmark is [level, title, page], in book order
    if page < 1:                                            # a broken bookmark with no valid page
        continue                                            # skip it
    title = clean_title(title)                              # tidy the title (non-breaking spaces, trailing '*')
    if level == 1:                                          # a top-level bookmark = a chapter (or preface, appendix, ...)
        if title.lower().endswith(".pdf"):                  # the junk bookmark named after the file
            continue                                        # skip it
        current_chapter = title                             # later level-2 bookmarks belong to this chapter
        chapters.append((page - 1, title))                  # bookmark pages start at 1, our markers at 0, so subtract 1
    elif level == 2:                                        # a second-level bookmark = a section
        sections_bm.append((page - 1, current_chapter, title))   # remember it together with its chapter
chapters.sort(key=lambda c: c[0])                           # order by page (stable: keeps book order for ties)
sections_bm.sort(key=lambda s: s[0])                        # same for sections
chapter_starts = [c[0] for c in chapters]                   # just the start pages, for fast lookup
section_starts = [s[0] for s in sections_bm]                # same for sections
print(f"Bookmarks used: {len(chapters)} chapters, {len(sections_bm)} sections")   # how many we found


def labels_for(page_idx):
    """Return (chapter title, section title) for a page."""
    k = bisect.bisect_right(chapter_starts, page_idx) - 1   # position of the last chapter starting at or before this page
    chapter = chapters[k][1] if k >= 0 else "(front matter)"   # before the first bookmark, call it front matter
    j = bisect.bisect_right(section_starts, page_idx) - 1   # position of the last section starting at or before this page
    section = ""                                            # default: no section (e.g. a chapter introduction page)
    if j >= 0 and sections_bm[j][1] == chapter:             # only accept a section that belongs to this same chapter
        section = sections_bm[j][2]                         # take its title
    return chapter, section                                 # hand both back


# ===================== STEP A: READ THE MARKDOWN INTO UNITS =====================
units = []                                                  # finished units (one chapter/section/part stretch of text)


def start_unit(prev, page, chapter, section, part):
    """Store the previous unit if it has text, and return a fresh empty unit."""
    if "".join(prev["lines"]).strip():                      # the previous unit contains real text
        units.append(prev)                                  # keep it
    return {                                                # a brand-new, empty unit
        "chapter": chapter, "section": section, "part": part,   # where it lives in the book
        "lines": [],                                        # no text yet
        "page_start": page, "page_end": page,               # page range so far
        "has_unreadable_figure": False,                     # becomes True if we drop a placeholder line inside it
    }


page = 0                                                    # the PDF page we are currently reading
seen = 0                                                    # non-blank lines seen since the last page marker
disagree_samples = []                                       # a few dropped figure lines, shown in the report
stripped = footers = placeholders = disagree_dropped = 0                       # counters for the report
chapter0, section0 = labels_for(0)                          # labels for the very first page
current = start_unit({"lines": []}, 0, chapter0, section0, "")   # the unit being built (the dummy 'prev' has no text, so it is not stored)

for line in Path(MD_PATH).read_text(encoding="utf-8").splitlines():   # read the file, one line at a time
    m_page = PAGE_RE.match(line)                            # is this line a page marker?
    if m_page:                                              # yes
        page = int(m_page.group(1))                         # remember the new page number
        seen = 0                                            # restart the count of lines on this page
        new_labels = labels_for(page)                       # which chapter and section does this page belong to?
        if new_labels != (current["chapter"], current["section"]):   # we crossed into a new chapter or section
            current = start_unit(current, page, new_labels[0], new_labels[1], "")   # close the old unit, open a new one
        elif "".join(current["lines"]).strip():             # same section, and the unit already has text
            current["lines"].append(f"{TAG_OPEN}{page}{TAG_CLOSE}")   # drop a page tag so we can find page breaks later
        else:                                               # same section, but the unit is still empty
            current["page_start"] = page                    # its real content starts on this page
        continue                                            # markers are not content, go to the next line
    if COMMENT_LINE_RE.match(line):                         # any other HTML-comment line (image markers etc.)
        continue                                            # skip it; the figure text lines themselves are kept
    text_line = line.strip()                                # the line without surrounding spaces
    if text_line:                                           # if the line has any text
        seen += 1                                           # count it as one more non-blank line on this page
        if seen <= HEADER_ZONE_LINES and HEADER_RE.match(text_line):   # near the top of the page and looks like a header
            stripped += 1                                   # count the removal
            continue                                        # drop the line
        if DROP_DISAGREE_FIGURES and DISAGREE_RE.match(text_line):   # OCR text where the two resolutions disagreed (often junk)
            disagree_dropped += 1                           # count the removal
            if len(disagree_samples) < 8:                   # keep a few examples for the report
                disagree_samples.append(text_line[:100])    # the first 100 characters of the line
            current["has_unreadable_figure"] = True         # remember that this unit had an image we could not read
            continue                                        # drop the line
        if FOOTER_RE.match(text_line):                      # the repeated OpenStax footer line
            footers += 1                                    # count the removal
            continue                                        # drop the line
        if DROP_PLACEHOLDERS and PLACEHOLDER_RE.match(text_line):   # a "[Figure: ...]" placeholder line
            placeholders += 1                               # count the removal
            current["has_unreadable_figure"] = True         # remember that this unit had an image we could not read
            continue                                        # drop the line
    m_head = HEADING_RE.match(line)                         # is this line a heading (at any level)?
    if m_head:                                              # yes
        title = clean_title(m_head.group(2))                # tidy its title
        if PART_RE.match(re.sub(r"\d+", "#", title.lower())):   # does the TITLE match a known part title (digits turned into '#')?
            current = start_unit(current, page, current["chapter"], current["section"], canonical_part(title))   # a new part begins here
            continue                                        # the title is stored in "part", so skip the line itself
        elif current["part"] and len(norm_title(title)) >= 12 and norm_title(current["section"]).startswith(norm_title(title)):   # a heading that repeats the section title ends any open part
            current = start_unit(current, page, current["chapter"], current["section"], "")   # open a fresh unit with no part label (the heading line is kept as text)
    current["lines"].append(clean_html(line))                           # an ordinary line (or a heading we do not trust): add it to the unit
    if text_line:                                           # if the line has any text
        current["page_end"] = page                          # the unit reaches at least this page

units.append(current) if "".join(current["lines"]).strip() else None   # after the loop, store the last unit if it has text
print(f"Removed: {stripped:,} header lines | {footers:,} footer lines | {placeholders:,} placeholder lines | {disagree_dropped:,} disagreeing-OCR figure lines")   # removal counts


# ===================== STEP B: TURN UNITS INTO CHUNKS =====================
chunks = []                                                 # all final chunks
for u_idx, unit in enumerate(units):                        # u_idx = unit number, unit = its data
    body = "\n".join(unit["lines"]).strip()                 # join the unit's lines back into one text
    body = re.sub(r"\n(?:[ \t]*\n){2,}", "\n\n", body)      # squeeze runs of blank lines down to a single blank line
    pieces = split_text(body, MAX_CHARS, SEPARATORS)        # cut by size if it is too long
    pieces = [p.strip() for p in pieces if p.strip()]       # trim pieces and drop empty ones
    last_page = unit["page_start"]                          # the page where the previous piece ended
    ranges = []                                             # (first page, last page) for each piece
    for piece in pieces:                                    # work out each piece's pages from the tags inside it
        tags = [int(t) for t in PAGE_TAG_RE.findall(piece)]    # every page tag in this piece, as numbers
        start = tags[0] if tags and piece.startswith(TAG_OPEN) else last_page   # starts on a new page, or continues the last one
        end = tags[-1] if tags else last_page               # ends on the last tag's page, or stays where it was
        ranges.append((start, end))                         # store the range
        last_page = end                                     # the next piece continues from here
    pieces = add_overlap(pieces, OVERLAP_CHARS)             # repeat the end of each piece at the start of the next
    path = " > ".join(p for p in (unit["chapter"], unit["section"], unit["part"]) if p)   # "chapter > section > part", skipping empty ones
    c_idx = 0                                               # chunk number inside this unit
    for piece, (p_start, p_end) in zip(pieces, ranges):     # go through each piece with its page range
        piece = re.sub(r"\n{3,}", "\n\n", PAGE_TAG_RE.sub("", piece)).strip()   # remove page tags, tidy blank lines
        if not piece or all(ln.strip().startswith("#") for ln in piece.splitlines() if ln.strip()):                                       # nothing left, or only heading lines (the path already holds the titles)
            continue                                        # skip it
        chunks.append({                                     # store one chunk
            "id": len(chunks),                              # running number: 0, 1, 2, ... (used as the Qdrant point id)
            "text": f"{path}\n\n{piece}",                   # path + body: THIS is what gets embedded
            "body": piece,                                  # the body alone (handy for display)
            "heading_path": path,                           # the full path (handy for display)
            "chapter": unit["chapter"],                     # the chapter alone (handy for filtering)
            "section": unit["section"],                     # the section alone
            "part": unit["part"],                           # the part alone (Learning Objectives, 2.1 EXERCISES, ...)
            "page_start": p_start,                          # first PDF page of this chunk (0-based index)
            "page_end": p_end,                              # last PDF page of this chunk (0-based index)
            "unit_index": u_idx,                            # which unit this chunk came from
            "chunk_index": c_idx,                           # which part of the unit this chunk is
            "has_figure_text": "[Figure text" in piece,     # True if the chunk contains text read from an image
            "has_unreadable_figure": unit["has_unreadable_figure"],   # True if its unit had an image we could not read
        })
        c_idx += 1                                          # count one more chunk in this unit


# ===================== STEP C: SAVE =====================
with open(OUT_PATH, "w", encoding="utf-8") as f:           # open the output file for writing
    for chunk in chunks:                                    # for every chunk
        f.write(json.dumps(chunk, ensure_ascii=False) + "\n")   # write it as one line of JSON


# ===================== STEP D: REPORT =====================
lengths = sorted(len(c["text"]) for c in chunks)            # size of every chunk, including the path
print(f"Units: {len(units):,} | chunks: {len(chunks):,} | saved to {OUT_PATH}")   # the headline numbers
print(f"Chunk size (characters, with path): median={int(statistics.median(lengths)):,}"
      f" | 90th pct={int(statistics.quantiles(lengths, n=10)[-1]):,} | max={lengths[-1]:,}")   # size summary
print(f"Chunks over 1,800 characters (may exceed the model limit): {sum(1 for n in lengths if n > 1800):,}")   # risk count
print(f"Chunks under 150 characters (tiny): {sum(1 for n in lengths if n < 150):,}")   # tiny-chunk count
for c in [c for c in chunks if len(c["text"]) < 150][:5]:   # the first 5 tiny chunks
    print(f"   tiny id={c['id']}: {c['body'][:90]!r}")      # show the start of each body, in quotes, so spaces and symbols are visible
print("Examples of dropped disagreeing-OCR figure lines (check that they really are junk):")   # heading for the samples
for s in disagree_samples:                                  # each saved example
    print(f"   {s!r}")                                      # show it in quotes so spaces and symbols are visible
print(f"Chunks with no section label: {sum(1 for c in chunks if not c['section']):,}")   # chunks whose page had no section bookmark

wide = [c for c in chunks if c["page_end"] - c["page_start"] > MAX_CHUNK_PAGE_SPAN]   # chunks stretching over many pages
print(f"Chunks spanning more than {MAX_CHUNK_PAGE_SPAN} pages (should be rare): {len(wide):,}")   # how many
for c in wide[:5]:                                          # show a few
    print(f"   id={c['id']} pages {c['page_start']}-{c['page_end']} | {c['heading_path'][:70]}")   # id, pages, path

per_chapter = Counter(c["chapter"] for c in chunks)         # how many chunks each chapter got
print("\nChunks per chapter:")                              # section title
for name, n in per_chapter.items():                         # in order of first appearance
    print(f"   {n:5,}  {name}")                             # count, then the chapter name

per_part = Counter(re.sub(r"\d+", "#", c["part"]) or "(no part label)" for c in chunks)   # chunks per part title (digits grouped)
print("\nChunks per part label (checks that the part titles are being detected):")   # section title
for name, n in per_part.most_common(15):                    # the 15 most common
    print(f"   {n:5,}  {name}")                             # count, then the label

line_counts = Counter()                                     # in how many chunks does each line appear?
for c in chunks:                                            # for every chunk
    distinct = {re.sub(r"\d+", "#", ln.strip().lower()) for ln in c["body"].splitlines() if len(ln.strip()) >= 3}   # its lines, digits replaced by '#'
    line_counts.update(distinct)                            # count each distinct line once per chunk
common = [(ln, n) for ln, n in line_counts.most_common(10) if n >= 50]   # lines found in 50+ chunks
print("\nLines repeated in 50+ chunks (real repeats like '|---|' are fine; leaked headers are not):")   # section title
for ln, n in common:                                        # show each one
    print(f"   {n:5,}  {ln[:70]}")                          # count, then the line (cut to 70 characters)
if not common:                                              # nothing repeated that often
    print("   none")                                       # say so

random.seed(42)                                             # same random picks every run
picks = [chunks[0], max(chunks, key=lambda c: len(c["text"]))] + random.sample(chunks, min(3, len(chunks)))   # first, longest, 3 random
sample_22 = next((c for c in chunks if c["section"].startswith("2.2")), None)   # the first chunk of section 2.2 (where v1 and v2 broke)
if sample_22:                                               # if section 2.2 exists
    picks.append(sample_22)                                 # show it too
for c in picks:                                             # show each picked chunk
    print("\n" + "-" * 70)                                  # a separator line
    print(f"id={c['id']} | pages {c['page_start']}-{c['page_end']} | chunk {c['chunk_index']} of its unit")   # its facts
    print(c["text"])                                        # the exact text that will be embedded