import pymupdf4llm
from pathlib import Path

pdf_path = "data/prealgebra-2e_-_WEB.pdf";
md_text = pymupdf4llm.to_markdown(pdf_path)
Path("data/parsed_prealgebra-2e_-_WEB.md").write_text(md_text,encoding="utf/8")

print(len(md_text), "characters")
print(md_text[:1500]) 
