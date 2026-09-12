---
name: docx
description: Read, create, or edit Word documents. Trigger on .docx/.dotx file paths, the words "Word document", "memo", "letter", "report" as a document (not a spreadsheet or slides), or a request to produce a written document with headings, tables, or formatted text.
---

# Working with Word documents

Use **python-docx**. It is already installed — do not try to `pip install`
anything; the sandbox has no network access and will fail if you do.

## Before writing anything: inspect first

```bash
python3 .skills/docx/scripts/docx_inspect.py path/to/file.docx
```

Prints paragraph count, table count, headers/footers, and a preview in the
document's *actual* reading order — see the trap below for why that last
part matters — without loading the whole document into context.

## Reading

```python
from docx import Document

doc = Document("file.docx")
for para in doc.paragraphs:
    print(para.style.name, ":", para.text)
```

**`doc.paragraphs` is not the whole document.** It excludes table cell text
entirely, and it excludes headers and footers entirely. A document whose
content lives mostly in tables (a lot of real-world reports do) will look
almost empty if you only read `doc.paragraphs`. Read tables separately:

```python
for table in doc.tables:
    for row in table.rows:
        print([cell.text for cell in row.cells])
```

Headers and footers live on the section, not the document:

```python
section = doc.sections[0]
print(section.header.paragraphs[0].text)
print(section.footer.paragraphs[0].text)
```

**Reading order is lost between `doc.paragraphs` and `doc.tables`.** They
are two separate lists — if the real document goes paragraph, table,
paragraph, you get `[para1, para2]` and `[table1]` with no way to tell the
table sat between them. If order matters (summarizing a document
top-to-bottom, for instance), walk the body XML directly instead:

```python
from docx.table import Table
from docx.text.paragraph import Paragraph

for child in doc.element.body:
    tag = child.tag.split("}")[-1]
    if tag == "p":
        print("paragraph:", Paragraph(child, doc).text)
    elif tag == "tbl":
        print("table:", [c.text for c in Table(child, doc).rows[0].cells])
```

## Writing

```python
doc = Document()  # or Document("template.docx") to start from an existing file
doc.add_heading("Quarterly Report", level=1)
doc.add_paragraph("Revenue grew 12% quarter over quarter.")
doc.add_paragraph("Flat this quarter.", style="List Bullet")
doc.save("report.docx")
```

Built-in style names worth knowing: `"Heading 1"`–`"Heading 9"`, `"List
Bullet"`, `"List Number"`, `"Normal"`, `"Quote"`. Starting from a template
(`Document("template.docx")`) inherits its actual styles and header/footer,
the same reasoning as the pptx skill's "prefer a template over a blank
deck" — a custom template may define different style names, so check
`doc.styles` if a style name you expect doesn't apply as you'd expect.

**`paragraph.text = "..."` collapses every run into one and wipes their
formatting** — verified: a bold run and a plain run reassigned this way
both come back with `bold=None`. To style *part* of a line, build runs
explicitly instead:

```python
from docx.shared import Pt, RGBColor

p = doc.add_paragraph()
run = p.add_run("12% growth")
run.bold = True
run.font.size = Pt(14)
run.font.color.rgb = RGBColor(0x1F, 0x49, 0xC4)
```

**Tables**: `doc.add_table(rows=2, cols=2)`, then `table.cell(r, c).text =
"..."` (same run-collapsing caveat as paragraphs — fine for plain values,
not for styled ones). `table.add_row()` to grow it after creation.

**Images**: `doc.add_picture("chart.png", width=Inches(5))` — a local path
only, no network access, so a URL will simply fail.

**Page breaks**: `doc.add_page_break()`.

## Verify what you actually wrote

Re-open the saved file fresh and check paragraph/table content before
reporting success — the same discipline as the xlsx and pptx skills. A bug
earlier in a long agent turn can leave the in-memory `Document` object out
of sync with what actually landed on disk.

## Reference

`scripts/docx_inspect.py` — paragraph/table counts, headers/footers, and a
true-reading-order preview. Run it, don't read its source unless you're
debugging it.
