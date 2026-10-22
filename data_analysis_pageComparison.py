import pymupdf
import pymupdf4llm

pdf_path = "data/Prealgebra-OpenStax.pdf"
page_index = 38

md = pymupdf4llm.to_markdown(pdf_path, pages=[page_index])
print(md)

doc = pymupdf.open(pdf_path)
doc[page_index].get_pixmap(dpi=110).save(f"data/compare_{page_index}.png")
print("Saved data/compare_%d.png" % page_index)
