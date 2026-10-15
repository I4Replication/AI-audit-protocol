#!/bin/bash
# build_docx.sh [--no-cache|--refresh-cache] [--preview-max-pages N]
#               <audit-dir> <output-basename> [final-report.md]
#
# Canonical builds take their ten-body-page limit only from the audit's hash-bound policy
# snapshot. Preview limits are explicit CLI input, live below report/previews/, never touch the
# canonical manifest/cache, and are permanently non-release.
set -uo pipefail

SELF="$(cd "$(dirname "$0")" 2>/dev/null && pwd)/$(basename "$0")"
SCRIPT_DIR=$(dirname "$SELF")
CACHE_HELPER="$SCRIPT_DIR/document_build_cache.py"
DOCUMENT_RELEASE="$SCRIPT_DIR/document_release.py"
CACHE_MODE="${BUILD_DOCX_CACHE_MODE:-use}"
PREVIEW_MAX=""

usage () { sed -n '2,9p' "$0"; }
fatal () { echo "FATAL: build_docx.sh: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --no-cache) CACHE_MODE="bypass"; shift ;;
    --refresh-cache) CACHE_MODE="refresh"; shift ;;
    --preview-max-pages)
      [ $# -ge 2 ] || fatal "--preview-max-pages requires a positive integer"
      PREVIEW_MAX="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    --*) fatal "unknown option $1" ;;
    *) break ;;
  esac
done
case "$CACHE_MODE" in use|bypass|refresh) ;; *) fatal "BUILD_DOCX_CACHE_MODE must be use, bypass, or refresh" ;; esac

DIR="${1:?audit dir}"
[ -d "$DIR" ] || fatal "audit directory does not exist: $DIR"
DIR="$(cd "$DIR" && pwd -P)"
OUT="${2:?output basename}"
SRC="${3:-$DIR/report/REPORT_final.md}"
case "$SRC" in /*) ;; *) SRC="$(cd "$(dirname "$SRC")" && pwd -P)/$(basename "$SRC")" ;; esac

BUILD_KIND="canonical"
if [ -n "$PREVIEW_MAX" ]; then
  BUILD_KIND="preview"
  case "$PREVIEW_MAX" in ''|*[!0-9]*) fatal "--preview-max-pages requires a positive integer" ;; esac
  [ "$PREVIEW_MAX" -ge 1 ] || fatal "--preview-max-pages requires a positive integer"
  CACHE_MODE="bypass"
  PREVIEW_NAME=$(basename "$OUT")
  case "$PREVIEW_NAME" in *.pdf|*.docx) PREVIEW_NAME=${PREVIEW_NAME%.*} ;; esac
  [ -n "$PREVIEW_NAME" ] && [ "$PREVIEW_NAME" != "." ] && [ "$PREVIEW_NAME" != ".." ] ||
    fatal "invalid preview basename"
  mkdir -p "$DIR/report/previews" || fatal "cannot create report/previews"
  OUT_BASE="$DIR/report/previews/$PREVIEW_NAME"
  CAP="$PREVIEW_MAX"
  MANIFEST_TARGET="$OUT_BASE.manifest.json"
else
  if [ "${MAX_PAGES+x}" = "x" ]; then
    fatal "MAX_PAGES is forbidden for canonical builds; use --preview-max-pages for a non-release preview"
  fi
  [ -f "$DOCUMENT_RELEASE" ] || fatal "document verifier missing: $DOCUMENT_RELEASE"
  CAP=$(python3 "$DOCUMENT_RELEASE" policy-cap "$DIR") || exit 1
  case "$OUT" in /*) OUT_BASE="$OUT" ;; *) OUT_BASE="$DIR/report/$OUT" ;; esac
  OUT_PARENT=$(dirname "$OUT_BASE")
  [ -d "$OUT_PARENT" ] || fatal "output directory does not exist: $OUT_PARENT"
  OUT_BASE="$(cd "$OUT_PARENT" && pwd -P)/$(basename "$OUT_BASE")"
  [ "$(dirname "$OUT_BASE")" = "$DIR/report" ] ||
    fatal "canonical output must be a direct child of $DIR/report"
  MANIFEST_TARGET="$DIR/document_build_manifest.json"
fi

OUT_DOCX="$OUT_BASE.docx"
OUT_PDF="$OUT_BASE.pdf"
DIAGNOSTIC_DIR="$DIR/document_build_diagnostics"
DIAGNOSTIC="$DIAGNOSTIC_DIR/last_failure.json"
TX_PARENT="$DIR/.document-build-transactions"
mkdir -p "$TX_PARENT" || fatal "cannot create private build transaction directory"
# Stale build transactions (older than an hour) belong to builds that are not running; the synced
# folder has been seen to restore removed ones with their contents. Sweep before starting.
find "$TX_PARENT" -mindepth 1 -maxdepth 1 -type d -name 'build.*' -mmin +60 -exec rm -rf {} + 2>/dev/null || true
chmod 700 "$TX_PARENT" 2>/dev/null || true
TX=$(mktemp -d "$TX_PARENT/build.XXXXXX") || fatal "cannot create private build transaction"
chmod 700 "$TX" 2>/dev/null || true
BUILD_STAGE="preflight"
BUILD_OK=0

cleanup_build () {
  rc=$1
  trap - EXIT HUP INT TERM
  if [ "$BUILD_OK" -ne 1 ] && [ "$rc" -ne 0 ]; then
    mkdir -p "$DIAGNOSTIC_DIR" 2>/dev/null || true
    if [ -f "$TX/failure.json" ]; then
      mv -f "$TX/failure.json" "$DIAGNOSTIC" 2>/dev/null || true
    elif [ -f "$DOCUMENT_RELEASE" ]; then
      python3 "$DOCUMENT_RELEASE" diagnostic --out "$DIAGNOSTIC" --build-kind "$BUILD_KIND" \
        --stage "$BUILD_STAGE" --message "document build failed before promotion" >/dev/null 2>&1 || true
    fi
  fi
  [ ! -d "$TX" ] || rm -rf "$TX"
  exit "$rc"
}
trap 'cleanup_build $?' EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

[ -f "$SRC" ] || fatal "$SRC not found"
[ -f "$DIR/report/front_matter.md" ] ||
  fatal "write $DIR/report/front_matter.md first (see reference/report_structure.md)"
# report_structure.md forbids a front matter ending in a line of only - or = characters: depending on the
# pandoc extensions it can retitle the line above (setext heading), open a YAML block, or leave a stray
# rule before the body. Refuse it here rather than let the byline's rendering depend on that.
LAST_FM_LINE="$(awk 'NF{last=$0} END{sub(/^[ \t]+/, "", last); sub(/[ \t\r]+$/, "", last); print last}' "$DIR/report/front_matter.md")"
if [[ "$LAST_FM_LINE" =~ ^-+$ ]] || [[ "$LAST_FM_LINE" =~ ^=+$ ]]; then
  fatal "report/front_matter.md ends with a bare '$LAST_FM_LINE' line; remove it (reference/report_structure.md forbids it: depending on pandoc extensions it retitles the line above, opens a YAML block, or leaves a stray rule)"
fi
[ -f "$DIR/report/captions.txt" ] ||
  fatal "write $DIR/report/captions.txt -- one caption per line, in document order"
[ -f "$CACHE_HELPER" ] || fatal "cache helper missing: $CACHE_HELPER"
[ -f "$DOCUMENT_RELEASE" ] || fatal "document verifier missing: $DOCUMENT_RELEASE"

if [ "$BUILD_KIND" = "canonical" ]; then
  python3 "$DOCUMENT_RELEASE" preflight-inventory "$DIR" \
    --pdf-target "$OUT_PDF" --docx-target "$OUT_DOCX" || exit 1
fi

PANDOC="${PANDOC:-$(command -v pandoc || true)}"
if [ -z "$PANDOC" ]; then
  for candidate in "${HOME:-}/miniconda3/envs/i4rdoc/bin/pandoc" \
                   /opt/homebrew/bin/pandoc /usr/local/bin/pandoc; do
    [ -x "$candidate" ] && PANDOC="$candidate" && break
  done
fi
[ -n "$PANDOC" ] && [ -x "$PANDOC" ] || fatal "pandoc not found; install it or set PANDOC=<path>"
SOFFICE=$(command -v soffice || true)
[ -n "$SOFFICE" ] || SOFFICE="/Applications/LibreOffice.app/Contents/MacOS/soffice"
[ -x "$SOFFICE" ] || fatal "LibreOffice/soffice is required; proxy PDF rendering is forbidden"
command -v pdfinfo >/dev/null 2>&1 || fatal "pdfinfo is required for exact page counting"
command -v pdftotext >/dev/null 2>&1 || fatal "pdftotext is required for exact boundary checks"

PANDOC_ID=$("$PANDOC" --version 2>/dev/null | sed -n '1p')
RENDERER_ID=$("$SOFFICE" --version 2>/dev/null | sed -n '1p')
PDFINFO_ID=$(pdfinfo -v 2>&1 | sed -n '1p')
PDFTOTEXT_ID=$(pdftotext -v 2>&1 | sed -n '1p')
[ -n "$PANDOC_ID" ] && [ -n "$RENDERER_ID" ] && [ -n "$PDFINFO_ID" ] && [ -n "$PDFTOTEXT_ID" ] ||
  fatal "renderer identity telemetry is incomplete"

BUILD_STAGE="reference_doc"
# pandoc stamps the current time into the zip entries of the reference document it prints, and the
# cache key binds those bytes, so before 2.6.8 no two builds shared a key and the cache never hit.
# A fixed SOURCE_DATE_EPOCH pins the timestamps; the member contents are the same either way.
SOURCE_DATE_EPOCH=0 "$PANDOC" --print-default-data-file reference.docx > "$TX/reference.docx" ||
  fatal "pandoc could not produce its exact reference document"
[ -s "$TX/reference.docx" ] || fatal "pandoc reference document is empty"

CACHE_DESCRIPTOR="$TX/cache-inputs.json"
CACHE_METADATA="$TX/cache-validation.json"
CACHE_KEY=""
CACHE_ARCHIVE=""
if [ "$CACHE_MODE" != "bypass" ]; then
  mkdir -p "$DIR/.cache/document-builds" || fatal "cannot create document cache directory"
  CACHE_KEY=$(python3 "$CACHE_HELPER" key \
    --descriptor "$CACHE_DESCRIPTOR" \
    --input "report=$SRC" \
    --input "front_matter=$DIR/report/front_matter.md" \
    --input "captions=$DIR/report/captions.txt" \
    --input "reference_doc=$TX/reference.docx" \
    --input "policy_snapshot=$DIR/pipeline-policy.snapshot.json" \
    --input "build_script=$SELF" \
    --input "document_verifier=$DOCUMENT_RELEASE" \
    --input "cache_helper=$CACHE_HELPER" \
    --option "body_page_cap=$CAP" \
    --option "build_kind=canonical" \
    --option "output_basename=$(basename "$OUT_BASE")" \
    --option "pandoc_reader=markdown+pipe_tables" \
    --tool "pandoc=$PANDOC_ID" \
    --tool "pdf_engine=$RENDERER_ID" \
    --tool "pdfinfo=$PDFINFO_ID" \
    --tool "pdftotext=$PDFTOTEXT_ID") || fatal "could not compute document cache key"
  [ -n "$CACHE_KEY" ] || fatal "empty document cache key"
  CACHE_ARCHIVE="$DIR/.cache/document-builds/$CACHE_KEY.zip"
fi

CACHE_HIT=0
if [ "$CACHE_MODE" = "use" ]; then
  BUILD_STAGE="cache_restore"
  python3 "$CACHE_HELPER" restore --archive "$CACHE_ARCHIVE" --key "$CACHE_KEY" \
    --docx "$TX/document.docx" --pdf "$TX/document.pdf" --root "$DIR"
  CACHE_RC=$?
  if [ "$CACHE_RC" -eq 0 ]; then
    CACHE_HIT=1
  elif [ "$CACHE_RC" -eq 4 ]; then
    echo "WARNING: invalid document cache entry ignored; rebuilding $CACHE_KEY" >&2
  elif [ "$CACHE_RC" -ne 3 ]; then
    fatal "document cache restore failed operationally (exit $CACHE_RC)"
  fi
fi

if [ "$CACHE_HIT" -ne 1 ]; then
  BUILD_STAGE="source_transform"
  python3 - "$SRC" "$DIR/report/captions.txt" "$TX/body.md" "$DOCUMENT_RELEASE" <<'PYBODY'
import pathlib, re, sys
source, caption_file, destination, release_module = map(pathlib.Path, sys.argv[1:])
# The findings-table helpers live in the document verifier, which the cache key and the manifest
# already bind; importing it must not leave bytecode beside the skill's scripts.
sys.dont_write_bytecode = True
sys.path.insert(0, str(release_module.parent))
import document_release
raw = source.read_text(encoding="utf-8")
starts = raw.count("<!-- I4R-GENERATED-CLAIM-TAGS:START -->")
ends = raw.count("<!-- I4R-GENERATED-CLAIM-TAGS:END -->")
if starts != ends or starts > 1:
    raise SystemExit(f"FATAL: malformed generated claim-tag block ({starts} start, {ends} end)")
raw, blocks = re.subn(
    r"(?ms)^<!-- I4R-GENERATED-CLAIM-TAGS:START -->.*?"
    r"^<!-- I4R-GENERATED-CLAIM-TAGS:END -->\s*", "", raw,
)
lines = raw.split("\n")
try:
    lines = lines[next(index for index, line in enumerate(lines)
                       if line.startswith("## Executive summary") or line.startswith("## Abstract")):]
except StopIteration:
    pass
captions = [line.strip() for line in caption_file.read_text(encoding="utf-8").splitlines()
            if line.strip()]
middle = []
for line in lines:
    section = re.match(r"^## (\d+)\. (.+)$", line)
    if section:
        middle.append(f"# Section {section.group(1)}. {section.group(2)}")
        continue
    if line.startswith("## Executive summary") or line.startswith("## Abstract"):
        middle.append("# " + line[3:])
        continue
    if (line.startswith("## Data and Code Availability") or
            line.startswith("## References") or line.startswith("## Appendix")):
        middle.extend([
            "```{=openxml}", '<w:p><w:pPr><w:pStyle w:val="I4RPageBreakBeforeNext"/></w:pPr></w:p>', "```", "",
            "# " + line[3:],
        ])
        continue
    subsection = re.match(r"^\*\*(\d+\.\d+[a-z]?(?:-[a-z]+)?) (.+?)\.?\*\*[ ]?(.*)$", line)
    if subsection:
        middle.extend([f"## {subsection.group(1)} {subsection.group(2)}", ""])
        if subsection.group(3):
            middle.append(subsection.group(3))
        continue
    middle.append(line)

output = []
caption_index = 0
index = 0
table_blocks = 0
findings_tags = 0
while index < len(middle):
    if middle[index].lstrip().startswith("|"):
        end = index
        while end < len(middle) and middle[end].lstrip().startswith("|"):
            end += 1
        table_blocks += 1
        if caption_index < len(captions):
            if output and output[-1].strip():
                output.append("")
            output.extend([f"*Table {caption_index + 1}. {captions[caption_index]}*", ""])
            caption_index += 1
        # The generated findings table renders literally, with fixed column widths; every other
        # table passes through unchanged. Its claim tags are stripped here, before escaping.
        block = middle[index:end]
        if document_release.is_findings_table(block):
            findings_tags += sum(len(document_release.CLAIM_TAG.findall(line)) for line in block)
            try:
                block = document_release.pandoc_findings_table(block)
            except document_release.DocumentReleaseError as exc:
                raise SystemExit(f"FATAL: {exc}")
        output.extend(block + [""])
        index = end
        continue
    output.append(middle[index])
    index += 1
text = "\n".join(output)
text = re.sub(r"§(\d+)\.(\d+)", r"Section \1.\2", text)
text = re.sub(r"§(\d+)", r"Section \1", text)
legacy_tags = findings_tags + len(re.findall(r"\s?\[claim \d+\]", text))
text = re.sub(r"\s?\[claim \d+\]", "", text)
if table_blocks != len(captions):
    raise SystemExit(f"FATAL: {table_blocks} table blocks but {len(captions)} captions")
destination.write_text(text, encoding="utf-8")
print(f"table blocks: {table_blocks}; captions placed: {caption_index}")
if blocks or legacy_tags:
    print(f"claim handoff stripped: {blocks} generated block(s), {legacy_tags} legacy tag(s)")
PYBODY
  [ $? -eq 0 ] || exit 1

  python3 - "$DIR/report/front_matter.md" "$TX/body.md" "$TX/full.md" <<'PYFULL'
import pathlib, sys
front, body, output = map(pathlib.Path, sys.argv[1:])
output.write_bytes(front.read_bytes() + b"\n" + body.read_bytes())
PYFULL

  BUILD_STAGE="pandoc"
  "$PANDOC" "$TX/full.md" -f markdown+pipe_tables -t docx \
    --reference-doc="$TX/reference.docx" -o "$TX/document.docx" || exit 1

  BUILD_STAGE="docx_link_style"
  python3 - "$TX/document.docx" <<'PYLINK'
import re, shutil, sys, zipfile
docx = sys.argv[1]
with zipfile.ZipFile(docx) as source:
    names = source.namelist()
    blobs = {name: source.read(name) for name in names}
document = blobs["word/document.xml"].decode("utf-8")
relationships = blobs["word/_rels/document.xml.rels"].decode("utf-8")
external = set(re.findall(r'Id="([^"]+)"[^>]*TargetMode="External"', relationships))

def colour(open_tag):
    anchor = re.search(r'w:anchor="([^"]*)"', open_tag)
    if anchor:
        return "0000FF" if anchor.group(1).startswith("ref-") else "FF0000"
    relation = re.search(r'r:id="([^"]*)"', open_tag)
    if relation and relation.group(1) in external:
        return "0000FF"
    return None

rpr = '<w:rPr><w:color w:val="%s"/><w:u w:val="single"/>'
pieces, cursor = [], 0
for match in re.finditer(r"<w:hyperlink\b[^>]*>.*?</w:hyperlink>", document, re.S):
    block = match.group(0)
    value = colour(block[:block.find(">") + 1])
    pieces.append(document[cursor:match.start()])
    cursor = match.end()
    if value:
        block = re.sub(r'<w:color w:val="[^"]*"\s*/>', "", block)
        block = block.replace("<w:rPr>", rpr % value)
        block = re.sub(r"<w:r>(?!<w:rPr>)", "<w:r>" + (rpr % value) + "</w:rPr>", block)
    pieces.append(block)
pieces.append(document[cursor:])
document = "".join(pieces)
# Back-matter headings start a new page through the heading's own pageBreakBefore, never through a
# separate paragraph holding a page-break run: when the body exactly fills a page, that paragraph
# spills onto the next page and the break then leaves it blank (a counted, empty body page).
MARKER = '<w:p><w:pPr><w:pStyle w:val="I4RPageBreakBeforeNext"/></w:pPr></w:p>'
marked = document.count(MARKER)
out, rest = [], document
while MARKER in rest:
    before, rest = rest.split(MARKER, 1)
    out.append(before)
    heading = re.match(r'((?:\s|<w:bookmark(?:Start|End)\b[^>]*/>)*)<w:p><w:pPr><w:pStyle w:val="Heading1" ?/>', rest)
    if not heading:
        raise SystemExit("FATAL: a back-matter page-break marker is not followed by a Heading1 paragraph")
    rest = rest[:heading.end()] + "<w:pageBreakBefore/>" + rest[heading.end():]
out.append(rest)
document = "".join(out)
if MARKER in document or document.count("<w:pageBreakBefore/>") < marked:
    raise SystemExit("FATAL: back-matter page breaks were not applied")
pieces = [document]
temporary = docx + ".styled"
with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as output:
    for name in names:
        output.writestr(name, "".join(pieces).encode("utf-8")
                        if name == "word/document.xml" else blobs[name])
shutil.move(temporary, docx)
PYLINK
  [ $? -eq 0 ] || exit 1

  BUILD_STAGE="libreoffice"
  # The profile location is passed to LibreOffice as a file:// URL. An audit directory named after
  # a paper title routinely contains colons and spaces, which are not URL-safe; LibreOffice aborts
  # (SIGABRT) on such a URL. Keep the throwaway profile in a URL-safe private temp directory and
  # pass the document and output directory as plain paths, which LibreOffice handles correctly.
  LO_PROFILE=$(mktemp -d "${TMPDIR:-/tmp}/i4r-lo-profile.XXXXXX") || exit 1
  "$SOFFICE" --headless -env:UserInstallation="file://$LO_PROFILE" --convert-to pdf \
    --outdir "$TX" "$TX/document.docx" >/dev/null 2>&1
  lo_rc=$?
  rm -rf "$LO_PROFILE"
  [ "$lo_rc" -eq 0 ] || exit 1
  [ -s "$TX/document.pdf" ] || fatal "LibreOffice did not produce the exact PDF"
fi

BUILD_STAGE="findings_table_integrity"
# Fail closed on the exact bytes, rebuilt or restored from the cache, before any manifest can bind
# them: each DOCX cell of the generated Appendix A table must be its literal source text, and no
# table word may be broken across lines in the PDF. Build-time only; it is not a manifest
# validation. A report without the generated table passes before either file is read.
python3 "$DOCUMENT_RELEASE" findings-table-integrity --report "$SRC" \
  --docx "$TX/document.docx" --pdf "$TX/document.pdf" \
  --build-kind "$BUILD_KIND" --diagnostic-out "$TX/failure.json" || exit 1

BUILD_STAGE="validation"
CREATE_ARGS=(
  create-manifest --audit "$DIR" --report "$SRC"
  --front-matter "$DIR/report/front_matter.md" --captions "$DIR/report/captions.txt"
  --docx "$TX/document.docx" --pdf "$TX/document.pdf"
  --docx-relative "${OUT_DOCX#$DIR/}" --pdf-relative "${OUT_PDF#$DIR/}"
  --builder "$SELF" --cache-helper "$CACHE_HELPER"
  --pandoc-identity "$PANDOC_ID" --renderer-identity "$RENDERER_ID"
  --pdfinfo-identity "$PDFINFO_ID" --pdftotext-identity "$PDFTOTEXT_ID"
  --build-kind "$BUILD_KIND" --cap "$CAP"
  --manifest-out "$TX/document_build_manifest.json" --diagnostic-out "$TX/failure.json"
)
python3 "$DOCUMENT_RELEASE" "${CREATE_ARGS[@]}" || exit 1

if [ "$BUILD_KIND" = "canonical" ] && [ "$CACHE_MODE" != "bypass" ] && [ "$CACHE_HIT" -ne 1 ]; then
  BUILD_STAGE="cache_store"
  python3 - "$TX/document_build_manifest.json" "$CACHE_METADATA" <<'PYCACHE'
import json, pathlib, sys
manifest = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
value = {
    "schema_version": manifest["schema_version"],
    "status": "passed",
    "build_kind": manifest["build_kind"],
    "all_validations_passed": manifest["all_validations_passed"],
    "pages": manifest["pages"],
}
pathlib.Path(sys.argv[2]).write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
PYCACHE
  python3 "$CACHE_HELPER" store --archive "$CACHE_ARCHIVE" --key "$CACHE_KEY" \
    --descriptor "$CACHE_DESCRIPTOR" --docx "$TX/document.docx" --pdf "$TX/document.pdf" \
    --metadata "$CACHE_METADATA"
  CACHE_RC=$?
  [ "$CACHE_RC" -eq 0 ] ||
    echo "WARNING: document passed but cache storage failed (exit $CACHE_RC); promotion continues" >&2
fi

BUILD_STAGE="promotion"
python3 "$DOCUMENT_RELEASE" promote --audit "$DIR" \
  --manifest-source "$TX/document_build_manifest.json" --manifest-target "$MANIFEST_TARGET" \
  --docx-source "$TX/document.docx" --docx-target "$OUT_DOCX" \
  --pdf-source "$TX/document.pdf" --pdf-target "$OUT_PDF" || exit 1

if [ "$BUILD_KIND" = "canonical" ]; then
  BUILD_STAGE="post_promotion_verification"
  python3 "$DOCUMENT_RELEASE" verify "$DIR" --manifest "$MANIFEST_TARGET" \
    --builder "$SELF" --boundary build-promotion || exit 1
else
  echo "preview built (NON-RELEASE): $OUT_PDF"
  echo "  body-page limit: $CAP; manifest: $MANIFEST_TARGET"
fi

BUILD_OK=1
if [ "$CACHE_HIT" -eq 1 ]; then
  echo "built from validated cache: $OUT_DOCX and $OUT_PDF"
else
  echo "built and validated: $OUT_DOCX and $OUT_PDF"
fi
exit 0
