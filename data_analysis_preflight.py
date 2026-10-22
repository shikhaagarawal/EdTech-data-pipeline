import pymupdf

pdf_path = "data/Prealgebra-OpenStax.pdf"
doc = pymupdf.open(pdf_path)

print("Pages:", len(doc))
print("Encrypted:", doc.is_encrypted)
print("Title:", doc.metadata.get("title"), "|Producer:", doc.metadata.get("producer"))

chars = [len(page.get_text()) for page in doc]
print("Average char per page:", sum(chars)//len(chars))
print("pages under 200 chars:", sum(1 for c in chars if c<200))

step = max(1, len(doc) //30)
print("\npage | chars | images | glyph_shapes | tables | bad_chars")
for i in range(0, len(doc), step):
	page = doc[i]
	text = page.get_text()
	glyphs = sum(
		1 for d in page.get_drawings()
		if d.get("fill") is not None and d["rect"].width < 14 and d["rect"].height<14
	)
	tables = len(page.find_tables().tables)
	print(i, "|", len(text), "|", len(page.get_images()), "|", glyphs, "|", tables, "|", text.count("\ufffd"))
