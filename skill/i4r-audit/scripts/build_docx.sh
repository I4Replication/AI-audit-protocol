#!/bin/bash
# build_docx.sh <audit-dir> <output-basename> [final-report.md]
# Renders an I4R-format .docx + PDF preview. Captions are inserted BEFORE each detected
# table block — never by line number, which lands them inside tables.
set -uo pipefail
DIR="${1:?audit dir}"; OUT="${2:?output basename}"; SRC="${3:-$DIR/report/REPORT_final.md}"

# pandoc is not always on PATH (this machine has it only in a dedicated conda env).
PANDOC="${PANDOC:-$(command -v pandoc || true)}"
[ -n "$PANDOC" ] || for p in "$HOME/miniconda3/envs/i4rdoc/bin/pandoc" /opt/homebrew/bin/pandoc /usr/local/bin/pandoc; do
  [ -x "$p" ] && PANDOC="$p" && break
done
[ -n "$PANDOC" ] || { echo "FATAL: pandoc not found. Install it (conda create -n i4rdoc -c conda-forge pandoc) or set PANDOC=<path>"; exit 1; }
[ -f "$SRC" ] || { echo "FATAL: $SRC not found"; exit 1; }
[ -f "$DIR/report/front_matter.md" ] || { echo "FATAL: write $DIR/report/front_matter.md first (see reference/report_structure.md)"; exit 1; }
[ -f "$DIR/report/captions.txt" ] || { echo "FATAL: write $DIR/report/captions.txt — one caption per line, in document order"; exit 1; }

python3 - "$SRC" "$DIR/report/captions.txt" "$DIR/report/_body.md" << 'PY'
import sys, re, pathlib
src, capfile, dest = sys.argv[1], sys.argv[2], sys.argv[3]
L = pathlib.Path(src).read_text().split("\n")
CAPS = [c.strip() for c in pathlib.Path(capfile).read_text().split("\n") if c.strip()]
try:    L = L[next(i for i,l in enumerate(L) if l.startswith("## Executive summary") or l.startswith("## Abstract")):]
except StopIteration: pass
mid=[]
for line in L:
    m=re.match(r"^## (\d+)\. (.+)$", line)
    if m: mid.append(f"# Section {m.group(1)}. {m.group(2)}"); continue
    if line.startswith("## Executive summary") or line.startswith("## Abstract"):
        mid.append("# "+line[3:]); continue
    if line.startswith("## References") or line.startswith("## Appendix"):
        # Back matter starts a new page. References and appendices do not count toward the page
        # cap, so they must begin cleanly rather than sharing a page with the body.
        mid += ["```{=openxml}", '<w:p><w:r><w:br w:type="page"/></w:r></w:p>', "```", ""]
        mid.append("# "+line[3:]); continue
    # subsection heading: "**6.1 Title.** body" or "**6.1 Title.**" alone on its line.
    # Suffixed numbers (6.8a, 7.1b-ii) are headings too.
    m2=re.match(r"^\*\*(\d+\.\d+[a-z]?(?:-[a-z]+)?) (.+?)\.?\*\*[ ]?(.*)$", line)
    if m2: mid += [f"## {m2.group(1)} {m2.group(2)}",""] + ([m2.group(3)] if m2.group(3) else []); continue
    mid.append(line)
out,k,i=[],0,0
while i<len(mid):
    if mid[i].lstrip().startswith("|"):
        j=i
        while j<len(mid) and mid[j].lstrip().startswith("|"): j+=1
        if k<len(CAPS):
            if out and out[-1].strip(): out.append("")
            out += [f"*Table {k+1}. {CAPS[k]}*",""]; k+=1
        out += mid[i:j]+[""]; i=j; continue
    out.append(mid[i]); i+=1
txt="\n".join(out)
txt=re.sub(r"§(\d+)\.(\d+)", r"Section \1.\2", txt); txt=re.sub(r"§(\d+)", r"Section \1", txt)
# [claim N] tags are the replicator-facing join key to the claims script and the verdict survey.
# They stay in REPORT_final.md; the published deliverable is built without them (meeting 2026-08-21).
ntags = len(re.findall(r"\s?\[claim \d+\]", txt))
txt = re.sub(r"\s?\[claim \d+\]", "", txt)
if ntags: print(f"claim tags stripped from the deliverable: {ntags} (they remain in REPORT_final.md)")
pathlib.Path(dest).write_text(txt)
nb = sum(1 for x,l in enumerate(out) if l.lstrip().startswith("|") and not out[x-1].lstrip().startswith("|"))
print(f"table blocks: {nb}   captions available: {len(CAPS)}   captions placed: {k}")
if nb != len(CAPS): print(f"  WARNING: {nb} blocks but {len(CAPS)} captions — check for a table split by an inserted paragraph")
PY

cd "$DIR/report"
"$PANDOC" --print-default-data-file reference.docx > _ref.docx
cat front_matter.md _body.md > _full.md
"$PANDOC" _full.md -f markdown+pipe_tables -t docx --reference-doc=_ref.docx -o "$OUT.docx" || exit 1


# ---- link colouring (house style, 2026-08-22) -------------------------------------------------
# Citations and URLs BLUE, cross-references to tables, figures and sections RED, matching the
# group's paper template (citecolor blue / linkcolor red). Word takes hyperlink colour from a
# character style, so two colours would need two styles; the run colour is set directly instead,
# which renders identically in Word, LibreOffice and any PDF converted from either.
python3 - "$OUT.docx" <<'PYLINK'
import re, shutil, sys, zipfile
docx = sys.argv[1]
z = zipfile.ZipFile(docx); names = z.namelist(); blobs = {n: z.read(n) for n in names}
doc = blobs["word/document.xml"].decode("utf-8")
rels = blobs["word/_rels/document.xml.rels"].decode("utf-8")
z.close()
external = set(re.findall(r'Id="([^"]+)"[^>]*TargetMode="External"', rels))

def colour_of(open_tag):
    m = re.search(r'w:anchor="([^"]*)"', open_tag)
    if m:
        return "0000FF" if m.group(1).startswith("ref-") else "FF0000"
    m = re.search(r'r:id="([^"]*)"', open_tag)
    if m and m.group(1) in external:
        return "0000FF"
    return None

RPR = '<w:rPr><w:color w:val="%s"/><w:u w:val="single"/>'
out, i, blue, red = [], 0, 0, 0
for m in re.finditer(r"<w:hyperlink\b[^>]*>.*?</w:hyperlink>", doc, re.S):
    blk = m.group(0)
    col = colour_of(blk[:blk.find(">") + 1])
    out.append(doc[i:m.start()]); i = m.end()
    if col:
        blk = re.sub(r'<w:color w:val="[^"]*"\s*/>', "", blk)
        blk = blk.replace("<w:rPr>", RPR % col)
        blk = re.sub(r"<w:r>(?!<w:rPr>)", "<w:r>" + (RPR % col) + "</w:rPr>", blk)
        blue += col == "0000FF"; red += col == "FF0000"
    out.append(blk)
out.append(doc[i:])
tmp = docx + ".tmp"
zo = zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED)
for nm in names:
    zo.writestr(nm, "".join(out).encode("utf-8") if nm == "word/document.xml" else blobs[nm])
zo.close(); shutil.move(tmp, docx)
print("  links coloured: %d blue (citations, URLs), %d red (tables, figures, sections)" % (blue, red))
PYLINK


# Page count. Preferred path is LibreOffice converting the actual .docx, because that is the
# document being delivered. Where soffice is absent, fall back to typesetting the same markdown
# through xelatex at I4R's page geometry — a PROXY, not the real thing, so it is labelled as one.
PAGE_SRC=""
SOF=$(command -v soffice || echo "/Applications/LibreOffice.app/Contents/MacOS/soffice")
if [ -x "$SOF" ]; then
  "$SOF" --headless -env:UserInstallation="file:///tmp/lo_$$" --convert-to pdf --outdir . "$OUT.docx" >/dev/null 2>&1
  PAGES=$(pdfinfo "$OUT.pdf" 2>/dev/null | awk '/Pages/{print $2}')
  [ -n "$PAGES" ] && PAGE_SRC="docx (LibreOffice)"
fi
if [ -z "${PAGES:-}" ] && command -v xelatex >/dev/null; then
  # Match pandoc default reference.docx as closely as xelatex allows. Heading spacing is pinned to the
  # reference Heading style (12pt before, 0 after) via titlesec: LaTeX default section spacing (~25pt)
  # inflated the proxy by ~a third of a page over 16 headings, failing documents the real format fits.
  # The remaining settings keep the proxy conservative rather than optimistic. That reference.docx sets w:sz=24 (12pt body) and
  # w:spacing w:after="200" (10pt after EVERY paragraph), on Word's default Letter page with 1in
  # margins. An earlier version of this proxy used 11pt with LaTeX's default indented paragraphs and
  # no inter-paragraph space, which under-counted pages — the opposite of a safe error.
  # Font is the remaining gap: the reference uses the minor theme font (Calibri), which is narrower
  # than any serif installed here, so a serif proxy still runs slightly long on text-heavy pages.
  "$PANDOC" _full.md -f markdown+pipe_tables --pdf-engine=xelatex \
    -V geometry:margin=1in -V fontsize=12pt -V mainfont="Arial" \
    -V indent=false \
    -V header-includes='\usepackage{etoolbox}\usepackage{titlesec}\setlength{\parskip}{10pt}\setlength{\parindent}{0pt}\AtBeginEnvironment{longtable}{\setlength{\parskip}{0pt}}\titlespacing*{\section}{0pt}{12pt}{0pt}\titlespacing*{\subsection}{0pt}{10pt}{0pt}' \
    -o "${OUT}_proxy.pdf" >/dev/null 2>&1
  PAGES=$(pdfinfo "${OUT}_proxy.pdf" 2>/dev/null | awk '/Pages/{print $2}')
  [ -n "$PAGES" ] && PAGE_SRC="xelatex PROXY at reference.docx geometry (12pt, 10pt para spacing) — LibreOffice absent, so the .docx itself is UNMEASURED"
fi
echo "built: $OUT.docx (${PAGES:-unknown} pages total${PAGE_SRC:+ — $PAGE_SRC})"
rm -f _ref.docx _full.md _body.md

# The cap counts the BODY only: front matter through the last numbered section. References and
# appendices start on their own page and are excluded (team decision 2026-08-22), because a
# comment should not lose a finding to make room for its own bibliography.
BODY_PAGES=""
if [ -n "${PAGES:-}" ] && [ -f "$OUT.pdf" ]; then
  BODY_PAGES=$(python3 - "$OUT.pdf" "$PAGES" <<'PYPG'
import subprocess, sys
pdf, pages = sys.argv[1], int(sys.argv[2])
def page_text(i):
    return subprocess.run(["pdftotext", "-f", str(i), "-l", str(i), pdf, "-"],
                          capture_output=True, text=True).stdout
first_back = None
for i in range(1, pages + 1):
    t = " ".join(page_text(i).split())
    if t.startswith("References") or t.startswith("Appendix"):
        first_back = i; break
print(first_back - 1 if first_back else pages)
PYPG
)
fi

# The abstract must not run onto page 2: it is the first thing an editor reads and it belongs
# whole on the opening page with the citation block.
if [ -f "$OUT.pdf" ] && [ -f "$DIR/report/REPORT_final.md" ]; then
  python3 - "$OUT.pdf" "$DIR/report/REPORT_final.md" <<'PYABS'
import re, subprocess, sys
pdf, md = sys.argv[1], sys.argv[2]
s = open(md, encoding="utf-8").read()
m = re.search(r"^## Abstract\s*\n(.*?)(?=^## )", s, re.S | re.M)
if m:
    txt = m.group(1)
    txt = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", txt)   # links render as their text
    txt = re.sub(r"[*`_]", "", txt)                          # emphasis markers do not render
    words = " ".join(txt.split()).split()
    if len(words) > 8:
        tail = " ".join(words[-8:]).rstrip(".")
        p1 = " ".join(subprocess.run(["pdftotext","-f","1","-l","1",pdf,"-"],
                                     capture_output=True, text=True).stdout.split())
        # self-test the matcher: a string known to be on page 1 must be found
        head = " ".join(words[:6])
        if head not in p1:
            print("  WARN: could not locate the abstract on page 1 — check the render by eye")
        elif tail not in p1:
            print("  FAIL: the abstract runs onto page 2; it must finish on page 1")
            sys.exit(1)
        else:
            print("  ok: the abstract finishes on page 1")
PYABS
  [ $? -ne 0 ] && { echo "Shorten the abstract, or tighten the front matter."; exit 1; }
fi

# Hard cap: MAX_PAGES rendered BODY pages (house rule 1). Default 10.
MAX_PAGES="${MAX_PAGES:-10}"
if [ -z "${PAGES:-}" ]; then
  echo "WARNING: no PDF page count — the ${MAX_PAGES}-page cap is UNVERIFIED. Convert to PDF and count before release."
elif [ -n "$BODY_PAGES" ]; then
  echo "  body pages (front matter through the last numbered section): $BODY_PAGES of ${MAX_PAGES}; back matter $((PAGES - BODY_PAGES)) page(s), uncounted"
  if [ "$BODY_PAGES" -gt "$MAX_PAGES" ]; then
    echo "FAIL: $BODY_PAGES body pages — capped at ${MAX_PAGES}."
    echo "Cut prose, not findings: shrink a finding to two body sentences and move the evidence"
    echo "detail to LEDGER.md / ADJUDICATION.md, which ship with the package."
    exit 1
  fi
elif [ "$PAGES" -gt "$MAX_PAGES" ]; then
  echo "FAIL: $PAGES pages — capped at ${MAX_PAGES} (back matter could not be located to exclude it)."
  exit 1
fi
echo ""
echo "NOW VERIFY: pdftoppm -jpeg -r 85 -f 1 -l 2 $OUT.pdf pg   then read pg-*.jpg"
echo "Check a page containing a table. Truncated tables mean a caption landed inside one."
