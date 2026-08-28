#!/bin/bash
# build_docx.sh [--no-cache|--refresh-cache] <audit-dir> <output-basename> [final-report.md]
# Renders an I4R-format .docx + PDF preview. Captions are inserted BEFORE each detected
# table block — never by line number, which lands them inside tables.
#
# Default builds use an audit-local content-addressed cache. --no-cache bypasses both lookup and
# storage. --refresh-cache forces a rebuild and atomically replaces the matching validated entry.
set -uo pipefail
SELF="$(cd "$(dirname "$0")" 2>/dev/null && pwd)/$(basename "$0")"
CACHE_HELPER="$(dirname "$SELF")/document_build_cache.py"
CACHE_MODE="${BUILD_DOCX_CACHE_MODE:-use}"
while [ $# -gt 0 ]; do
  case "$1" in
    --no-cache) CACHE_MODE="bypass"; shift ;;
    --refresh-cache) CACHE_MODE="refresh"; shift ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    --*) echo "FATAL: unknown option $1" >&2; exit 2 ;;
    *) break ;;
  esac
done
case "$CACHE_MODE" in use|bypass|refresh) ;; *) echo "FATAL: BUILD_DOCX_CACHE_MODE must be use, bypass, or refresh" >&2; exit 2 ;; esac

DIR="${1:?audit dir}"; DIR="$(cd "$DIR" && pwd)"
OUT="${2:?output basename}"; SRC="${3:-$DIR/report/REPORT_final.md}"
case "$SRC" in /*) ;; *) SRC="$(cd "$(dirname "$SRC")" && pwd)/$(basename "$SRC")" ;; esac
case "$OUT" in /*) OUT_BASE="$OUT" ;; *) OUT_BASE="$DIR/report/$OUT" ;; esac
OUT_PARENT=$(dirname "$OUT_BASE")
[ -d "$OUT_PARENT" ] || { echo "FATAL: output directory $OUT_PARENT does not exist"; exit 1; }
OUT_BASE="$(cd "$OUT_PARENT" && pwd)/$(basename "$OUT_BASE")"
case "$OUT_BASE" in "$DIR"/*) ;; *) echo "FATAL: output basename escapes audit root: $OUT_BASE" >&2; exit 1 ;; esac
OUT_DOCX="$OUT_BASE.docx"
OUT_PDF="$OUT_BASE.pdf"
MAX_PAGES="${MAX_PAGES:-10}"
CACHE_DESCRIPTOR=""
CACHE_METADATA=""
cleanup_cache_temps () {
  [ -n "$CACHE_DESCRIPTOR" ] && rm -f "$CACHE_DESCRIPTOR"
  [ -n "$CACHE_METADATA" ] && rm -f "$CACHE_METADATA"
}
trap cleanup_cache_temps EXIT INT TERM

write_document_manifest () {
  python3 - "$DIR" "$SRC" "$OUT_DOCX" "$OUT_PDF" <<'PYBUILD'
import datetime,hashlib,json,os,pathlib,sys,tempfile
root,report,docx,pdf=map(pathlib.Path,sys.argv[1:]); root=root.resolve()
def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def item(path):
    path=path.resolve()
    try: relative=path.relative_to(root).as_posix()
    except ValueError: raise SystemExit(f"built document escapes audit root: {path}")
    return {"path":relative,"sha256":digest(path),"bytes":path.stat().st_size}
manifest={"schema_version":"2.0.0",
          "created_at":datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
          "report":item(report),"docx":item(docx),"pdf":item(pdf)}
target=root/"document_build_manifest.json"
fd,tmp=tempfile.mkstemp(prefix=target.name+".",dir=root)
with os.fdopen(fd,"w",encoding="utf-8") as handle:
    json.dump(manifest,handle,indent=2,sort_keys=True); handle.write("\n")
    handle.flush(); os.fsync(handle.fileno())
os.replace(tmp,target)
print(f"document build manifest: {target}")
PYBUILD
}

# pandoc is not always on PATH (this machine has it only in a dedicated conda env).
PANDOC="${PANDOC:-$(command -v pandoc || true)}"
[ -n "$PANDOC" ] || for p in "$HOME/miniconda3/envs/i4rdoc/bin/pandoc" /opt/homebrew/bin/pandoc /usr/local/bin/pandoc; do
  [ -x "$p" ] && PANDOC="$p" && break
done
[ -n "$PANDOC" ] || { echo "FATAL: pandoc not found. Install it (conda create -n i4rdoc -c conda-forge pandoc) or set PANDOC=<path>"; exit 1; }
[ -f "$SRC" ] || { echo "FATAL: $SRC not found"; exit 1; }
[ -f "$DIR/report/front_matter.md" ] || { echo "FATAL: write $DIR/report/front_matter.md first (see reference/report_structure.md)"; exit 1; }
[ -f "$DIR/report/captions.txt" ] || { echo "FATAL: write $DIR/report/captions.txt — one caption per line, in document order"; exit 1; }
[ -f "$CACHE_HELPER" ] || { echo "FATAL: cache helper missing: $CACHE_HELPER"; exit 1; }

# Resolve the exact reference document before computing the build key. The small default-reference
# lookup is cached separately by Pandoc identity; the document cache itself hashes the exact bytes.
mkdir -p "$DIR/.cache"
PANDOC_KEY=$("$PANDOC" --version | head -1 | shasum -a 256 | cut -c1-12)
REF_CACHE="$DIR/.cache/reference-${PANDOC_KEY}.docx"
if [ ! -s "$REF_CACHE" ]; then
  "$PANDOC" --print-default-data-file reference.docx > "$REF_CACHE.tmp" || exit 1
  mv "$REF_CACHE.tmp" "$REF_CACHE"
fi

SOF=$(command -v soffice || echo "/Applications/LibreOffice.app/Contents/MacOS/soffice")
PANDOC_ID=$("$PANDOC" --version 2>/dev/null | head -1)
PYTHON_ID=$(python3 --version 2>&1)
PDFINFO_ID=$(pdfinfo -v 2>&1 | head -1 || true)
PDFTOTEXT_ID=$(pdftotext -v 2>&1 | head -1 || true)
if [ -x "$SOF" ]; then
  PDF_ENGINE="libreoffice"
  PDF_ENGINE_ID=$("$SOF" --version 2>/dev/null | head -1)
else
  PDF_ENGINE="xelatex-proxy"
  PDF_ENGINE_ID=$(xelatex --version 2>/dev/null | head -1 || echo unavailable)
fi

CACHE_KEY=""
CACHE_ARCHIVE=""
if [ "$CACHE_MODE" != "bypass" ]; then
  mkdir -p "$DIR/.cache/document-builds"
  CACHE_DESCRIPTOR=$(mktemp "$DIR/.cache/.document-build-inputs.XXXXXX.json") || exit 1
  CACHE_METADATA=$(mktemp "$DIR/.cache/.document-build-validation.XXXXXX.json") || exit 1
  CACHE_KEY=$(python3 "$CACHE_HELPER" key \
    --descriptor "$CACHE_DESCRIPTOR" \
    --input "report=$SRC" \
    --input "front_matter=$DIR/report/front_matter.md" \
    --input "captions=$DIR/report/captions.txt" \
    --input "reference_doc=$REF_CACHE" \
    --input "build_script=$SELF" \
    --input "cache_helper=$CACHE_HELPER" \
    --option "max_pages=$MAX_PAGES" \
    --option "output_basename=$(basename "$OUT_BASE")" \
    --option "pandoc_reader=markdown+pipe_tables" \
    --option "pdf_engine=$PDF_ENGINE" \
    --tool "pandoc=$PANDOC_ID" \
    --tool "pdf_engine=$PDF_ENGINE_ID" \
    --tool "pdfinfo=$PDFINFO_ID" \
    --tool "pdftotext=$PDFTOTEXT_ID" \
    --tool "python=$PYTHON_ID")
  [ $? -eq 0 ] && [ -n "$CACHE_KEY" ] || { echo "FATAL: could not compute document cache key" >&2; exit 1; }
  CACHE_ARCHIVE="$DIR/.cache/document-builds/$CACHE_KEY.zip"
fi

if [ "$CACHE_MODE" = "use" ]; then
  python3 "$CACHE_HELPER" restore --archive "$CACHE_ARCHIVE" --key "$CACHE_KEY" \
    --docx "$OUT_DOCX" --pdf "$OUT_PDF" --root "$DIR" --metadata-out "$CACHE_METADATA"
  CACHE_RC=$?
  if [ "$CACHE_RC" -eq 0 ]; then
    write_document_manifest || exit 1
    python3 - "$CACHE_METADATA" <<'PYCACHEHIT'
import json,sys
v=json.load(open(sys.argv[1],encoding="utf-8"))
print("built from validated cache: %s pages total%s" %
      (v.get("pages","unknown"), (" — "+v["page_source"]) if v.get("page_source") else ""))
PYCACHEHIT
    echo ""
    echo "NOW VERIFY: pdftoppm -jpeg -r 85 -f 1 -l 2 $OUT_PDF pg   then read pg-*.jpg"
    echo "Check a page containing a table. Cached bytes passed their original page, abstract, and table gates."
    exit 0
  elif [ "$CACHE_RC" -eq 4 ]; then
    echo "WARNING: invalid document cache entry ignored; rebuilding $CACHE_KEY" >&2
  elif [ "$CACHE_RC" -ne 3 ]; then
    echo "FATAL: document cache restore failed operationally (exit $CACHE_RC)" >&2
    exit 1
  fi
fi

python3 - "$SRC" "$DIR/report/captions.txt" "$DIR/report/_body.md" << 'PY'
import sys, re, pathlib
src, capfile, dest = sys.argv[1], sys.argv[2], sys.argv[3]
raw = pathlib.Path(src).read_text()
# The source-only claim cross-reference is a machine join, not journal prose. Remove the exact
# generated block before document conversion; deleting only the `[claim N]` tokens would leave a
# duplicate findings list in the published body.
starts = raw.count("<!-- I4R-GENERATED-CLAIM-TAGS:START -->")
ends = raw.count("<!-- I4R-GENERATED-CLAIM-TAGS:END -->")
if starts != ends or starts > 1:
    print(f"  FATAL: malformed generated claim-tag block ({starts} start, {ends} end)")
    raise SystemExit(2)
raw, tag_blocks = re.subn(
    r"(?ms)^<!-- I4R-GENERATED-CLAIM-TAGS:START -->.*?"
    r"^<!-- I4R-GENERATED-CLAIM-TAGS:END -->\s*", "", raw,
)
L = raw.split("\n")
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
if tag_blocks or ntags:
    print(f"claim handoff stripped from the deliverable: {tag_blocks} generated block(s), "
          f"{ntags} legacy inline tag(s) (source remains in REPORT_final.md)")
pathlib.Path(dest).write_text(txt)
nb = sum(1 for x,l in enumerate(out) if l.lstrip().startswith("|") and not out[x-1].lstrip().startswith("|"))
print(f"table blocks: {nb}   captions available: {len(CAPS)}   captions placed: {k}")
if nb != len(CAPS):
    print(f"  FATAL: {nb} table blocks but {len(CAPS)} captions")
    raise SystemExit(2)
PY
[ $? -eq 0 ] || exit 1

cd "$DIR/report"
cat front_matter.md _body.md > _full.md
"$PANDOC" _full.md -f markdown+pipe_tables -t docx --reference-doc="$REF_CACHE" -o "$OUT_DOCX" || exit 1


# ---- link colouring (house style, 2026-08-22) -------------------------------------------------
# Citations and URLs BLUE, cross-references to tables, figures and sections RED, matching the
# group's paper template (citecolor blue / linkcolor red). Word takes hyperlink colour from a
# character style, so two colours would need two styles; the run colour is set directly instead,
# which renders identically in Word, LibreOffice and any PDF converted from either.
python3 - "$OUT_DOCX" <<'PYLINK'
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
PAGES=""
rm -f "$OUT_PDF" "${OUT_BASE}_proxy.pdf"
if [ -x "$SOF" ]; then
  LO_PROFILE=$(mktemp -d "${TMPDIR:-/tmp}/i4r-lo.XXXXXX")
  "$SOF" --headless -env:UserInstallation="file://$LO_PROFILE" --convert-to pdf \
    --outdir "$(dirname "$OUT_PDF")" "$OUT_DOCX" >/dev/null 2>&1
  rm -rf "$LO_PROFILE"
  PAGES=$(pdfinfo "$OUT_PDF" 2>/dev/null | awk '/Pages/{print $2}')
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
    -o "${OUT_BASE}_proxy.pdf" >/dev/null 2>&1
  PAGES=$(pdfinfo "${OUT_BASE}_proxy.pdf" 2>/dev/null | awk '/Pages/{print $2}')
  [ -n "$PAGES" ] && PAGE_SRC="xelatex PROXY at reference.docx geometry (12pt, 10pt para spacing) — LibreOffice absent, so the .docx itself is UNMEASURED"
fi
echo "built: $OUT_DOCX (${PAGES:-unknown} pages total${PAGE_SRC:+ — $PAGE_SRC})"
rm -f _full.md _body.md

# The cap counts the BODY only: front matter through the last numbered section. References and
# appendices start on their own page and are excluded (team decision 2026-08-22), because a
# comment should not lose a finding to make room for its own bibliography.
BODY_PAGES=""
if [ -n "${PAGES:-}" ] && [ -f "$OUT_PDF" ]; then
BODY_PAGES=$(python3 - "$OUT_PDF" "$PAGES" <<'PYPG'
import subprocess, sys
pdf, pages = sys.argv[1], int(sys.argv[2])
raw = subprocess.run(["pdftotext", "-layout", pdf, "-"], capture_output=True, text=True).stdout
texts = raw.split("\f")
first_back = None
for i in range(1, pages + 1):
    t = " ".join((texts[i-1] if i-1 < len(texts) else "").split())
    if t.startswith("References") or t.startswith("Appendix"):
        first_back = i; break
print(first_back - 1 if first_back else pages)
PYPG
)
fi

# The abstract must not run onto page 2: it is the first thing an editor reads and it belongs
# whole on the opening page with the citation block.
if [ -f "$OUT_PDF" ] && [ -f "$SRC" ]; then
  python3 - "$OUT_PDF" "$SRC" <<'PYABS'
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
echo "NOW VERIFY: pdftoppm -jpeg -r 85 -f 1 -l 2 $OUT_PDF pg   then read pg-*.jpg"
echo "Check a page containing a table. Truncated tables mean a caption landed inside one."

# Bind the exact editable source to the exact deliverables. Publication refuses an arbitrary
# lone PDF even when it happens to be named correctly.
if [ -f "$OUT_PDF" ]; then
  write_document_manifest || exit 1
  if [ "$CACHE_MODE" != "bypass" ] && [ -n "${PAGES:-}" ]; then
    python3 - "$CACHE_METADATA" "$PAGES" "$BODY_PAGES" "$PAGE_SRC" "$MAX_PAGES" <<'PYCACHEMETA'
import json,os,pathlib,sys,tempfile
target=pathlib.Path(sys.argv[1])
def integer(value): return int(value) if value else None
value={"status":"passed","pages":integer(sys.argv[2]),"body_pages":integer(sys.argv[3]),
       "page_source":sys.argv[4] or None,"max_pages":int(sys.argv[5]),"exact_pdf":True}
fd,tmp=tempfile.mkstemp(prefix=target.name+".",dir=target.parent)
with os.fdopen(fd,"w",encoding="utf-8") as handle:
    json.dump(value,handle,sort_keys=True,separators=(",",":")); handle.write("\n")
    handle.flush(); os.fsync(handle.fileno())
os.replace(tmp,target)
PYCACHEMETA
    python3 "$CACHE_HELPER" store --archive "$CACHE_ARCHIVE" --key "$CACHE_KEY" \
      --descriptor "$CACHE_DESCRIPTOR" --docx "$OUT_DOCX" --pdf "$OUT_PDF" \
      --metadata "$CACHE_METADATA"
    CACHE_RC=$?
    [ "$CACHE_RC" -eq 0 ] || echo "WARNING: document built successfully but cache storage failed (exit $CACHE_RC)" >&2
  elif [ "$CACHE_MODE" != "bypass" ]; then
    echo "WARNING: exact PDF exists but its page gate was unverified; this build was not cached" >&2
  fi
else
  echo "WARNING: no exact PDF was built; document_build_manifest.json was not created. Publication will remain blocked."
fi
