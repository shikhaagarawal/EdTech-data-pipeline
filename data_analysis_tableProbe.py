import pymupdf
import pytesseract
from PIL import Image

doc = pymupdf.open("data/Prealgebra-OpenStax.pdf")
page = doc[38]

for n, table in enumerate(page.find_tables().tables):
	print("TABLE", n, "bbox:", [round(v) for v in table.bbox],
		"rows:",table.row_count, "cols:", table.col_count)
	for row in table.extract():
		print(" ", row)

print("\n Image boxes on this page:")
for info in page.get_image_info():
	#print(" ", [round(v) for v in info["bbox"]])
	x0, y0, x1, y1 = info["bbox"]
	if x1-x0 <20:
		continue
	print("box", [round(v) for v in info["bbox"]],
		"native pixels:", info["width"], "x", info["height"])
	clip = pymupdf.Rect(x0-2, y0-2, x1+2, y1+2)
	for dpi in (150,300,600):
		pix = page.get_pixmap(dpi=dpi, clip=clip)
		img = Image.frombytes("RGB",(pix.width, pix.height), pix.samples)
		text = pytesseract.image_to_string(img, config="--psm 6")
		print(" dpi", dpi, "->", " | ".join(text.split("\n")).strip())
