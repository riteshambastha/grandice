---
name: pdf
description: Read, merge, split, rotate, watermark, encrypt, or extract text from PDF files. Trigger on .pdf file paths, the words "PDF", "merge these PDFs", "split this PDF", "watermark", "extract text from this PDF", or a request to fill a PDF form.
---

# Working with PDFs

Use **pypdf**. It is already installed — do not try to `pip install`
anything; the sandbox has no network access and will fail if you do.

**What this skill cannot do, stated up front:** pypdf manipulates *existing*
PDF pages — it has no API for drawing new text or laying out a page from
scratch. If the task is "create a new PDF report" rather than "do something
to a PDF that already exists," that content needs to come from somewhere
else (the docx/xlsx/pptx skills, or a document already in hand) — don't
attempt to fake page authoring with this library. It also does **no OCR**:
a scanned or image-only PDF will extract empty or near-empty text, not an
error, so the failure is easy to miss without checking.

## Before writing anything: inspect first

```bash
python3 .skills/pdf/scripts/pdf_inspect.py path/to/file.pdf
```

Prints page count, metadata, whether the file is encrypted, and a text
preview per page — including a warning if a page's extracted text is
suspiciously short, the signal that it's scanned/image-based rather than
real text.

## Reading and extracting text

```python
from pypdf import PdfReader

reader = PdfReader("file.pdf")
print(len(reader.pages), "pages")
for page in reader.pages:
    text = page.extract_text()
    ...
```

**Extraction quality varies a lot and is never guaranteed.** Multi-column
layouts often interleave text from different columns out of reading order;
tables usually come out as loose words, not rows; a scanned page returns
`""` or near-empty text with no error at all. Don't assume a short or empty
result means you made a mistake — check the page visually (or note the
limitation) before concluding the content isn't there.

**Metadata and encryption:**

```python
print(reader.metadata)          # title, author, creation date, ...
if reader.is_encrypted:
    reader.decrypt("password")  # must succeed before .pages / extract_text work
```

## Merging

`PdfMerger` does not exist in the currently installed version — verified;
merging is a `PdfWriter` method now:

```python
from pypdf import PdfWriter

writer = PdfWriter()
writer.append("first.pdf")
writer.append("second.pdf")
writer.write("combined.pdf")
```

## Splitting

Build a new `PdfWriter` from whichever pages you want, by index (0-based):

```python
reader = PdfReader("source.pdf")
writer = PdfWriter()
for i in range(0, 5):       # first 5 pages
    writer.add_page(reader.pages[i])
writer.write("first_five.pdf")
```

## Rotating

```python
page = reader.pages[0]
page.rotate(90)              # clockwise degrees; must be a multiple of 90
writer = PdfWriter()
writer.add_page(page)
writer.write("rotated.pdf")
```

## Watermarking

Overlay one page's content onto another with `merge_page` — the watermark
itself has to already be a PDF page (e.g. a one-page PDF you were given, or
one built elsewhere), since pypdf can't draw the watermark text itself:

```python
base = PdfReader("report.pdf").pages[0]
overlay = PdfReader("watermark.pdf").pages[0]
base.merge_page(overlay)     # base now has both layers; original text is still extractable
```

## Encrypting / decrypting

```python
writer = PdfWriter()
writer.append("file.pdf")
writer.encrypt("a-password")
writer.write("protected.pdf")
```

## Filling a form

```python
reader = PdfReader("form.pdf")
print(reader.get_fields())   # field names and current values, inspect before filling
writer = PdfWriter()
writer.append(reader)
writer.update_page_form_field_values(writer.pages[0], {"name": "Jane Doe"})
writer.write("filled.pdf")
```

## Extracting images

```python
for page in reader.pages:
    for image in page.images:
        with open(image.name, "wb") as f:
            f.write(image.data)
```

## Verify what you actually wrote

Re-open the saved file fresh and check page count / extracted text before
reporting success — the same discipline as the other document skills.

## Reference

`scripts/pdf_inspect.py` — page count, metadata, encryption status, and a
per-page text preview with a short-extraction warning. Run it, don't read
its source unless you're debugging it.
