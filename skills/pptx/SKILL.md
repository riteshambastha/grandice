---
name: pptx
description: Read, create, or edit PowerPoint presentations. Trigger on .pptx/.potx file paths, the words "slide deck", "presentation", "slides", "speaker notes", or a request to produce a summary/brief as slides.
---

# Working with presentations

Use **python-pptx**. It is already installed — do not try to `pip install`
anything; the sandbox has no network access and will fail if you do.

## Before writing anything: inspect first

```bash
python3 .skills/pptx/scripts/pptx_inspect.py path/to/file.pptx
```

Prints slide count, each slide's layout name, its placeholders, and a text
preview — without dumping the whole deck into context. Run this on any
existing file (including a template you plan to build from) before deciding
how to add or edit slides.

## Prefer a template over a blank deck

`Presentation()` with no argument gives you PowerPoint's generic default
template. `Presentation("template.pptx")` starts from an existing file's
layouts, fonts and placeholder positions, and produces something that looks
designed rather than assembled by hand. If the user gave you a template or a
reference deck, build from it — don't recreate its look with raw shapes.

## Layout indices are template-specific

```python
from pptx import Presentation

prs = Presentation()  # or Presentation("template.pptx")
for i, layout in enumerate(prs.slide_layouts):
    print(i, layout.name)
```

Do not assume `prs.slide_layouts[1]` is "title and content" — that only
holds for PowerPoint's default template. Enumerate layouts for whatever file
you're actually working with; a custom template numbers them differently.

## Adding a slide and filling placeholders

```python
layout = prs.slide_layouts[1]
slide = prs.slides.add_slide(layout)
slide.shapes.title.text = "Q3 Results"

for ph in slide.placeholders:
    print(ph.placeholder_format.idx, ph.placeholder_format.type, ph.name)
body = slide.placeholders[1]  # index from the inspection above, not assumed
body.text_frame.text = "Revenue grew 12% quarter over quarter."
```

**Prefer filling existing placeholders over adding textboxes.** A placeholder
inherits the layout's font, size and position; a manually added textbox
(`slide.shapes.add_textbox(...)`) needs explicit position and size and will
not match the template's styling. Reach for `add_textbox` only when there is
no suitable placeholder.

**A placeholder index that doesn't exist on this layout raises `KeyError`.**
Always enumerate `slide.placeholders` first rather than assuming index `1`
or `2` exists — different layouts expose different placeholders.

## Bullet lists

A body placeholder holds one paragraph per bullet point — setting `.text`
once gives you a single line, which is rarely what "produce a summary as
slides" actually means. Add one paragraph per point instead:

```python
tf = slide.placeholders[1].text_frame
tf.text = "Headcount flat this quarter"          # first bullet — sets paragraph 0
for point in ["Two projects slipped to Q3", "Budget on track"]:
    p = tf.add_paragraph()
    p.text = point
```

Every paragraph renders at the same outline level by default. For a
sub-bullet, set `p.level = 1` (0-indexed; how many levels an existing layout
actually supports depends on its master, so don't assume more than one or
two are safe without checking the template).

## Units are EMU — use the helpers, not raw integers

```python
from pptx.util import Inches, Pt, Emu

slide.shapes.add_textbox(Inches(1), Inches(2), Inches(4), Inches(1))
run.font.size = Pt(18)
```

Check `prs.slide_width` / `prs.slide_height` before placing a free-floating
shape (textbox, picture, chart) by hand — a 16:9 template is 13.33in wide, a
4:3 one is 10in, and a position that looks right on one runs off the edge of
the other. Placeholders don't have this problem since their position comes
from the layout; it only matters for shapes you position yourself.

## Per-word formatting needs runs, not paragraph.text

Setting `paragraph.text = "..."` (or `text_frame.text = "..."`) replaces all
runs in that paragraph — any per-word bold/color/size you set before is gone.
To style part of a line, build runs explicitly:

```python
p = slide.placeholders[1].text_frame.paragraphs[0]
p.text = ""  # clear first
run = p.add_run()
run.text = "12% growth"
run.font.bold = True
run.font.size = Pt(20)
```

## Images, tables, charts

```python
slide.shapes.add_picture("chart.png", Inches(1), Inches(1.5), width=Inches(6))
# only one of width/height keeps the aspect ratio; a local path only — no
# network access, so a URL will simply fail

table = slide.shapes.add_table(rows=3, cols=2, left=Inches(1), top=Inches(2),
                                width=Inches(6), height=Inches(2)).table
table.cell(0, 0).text = "Product"

from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE

data = CategoryChartData()
data.categories = ["Q1", "Q2", "Q3"]
data.add_series("Revenue", (120, 140, 165))
slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1), Inches(1.5),
                        Inches(6), Inches(4), data)
```

## Speaker notes and saving

```python
slide.notes_slide.notes_text_frame.text = "Mention the Q3 pipeline slip here."
prs.save("deck.pptx")  # always .pptx — python-pptx cannot write legacy .ppt
```

## Verify what you actually wrote

Re-open the saved file fresh and check slide count and key text before
reporting success — the same discipline as the xlsx skill. A bug earlier in
a long agent turn can leave the in-memory `Presentation` object out of sync
with what actually landed on disk.

## Reference

`scripts/pptx_inspect.py` — slide count, layout names, placeholders, text
preview. Run it, don't read its source unless you're debugging it.
