#!/usr/bin/env python3
"""Refuse a release that reproduces an identifying value from the deposit (stage check 3c, 2.6.8).

    python3 scripts/check_deposit_values.py <audit-dir> <release-dir> [--json <summary.json>]
    python3 scripts/check_deposit_values.py --self-test

Why this exists. From 2.6.8 a deposit holding personal data is audited like any other and the data
are reported as a disclosure finding that names the kind of identifier, the file, the column and
the counts. That policy invites more privacy findings, and until now no gate caught a finding that
quoted the value itself: a participant's name, e-mail address, date of birth or IP address passed
every existing check (check_release_privacy.py knows participant codes, timestamps and paths only).
This gate closes that gap. It judges only the audit's OWN outputs. It never refuses because the
deposit contains personal data: a deposit file it cannot read is reported as "not scanned" and the
audit goes on.

What it does.

1. Collect. From the audit's frozen deposit copy (``pkg/``, whose bytes ``package_manifest.json``
   binds at intake; the Phase-1 snapshot copy ``phase1/snapshots/*/raw/pkg`` when ``pkg/`` is
   gone) it reads every table it can: delimited text (.csv .tsv .tab .dat, and .txt that parses as
   a table), Excel workbooks (.xlsx .xlsm), JSON and JSON Lines, the same inside .zip and .gz
   containers, and -- when ``Rscript`` is on PATH -- SPSS, Stata, SAS and R files (.sav .zsav .por
   .dta .sas7bdat .xpt through the haven package; .rds .rdata .rda through base R). A column is
   collected when its HEADER (or its SPSS/Stata variable label) looks like personal data: a name,
   an e-mail address, a date of birth, an IP address (IPv4 or IPv6), a postal address or a
   telephone number. The header vocabulary covers English, German, French, Spanish, Italian,
   Portuguese and Dutch (Name, Vorname, Nachname, Nom, Prénom, Nombre, Apellido, Nome, Cognome,
   Sobrenome, Naam, Voornaam, Achternaam; E-Mail, courriel, adresse électronique, correo
   electrónico; DOB, Geburtsdatum, date de naissance, fecha de nacimiento, data di nascita, data de
   nascimento, geboortedatum; IPAddress; Adresse, indirizzo, endereço; Telefon, teléfono, ...). A
   header written in any other language is not recognised. Headers are split into words
   (camelCase, snake_case and digits separated) and matched word by word, so ``IPAQ_total`` is not
   an IP column and ``study_name`` is not a person's name. For SPSS, Stata, SAS and R files, R
   first sends every column's name and label, this module classifies them by the same rules, and
   R then sends the selected columns only. Work is capped per file (rows, distinct values, bytes)
   and per run (300 files through R). A file or column read only in part is listed as truncated,
   a personal-data column that yields no usable value is listed too, and nothing past a cap is
   guessed at.
2. Keep keyed hashes, in memory only. Each value is normalised and hashed with keyed BLAKE2b under
   a key drawn at random for this run. The key and the hashes live in this process's memory and
   are discarded when it exits: nothing value-derived is ever written to disk, and every run
   rebuilds them (seconds for an ordinary deposit). R output is read from a pipe. The optional
   ``--json`` summary holds counts, locations and value kinds only.
3. Scan every release-bound text: each staged file with a text suffix (the release privacy gate's
   own list), JSON string by string (escapes decoded), HTML with its markup removed and its
   attribute values read, the visible text of the staged DOCX, and the text and metadata of the
   staged PDF.
   A location is REFUSED when it holds
     * a collected multi-word name or address -- a whole cell of two words or more, or one row's
       first and last name from paired first- and last-name columns, in either order and as
       "Last, First" -- matched in any case, across line breaks and hyphenated line ends;
     * a collected date of birth in any common written form, or a collected e-mail, IP or phone
       value; or
     * any e-mail address, IPv4 or IPv6 address at all, deposit-derived or not (exemptions below).
   A single word that matches a one-word value of a name or address column (a given name, or a
   surname quoted alone) is a WARNING: printed with its location and value class, never a
   refusal. One-word names are too often also ordinary words, statistical eponyms or cited
   authors for a refusal to be safe, and a warning keeps them in front of the operator.
   The skill's own generated and provenance files are read by the e-mail and IP detectors but are
   never matched against other deposit values: their text is written by the skill and its tools
   (build and interpreter versions, hashes, telemetry), it cannot be reworded, and it never quotes
   a deposit cell. They are the files in ``PROVENANCE_FILES`` and under ``scripts/``, and the
   provenance and telemetry fields of ``run.json`` and ``comment.json``
   (``RUN_JSON_PROVENANCE_KEYS``, ``COMMENT_JSON_PROVENANCE_KEYS``; the text of a journal note,
   which a person types, is still matched).
   The report names the location (file and line, PDF page and line, DOCX paragraph) and the kind
   of value, never the value. A deposit source is named by its manifest path, its column and an
   ordinal (``sheet 2``, ``member 5``, ``object 3``), never by a sheet, archive-member or R-object
   name; a path whose name is participant-code-shaped or value-shaped (an e-mail address, an IP
   address, a full date) is named by its manifest index instead (``manifest file #12``).

False-positive rules, and why each is conservative.
  * A collected value shorter than five characters is not used (initials, "Li", a birth year).
  * A name value containing a digit or an underscore is not used: a numbered pseudonym or a code is
    not a name (the participant-code gates own those), and no personal name holds an underscore.
  * A column headed only "name" is not a person's name when the table is a data dictionary or a
    survey definition (XLSForm, formr, an SPSS variable view): when a sibling header holds a
    dictionary word (label, variable, varname, showif, relevant, constraint, decimals, ...; words
    a table of people does not use as headers). Nor is any name column whose distinct values are
    mostly identifiers (at least half hold a digit or an underscore): that lists variables, not
    people. Such a column is counted as "dismissed" in the report, never silently dropped, and an
    identifier column still has its full names (two words or more) collected: variable names hold
    no space, and a mostly pseudonymised column keeps its leftover real names under watch.
  * One-word values only ever warn (above), and even a warning needs the release to write the
    word capitalised (names are proper nouns), the word not to be an ordinary English word from
    the built-in list below, and the paper not to write it in lower case. The list is built in
    (not the host's dictionary) so the verdict is the same on every machine. A multi-word value
    (a full name) is matched in any case with no word-list exemption, because a sequence of two
    name words is not ordinary prose; each of its parts is also kept as a one-word value, so a
    surname quoted alone still warns.
  * Values that are public are exempt everywhere: anything that also occurs in the paper's own
    text (paper/*.txt, else paper/*.pdf) -- the byline, the corresponding-author e-mail, cited
    authors --, in the skill's house citations (reference/house_citations.md), in the comment's own
    References section (report/REPORT_final.md), and anywhere in report/front_matter.md. An e-mail
    address the authors wrote into their deposited code or documentation (a "contact:" line, never
    a data file) is exempt when every part of its local part is a word of the paper's first page,
    i.e. it names a byline author.
  * A multi-word value counts as a full name only when two of its words have two letters or more:
    a word beside initials ("J. Roe", a surname paired with a one-letter given name) is a one-word
    value, so code such as ``pearson(x, y)`` is not the name "Pearson X".
  * Dates in release text: a date inside a URL or URI (an XMP namespace such as
    ``.../1999/02/22-rdf-syntax-ns#``, a DOI) or just after a '/' is not read as a date; nor is a
    numeric date glued to an identifier (``FIND-02/03/05``, ``v3.1.11``), one link of a longer
    chain of numbers (``6/10/12/17``), one followed by ".digit" or by a unit (``10/12/14 km/h``),
    or a dotted one just after a version cue (``Python 3.12.12``, ``bash_version 3.2.57``). PDF
    metadata is read without XMP schema URIs and without pdfinfo's structural fields (page size,
    file size, creation date, creator and producer).
  * The generic detectors skip what identifies no one: e-mail addresses at documentation domains
    (example.*, .invalid, .test, .localhost) and code that only looks like an address (a matrix
    product ``X.T@X.values``, a method call, an scp remote); IPv4 addresses in the loopback,
    unspecified, broadcast, documentation (RFC 5737), private (RFC 1918), shared (RFC 6598),
    link-local and multicast ranges; IPv6 addresses outside global unicast (2000::/3) or in a
    documentation, special-purpose or multicast range; and a version-shaped dotted quad -- every
    part after the first at most 31, as in a LibreOffice, SPSS or pandoc build number or a section
    number -- unless a network word (IP, address, host, server, ...) precedes it on its line. None
    of these exemptions applies to a value collected from the deposit's own e-mail or IP column,
    which is caught in any form.

Exit status: 0 clean (warnings may have been printed); 1 at least one location reproduces a value
(the stage refuses; the remedy is to reword the finding); 2 nothing could be certified -- a release
document could not be read, pdftotext or pdfinfo is missing, the arguments are wrong, or the check
stopped on an internal error (printed by exception class only). The remedy for 2 is to fix the
document or the environment, not the text. A self-test with synthetic, out-of-range controls runs
before the real check.
"""
from __future__ import annotations

import argparse
import bisect
import codecs
import csv
import dataclasses
import datetime as dt
import gzip
import hashlib
import html
import io
import ipaddress
import json
import os
import pathlib
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import check_release_privacy as crp  # noqa: E402  (text-suffix list and declared redactions only)
import provider_safety  # noqa: E402  (participant-code shapes are masked in printed paths)

SCHEMA = "i4r-deposit-values-1"
# A value store written by a pre-release build of this gate. Nothing writes it any more; one left
# in an audit's work/ is reported, and one inside a release is refused.
STORE_NAME = "deposit_value_hashes.json"
HOUSE_CITATIONS = HERE.parent / "reference" / "house_citations.md"
MIN_VALUE_CHARS = 5
MAX_PHRASE_TOKENS = 8
MAX_CELL_CHARS = 200
MAX_ROWS = 1_000_000                      # per table
MAX_DISTINCT_PER_COLUMN = 200_000
MAX_TEXT_TABLE_BYTES = 512 * 1024 * 1024  # delimited text read per file (streamed)
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_XLSX_BYTES = 256 * 1024 * 1024         # a workbook is read into memory whole
MAX_XLSX_UNCOMPRESSED = 1024 * 1024 * 1024
MAX_XLSX_SHARED_STRINGS = 256 * 1024 * 1024
MAX_MEMBER_BYTES = 256 * 1024 * 1024      # one member of a .zip read into the scan
MAX_ZIP_RATIO = 200                       # an inflation ratio above this is a zip bomb
MAX_RELEASE_TEXT_BYTES = 64 * 1024 * 1024
R_TIMEOUT_SECONDS = 600
MAX_R_FILES = 300                         # R processes per run
MAX_PRINTED_HITS = 40
MAX_PRINTED_WARNINGS = 20
MAX_PRINTED_NOTICES = 20
FIRST_PAGE_CHARS = 8000                   # the paper's byline is on its first page
SOURCE_REFS_PER_VALUE = 3

VALUE_CLASSES = ("name", "address", "email", "birthdate", "ip", "phone")
CLASS_LABEL = {
    "name": "a name",
    "address": "a postal address",
    "email": "an e-mail address",
    "birthdate": "a date of birth",
    "ip": "an IP address",
    "phone": "a telephone number",
}
REFUSE, WARN = "refuse", "warn"
REMEDY = (
    "reword the finding to describe the identifier by kind, file, column and count (for example "
    "\"data/survey.csv, column Name: 57 participant names\"), never its value; then rebuild the "
    "documents and restage"
)
INSPECTION_REMEDY = (
    "this is a document or environment problem, not a value in the text: rebuild the document so "
    "that it can be read, or put poppler's pdftotext and pdfinfo on PATH, then re-run"
)
INTERNAL_REMEDY = (
    "nothing was certified; re-run the same command, and report the error class if it recurs "
    "(the text needs no rewording on this account)"
)
WARNING_NOTE = (
    "a one-word match is never a refusal: confirm that each word is the ordinary word, eponym or "
    "cited author it appears to be, and reword any that quotes a participant"
)

# ------------------------------------------------------------------------------------------------
# Header classification (English, German, French, Spanish, Italian, Portuguese, Dutch)
# ------------------------------------------------------------------------------------------------
_NAME_WORDS = {"name", "names", "namen", "nom", "noms", "nombre", "nombres", "nome", "nomi",
               "nomes", "naam"}
_NAME_STRONG_FIRST = {
    "firstname", "firstnames", "forename", "forenames", "givenname", "givennames", "vorname",
    "vornamen", "rufname", "prénom", "prénoms", "prenom", "prenoms", "prenome", "prenomes",
    "voornaam", "voornamen", "roepnaam", "primernombre",
}
_NAME_STRONG_LAST = {
    "surname", "surnames", "lastname", "lastnames", "familyname", "familynames", "nachname",
    "nachnamen", "familienname", "geburtsname", "maidenname", "nomdefamille", "apellido",
    "apellidos", "cognome", "cognomi", "sobrenome", "sobrenomes", "apelido", "apelidos",
    "achternaam", "achternamen", "familienaam", "geslachtsnaam",
}
_NAME_STRONG_FULL = {
    "fullname", "fullnames", "vollername", "personname", "nomcomplet", "nomprénom", "nomprenom",
    "nometprénom", "nometprenom", "prénometnom", "prenometnom", "nombrecompleto", "nomecompleto",
    "volledigenaam",
}
_NAME_PAIR = {"first": "first", "given": "first", "fore": "first", "middle": "full",
              "last": "last", "family": "last", "sur": "last", "maiden": "last", "full": "full",
              "birth": "last", "real": "full", "complete": "full"}
# Words that may stand beside a name word in a header naming a PERSON. Anything else ("study
# name", "file name", "team name", "nom de l'étude") makes the header describe something else.
_PERSON_CONTEXT = {
    # English and German
    "participant", "participants", "subject", "subjects", "respondent", "respondents", "person",
    "persons", "student", "students", "athlete", "athletes", "player", "players", "patient",
    "patients", "child", "children", "kid", "parent", "parents", "mother", "father", "guardian",
    "coach", "coaches", "teacher", "teachers", "pupil", "pupils", "runner", "member", "members",
    "client", "clients", "proband", "probandin", "probanden", "teilnehmer", "teilnehmerin",
    "teilnehmende", "versuchsperson", "vp", "vpn", "sub", "subj", "pp", "ppt", "your", "my", "his",
    "her", "their", "full", "real", "the", "of", "der", "des", "die", "und", "and", "legal",
    "contact", "recipient", "user", "employee", "worker", "spieler", "spielerin", "schueler",
    "schüler", "sportler", "sportlerin", "trainer", "trainerin", "kind", "eltern", "mutter", "vater",
    # French
    "participante", "sujet", "sujets", "élève", "eleve", "élèves", "eleves", "athlète", "joueur",
    "joueuse", "patiente", "enfant", "mère", "mere", "père", "pere", "entraîneur", "entraineur",
    "étudiant", "etudiant", "étudiante", "etudiante", "répondant", "repondant", "votre", "ton",
    "du", "de", "la", "le", "et", "complet", "famille",
    # Spanish
    "participantes", "sujeto", "alumno", "alumna", "estudiante", "jugador", "jugadora", "atleta",
    "paciente", "niño", "nino", "niña", "nina", "padre", "madre", "entrenador", "entrenadora",
    "encuestado", "encuestada", "su", "tu", "del", "y", "completo", "completa", "familia", "primer",
    "segundo",
    # Italian
    "partecipante", "soggetto", "studente", "studentessa", "giocatore", "giocatrice", "bambino",
    "bambina", "genitore", "allenatore", "suo", "tuo", "di", "famiglia",
    # Portuguese
    "sujeito", "aluno", "aluna", "estudante", "jogador", "jogadora", "criança", "crianca", "pai",
    "mãe", "mae", "treinador", "seu", "sua", "teu", "do", "da", "dos", "das", "família",
    # Dutch
    "deelnemer", "deelnemers", "proefpersoon", "leerling", "speler", "sporter", "atleet", "patiënt",
    "ouder", "moeder", "vader", "uw", "je", "jouw", "van", "het", "en", "volledige",
}
_QUESTION_WORDS = {
    "what", "is", "are", "please", "enter", "state", "write", "wie", "ist", "lautet", "bitte",
    "angeben", "geben", "sie", "ihr", "ihren", "dein", "deinen", "your", "quel", "quelle", "est",
    "indiquez", "entrez", "veuillez", "cuál", "cual", "es", "escriba", "indique", "por", "favor",
    "qual", "è", "il", "inserisci", "indica", "é", "o", "informe", "digite", "wat", "vul", "in",
}
_EMAIL_WORDS = {"email", "emails", "mail", "mails", "emailadresse", "mailadresse", "emailaddress",
                "emailadress", "courriel", "courriels", "correo", "emailadres", "mailadres"}
# "adresse électronique", "correo electrónico", "posta elettronica", "endereço eletrônico".
_EMAIL_CARRIERS = {"adresse", "adres", "address", "correo", "correio", "posta", "indirizzo",
                   "endereço", "endereco", "dirección", "direccion", "courrier"}
_ELECTRONIC = {"électronique", "electronique", "électronic", "electronic", "electrónico",
               "electronico", "electrónica", "electronica", "eletrônico", "eletronico",
               "electrónico", "elettronica", "elettronico", "elektronisch", "elektronische"}
_BIRTH_WORDS = {"birth", "birthdate", "birthdates", "birthday", "birthdays", "dob", "geburt",
                "geburtsdatum", "geburtstag", "gebdat", "gebdatum", "geb", "birthdt", "naissance",
                "datenaissance", "ddn", "naiss", "anniversaire", "nacimiento", "fechanacimiento",
                "cumpleaños", "cumpleanos", "nascita", "datanascita", "compleanno", "nascimento",
                "datanascimento", "geboorte", "geboortedatum", "verjaardag"}
# Abbreviations that mean "birth" only beside a date word ("fecha_nac", "data_nasc", "dt_nasc").
_BIRTH_ABBREVIATIONS = {"nac", "nasc"}
_DATE_WORDS = {"date", "datum", "fecha", "data", "dt", "dat", "day", "jour", "tag", "dag", "dia",
               "día", "giorno"}
# A birth word that names a birth NAME when a name word stands beside it ("birth name",
# "nom de naissance").
_BIRTH_NAME_WORDS = {"birth", "geburt", "naissance", "nacimiento", "nascita", "nascimento",
                     "geboorte", "geb"}
_IP_WORDS = {"ip", "ipaddress", "ipadresse", "ipv4", "ipv6", "ipaddr", "ipadres"}
_ADDRESS_WORDS = {"address", "addresses", "adresse", "adressen", "anschrift", "street", "strasse",
                  "streetaddress", "wohnadresse", "postaddress", "postanschrift", "addr", "indirizzo",
                  "endereço", "endereco", "domicilio", "morada", "adres", "woonadres", "rue", "calle",
                  "straat"}
_ADDRESS_BLOCKERS = {"email", "mail", "ip", "web", "url", "mac", "e"}
_PHONE_WORDS = {"phone", "phones", "telephone", "telefon", "telefonnummer", "phonenumber",
                "handynummer", "mobilnummer", "mobiltelefon", "tel", "telnr", "cellphone",
                "mobilephone", "handy", "téléphone", "teléfono", "telefono", "telefone", "telefoon",
                "celular", "cellulare"}
_PHONE_QUALIFIED = {"mobile", "cell", "mobil"}
_NUMBER_WORDS = {"number", "nr", "no", "nummer", "phone"}


def header_tokens(header: str) -> list[str]:
    text = unicodedata.normalize("NFKC", str(header))
    text = re.sub(r"(?<=[a-zäöüßàâçéèêëîïôûùüÿñæœáíóúãõ])(?=[A-ZÄÖÜÀÂÇÉÈÊËÎÏÔÛÙÜŸÑÆŒÁÍÓÚÃÕ])", " ",
                  text)
    text = re.sub(r"(?<=[A-ZÄÖÜ])(?=[A-ZÄÖÜ][a-zäöüß])", " ", text)
    text = re.sub(r"(?<=[^\W\d_])(?=\d)|(?<=\d)(?=[^\W\d_])", " ", text)
    return [token for token in re.split(r"[\W_]+", text.casefold()) if token]


def _neutral(token: str) -> bool:
    return token.isdigit() or len(token) == 1


def _windows(tokens: list[str]) -> set[str]:
    """Every run of one to four adjacent header words, joined ("nom de famille" -> nomdefamille)."""
    return {"".join(tokens[start:end]) for start in range(len(tokens))
            for end in range(start + 1, min(len(tokens), start + 4) + 1)}


def classify_header(header: str, *, label: bool = False) -> tuple[str, str] | None:
    """(class, subtype) of a column whose header looks like personal data, else None.

    ``label`` applies the rule to an SPSS/Stata variable label, which is a phrase ("What is your
    name?"), so question words may stand beside a name word there.
    """
    tokens = header_tokens(header)
    if not tokens or len(tokens) > (10 if label else 6):
        return None
    words = set(tokens)
    windows = _windows(tokens)
    pairs = list(zip(tokens, tokens[1:]))
    if (words & _EMAIL_WORDS or ("e", "mail") in pairs or windows & {"email", "emailaddress"}
            or any(a in _EMAIL_CARRIERS and b in _ELECTRONIC for a, b in pairs)):
        return ("email", "")
    if words & _IP_WORDS:
        return ("ip", "")
    if words & _PHONE_WORDS or any(a in _PHONE_QUALIFIED and b in _NUMBER_WORDS for a, b in pairs):
        return ("phone", "")
    birth = words & _BIRTH_WORDS
    named = words & _NAME_WORDS or windows & (_NAME_STRONG_FIRST | _NAME_STRONG_LAST | _NAME_STRONG_FULL)
    if birth and named and birth <= _BIRTH_NAME_WORDS and not words & _DATE_WORDS:
        return ("name", "last")                        # a birth (maiden) name, not a date
    if (birth or ("date", "of") in pairs and "birth" in words
            or ("datum", "der") in pairs and "geburt" in words
            or any(a in _DATE_WORDS and b in _BIRTH_ABBREVIATIONS for a, b in pairs)):
        return ("birthdate", "")
    if windows & _NAME_STRONG_FIRST:
        return ("name", "first")
    if windows & _NAME_STRONG_LAST:
        return ("name", "last")
    if windows & _NAME_STRONG_FULL:
        return ("name", "full")
    for first, second in pairs:
        if first in _NAME_PAIR and second in _NAME_WORDS:
            return ("name", _NAME_PAIR[first])
    if words & _NAME_WORDS:
        allowed = _PERSON_CONTEXT | (_QUESTION_WORDS if label else set())
        others = [token for token in tokens if token not in _NAME_WORDS]
        if all(_neutral(token) or token in allowed for token in others):
            return ("name", "full")
    if words & _ADDRESS_WORDS and not words & _ADDRESS_BLOCKERS:
        return ("address", "")
    return None


def classify_column(name: str, label: str = "") -> tuple[str, str] | None:
    found = classify_header(name)
    if found is None and label:
        found = classify_header(label, label=True)
    return found


# ------------------------------------------------------------------------------------------------
# Ordinary words. A one-word name or address value that is one of these is never used; nor does
# one the paper itself writes in lower case warn. The list covers words of five letters or more
# that are also common given names, surnames or place names, plus placeholder values a name column
# holds.
# ------------------------------------------------------------------------------------------------
ORDINARY_WORDS = frozenset("""
about above abbey absent adult adults after again against agent alert alike alive alpha amber
among angel anger angle ankle apple april archer arena armor arrow aspen athlete athletes august
autumn award badge baker banks baron barker barley basin beach beaver before being below bench
berry birch bishop black blade blank blaze bless bliss block bloom blossom board bonus booth brand
brave bread bridge bright brook brooks brown brush buckle bunny burns butler butter cable camel
cameo candy canon cannon canyon cardinal carol carpenter carter carver cases castle chain chair
chance chapel chaplain charity charm chase cheer cherry chess chief child children church cider
civil class clay clear clement cliff clock cloud clover coach coaches coast codes coins colonel
column comfort common condition control cooper copper coral corner cotton could count country
cousin crane creek cross crown crystal daisy dance darling daylight deacon dealer default deleted
delta desire destiny diamond doctor dollar donor dozen drake dream dropout drummer during dusty
eagle early earnest easter ebony ember empty ending entry error evening every example excluded
faith falcon fancy farmer father feather female fence ferry field fields fifth figure final finch
first fisher fletcher flint flora flower flowers forest forester fortune fowler frank freedom
freeman friday friend frost fuller gable garden gardener garland gates ghost ginger glass glory
golden goodman grace grand grant grave graves green group groups grove guard guest hammer hardy
harmony harper harvest haven hazel heart heath heather hedge hello herald heron hills holly honey
honor house however human hunter index initial input invalid island items ivory jewel joiner jolly
judge justice karma keeper kingdom kings label lance large later laurel lawyer leader least legend
lemon level light lilly local lords lovely lower lucky major males manner manor maple march marine
marks marsh marshal master mayor meadow meadows melody member mentor mercy merit merry middle miles
miller mills minor missing misty model money monday moody morning mother music nature night noble
north notes number ocean older olive orange orchid other others otter output owner paddy palmer
parent partner pastor patience patient pearl penny people pepper person phase pilot piper place
plant player players porter potter power powers prefer price prince private prosper public queen
rainy ranger rater raven reader reading record reeves regent removed report response result
results rider ridge right river rivers robin robins rocky rosie round rowan royal rusty saint
salmon sample sandy saturday savage sawyer scale scarlet school score second session seven shade
shadow sharp shepherd shield short silver since single singer skinner slate slater small smart
smith snowy source south sparks sparrow speed spice spring squire staff stamp stark start state
steel sterling stern steward still stone stones storm story strange street strong student study
subject summer summers sunday sunny swallow sweet swift table tanner teacher temple their there
these third thorn those three tiger total tower towers trial trinity trust tucker turner under
union unity unknown upper using valentine valiant value verity victory violet virtue walker walls
warden warner waters weaver wells western whale wheeler where which while white whole wilder
willow winter winters withheld within woods woody worth would write years young youth
keine unbekannt fehlt fehlend ohne angabe teilnehmer teilnehmerin proband probandin
inconnu inconnue anonyme manquant desconocido desconocida anonimo anonima sconosciuto sconosciuta
mancante desconhecido desconhecida ausente onbekend anoniem ontbreekt participante partecipante
deelnemer
""".split())
# Placeholder and function words. A multi-word value made only of these is not a name
# ("Prefer not to say", "Recipient Last Name").
NON_NAME_WORDS = frozenset("""
a an and the of to in on at for by or not no yes none null nan na n prefer say said rather would
like unknown anonymous anon missing blank empty test testing tester dummy sample example value
values participant participants subject subjects proband person respondent recipient contact
first last full given family middle name names surname email mail address phone number date birth
of der die das und nicht keine angabe unbekannt vorname nachname
nom prénom prenom nombre apellido nome cognome sobrenome naam voornaam achternaam de du la le et
y del e di da do van inconnu anonyme desconocido anonimo sconosciuto desconhecido onbekend
""".split())

NAME_TITLES = frozenset("dr prof mr mrs ms miss mx sir frau herr mag dipl ing mme mlle sr sra srta "
                        "dott sig dhr mevr".split())

# ------------------------------------------------------------------------------------------------
# Value normalisation and text patterns
# ------------------------------------------------------------------------------------------------
TOKEN = re.compile(r"[^\W_]+")
EMAIL = re.compile(
    r"(?<![\w.+%-])[A-Za-z0-9](?:[A-Za-z0-9._%+-]{0,62}[A-Za-z0-9_%+-])?@"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,24}(?![\w-])"
)
IPV4 = re.compile(r"(?<![\w.])((?:\d{1,3}\.){3}\d{1,3})(?![\w]|\.\d)")
# Candidates only: ipaddress decides. Two colons at least, hex groups, an optional IPv4 tail and
# zone; not glued to a word, so "std::vector" and "dplyr::filter" are never candidates.
IPV6 = re.compile(r"(?<![\w:.%])((?:[0-9A-Fa-f]{0,4}:){2,7}(?:[0-9A-Fa-f]{1,4}|(?:\d{1,3}\.){3}\d{1,3})?)"
                  r"(?:%[\w.-]+)?(?![\w:])")
PHONE_VALUE = re.compile(r"\A\+?[\d\s()./-]{7,24}\Z")
PHONE_TEXT = re.compile(r"(?<![\w.+/-])\+?\(?\d[\d ()./-]{5,22}\d(?![\w]|[./-]\d)")
_MONTHS = {
    # English and German
    "january": 1, "jan": 1, "januar": 1, "jänner": 1, "february": 2, "feb": 2, "februar": 2,
    "march": 3, "mar": 3, "märz": 3, "maerz": 3, "april": 4, "apr": 4, "may": 5, "mai": 5,
    "june": 6, "jun": 6, "juni": 6, "july": 7, "jul": 7, "juli": 7, "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9, "october": 10, "oct": 10, "oktober": 10, "okt": 10,
    "november": 11, "nov": 11, "december": 12, "dec": 12, "dezember": 12, "dez": 12,
    # French
    "janvier": 1, "janv": 1, "février": 2, "fevrier": 2, "févr": 2, "fevr": 2, "mars": 3,
    "avril": 4, "avr": 4, "juin": 6, "juillet": 7, "juil": 7, "août": 8, "aout": 8,
    "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12, "déc": 12,
    # Spanish
    "enero": 1, "ene": 1, "febrero": 2, "marzo": 3, "abril": 4, "abr": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "ago": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12, "dic": 12,
    # Italian
    "gennaio": 1, "gen": 1, "febbraio": 2, "aprile": 4, "maggio": 5, "mag": 5, "giugno": 6,
    "giu": 6, "luglio": 7, "lug": 7, "settembre": 9, "set": 9, "ottobre": 10, "ott": 10,
    "dicembre": 12,
    # Portuguese
    "janeiro": 1, "fevereiro": 2, "fev": 2, "março": 3, "marco": 3, "maio": 5, "junho": 6,
    "julho": 7, "setembro": 9, "outubro": 10, "out": 10, "novembro": 11, "dezembro": 12,
    # Dutch
    "januari": 1, "februari": 2, "maart": 3, "mrt": 3, "mei": 5, "augustus": 8,
}
_MONTH_ALT = "|".join(sorted((re.escape(name) for name in _MONTHS), key=len, reverse=True))
_ORDINAL = r"(?:st|nd|rd|th|er|re|ème|eme|º|ª|°)?"
DATE_PATTERNS = (
    # 1901-04-03, 1901/4/3, 1901.04.03
    ("ymd", re.compile(r"(?<![\d.])(\d{4})([-/.])(\d{1,2})[-/.](\d{1,2})(?!\d|\.\d)")),
    # 03.04.1901, 3/4/01, 03-04-1901 (both readings of an ambiguous one)
    ("dmy", re.compile(r"(?<![\d.])(\d{1,2})([-/.])(\d{1,2})\2(\d{4}|\d{2})(?!\d|\.\d)")),
    # 03-Apr-1901, 03/APR/01, 3.Apr.1901 (SPSS DATE11, Excel dd-mmm-yyyy)
    ("d_mon_y", re.compile(r"(?<![\w])(\d{1,2})" + _ORDINAL + r"([-/.])(" + _MONTH_ALT
                           + r")\.?\2(\d{4}|\d{2})(?![\w])", re.IGNORECASE)),
    # 3 April 1901, 3rd April 1901, 3. April 1901, 1er avril 1901, 3 de abril de 1901
    ("d_month_y", re.compile(r"(?<![\w])(\d{1,2})" + _ORDINAL + r"\.?\s+(?:de\s+)?(" + _MONTH_ALT
                             + r")\.?,?\s+(?:de\s+|del\s+)?(\d{4})(?![\d])", re.IGNORECASE)),
    # April 3, 1901; April 3rd 1901
    ("month_d_y", re.compile(r"(?<![\w])(" + _MONTH_ALT + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+"
                             r"(\d{4})(?![\d])", re.IGNORECASE)),
    # 1901-Apr-03, 1901 Apr 03
    ("y_mon_d", re.compile(r"(?<![\w])(\d{4})([-/.\s])(" + _MONTH_ALT + r")\.?\2(\d{1,2})(?![\w])",
                           re.IGNORECASE)),
)
# Where a date shape is not a date in release text: URLs, URIs (an XMP namespace carries
# .../1999/02/22-rdf-syntax-ns#) and DOIs.
_URL_SPAN = re.compile(r"(?i)(?:\b[a-z][a-z0-9+.-]*://|\bwww\.|\bdoi:\s*|\burn:|\b10\.\d{4,9}/)"
                       r"[^\s<>\"'\])]+")
# A dotted numeric date right after one of these is a version number (Python 3.12.12,
# bash_version 3.2.57, __version__ = "3.1.11", pandoc 3.1.11, lme4 1.1.35), not a date.
_DATE_VERSION_CUE = re.compile(
    r"(?i)(?:version|\bver|release|build|\b(?:python|py|r|rscript|rstudio|bash|zsh|"
    r"pandoc|quarto|libreoffice|office|excel|spss|stata|sas|jasp|jamovi|matlab|octave|julia|node|"
    r"npm|pip|conda|mamba|java|gcc|clang|perl|ruby|poppler|pdftotext|pdfinfo|texlive|latex|"
    r"xelatex|lualatex|numpy|pandas|scipy|statsmodels|pingouin|sklearn|lme4|lmertest|nlme|"
    r"emmeans|glmmtmb|brms|rstan|stan|ggplot2|dplyr|tidyverse|haven|macos|darwin|linux|kernel|"
    r"windows|ubuntu|debian|fedora|chrome|firefox|safari|qualtrics|psychopy|opensesame|eprime|"
    r"inquisit|redcap|git))\d*[\W_]{0,6}\Z")


def _valid_date(year: int, month: int, day: int) -> str | None:
    if not 1850 <= year <= 2100:
        return None
    try:
        return dt.date(year, month, day).isoformat()
    except ValueError:
        return None


def _two_digit_years(value: str) -> list[int]:
    number = int(value)
    return [number] if len(value) == 4 else [1900 + number, 2000 + number]


# A unit after a numeric "date" makes it a list of measurements ("10/12/14 km/h", "6/6/15 frames").
_UNIT_AFTER = re.compile(
    r"\A\s?[(\[]?(?:km/h|m/s|kph|mph|kg|km|cm|mm|ms|min|hz|bpm|rpm|kcal|kj|kgs|lbs?|reps?|sets?|trials?|"
    r"frames?|points?|items?|participants?|subjects?|%|°|m|s|h|g|n|w)(?![^\W\d_])", re.IGNORECASE)


def _not_a_date_in_prose(text: str, kind: str, match: re.Match[str], urls: list[tuple[int, int]]
                         ) -> bool:
    start, end = match.span()
    if any(low < end and start < high for low, high in urls):
        return True                                   # inside a URL, a URI or a DOI
    if start and text[start - 1] in "/\\":
        return True                                   # a path segment
    if kind not in {"ymd", "dmy"}:
        return False
    before = text[start - 1] if start else ""
    if before.isalnum() or before == "_" or (before == "-" and start > 1
                                             and text[start - 2].isalnum()):
        return True                                   # glued to an identifier (FIND-02/03/05)
    separator = match.group(2)
    if text[end:end + 1] == separator and text[end + 1:end + 2].isdigit():
        return True                                   # one link of a longer chain (6/10/12/17)
    if _UNIT_AFTER.match(text[end:end + 12]):
        return True                                   # measurements (10/12/14 km/h)
    if separator == ".":
        if _DATE_VERSION_CUE.search(text[max(0, start - 40):start]):
            return True                               # a version number (v3.1.11 is glued, above)
        if _VERSION_SUFFIX_AFTER.match(text[end:end + 2]):
            return True
    return False


def date_matches(text: str, *, prose: bool = False) -> Iterator[tuple[int, int, set[str]]]:
    """(start, end, ISO candidates) for every written date in ``text``.

    An ambiguous numeric date (04/03/1998) yields both readings; a two-digit year both centuries.
    ``prose`` applies the release-text rules: no date inside a URL or URI or after a '/'; no
    numeric date glued to an identifier, inside a longer chain of numbers, before a unit, or
    (dotted) after a version cue.
    """
    urls = [match.span() for match in _URL_SPAN.finditer(text)] if prose else []
    for kind, pattern in DATE_PATTERNS:
        for match in pattern.finditer(text):
            if prose and _not_a_date_in_prose(text, kind, match, urls):
                continue
            found: set[str] = set()
            if kind == "ymd":
                iso = _valid_date(int(match.group(1)), int(match.group(3)), int(match.group(4)))
                if iso:
                    found.add(iso)
            elif kind == "dmy":
                first, second = int(match.group(1)), int(match.group(3))
                for year in _two_digit_years(match.group(4)):
                    for day, month in ((first, second), (second, first)):
                        iso = _valid_date(year, month, day)
                        if iso:
                            found.add(iso)
            elif kind == "d_mon_y":
                month = _MONTHS.get(match.group(3).casefold())
                if month:
                    for year in _two_digit_years(match.group(4)):
                        iso = _valid_date(year, month, int(match.group(1)))
                        if iso:
                            found.add(iso)
            elif kind == "d_month_y":
                month = _MONTHS.get(match.group(2).casefold())
                if month:
                    iso = _valid_date(int(match.group(3)), month, int(match.group(1)))
                    if iso:
                        found.add(iso)
            elif kind == "month_d_y":
                month = _MONTHS.get(match.group(1).casefold())
                if month:
                    iso = _valid_date(int(match.group(3)), month, int(match.group(2)))
                    if iso:
                        found.add(iso)
            else:  # y_mon_d
                month = _MONTHS.get(match.group(3).casefold())
                if month:
                    iso = _valid_date(int(match.group(1)), month, int(match.group(4)))
                    if iso:
                        found.add(iso)
            if found:
                yield match.start(), match.end(), found


def _excel_serial(number: float, date1904: bool) -> str | None:
    if not 1 <= number <= 80_000:
        return None
    base = dt.date(1904, 1, 1) if date1904 else dt.date(1899, 12, 30)
    try:
        value = base + dt.timedelta(days=int(number))
    except OverflowError:
        return None
    return _valid_date(value.year, value.month, value.day)


def birthdate_values(value: Any, *, spreadsheet: bool = False, date1904: bool = False) -> set[str]:
    if isinstance(value, (dt.date, dt.datetime)):
        iso = _valid_date(value.year, value.month, value.day)
        return {iso} if iso else set()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if float(value).is_integer() and 1850 <= value <= 2100:
            return set()  # a birth YEAR, not a date (and not an Excel serial)
        if spreadsheet:
            iso = _excel_serial(float(value), date1904)
            return {iso} if iso else set()
        value = str(int(value)) if float(value).is_integer() else str(value)
    text = unicodedata.normalize("NFKC", str(value)).strip()
    if spreadsheet and re.fullmatch(r"\d+(?:\.\d+)?", text):
        if float(text).is_integer() and 1850 <= float(text) <= 2100:
            return set()
        iso = _excel_serial(float(text), date1904)
        return {iso} if iso else set()
    compact = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", text)
    if compact:
        iso = _valid_date(int(compact.group(1)), int(compact.group(2)), int(compact.group(3)))
        return {iso} if iso else set()
    found: set[str] = set()
    for _start, _end, candidates in date_matches(text):
        found |= candidates
    return found


def ipv4_canonical(text: str) -> str | None:
    parts = text.split(".")
    if len(parts) != 4 or not all(part.isdigit() and len(part) <= 3 for part in parts):
        return None
    numbers = [int(part) for part in parts]
    if any(number > 255 for number in numbers):
        return None
    return ".".join(str(number) for number in numbers)


def ipv4_exempt(canonical: str) -> bool:
    """Ranges whose addresses identify no one (the generic detector only)."""
    numbers = [int(part) for part in canonical.split(".")]
    first, second = numbers[0], numbers[1]
    if first in (0, 10, 127) or canonical == "255.255.255.255":
        return True                                    # unspecified, private, loopback, broadcast
    if (first == 172 and 16 <= second <= 31) or (first == 192 and second == 168):
        return True                                    # private (RFC 1918)
    if (first == 100 and 64 <= second <= 127) or (first == 169 and second == 254):
        return True                                    # shared (RFC 6598), link-local
    if 224 <= first <= 239:
        return True                                    # multicast
    return numbers[:3] in ([192, 0, 2], [198, 51, 100], [203, 0, 113])   # documentation


_GLOBAL_UNICAST = ipaddress.IPv6Network("2000::/3")


def ipv6_canonical(text: str) -> str | None:
    """The compressed form of an IPv6 address, or the dotted form of an IPv4-mapped one."""
    try:
        address = ipaddress.IPv6Address(text.split("%", 1)[0])
    except ValueError:
        return None
    mapped = address.ipv4_mapped
    return str(mapped) if mapped is not None else address.compressed


def ipv6_exempt(canonical: str) -> bool:
    """Outside global unicast (2000::/3), or special-purpose, documentation or multicast inside it
    (the generic detector only)."""
    address = ipaddress.IPv6Address(canonical)
    return (address not in _GLOBAL_UNICAST or not address.is_global or address.is_multicast
            or address in ipaddress.IPv6Network("2001:db8::/32")
            or address in ipaddress.IPv6Network("3fff::/20"))


def ip_values(text: str) -> Iterator[tuple[int, int, str, str]]:
    """(start, end, canonical form, family) for every IPv4 and IPv6 address in ``text``; an
    IPv4-mapped IPv6 address counts as IPv4."""
    for match in IPV4.finditer(text):
        canonical = ipv4_canonical(match.group(1))
        if canonical:
            yield match.start(), match.end(), canonical, "4"
    if ":" in text:
        for match in IPV6.finditer(text):
            canonical = ipv6_canonical(match.group(1))
            if canonical:
                yield match.start(), match.end(1), canonical, "4" if "." in canonical else "6"


VERSION_PART_MAX = 31
# A network word before a dotted quad makes it an address whatever its shape.
_IP_CUE = re.compile(
    r"(?i)(?:\bip(?:v4)?\b|\bip[-_ ]?addr\w*|\baddress(?:es)?\b|\badresse[n]?\b|\bhost(?:name)?s?\b|"
    r"\bservers?\b|\bclients?\b|\bremote\b|\bgateway\b|\brouter\b|\bproxy\b|\bdns\b|\bsubnet\b)"
    r"[^\n]{0,30}\Z"
)


def version_shaped(canonical: str) -> bool:
    """Every part after the first at most 31: a build or section number, not a host address.

    LibreOffice 24.2.4.2, SPSS 29.0.1.0, pandoc 3.1.11.1 and section 2.4.1.3 all have this shape;
    a random public address has it about once in five hundred.
    """
    return all(int(part) <= VERSION_PART_MAX for part in canonical.split(".")[1:])


# A version number whatever its parts: a version word just before it ("Linux 5.15.153.1",
# "Version 7.1.130.4"), or a release suffix glued after it ("5.15.153.1-microsoft", "+deb12").
_VERSION_WORD_BEFORE = re.compile(
    r"(?i)\b(?:version|ver|release|build|kernel|linux|darwin|windows|macos|firmware|patch|"
    r"update)\b[\s:=(\"'v]{0,4}\Z")
_VERSION_SUFFIX_AFTER = re.compile(r"\A[-+~][A-Za-z]")


def version_word_before(text: str, start: int) -> bool:
    return bool(_VERSION_WORD_BEFORE.search(text[max(0, start - 30):start]))


def version_suffix_after(text: str, end: int) -> bool:
    return bool(_VERSION_SUFFIX_AFTER.match(text[end:end + 2]))


# Code that looks like an address to a regular expression: matrix products (X.T@X.values,
# M@model.params), method calls (x@W.to(device)), attribute chains (x@self.W.data, R's obj@meta),
# decorators and scp remotes (git@github.com:org/pkg.git). The generic detector only; a value from the
# deposit's own e-mail column is caught whatever follows it. (The rules are those the release
# privacy gate used for addresses before 2.6.8.)
_CODE_ATTRIBUTES = frozenset("""
t values params shape index columns loc iloc iat coef coefficients resid residuals fittedvalues
dtype size ndim flat real imag sum mean std var transpose inv pinv solve weight weights bias grad
mat matrix array cpu cuda numpy item tolist copy reshape flatten squeeze unsqueeze frame table
ident assay
""".split())
_AMBIGUOUS_TLDS = frozenset({"at", "to", "data", "dot", "mt"})


def code_shaped_address(text: str, start: int, end: int) -> bool:
    address = text[start:end].casefold()
    local, domain = address.rsplit("@", 1)
    after = text[end:end + 2]
    if (len(after) == 2 and after[0] == ":" and not after[1].isspace()) \
            or text[:start].endswith("://"):
        return True                                   # an scp remote, a URL's user part
    subscript = after[:1] == "[" and not (after[1:2].isdigit() or after[1:2] == "^")
    attribute = (after[:1] == "." and after[1:2].isalnum()) or \
        (after[:1] == "$" and (after[1:2].isalpha() or after[1:2] in "._"))
    labels = domain.split(".")
    if after[:1] == "(" or subscript or attribute or labels[-1] in _CODE_ATTRIBUTES:
        return True
    return labels[-1] in _AMBIGUOUS_TLDS and (
        (labels[-1] in ("data", "dot") and any(part in ("self", "meta", "weight")
                                              for part in labels[:-1]))
        or (labels[-1] in ("data", "dot", "mt")
            and all(len(segment) <= 2 or segment == "self" for segment in local.split("."))
            and any(len(part) == 1 for part in labels[:-1])))


def not_an_email_line(label: str, text: str, start: int, end: int) -> bool:
    """The release privacy gate's per-line escape (``# i4r-privacy: not-an-email``) on the line of this
    match, in a code file (check 4's own rule and its own marker, read from check_release_privacy so the two
    gates cannot drift). It exempts the generic detector only: a value from the deposit's own e-mail column
    is caught on a marked line too, as check 4 would not."""
    if not crp._code_file(label):
        return False
    breaks = [found.end() for found in crp._LINE_BREAK.finditer(text, 0, start)]
    line_start = breaks[-1] if breaks else 0
    following = crp._LINE_BREAK.search(text, end)
    return crp._NOT_AN_EMAIL in text[line_start:following.start() if following else len(text)]


def email_exempt_domain(address: str) -> bool:
    domain = address.rsplit("@", 1)[-1].casefold()
    labels = domain.split(".")
    return ("example" in labels or labels[-1] in {"invalid", "test", "localhost", "example"}
            or domain == "localhost")


def phrase_tokens(text: str) -> list[str]:
    return TOKEN.findall(unicodedata.normalize("NFKC", text).casefold())


def last_first(text: str) -> str | None:
    """"Last, First" as "First Last" (the order prose writes it in), else None."""
    if text.count(",") != 1:
        return None
    last, first = (part.strip() for part in text.split(","))
    return f"{first} {last}" if last and first else None


def identifier_like(value: Any) -> bool:
    """A digit or an underscore: a variable name, a numbered pseudonym or a code, never a name."""
    return bool(re.search(r"[\d_]", str(value)))


# Characters that split a word without showing: soft hyphens and zero-width marks.
_INVISIBLE = re.compile("[\u00ad\u200b\u200c\u200d\u2060\ufeff]")
_HYPHEN_BREAK = re.compile(r"(?<=[^\W\d_])-[ \t]*\r?\n[ \t]*(?=[^\W\d_])")


def prepare(text: str) -> str:
    """NFKC, without invisible word-splitting marks. Line and page breaks are kept, so locations
    are counted on the prepared text."""
    return _INVISIBLE.sub("", unicodedata.normalize("NFKC", text))


def dehyphenate(text: str) -> tuple[str, Callable[[int], int]] | None:
    """``text`` with hyphenated line ends joined ("Brummel-\\nstedtova" -> "Brummelstedtova"),
    and a map from its offsets back to ``text``'s; None when there is none."""
    pieces: list[str] = []
    starts: list[int] = []
    shifts: list[int] = []
    last = removed = 0
    for match in _HYPHEN_BREAK.finditer(text):
        pieces.append(text[last:match.start()])
        starts.append(match.start() - removed)
        removed += match.end() - match.start()
        shifts.append(removed)
        last = match.end()
    if not starts:
        return None
    pieces.append(text[last:])

    def original(offset: int) -> int:
        index = bisect.bisect_right(starts, offset) - 1
        return offset + (shifts[index] if index >= 0 else 0)

    return "".join(pieces), original


def _decode_fallback(error: UnicodeError) -> tuple[str, int]:
    """Decode bytes that are not UTF-8 as Windows-1252 (Latin-1 where cp1252 has no character),
    so a Latin-1 name after a UTF-8-looking head is read, not replaced by U+FFFD."""
    if not isinstance(error, UnicodeDecodeError):
        raise error
    chunk = error.object[error.start:error.end]
    decoded = []
    for byte in chunk:
        try:
            decoded.append(bytes([byte]).decode("cp1252"))
        except UnicodeDecodeError:
            decoded.append(chr(byte))
    return "".join(decoded), error.end


FALLBACK_ERRORS = "i4r-cp1252-fallback"
codecs.register_error(FALLBACK_ERRORS, _decode_fallback)


def decode_text(data: bytes) -> str:
    """UTF-8 (with or without BOM) or UTF-16 by BOM; bytes that are not UTF-8 as Windows-1252."""
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass
    return data.decode("utf-8-sig", errors=FALLBACK_ERRORS)


# ------------------------------------------------------------------------------------------------
# The keyed hashes (in memory only)
# ------------------------------------------------------------------------------------------------
KINDS = ("phrase", "single", "email", "date", "ip", "phone")


class Hasher:
    """Keyed BLAKE2b under a key that exists only in this process (never stored, never printed)."""

    def __init__(self, key: bytes | None = None):
        self._key = key or secrets.token_bytes(32)

    def __call__(self, kind: str, value: str) -> str:
        return hashlib.blake2b(f"{kind}\x00{value}".encode("utf-8"), key=self._key,
                               digest_size=16).hexdigest()

    def __repr__(self) -> str:                  # never shows the key
        return "Hasher(<one-run key>)"


@dataclass
class Collector:
    hasher: Hasher
    sources: list[dict[str, str]] = field(default_factory=list)
    entries: dict[str, dict[str, list[int]]] = field(
        default_factory=lambda: {kind: {} for kind in KINDS})
    first_tokens: set[str] = field(default_factory=set)
    phrase_lengths: set[int] = field(default_factory=set)
    source_values: dict[int, int] = field(default_factory=dict)
    kept_sources: set[int] = field(default_factory=set)
    _source_index: dict[tuple[str, str, str], int] = field(default_factory=dict)

    def source(self, file_label: str, column: str, cls: str) -> int:
        key = (file_label, column, cls)
        if key not in self._source_index:
            self._source_index[key] = len(self.sources)
            self.sources.append({"file": file_label, "column": column, "class": cls})
        return self._source_index[key]

    def _put(self, kind: str, value: str, source: int) -> bool:
        digest = self.hasher("phrase" if kind == "single" else kind, value)
        refs = self.entries[kind].setdefault(digest, [])
        if source not in refs and len(refs) < SOURCE_REFS_PER_VALUE:
            refs.append(source)
        self.source_values[source] = self.source_values.get(source, 0) + 1
        return True

    def add_phrase(self, tokens: list[str], source: int, *, header_echo: set[str]) -> bool:
        joined = " ".join(tokens)
        if (not tokens or len(tokens) > MAX_PHRASE_TOKENS or len(joined) < MIN_VALUE_CHARS
                or joined in header_echo or all(token in NON_NAME_WORDS for token in tokens)):
            return False
        words = [token for token in tokens if len(token) >= 2]
        if len(words) == 1:
            # One word beside initials ("J. Roe", "Roe X") is a one-word value: an initial and a
            # surname are no stronger than the surname ("pearson(x" is code, not a name).
            tokens = words
        if len(tokens) == 1:
            token = tokens[0]
            if (not token.isalpha() or token in ORDINARY_WORDS or len(token) < MIN_VALUE_CHARS
                    or token in header_echo or token in NON_NAME_WORDS):
                return False
            return self._put("single", token, source)
        if not words:
            return False
        self.first_tokens.add(self.hasher("first", tokens[0]))
        self.phrase_lengths.add(len(tokens))
        return self._put("phrase", joined, source)

    def add(self, cls: str, value: Any, source: int, *, header_echo: set[str] = frozenset(),
            spreadsheet: bool = False, date1904: bool = False) -> bool:
        if value is None or isinstance(value, bool):
            return False
        if cls == "birthdate":
            added = False
            for iso in birthdate_values(value, spreadsheet=spreadsheet, date1904=date1904):
                added = self._put("date", iso, source) or added
            return added
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        text = unicodedata.normalize("NFKC", str(value)).strip()
        if not text or len(text) > MAX_CELL_CHARS or text.startswith(("{", "[")):
            return False
        if cls == "email":
            added = False
            for match in EMAIL.finditer(text):
                added = self._put("email", match.group(0).casefold(), source) or added
            return added
        if cls == "ip":
            added = False
            for _start, _end, canonical, _family in ip_values(text):
                added = self._put("ip", canonical, source) or added
            return added
        if cls == "phone":
            if not PHONE_VALUE.fullmatch(text):
                return False
            digits = re.sub(r"\D", "", text)
            if 7 <= len(digits) <= 15:
                return self._put("phone", digits, source)
            return False
        if "@" in text:
            return False
        if cls == "name" and identifier_like(text):
            return False
        tokens = phrase_tokens(text)
        added = self.add_phrase(tokens, source, header_echo=header_echo)
        if cls == "name" and len(tokens) > 1:
            # A leak can quote part of a name: each part is kept as a one-word value too (it only
            # ever warns), and a leading title is also dropped ("Dr Jane Roe" -> "jane roe").
            for token in tokens:
                if token not in NON_NAME_WORDS and token not in NAME_TITLES:
                    added = self.add_phrase([token], source, header_echo=header_echo) or added
            if tokens[0] in NAME_TITLES and len(tokens) > 2:
                added = self.add_phrase(tokens[1:], source, header_echo=header_echo) or added
            # A "First Last" cell may be quoted surname first ("Last First", or "Last, First",
            # which tokenizes the same): keep that order too, so it refuses rather than warns.
            core = tokens[1:] if tokens[0] in NAME_TITLES and len(tokens) > 2 else tokens
            if 2 <= len(core) <= 4:
                added = self.add_phrase([core[-1], *core[:-1]], source, header_echo=header_echo) or added
        swapped = last_first(text) if cls == "name" else None
        if swapped:
            added = self.add_phrase(phrase_tokens(swapped), source, header_echo=header_echo) or added
        return added

    def add_full_name(self, first: Any, last: Any, source: int) -> bool:
        if first is None or last is None:
            return False
        first_text, last_text = str(first).strip(), str(last).strip()
        if (not first_text or not last_text or "@" in first_text + last_text
                or identifier_like(first_text) or identifier_like(last_text)
                or len(first_text) + len(last_text) > MAX_CELL_CHARS):
            return False
        a, b = phrase_tokens(first_text), phrase_tokens(last_text)
        added = self.add_phrase(a + b, source, header_echo=set())
        return self.add_phrase(b + a, source, header_echo=set()) or added

    def counts(self) -> dict[str, int]:
        return {kind: len(values) for kind, values in self.entries.items()}


# ------------------------------------------------------------------------------------------------
# Reading the deposit
# ------------------------------------------------------------------------------------------------
PY_TABLE_SUFFIXES = {".csv", ".tsv", ".tab", ".dat", ".txt"}
PY_SHEET_SUFFIXES = {".xlsx", ".xlsm"}
PY_JSON_SUFFIXES = {".json", ".jsonl", ".ndjson"}
R_HAVEN_KINDS = {".sav": "sav", ".zsav": "zsav", ".por": "por", ".dta": "dta",
                 ".sas7bdat": "sas7bdat", ".xpt": "xpt"}
R_BASE_KINDS = {".rds": "rds", ".rdata": "rdata", ".rda": "rda"}
UNSUPPORTED_DATA_SUFFIXES = (provider_safety.OPAQUE_SUFFIXES | {".xls", ".ods", ".omv", ".jasp",
                                                                 ".sas7bcat", ".sd2", ".mdb",
                                                                 ".accdb", ".db"})
NON_DATA_SUFFIXES = (provider_safety.CODE_SUFFIXES | provider_safety.DOCUMENT_SUFFIXES
                     | provider_safety.MEDIA_SUFFIXES | {".xml", ".yaml", ".yml", ".toml", ".cfg",
                                                         ".ini", ".css", ".bib", ".sps", ".spv"})


class NotScanned(Exception):
    """A deposit file that could not be read. Reported, never a refusal."""


@dataclass
class FileReport:
    file: str
    status: str                    # scanned | not_scanned | skipped
    reader: str = ""
    reason: str = ""
    tables: int = 0
    pii_columns: dict[str, int] = field(default_factory=dict)
    dismissed: dict[str, int] = field(default_factory=dict)
    truncated: list[str] = field(default_factory=list)

    def count(self, cls: str, *, dismissed: bool = False) -> None:
        target = self.dismissed if dismissed else self.pii_columns
        target[cls] = target.get(cls, 0) + 1

    def cut(self, what: str) -> None:
        if what not in self.truncated:
            self.truncated.append(what)


# A printed COLUMN label never carries something that could be a value: a headerless table's
# first row is read as its header, so a "column name" can be a participant's e-mail address, birth
# date or telephone number. (Sheet, archive-member and R-object names are never printed at all:
# they are ordinals.)
_UNSAFE_LABEL = re.compile(r"@|\d{4}|(?:\d{1,3}\.){3}\d{1,3}|\d{1,2}[./-]\d{1,2}[./-]\d{2}")


def safe_label(text: str, fallback: str = "<withheld name>") -> str:
    text = str(text)
    if _UNSAFE_LABEL.search(text) or provider_safety.first_participant_code(text):
        return fallback
    text = re.sub(r"[\x00-\x1f\x7f]", "?", text)
    return text if len(text) <= 60 else text[:57] + "..."


# A deposit FILE name is printed as its manifest path unless a segment is shaped like a participant
# code or like a value: an e-mail address, an IP address, a full date.
_VALUE_SHAPED_NAME = re.compile(
    r"@|(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)|(?<!\d)\d{1,2}[./_-]\d{1,2}[./_-](?:\d{4}|\d{2})(?!\d)"
    r"|(?<!\d)\d{4}[./_-]\d{1,2}[./_-]\d{1,2}(?!\d)|(?<![\d])(?:19|20)\d{2}(?:0[1-9]|1[0-2])"
    r"(?:0[1-9]|[12]\d|3[01])(?!\d)")


def unsafe_name(text: str) -> bool:
    return bool(_VALUE_SHAPED_NAME.search(text)) or provider_safety.first_participant_code(text) \
        is not None


def deposit_label(relative: str, number: int, listed: bool) -> str:
    """The printed name of a deposit file: its manifest path, or its manifest index when a
    segment of the path is code- or value-shaped."""
    if any(unsafe_name(part) for part in relative.split("/")):
        return f"manifest file #{number}" if listed else f"deposit file #{number}"
    return re.sub(r"[\x00-\x1f\x7f]", "?", relative)


def _header_echo(headers: Iterable[str]) -> set[str]:
    return {" ".join(header_tokens(header)) for header in headers}


# A sibling header holding one of these words makes the table a data dictionary or a survey
# definition (XLSForm, formr, an SPSS variable view), whose bare "name" column lists variables.
# Kept to words a table of PEOPLE does not use as a header.
_DICTIONARY_WORDS = frozenset({
    "label", "labels", "varlabel", "variable", "variables", "varname", "showif", "relevant",
    "constraint", "decimals", "listname", "codebook", "dictionary",
})
IDENTIFIER_SHARE = 0.5


def dictionary_context(header: str, siblings: Iterable[str]) -> bool:
    """A header that is only a name word ("name", "nom", "naam"), in a table that describes
    variables."""
    if not set(header_tokens(header)) <= _NAME_WORDS:
        return False
    for sibling in siblings:
        if sibling == header:
            continue
        tokens = header_tokens(sibling)
        if set(tokens) & _DICTIONARY_WORDS or "".join(tokens) in _DICTIONARY_WORDS:
            return True
    return False


def identifier_column(values: Iterable[Any]) -> bool:
    """At least half of the distinct values hold a digit or an underscore: variables, not people."""
    distinct = {str(value).strip() for value in values if value not in (None, "")}
    distinct.discard("")
    if not distinct:
        return False
    return sum(1 for value in distinct if identifier_like(value)) >= IDENTIFIER_SHARE * len(distinct)


def full_name_shaped(value: Any) -> bool:
    """Two or more words and no digit or underscore: kept even from an identifier column.

    Variable names never hold a space, so a mostly pseudonymised name column (codes, and a few
    real names left behind) keeps its real full names under watch.
    """
    return not identifier_like(value) and len(phrase_tokens(str(value))) >= 2


def process_table(collector: Collector, file_label: str, headers: list[str],
                  rows: Iterable[list[Any]], report: FileReport, *, labels: list[str] | None = None,
                  table_label: str = "", spreadsheet: bool = False, date1904: bool = False,
                  context_headers: list[str] | None = None) -> None:
    """Collect the values of a table's personal-data columns.

    Name columns are buffered (distinct values, capped) and judged as a whole before any value is
    kept: a column that turns out to list identifiers is dismissed, not collected.
    ``table_label`` is an ordinal ("sheet 2", "object 3"), never a sheet or object name.
    ``context_headers`` are the table's full header row when ``headers`` is a selection (R).
    """
    report.tables += 1
    labels = labels or [""] * len(headers)
    siblings = [str(header or "") for header in (context_headers or headers)]
    where = f"{file_label} {table_label}" if table_label else file_label
    selected: list[tuple[int, str, str, int]] = []
    for index, header in enumerate(headers):
        found = classify_column(str(header or ""), str(labels[index] or ""))
        if not found:
            continue
        if found[0] == "name" and dictionary_context(str(header or ""), siblings):
            report.count("name", dismissed=True)
            continue
        column = safe_label(str(header), f"column {index + 1}")
        selected.append((index, found[0], found[1], collector.source(where, column, found[0])))
    if not selected:
        return
    echo = _header_echo([str(header) for header in headers] + [str(label) for label in labels])
    firsts = [item for item in selected if item[1] == "name" and item[2] == "first"]
    lasts = [item for item in selected if item[1] == "name" and item[2] == "last"]
    # A bare name column beside a first-name column is the surname (Name + Vorname, Nom + Prénom),
    # and beside a surname column the given name (Nombre + Apellido, Nome + Cognome).
    bare = [item for item in selected if item[1] == "name" and item[2] == "full"
            and set(header_tokens(str(headers[item[0]]))) <= _NAME_WORDS]
    if len(bare) == 1 and len(firsts) + len(lasts) == 1:
        firsts, lasts = (firsts, bare) if firsts else (bare, lasts)
    pair = (firsts[0], lasts[0]) if len(firsts) == 1 and len(lasts) == 1 else None
    distinct: dict[int, set[Any]] = {item[0]: set() for item in selected}
    pending: dict[int, list[Any]] = {item[0]: [] for item in selected if item[1] == "name"}
    pairs: list[tuple[Any, Any]] = []
    seen_pairs: set[tuple[Any, Any]] = set()
    prefix = f"{table_label}: " if table_label else ""
    for number, row in enumerate(rows):
        if number >= MAX_ROWS:
            report.cut(f"{prefix}first {MAX_ROWS} rows read")
            break
        for index, cls, _subtype, source in selected:
            value = row[index] if index < len(row) else None
            if value is None or value == "":
                continue
            seen = distinct[index]
            if value in seen:
                continue
            if len(seen) >= MAX_DISTINCT_PER_COLUMN:
                if index in pending:
                    # a name column keeps its first distinct values only
                    report.cut(f"{prefix}column {index + 1}: first {MAX_DISTINCT_PER_COLUMN} "
                               "distinct values used")
                    continue
            else:
                seen.add(value)
            if index in pending:
                pending[index].append(value)
            else:
                collector.add(cls, value, source, header_echo=echo, spreadsheet=spreadsheet,
                              date1904=date1904)
        if pair:
            first_index, last_index = pair[0][0], pair[1][0]
            if first_index < len(row) and last_index < len(row):
                key = (row[first_index], row[last_index])
                if key not in seen_pairs:
                    if len(seen_pairs) < MAX_DISTINCT_PER_COLUMN:
                        seen_pairs.add(key)
                        pairs.append(key)
                    else:
                        report.cut(f"{prefix}first {MAX_DISTINCT_PER_COLUMN} first+last name pairs "
                                   "used")
    kept: set[int] = set()
    for index, cls, _subtype, source in selected:
        if index in pending and identifier_column(pending[index]):
            report.count(cls, dismissed=True)
            for value in pending[index]:
                if full_name_shaped(value):
                    collector.add(cls, value, source, header_echo=echo)
            continue
        report.count(cls)
        kept.add(index)
        collector.kept_sources.add(source)
        for value in pending.get(index, []):
            collector.add(cls, value, source, header_echo=echo)
    if pair and pair[0][0] in kept and pair[1][0] in kept and pairs:
        first_header = safe_label(str(headers[pair[0][0]]), f"column {pair[0][0] + 1}")
        last_header = safe_label(str(headers[pair[1][0]]), f"column {pair[1][0] + 1}")
        pair_source = collector.source(where, f"{first_header} + {last_header}", "name")
        for first, last in pairs:
            collector.add_full_name(first, last, pair_source)


def _encoding(head: bytes) -> str:
    if head.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if head.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"
    # Everything else is read as UTF-8, with any byte that is not UTF-8 read as Windows-1252
    # (FALLBACK_ERRORS): a Latin-1 name deep in a file whose head is ASCII is read, not mangled.
    return "utf-8"


def _errors(encoding: str) -> str:
    return "replace" if encoding == "utf-16" else FALLBACK_ERRORS


class _Bounded(io.RawIOBase):
    """Stop reading a stream after ``limit`` bytes (the per-file cap), and say whether it did."""

    def __init__(self, raw: Any, limit: int):
        self.raw, self.remaining = raw, limit
        self.cut = False

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        if self.remaining <= 0:
            if not self.cut and self.raw.read(1):
                self.cut = True
            return 0
        chunk = self.raw.read(min(len(buffer), self.remaining))
        if not chunk:
            return 0
        buffer[: len(chunk)] = chunk
        self.remaining -= len(chunk)
        return len(chunk)


def read_delimited(opener: Callable[[], io.BufferedIOBase], suffix: str, collector: Collector,
                   label: str, report: FileReport) -> None:
    with opener() as handle:
        head = handle.read(64 * 1024)
    if not head.strip():
        raise NotScanned("empty file")
    if b"\x00" in head and not head.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        raise NotScanned("binary content")
    encoding = _encoding(head)
    sample = head.decode(encoding, errors=_errors(encoding)).splitlines()[:50]
    first = sample[0] if sample else ""
    if suffix in {".tsv", ".tab"}:
        delimiter = "\t"
    else:
        counts = {candidate: first.count(candidate) for candidate in (",", ";", "\t", "|")}
        delimiter = max(counts, key=lambda key: counts[key])
        if counts[delimiter] == 0:
            if suffix in {".txt", ".dat"}:
                report.status, report.reason = "skipped", "text, not a table"
                return
            delimiter = ","
    if suffix in {".txt", ".dat"}:
        parsed = list(csv.reader(sample, delimiter=delimiter))
        width = len(parsed[0]) if parsed else 0
        consistent = sum(1 for row in parsed[1:] if len(row) == width)
        if width < 2 or len(parsed) < 2 or consistent < 0.8 * (len(parsed) - 1):
            report.status, report.reason = "skipped", "text, not a table"
            return
    csv.field_size_limit(16 * 1024 * 1024)
    bounds: list[_Bounded] = []

    def rows() -> Iterator[list[str]]:
        with opener() as raw:
            bounded = _Bounded(raw, MAX_TEXT_TABLE_BYTES)
            bounds.append(bounded)
            text = io.TextIOWrapper(io.BufferedReader(bounded), encoding=encoding,
                                    errors=_errors(encoding), newline="")
            reader = csv.reader(text, delimiter=delimiter)
            try:
                yield from reader
            except csv.Error as exc:
                raise NotScanned(f"malformed table ({exc.__class__.__name__})") from None

    iterator = rows()
    try:
        headers = next(iterator)
    except StopIteration:
        raise NotScanned("no header row") from None
    headers = [cell.strip().strip('"') for cell in headers]
    process_table(collector, label, headers, iterator, report)
    if any(bounded.cut for bounded in bounds):
        report.cut(f"first {MAX_TEXT_TABLE_BYTES // (1024 * 1024)} MB read")
    report.status, report.reader = "scanned", "python-delimited"


_XLSX_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_XLSX_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def _column_index(reference: str) -> int | None:
    match = re.match(r"([A-Z]+)", reference or "")
    if not match:
        return None
    index = 0
    for letter in match.group(1):
        index = index * 26 + (ord(letter) - 64)
    return index - 1


def read_xlsx(opener: Callable[[], Any], collector: Collector, label: str,
              report: FileReport) -> None:
    try:
        with opener() as handle:
            data = handle.read(MAX_XLSX_BYTES + 1)
        if len(data) > MAX_XLSX_BYTES:
            raise NotScanned("workbook exceeds the scan limit")
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (OSError, zipfile.BadZipFile, ValueError) as exc:
        raise NotScanned(f"not a readable workbook ({exc.__class__.__name__})") from None
    with archive:
        infos = archive.infolist()
        if sum(info.file_size for info in infos) > MAX_XLSX_UNCOMPRESSED:
            raise NotScanned("workbook inflates beyond the scan limit")
        names = {info.filename for info in infos}
        if "xl/workbook.xml" not in names:
            raise NotScanned("workbook part is missing")
        try:
            workbook = ET.fromstring(archive.read("xl/workbook.xml"))
            date1904 = any(element.get("date1904") in {"1", "true"}
                           for element in workbook.iter(f"{_XLSX_MAIN}workbookPr"))
            targets: dict[str, str] = {}
            if "xl/_rels/workbook.xml.rels" in names:
                rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
                for rel in rels:
                    target = rel.get("Target") or ""
                    target = target.lstrip("/") if target.startswith("/") else f"xl/{target}"
                    targets[rel.get("Id") or ""] = target
            # Sheets are named by their position only: a sheet name can be a participant's name.
            sheets = [(f"sheet {number}", targets.get(sheet.get(f"{_XLSX_REL}id") or ""))
                      for number, sheet in enumerate(workbook.iter(f"{_XLSX_MAIN}sheet"), 1)]
            shared: list[str] = []
            if "xl/sharedStrings.xml" in names:
                if archive.getinfo("xl/sharedStrings.xml").file_size > MAX_XLSX_SHARED_STRINGS:
                    raise NotScanned("shared-string table exceeds the scan limit")
                with archive.open("xl/sharedStrings.xml") as stream:
                    for _event, element in ET.iterparse(stream, events=("end",)):
                        if element.tag == f"{_XLSX_MAIN}si":
                            # Text runs only; phonetic guides (rPh) repeat the reading.
                            parts = []
                            for child in element:
                                if child.tag == f"{_XLSX_MAIN}t":
                                    parts.append(child.text or "")
                                elif child.tag == f"{_XLSX_MAIN}r":
                                    parts.extend(node.text or ""
                                                 for node in child.iter(f"{_XLSX_MAIN}t"))
                            shared.append("".join(parts))
                            element.clear()
        except ET.ParseError as exc:
            raise NotScanned(f"workbook XML is malformed ({exc.__class__.__name__})") from None
        for sheet_label, target in sheets:
            if not target or target not in names:
                continue

            def rows(target: str = target) -> Iterator[list[Any]]:
                with archive.open(target) as stream:
                    for _event, element in ET.iterparse(stream, events=("end",)):
                        if element.tag != f"{_XLSX_MAIN}row":
                            continue
                        cells: dict[int, Any] = {}
                        position = -1
                        for cell in element.iter(f"{_XLSX_MAIN}c"):
                            index = _column_index(cell.get("r") or "")
                            position = index if index is not None else position + 1
                            kind = cell.get("t")
                            value_node = cell.find(f"{_XLSX_MAIN}v")
                            raw = value_node.text if value_node is not None else None
                            if kind == "s" and raw is not None:
                                try:
                                    value: Any = shared[int(raw)]
                                except (ValueError, IndexError):
                                    value = None
                            elif kind == "inlineStr":
                                value = "".join(node.text or ""
                                                for node in cell.iter(f"{_XLSX_MAIN}t"))
                            elif kind in {"str", "d", "e"}:
                                value = raw
                            elif kind == "b":
                                value = None
                            else:
                                try:
                                    value = float(raw) if raw is not None else None
                                except ValueError:
                                    value = raw
                            if value is not None and value != "":
                                cells[position] = value
                        element.clear()
                        if not cells:
                            continue
                        width = max(cells) + 1
                        yield [cells.get(index) for index in range(width)]

            iterator = rows()
            try:
                header_row = next(iterator)
            except StopIteration:
                continue
            except ET.ParseError:
                raise NotScanned("worksheet XML is malformed") from None
            headers = [("" if cell is None else str(cell)).strip() for cell in header_row]
            try:
                process_table(collector, label, headers, iterator, report, table_label=sheet_label,
                              spreadsheet=True, date1904=date1904)
            except ET.ParseError:
                raise NotScanned("worksheet XML is malformed") from None
    report.status, report.reader = "scanned", "python-xlsx"


def read_json(opener: Callable[[], Any], suffix: str, collector: Collector, label: str,
              report: FileReport) -> None:
    with opener() as handle:
        data = handle.read(MAX_JSON_BYTES + 1)
    if len(data) > MAX_JSON_BYTES:
        raise NotScanned("JSON file exceeds the scan limit")
    text = decode_text(data)
    documents: list[Any] = []
    try:
        if suffix in {".jsonl", ".ndjson"}:
            documents = [json.loads(line) for line in text.splitlines() if line.strip()]
        else:
            documents = [json.loads(text)]
    except ValueError:
        raise NotScanned("not valid JSON") from None
    columns: dict[tuple[str, str], int] = {}
    classes: dict[int, str] = {}
    echoes: dict[int, set[str]] = {}
    dismissed: set[tuple[str, str]] = set()
    pending: dict[int, list[Any]] = {}
    pairs: list[tuple[Any, Any]] = []

    def visit(node: Any, depth: int = 0) -> None:
        if depth > 64:
            return
        if isinstance(node, dict):
            first = last = bare = None
            keys = [str(key) for key in node]
            for key, value in node.items():
                if isinstance(value, (dict, list)):
                    visit(value, depth + 1)
                    continue
                cls = classify_header(str(key))
                if not cls:
                    continue
                name = safe_label(str(key), "a key")
                source_key = (name, cls[0])
                if cls[0] == "name" and dictionary_context(str(key), keys):
                    if source_key not in dismissed:
                        dismissed.add(source_key)
                        report.count("name", dismissed=True)
                    continue
                if source_key not in columns:
                    columns[source_key] = collector.source(label, name, cls[0])
                    classes[columns[source_key]] = cls[0]
                    echoes[columns[source_key]] = _header_echo([str(key)])
                source = columns[source_key]
                if cls[0] == "name":
                    bucket = pending.setdefault(source, [])
                    if len(bucket) < MAX_DISTINCT_PER_COLUMN:
                        bucket.append(value)
                    else:
                        report.cut(f"key {name}: first {MAX_DISTINCT_PER_COLUMN} values used")
                else:
                    collector.add(cls[0], value, source, header_echo=_header_echo([str(key)]))
                if cls == ("name", "first"):
                    first = value
                elif cls == ("name", "last"):
                    last = value
                elif cls == ("name", "full") and set(header_tokens(str(key))) <= _NAME_WORDS:
                    bare = value
            if bare is not None and (first is None) != (last is None):
                first, last = (first, bare) if first is not None else (bare, last)
            if first is not None and last is not None:
                if len(pairs) < MAX_DISTINCT_PER_COLUMN:
                    pairs.append((first, last))
                else:
                    report.cut(f"first {MAX_DISTINCT_PER_COLUMN} first+last name pairs used")
        elif isinstance(node, list):
            if len(node) > MAX_ROWS:
                report.cut(f"first {MAX_ROWS} items of a list read")
            for item in node[:MAX_ROWS]:
                visit(item, depth + 1)

    for document in documents:
        visit(document)
    identifier_sources = {source for source, values in pending.items() if identifier_column(values)}
    for source, cls in classes.items():
        report.count(cls, dismissed=source in identifier_sources)
        if source not in identifier_sources:
            collector.kept_sources.add(source)
    for source, values in pending.items():
        for value in values:
            if source not in identifier_sources or full_name_shaped(value):
                collector.add("name", value, source, header_echo=echoes.get(source, set()))
    if pairs and not identifier_sources:
        pair_source = collector.source(label, "first + last name", "name")
        for first, last in pairs:
            collector.add_full_name(first, last, pair_source)
    report.tables += 1
    report.status, report.reader = "scanned", "python-json"


# R reads a file and talks to this module over two pipes: for each data frame it sends every
# column's name and label, then waits for the column numbers this module selected (classified by
# the same Python rules as every other reader, so no R-side pattern has to mirror them), and sends
# only those columns' rows. Frame and object names are never sent: they are numbered here.
R_READER = r"""
args <- commandArgs(trailingOnly = TRUE)
path <- args[[1]]; kind <- args[[2]]; max_rows <- as.numeric(args[[3]])
out <- stdout()
inp <- file("stdin", open = "r")
has_haven <- requireNamespace("haven", quietly = TRUE)
clean <- function(x) {
  if (inherits(x, "Date")) {
    x <- format(x, "%Y-%m-%d")
  } else if (inherits(x, "POSIXt")) {
    x <- format(x, "%Y-%m-%d")
  } else {
    if (has_haven && inherits(x, "haven_labelled")) x <- haven::zap_labels(x)
    if (is.factor(x)) x <- as.character(x)
    if (is.list(x)) x <- vapply(x, function(e) paste(as.character(unlist(e)), collapse = " "), "")
    x <- as.character(x)
  }
  x[is.na(x)] <- ""
  x <- enc2utf8(x)
  gsub("[\t\r\n]+", " ", x, perl = TRUE)
}
label_of <- function(col) {
  l <- attr(col, "label", exact = TRUE)
  if (is.null(l)) "" else paste(as.character(l), collapse = " ")
}
ask <- function(frame) {
  nm <- names(frame)
  if (is.null(nm)) nm <- character(0)
  lab <- if (length(nm)) vapply(frame, label_of, "") else character(0)
  cat("F\t", length(nm), "\n", sep = "", file = out)
  for (j in seq_along(nm)) cat("H\t", clean(nm[j]), "\t", clean(lab[j]), "\n", sep = "", file = out)
  cat("S\n", file = out)
  flush(out)
  answer <- readLines(inp, n = 1, warn = FALSE)
  if (!length(answer) || !nzchar(answer)) return(integer(0))
  picked <- suppressWarnings(as.integer(strsplit(answer, " ", fixed = TRUE)[[1]]))
  picked[!is.na(picked) & picked >= 1 & picked <= length(nm)]
}
emit_rows <- function(frame) {
  frame <- as.data.frame(frame, stringsAsFactors = FALSE, optional = TRUE)
  n <- nrow(frame)
  if (ncol(frame) && n > 0) {
    if (n > max_rows) {
      cat("T\t", n, "\n", sep = "", file = out)
      frame <- frame[seq_len(max_rows), , drop = FALSE]
    }
    cols <- lapply(frame, clean)
    lines <- do.call(paste, c(unname(cols), sep = "\t"))
    writeLines(paste0("R\t", lines), out, useBytes = TRUE)
  }
  cat("E\n", file = out)
  flush(out)
}
one_frame <- function(frame) {
  frame <- as.data.frame(frame, stringsAsFactors = FALSE, optional = TRUE)
  picked <- ask(frame)
  emit_rows(frame[, picked, drop = FALSE])
}
frames_of <- function(x) {
  if (is.data.frame(x)) return(list(x))
  if (is.list(x)) return(unname(Filter(is.data.frame, x)))
  list()
}
status <- tryCatch({
  readers <- c(sav = "read_sav", zsav = "read_sav", por = "read_por", dta = "read_dta",
               sas7bdat = "read_sas", xpt = "read_xpt")
  if (kind %in% names(readers)) {
    if (!has_haven) quit(save = "no", status = 4)
    reader <- getExportedValue("haven", readers[[kind]])
    meta <- reader(path, n_max = 0)
    picked <- ask(meta)
    keep <- names(meta)[picked]
    frame <- if (length(keep)) reader(path, col_select = tidyselect::all_of(keep)) else meta[, integer(0), drop = FALSE]
    emit_rows(frame)
  } else if (kind == "rds") {
    frames <- frames_of(readRDS(path))
    if (!length(frames)) quit(save = "no", status = 5)
    for (f in frames) one_frame(f)
  } else {
    e <- new.env(parent = emptyenv())
    load(path, envir = e)
    frames <- list()
    for (n in ls(e, all.names = TRUE)) frames <- c(frames, frames_of(get(n, envir = e)))
    if (!length(frames)) quit(save = "no", status = 5)
    for (f in frames) one_frame(f)
  }
  0
}, error = function(e) 3)
quit(save = "no", status = status)
"""

_R_STATE: dict[str, Any] = {}


def r_capability() -> dict[str, Any]:
    """Whether Rscript (and haven) can read SPSS, Stata, SAS and R files here. Probed once."""
    if "value" in _R_STATE:
        return _R_STATE["value"]
    value = {"rscript": None, "haven": False}
    if os.environ.get("I4R_DEPOSIT_VALUES_NO_R") != "1":
        rscript = shutil.which("Rscript")
        if rscript:
            try:
                probe = subprocess.run(
                    [rscript, "--vanilla", "-e",
                     'cat(requireNamespace("haven", quietly = TRUE) && '
                     'requireNamespace("tidyselect", quietly = TRUE))'],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60, check=False)
                value = {"rscript": rscript, "haven": probe.stdout.strip() == b"TRUE"}
            except (OSError, subprocess.TimeoutExpired):
                pass
    _R_STATE["value"] = value
    return value


def read_with_r(path: pathlib.Path, kind: str, collector: Collector, label: str,
                report: FileReport) -> None:
    capability = r_capability()
    if not capability["rscript"]:
        raise NotScanned("Rscript is not available")
    if kind in R_HAVEN_KINDS.values() and not capability["haven"]:
        raise NotScanned("the R package haven is not available")
    # One R process per file: a deposit of hundreds of per-participant SPSS files is capped, and
    # the rest are listed as not scanned rather than holding staging for an hour.
    launched = _R_STATE.get("launched", 0)
    if launched >= MAX_R_FILES:
        raise NotScanned(f"more than {MAX_R_FILES} files need R; the rest are not read")
    _R_STATE["launched"] = launched + 1
    environment = dict(os.environ, LC_ALL="en_US.UTF-8", LANG="en_US.UTF-8")
    try:
        # Values travel through the pipe only: nothing is written to disk and R's own messages
        # (which could quote a cell) are discarded, never printed.
        process = subprocess.Popen(
            [capability["rscript"], "--vanilla", "-e", R_READER, str(path), kind, str(MAX_ROWS)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=environment)
    except OSError as exc:
        raise NotScanned(f"R reader could not start ({exc.__class__.__name__})") from None
    state = {"timed_out": False}

    def expire() -> None:
        state["timed_out"] = True
        process.kill()

    timer = threading.Timer(R_TIMEOUT_SECONDS, expire)
    timer.daemon = True
    timer.start()
    base = kind in R_BASE_KINDS.values()
    frames = 0
    headers: list[str] = []
    texts: list[str] = []
    selection: list[int] = []
    rows: list[list[str]] = []
    total_rows = ""
    broken = finished = False
    try:
        assert process.stdout is not None and process.stdin is not None
        for raw in process.stdout:
            line = raw.decode("utf-8", errors=FALLBACK_ERRORS).rstrip("\n")
            tag, _, rest = line.partition("\t")
            if tag == "F":
                frames += 1
                headers, texts, selection, rows, total_rows = [], [], [], [], ""
            elif tag == "H":
                name, _, text = rest.partition("\t")
                headers.append(name)
                texts.append(text)
            elif tag == "S":
                selection = [index for index, (name, text) in enumerate(zip(headers, texts))
                             if classify_column(name, text)]
                try:
                    process.stdin.write((" ".join(str(index + 1) for index in selection)
                                         + "\n").encode("ascii"))
                    process.stdin.flush()
                except OSError:
                    broken = True
                    break
            elif tag == "T":
                total_rows = rest
            elif tag == "R":
                rows.append(rest.split("\t"))
            elif tag == "E":
                table = f"object {frames}" if base else ""
                process_table(collector, label, [headers[index] for index in selection], rows,
                              report, labels=[texts[index] for index in selection],
                              table_label=table, context_headers=headers)
                if total_rows:
                    report.cut(f"{table + ': ' if table else ''}first {MAX_ROWS} rows read")
                rows = []
        finished = not broken
    finally:
        timer.cancel()
        if not finished and process.poll() is None:
            process.kill()          # a broken pipe, or an error on this side: never leave R running
        for stream in (process.stdin, process.stdout):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        try:
            returncode = process.wait(timeout=R_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            returncode = process.wait()
    if state["timed_out"]:
        raise NotScanned(f"R reader exceeded {R_TIMEOUT_SECONDS} s")
    if returncode == 5:
        report.status, report.reader, report.reason = "scanned", "R", "holds no data frame"
        return
    if returncode != 0 or broken:
        raise NotScanned(f"R reader failed (exit {returncode})")
    report.status = "scanned"
    report.reader = "R-haven" if kind in R_HAVEN_KINDS.values() else "R"


def _suffix(name: str) -> str:
    return pathlib.PurePosixPath(name).suffix.lower()


def _merge(report: FileReport, member: FileReport, member_label: str) -> None:
    report.tables += member.tables
    for cls, count in member.pii_columns.items():
        report.pii_columns[cls] = report.pii_columns.get(cls, 0) + count
    for cls, count in member.dismissed.items():
        report.dismissed[cls] = report.dismissed.get(cls, 0) + count
    for what in member.truncated:
        report.cut(f"{member_label}: {what}")


def read_zip(path: pathlib.Path, collector: Collector, label: str, report: FileReport) -> None:
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise NotScanned(f"not a readable zip archive ({exc.__class__.__name__})") from None
    members = scanned = unreadable = 0
    with archive, tempfile.TemporaryDirectory(prefix="i4r-deposit-member-") as scratch:
        for info in archive.infolist():
            if info.is_dir():
                continue
            suffix = _suffix(info.filename)
            if suffix not in PY_TABLE_SUFFIXES | PY_SHEET_SUFFIXES | PY_JSON_SUFFIXES \
                    | set(R_HAVEN_KINDS) | set(R_BASE_KINDS):
                continue
            members += 1
            # Members are numbered, never named: a member name can be a participant's name.
            member_name = f"member {members}"
            member_label = f"{label} {member_name}"
            member_report = FileReport(member_label, "not_scanned")
            if info.file_size > MAX_MEMBER_BYTES:
                unreadable += 1
                report.cut(f"{member_name}: larger than the {MAX_MEMBER_BYTES // (1024 * 1024)} MB "
                           "member limit, not read")
                continue
            if (info.compress_size and info.file_size / max(info.compress_size, 1) > MAX_ZIP_RATIO
                    and info.file_size > 16 * 1024 * 1024):
                unreadable += 1
                report.cut(f"{member_name}: inflates more than {MAX_ZIP_RATIO}-fold, not read")
                continue
            try:
                if suffix in set(R_HAVEN_KINDS) | set(R_BASE_KINDS):
                    target = pathlib.Path(scratch) / f"member{members}{suffix}"
                    with archive.open(info) as source, target.open("wb") as sink:
                        shutil.copyfileobj(source, sink)
                    kind = R_HAVEN_KINDS.get(suffix) or R_BASE_KINDS[suffix]
                    try:
                        read_with_r(target, kind, collector, member_label, member_report)
                    finally:
                        target.unlink()
                else:
                    opener = lambda info=info: archive.open(info)  # noqa: E731
                    read_member(opener, suffix, collector, member_label, member_report)
            except NotScanned as exc:
                unreadable += 1
                report.cut(f"{member_name}: not read ({exc})")
                continue
            except (OSError, zipfile.BadZipFile, RuntimeError, EOFError, ValueError, UnicodeError,
                    ET.ParseError) as exc:
                unreadable += 1
                report.cut(f"{member_name}: not read ({exc.__class__.__name__})")
                continue
            if member_report.status == "scanned":
                scanned += 1
                _merge(report, member_report, member_name)
    if members == 0:
        report.status, report.reason = "skipped", "archive holds no data file"
        return
    report.status, report.reader = "scanned", "zip"
    report.reason = f"{scanned} of {members} data members read"
    if unreadable:
        report.reason += f"; {unreadable} not scanned"


def read_member(opener: Callable[[], Any], suffix: str, collector: Collector, label: str,
                report: FileReport) -> None:
    if suffix in PY_TABLE_SUFFIXES:
        read_delimited(opener, suffix, collector, label, report)
    elif suffix in PY_SHEET_SUFFIXES:
        read_xlsx(opener, collector, label, report)
    elif suffix in PY_JSON_SUFFIXES:
        read_json(opener, suffix, collector, label, report)
    else:
        raise NotScanned("unsupported format")


def read_deposit_file(path: pathlib.Path, relative: str, collector: Collector,
                      label: str | None = None) -> FileReport:
    label = label if label is not None else deposit_label(relative, 0, False)
    report = FileReport(label, "not_scanned")
    suffix = _suffix(relative)
    try:
        if suffix in {".gz", ".gzip"}:
            inner = _suffix(pathlib.PurePosixPath(relative).stem)
            if inner in PY_TABLE_SUFFIXES | PY_SHEET_SUFFIXES | PY_JSON_SUFFIXES:
                read_member(lambda: gzip.open(path, "rb"), inner, collector, label, report)
            elif inner in NON_DATA_SUFFIXES:
                report.status, report.reason = "skipped", "not a data file"
            else:
                raise NotScanned("unsupported compressed format")
        elif suffix in PY_TABLE_SUFFIXES | PY_SHEET_SUFFIXES | PY_JSON_SUFFIXES:
            read_member(lambda: path.open("rb"), suffix, collector, label, report)
        elif suffix in R_HAVEN_KINDS:
            read_with_r(path, R_HAVEN_KINDS[suffix], collector, label, report)
        elif suffix in R_BASE_KINDS:
            read_with_r(path, R_BASE_KINDS[suffix], collector, label, report)
        elif suffix == ".zip":
            read_zip(path, collector, label, report)
        elif suffix in UNSUPPORTED_DATA_SUFFIXES:
            raise NotScanned("unsupported data format")
        else:
            report.status = "skipped"
            report.reason = "not a data file" if suffix in NON_DATA_SUFFIXES else "not a table format"
    except NotScanned as exc:
        report.status, report.reason = "not_scanned", str(exc)
    except (OSError, EOFError, gzip.BadGzipFile, zipfile.BadZipFile, UnicodeError, ValueError,
            RuntimeError, ET.ParseError) as exc:
        report.status, report.reason = "not_scanned", f"unreadable ({exc.__class__.__name__})"
    except MemoryError:
        report.status, report.reason = "not_scanned", "too large to read"
    except Exception as exc:  # noqa: BLE001 -- a deposit file may never stop the check
        # Only the exception's class is kept: its message could quote a cell.
        report.status, report.reason = "not_scanned", f"reader error ({exc.__class__.__name__})"
    return report


def deposit_root(audit: pathlib.Path) -> tuple[pathlib.Path | None, str]:
    """The frozen deposit copy staging can rely on: pkg/, else the newest Phase-1 snapshot copy."""
    pkg = audit / "pkg"
    if pkg.is_dir() and not pkg.is_symlink():
        return pkg, "pkg"
    snapshots = [path for path in (audit / "phase1/snapshots").glob("*/raw/pkg")
                 if path.is_dir() and not path.is_symlink()]
    if snapshots:
        return max(snapshots, key=lambda path: path.stat().st_mtime), "phase1 snapshot"
    return None, "none"


def deposit_files(root: pathlib.Path) -> list[tuple[str, pathlib.Path]]:
    rows = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        rows.append((path.relative_to(root).as_posix(), path))
    return rows


# Where authors write their own contact address: code headers, READMEs, documentation. Never a data
# file. A .txt counts only when it did not parse as a table.
CONTACT_SUFFIXES = provider_safety.CODE_SUFFIXES | {".md", ".markdown", ".rst", ".html", ".htm",
                                                    ".tex", ".txt", ".rtf"}
MAX_CONTACT_BYTES = 4 * 1024 * 1024


def contact_emails(files: list[tuple[str, pathlib.Path]], reports: list[FileReport],
                   hasher: Hasher) -> set[str]:
    """Keyed hashes of the e-mail addresses in the deposit's code and documentation files."""
    found: set[str] = set()
    for (relative, path), report in zip(files, reports):
        suffix = _suffix(relative)
        if suffix not in CONTACT_SUFFIXES or (suffix == ".txt" and report.status != "skipped"):
            continue
        try:
            with path.open("rb") as handle:
                text = decode_text(handle.read(MAX_CONTACT_BYTES))
        except OSError:
            continue
        for match in EMAIL.finditer(unicodedata.normalize("NFKC", text)):
            found.add(hasher("contact", match.group(0).casefold()))
    return found


def _manifest_order(audit: pathlib.Path) -> dict[str, int]:
    """1-based position of each path in package_manifest.json (an index the provider already saw)."""
    manifest = audit / "package_manifest.json"
    order: dict[str, int] = {}
    if manifest.is_file():
        try:
            rows = json.loads(manifest.read_text(encoding="utf-8")).get("files", [])
            for number, row in enumerate(rows, 1):
                if isinstance(row, dict) and isinstance(row.get("path"), str):
                    order.setdefault(row["path"], number)
        except (OSError, ValueError, AttributeError):
            return {}
    return order


def build_store(audit: pathlib.Path, *, key: bytes | None = None) -> dict[str, Any]:
    """Read the deposit and return the keyed hashes of its personal-data values, in memory only.

    The key is drawn at random here (``key`` is for tests) and lives only in the returned Hasher.
    Nothing in the result is ever written to disk.
    """
    root, origin = deposit_root(audit)
    files = deposit_files(root) if root is not None else []
    collector = Collector(Hasher(key))
    _R_STATE["launched"] = 0
    order = _manifest_order(audit)
    manifest_size: dict[str, Any] = {}
    manifest = audit / "package_manifest.json"
    if order:
        try:
            for row in json.loads(manifest.read_text(encoding="utf-8")).get("files", []):
                if isinstance(row, dict) and isinstance(row.get("path"), str):
                    manifest_size[row["path"]] = row.get("size")
        except (OSError, ValueError, AttributeError):
            manifest_size = {}
    reports = []
    for number, (relative, path) in enumerate(files, 1):
        listed = relative in order
        label = deposit_label(relative, order.get(relative, number), listed)
        reports.append(read_deposit_file(path, relative, collector, label))
    contacts = contact_emails(files, reports, collector.hasher)
    unlisted = sum(1 for relative, _ in files if order and relative not in order)
    resized = sum(1 for relative, path in files
                  if relative in manifest_size and manifest_size[relative] not in
                  (None, path.stat().st_size))
    pii_columns: dict[str, int] = {}
    dismissed: dict[str, int] = {}
    for report in reports:
        for cls, count in report.pii_columns.items():
            pii_columns[cls] = pii_columns.get(cls, 0) + count
        for cls, count in report.dismissed.items():
            dismissed[cls] = dismissed.get(cls, 0) + count
    empty = [{"file": source["file"], "column": source["column"], "class": source["class"]}
             for number, source in enumerate(collector.sources)
             if number in collector.kept_sources and not collector.source_values.get(number)]
    stale = audit / "work" / STORE_NAME
    return {
        "schema_version": SCHEMA,
        "hasher": collector.hasher,
        "deposit": {
            "source": origin,
            "files": len(files),
            "scanned": sum(1 for report in reports if report.status == "scanned"),
            "not_scanned": [{"file": report.file, "reason": report.reason}
                            for report in reports if report.status == "not_scanned"],
            "truncated": [{"file": report.file, "reason": "; ".join(report.truncated)}
                          for report in reports if report.truncated],
            "skipped": sum(1 for report in reports if report.status == "skipped"),
            "read_by_r": sum(1 for report in reports if report.reader.startswith("R")),
            "tables": sum(report.tables for report in reports),
            "pii_columns": dict(sorted(pii_columns.items())),
            # Personal-data headers judged not to hold people's values: a dictionary's variable
            # names, or a column of identifiers (see the module docstring).
            "pii_columns_dismissed": dict(sorted(dismissed.items())),
            # Personal-data columns from which no value could be kept (all blank, all too short,
            # or a format the gate does not read): listed so that a silent zero is never mistaken
            # for coverage.
            "pii_columns_without_values": empty,
            "contact_emails_in_code_or_docs": len(contacts),
            "files_not_in_package_manifest": unlisted,
            "files_whose_size_differs_from_package_manifest": resized,
            "r_capability": {key: bool(value) for key, value in r_capability().items()},
            "stale_value_store": stale.is_file(),
        },
        "values": collector.counts(),
        "sources": collector.sources,
        "entries": collector.entries,
        "first_tokens": collector.first_tokens,
        "phrase_lengths": sorted(collector.phrase_lengths),
        "contact_emails": contacts,
    }


# ------------------------------------------------------------------------------------------------
# Scanning release-bound text
# ------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Hit:
    location: str
    kind: str
    source: str = ""
    severity: str = REFUSE
    # Where the match lies in its segment's text, and which release file and segment it is in: what the
    # release-copy masking (``mask_records``) needs. Never printed, never written.
    start: int = field(default=-1, compare=False)
    end: int = field(default=-1, compare=False)
    file: str = field(default="", compare=False)
    segment: Any = field(default=None, compare=False, repr=False)


class Locator:
    """Offset -> "line N", or "page P line N" for text with form-feed page breaks."""

    def __init__(self, text: str, *, pages: bool = False, prefix: str = "line"):
        self.pages = pages
        self.prefix = prefix
        self.line_starts = [0] + [match.end() for match in re.finditer(r"\n", text)]
        self.page_starts = [0] + [match.end() for match in re.finditer(r"\f", text)] if pages else [0]

    def __call__(self, offset: int) -> str:
        line = bisect.bisect_right(self.line_starts, offset)
        if not self.pages:
            return f"{self.prefix} {line}"
        page = bisect.bisect_right(self.page_starts, offset)
        first_line = bisect.bisect_right(self.line_starts, self.page_starts[page - 1])
        return f"page {page} line {line - first_line + 1}"


@dataclass
class Segment:
    """One piece of release text. ``values`` False: only the e-mail and IP detectors read it.

    ``mask`` says how a match can be masked in the file this text came from (``mask_records``):
    "text" (the file's whole text, line for line), "json" (one string literal, at ``token`` in the file's
    decoded text), or "" (markup, a document: not maskable, so a match there still refuses)."""
    label: str
    text: str
    locate: Callable[[int], str]
    values: bool = True
    mask: str = ""
    token: tuple[int, int] | None = None


class Matcher:
    def __init__(self, store: dict[str, Any]):
        self.hash: Hasher = store["hasher"]
        self.entries = {kind: store["entries"].get(kind, {}) for kind in KINDS}
        self.first_tokens = set(store.get("first_tokens", ()))
        self.lengths = sorted(length for length in store.get("phrase_lengths", []) if length >= 2)
        self.sources = store.get("sources", [])
        self.allowed: set[str] = set()
        self.allowed_emails: set[str] = set()
        self.paper_lower: set[str] = set()
        self.contacts = set(store.get("contact_emails", ()))
        self.byline: set[str] = set()
        self.has_values = any(self.entries[kind] for kind in KINDS)

    # -- allowlists -------------------------------------------------------------------------------
    def allow_text(self, text: str, *, paper: bool = False) -> None:
        """Everything in ``text`` is public (the article, the house citations, the comment's own
        front matter and reference list)."""
        text = prepare(text)
        joined = dehyphenate(text)
        for variant in (text, joined[0]) if joined else (text,):
            self._allow(variant, paper=paper)

    def _allow(self, text: str, *, paper: bool) -> None:
        for match in EMAIL.finditer(text):
            address = match.group(0).casefold()
            self.allowed_emails.add(address)
            self.allowed.add(self.hash("email", address))
        for _start, _end, canonical, _family in ip_values(text):
            self.allowed.add(self.hash("ip", canonical))
        for _start, _end, candidates in date_matches(text):
            self.allowed.update(self.hash("date", iso) for iso in candidates)
        for match in PHONE_TEXT.finditer(text):
            self.allowed.add(self.hash("phone", re.sub(r"\D", "", match.group(0))))
        tokens = TOKEN.findall(text)
        folded = [token.casefold() for token in tokens]
        if paper:
            self.paper_lower.update(token for token, original in zip(folded, tokens)
                                    if original.islower())
        if not self.has_values:
            return
        pairs = 2 in self.lengths
        for index, token in enumerate(folded):
            self.allowed.add(self.hash("phrase", token))
            for length in self.lengths:
                if index + length <= len(folded):
                    self.allowed.add(self.hash("phrase", " ".join(folded[index:index + length])))
            if pairs and index + 1 < len(folded):
                # A reference list writes "Last, First": the name in its other order is public too.
                self.allowed.add(self.hash("phrase", f"{folded[index + 1]} {token}"))

    def allow_byline(self, first_page: str) -> None:
        """The words of the paper's first page, where its authors are named."""
        self.byline.update(token.casefold() for token in TOKEN.findall(prepare(first_page)))

    def author_contact(self, address: str) -> bool:
        """An address the authors wrote into their deposited code or documentation, naming a
        byline author: every letter run of its local part is a word of the paper's first page
        (initials aside, and an initial may lead a surname: "a.author", "aauthor"), and at least
        one such word has three letters or more."""
        if self.hash("contact", address) not in self.contacts:
            return False
        parts = [part for part in re.split(r"[^a-z]+", address.split("@", 1)[0]) if part]

        def named(part: str) -> bool:
            return part in self.byline or (len(part) >= 4 and part[1:] in self.byline)

        words = [part for part in parts if len(part) >= 2]
        return (bool(words) and all(named(part) for part in words)
                and any(len(part) >= 3 for part in words))

    # -- scanning ---------------------------------------------------------------------------------
    def _source(self, refs: list[int]) -> str:
        if not refs or refs[0] >= len(self.sources):
            return ""
        source = self.sources[refs[0]]
        return f"matches deposit column {source['column']!r} in {source['file']}"

    def _class(self, refs: list[int]) -> str:
        source = self.sources[refs[0]] if refs and refs[0] < len(self.sources) else {}
        return CLASS_LABEL.get(source.get("class", "name"), CLASS_LABEL["name"])

    def scan(self, segment: Segment) -> list[Hit]:
        text = segment.text
        hits: list[Hit] = []

        def hit(offset: int, kind: str, refs: list[int] | None = None,
                severity: str = REFUSE, end: int = -1) -> None:
            where = segment.locate(offset)
            location = f"{segment.label} {where}" if where else segment.label
            hits.append(Hit(location, kind, self._source(refs or []), severity, offset,
                            end if end > offset else offset + 1, segment=segment))

        # E-mail and IP addresses: every segment, provenance included.
        if "@" in text:
            for match in EMAIL.finditer(text):
                address = match.group(0).casefold()
                digest = self.hash("email", address)
                if digest in self.allowed or address in self.allowed_emails:
                    continue
                if digest in self.entries["email"]:
                    hit(match.start(), CLASS_LABEL["email"] + " from the deposit",
                        self.entries["email"][digest], end=match.end())
                elif (not email_exempt_domain(address) and not self.author_contact(address)
                      and not code_shaped_address(text, match.start(), match.end())
                      and not not_an_email_line(segment.label, text, match.start(), match.end())):
                    hit(match.start(), "an e-mail address", end=match.end())
        for offset, end, canonical, family in ip_values(text):
            digest = self.hash("ip", canonical)
            if digest in self.allowed:
                continue
            if digest in self.entries["ip"]:
                hit(offset, CLASS_LABEL["ip"] + " from the deposit", self.entries["ip"][digest],
                    end=end)
            elif family == "6":
                if not ipv6_exempt(canonical):
                    hit(offset, "an IPv6 address", end=end)
            elif ipv4_exempt(canonical):
                continue
            elif version_word_before(text, offset):
                continue          # "Linux 5.15.153.1", "Version 7.1.130.4": the closest word decides
            elif ((version_shaped(canonical) or version_suffix_after(text, end))
                  and not _IP_CUE.search(text[max(0, offset - 60):offset])):
                continue
            else:
                hit(offset, "an IPv4 address", end=end)
        if not self.has_values or not segment.values:
            return hits
        if self.entries["date"]:
            for start, date_end, candidates in date_matches(text, prose=True):
                for iso in sorted(candidates):
                    digest = self.hash("date", iso)
                    if digest in self.entries["date"] and digest not in self.allowed:
                        hit(start, CLASS_LABEL["birthdate"] + " from the deposit",
                            self.entries["date"][digest], end=date_end)
                        break
        if self.entries["phone"]:
            for match in PHONE_TEXT.finditer(text):
                digits = re.sub(r"\D", "", match.group(0))
                digest = self.hash("phone", digits)
                if 7 <= len(digits) <= 15 and digest in self.entries["phone"] \
                        and digest not in self.allowed:
                    hit(match.start(), CLASS_LABEL["phone"] + " from the deposit",
                        self.entries["phone"][digest], end=match.end())
        if self.entries["single"] or self.entries["phrase"]:
            self._names(text, lambda offset: offset, hit)
            joined = dehyphenate(text)
            if joined:
                self._names(joined[0], joined[1], hit)
        return hits

    def _names(self, text: str, original: Callable[[int], int],
               hit: Callable[..., None]) -> None:
        """Multi-word deposit values refuse; one-word values warn."""
        singles, phrases = self.entries["single"], self.entries["phrase"]
        tokens = list(TOKEN.finditer(text))
        folded = [token.group(0).casefold() for token in tokens]
        covered: set[int] = set()
        if phrases:
            for index, word in enumerate(folded):
                if self.hash("first", word) not in self.first_tokens:
                    continue
                for length in self.lengths:
                    if index + length > len(tokens):
                        break
                    digest = self.hash("phrase", " ".join(folded[index:index + length]))
                    if digest in phrases and digest not in self.allowed:
                        refs = phrases[digest]
                        hit(original(tokens[index].start()),
                            self._class(refs) + " from the deposit", refs,
                            end=original(tokens[index + length - 1].end() - 1) + 1)
                        covered.update(range(index, index + length))
                        break
        if not singles:
            return
        for index, token in enumerate(tokens):
            if index in covered:
                continue
            word, written = folded[index], token.group(0)
            if (len(word) < MIN_VALUE_CHARS or not written[:1].isupper() or not word.isalpha()
                    or word in ORDINARY_WORDS or word in self.paper_lower):
                continue
            digest = self.hash("phrase", word)
            if digest in singles and digest not in self.allowed:
                refs = singles[digest]
                hit(original(token.start()), self._class(refs) + " (one word) from the deposit",
                    refs, WARN, end=original(token.end() - 1) + 1)


class InspectionFailure(Exception):
    """A release-bound document could not be read, so it cannot be certified."""


def _run(command: list[str], label: str) -> str:
    executable = shutil.which(command[0])
    if executable is None:
        raise InspectionFailure(f"cannot inspect {label}: {command[0]} is not on PATH")
    try:
        result = subprocess.run([executable, *command[1:]], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InspectionFailure(f"cannot inspect {label}: {exc.__class__.__name__}") from None
    if result.returncode != 0:
        raise InspectionFailure(f"cannot inspect {label}: {command[0]} exited {result.returncode}")
    return result.stdout.decode("utf-8", errors="replace")


_DOCX_PARTS = re.compile(r"^(word/(document|header\d*|footer\d*|footnotes|endnotes|comments)\.xml"
                         r"|docProps/(core|app|custom)\.xml)$", re.I)


def docx_text(path: pathlib.Path, label: str) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            chunks = []
            for info in archive.infolist():
                if not _DOCX_PARTS.match(info.filename):
                    continue
                if info.file_size > MAX_RELEASE_TEXT_BYTES:
                    raise InspectionFailure(f"cannot inspect {label}: {info.filename} is too large")
                xml = archive.read(info).decode("utf-8", errors="replace")
                xml = re.sub(r"</w:p>|<w:br\b[^>]*/?>|<w:cr\b[^>]*/?>|</w:tr>", "\n", xml)
                xml = re.sub(r"<w:tab\b[^>]*/?>|</w:tc>", "\t", xml)
                xml = re.sub(r"</(?:dc|cp|dcterms):[^>]*>", "\n", xml)
                chunks.append(html.unescape(re.sub(r"<[^>]*>", "", xml)))
            return "\n".join(chunks)
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise InspectionFailure(f"cannot inspect {label}: {exc.__class__.__name__}") from None


# HTML: inline markup is removed outright, so a word split by it is whole again (Qor<b>velline</b>);
# every other tag separates words. Line breaks inside a tag are kept, so line numbers stay true.
_INLINE_TAGS = ("a|abbr|b|bdi|bdo|big|cite|code|data|del|dfn|em|font|i|ins|kbd|mark|nobr|q|s|samp|"
                "small|span|strike|strong|sub|sup|time|tt|u|var|wbr")
_INLINE_TAG = re.compile(r"</?(?:" + _INLINE_TAGS + r")\b[^<>]*>", re.IGNORECASE)
_ANY_TAG = re.compile(r"<[^<>]*>")


def _keep_breaks(match: re.Match[str], empty: str) -> str:
    return "\n" * match.group(0).count("\n") or empty


def strip_markup(text: str, *, inline: bool = True) -> str:
    if inline:
        text = _INLINE_TAG.sub(lambda match: _keep_breaks(match, ""), text)
    text = _ANY_TAG.sub(lambda match: _keep_breaks(match, " "), text)
    return html.unescape(text)


# pdfinfo fields that describe the file, not its content: their text is not matched against
# deposit values (the e-mail and IP detectors still read them).
_PDFINFO_STRUCTURAL = re.compile(
    r"^(?:Pages|Encrypted|Page size|Page rot|File size|Optimized|PDF version|Tagged|UserProperties|"
    r"Suspects|Form|JavaScript|CreationDate|ModDate|Metadata Stream|Custom Metadata|Producer|"
    r"Creator):", re.MULTILINE)


# XMP namespace declarations and resource URIs name schemas, never people; one carries a date
# (xmlns:rdf=".../1999/02/22-rdf-syntax-ns#"). Every other attribute value is kept: XMP may write a
# property such as dc:title in attribute form.
_XMP_SCHEMA_ATTRIBUTE = re.compile(
    r"""\s(?:xmlns(?::[\w.-]+)?|rdf:about|rdf:resource)\s*=\s*(?:"[^"]*"|'[^']*')""")
_ATTRIBUTE_VALUE = re.compile(r"""=\s*(?:"([^"]*)"|'([^']*)')""")


def tag_attribute_text(text: str) -> str:
    """``text`` with every tag replaced by its attribute values, line for line."""
    def tag(match: re.Match[str]) -> str:
        values = [double or single for double, single in _ATTRIBUTE_VALUE.findall(match.group(0))]
        return " " + " ".join(values) + " " + "\n" * match.group(0).count("\n")

    return html.unescape(_ANY_TAG.sub(tag, text))


def xmp_text(meta: str) -> str:
    """XMP element text and attribute values, without markup or schema URIs, line for line."""
    return tag_attribute_text(
        _XMP_SCHEMA_ATTRIBUTE.sub(lambda match: _keep_breaks(match, " "), meta))


def pdf_metadata_for_values(info: str, meta: str) -> str:
    """The Info text fields and the XMP text, line for line."""
    lines = [("" if _PDFINFO_STRUCTURAL.match(line) else line) for line in info.split("\n")]
    return "\n".join(lines) + "\n" + xmp_text(meta)


# ---- The skill's own generated and provenance files ----------------------------------------------
# Read by the e-mail and IP detectors, never matched against deposit values: their text is written
# by the skill and its tools (build and interpreter versions, hashes, telemetry, fixed boilerplate),
# cannot be reworded, and never quotes a deposit cell. Paths are relative to the release (or the
# flat publication set).
PROVENANCE_FILES = frozenset({
    "SETTLED.json", "SETTLED.md", "ENV.txt", "RELEASE_PATH_NEUTRALISATION.txt", "RELEASE_VALUE_REDACTION.txt",
    "verification_status.txt", "document_build_manifest.json", "verification_manifest.json",
    "finalization_receipt.json", "release-manifest.json", "pipeline-policy.snapshot.json",
    "package_manifest.json", "invocations.jsonl", "invocations.watermark.json",
    "run.events.jsonl", "run_metrics.json",
    # 2.6.9: staged since 2.6.8 (renumber maps and invocation ids, written by the skill): nobody could
    # reword a hit in it. Its e-mail and IP detectors still read it, as for every provenance file.
    "critique/id_map_history.jsonl",
})
PROVENANCE_DIRS = ("scripts/",)
# run.json: every top-level field below is provenance or telemetry (the journal, the invocation
# ledger, model identities, timings, token and cost accounting, the toolkit and package digests,
# compliance, delivery and retention records, the software environment capture_environment.sh
# measured (a metadata-2.1 run.json keeps it only since 2.7.4), and the skill's own boilerplate notes). Only the
# text of a journal note (events[].payload.text or .note, which a person types with log_event.sh note)
# is matched below them. comment.json: the per-finding provenance and verification records, the
# run reference, the roll-up and the redaction declaration.
RUN_JSON_PROVENANCE_KEYS = frozenset({
    "schema_version", "audit_id", "audit_dir", "protocol_version", "gate_set_version", "status",
    "started_at", "ended_at", "active_seconds", "wall_clock_seconds", "auditor_model",
    "coordinator_model", "invocations", "events", "journal_watermark", "toolkit", "durations",
    "token_accounting", "tokens", "costs", "compliance", "delivery", "package", "phases", "rounds",
    "records_retained", "records_retained_checked_at", "settled_facts", "model_assignments",
    "diagnostic_artifacts", "failure_events", "publication_redaction", "registry",
    "changed_claim_ids", "rereviewed_claim_ids", "unresolved_blocking_item_ids",
    "unresolved_conclusion_critical_item_ids", "round_cap", "round_target", "rounds_run",
    "stopping_reason", "safe_to_publish", "post_delta_gates_passed", "human_effort", "paper_doi",
    "environment",
})
COMMENT_JSON_PROVENANCE_KEYS = frozenset({
    "schema_version", "audit_id", "comment_id", "safe_to_publish", "enumeration_scheme",
    "publication_redaction", "run_ref", "provenance", "verification", "verification_script",
    "rollup",
})
JOURNAL_NOTE_KEYS = frozenset({"note", "text"})


def provenance_file(relative: str) -> bool:
    return (relative in PROVENANCE_FILES or relative.startswith(PROVENANCE_DIRS)
            or relative.startswith("pkg_manifest"))


def value_matched_field(relative: str, keys: list[str]) -> bool:
    """Whether a run.json or comment.json string at this key path is matched against values."""
    if relative == "run.json":
        if keys and keys[0] in RUN_JSON_PROVENANCE_KEYS:
            return keys[:2] == ["events", "payload"] and len(keys) == 3 \
                and keys[2] in JOURNAL_NOTE_KEYS
        return True
    if relative == "comment.json":
        return not any(key in COMMENT_JSON_PROVENANCE_KEYS for key in keys)
    return True


_JSON_TOKEN = re.compile(r'"(?:[^"\\\n]|\\.)*"|[{}\[\]:,]|[^\s{}\[\]:,"]+')


def json_strings(text: str) -> Iterator[tuple[int, str, list[str]]]:
    """(offset, decoded string, key path) for every string literal (keys included) of valid JSON
    ``text``, in order. The key path is internal (it decides provenance) and is never printed."""
    stack: list[dict[str, Any]] = []
    for match in _JSON_TOKEN.finditer(text):
        token = match.group(0)
        top = stack[-1] if stack else None
        if token in ("{", "["):
            stack.append({"object": token == "{", "key": None, "expect_key": token == "{"})
        elif token in ("}", "]"):
            if stack:
                stack.pop()
        elif token == ":":
            if top is not None:
                top["expect_key"] = False
        elif token == ",":
            if top is not None and top["object"]:
                top["expect_key"], top["key"] = True, None
        elif token.startswith('"'):
            try:
                value = json.loads(token)
            except ValueError:
                continue
            if top is not None and top["object"] and top["expect_key"]:
                top["key"] = value
            keys = [frame["key"] for frame in stack if frame["object"] and frame["key"] is not None]
            yield match.start(), value, keys


def _json_segments(relative: str, text: str) -> list[Segment] | None:
    """One segment per string literal, located by the line it starts on; None if not JSON."""
    suffix = pathlib.PurePosixPath(relative).suffix.lower()
    try:
        if suffix in {".jsonl", ".ndjson"}:
            for line in text.splitlines():
                if line.strip():
                    json.loads(line)
        else:
            json.loads(text)
    except ValueError:
        return None
    locate_line = Locator(text)
    whole = provenance_file(relative)
    segments = []
    for offset, value, keys in json_strings(text):
        where = locate_line(offset)
        token = _JSON_TOKEN.match(text, offset)
        segments.append(Segment(relative, prepare(value), lambda _offset, where=where: where,
                                values=not whole and value_matched_field(relative, keys),
                                mask="json", token=(offset, token.end() if token else offset)))
    return segments


def release_files(release: pathlib.Path) -> Iterator[tuple[str, str, list[Segment]]]:
    """(relative path, "text" or "document", segments) for every release-bound file."""
    for path in sorted(release.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(release).as_posix()
        suffix = path.suffix.lower()
        values = not provenance_file(relative)
        if suffix == ".pdf":
            # The same three extractions the release privacy gate inspects (info with custom keys,
            # XMP metadata, text).
            info = _run(["pdfinfo", "-custom", str(path)], relative)
            meta = _run(["pdfinfo", "-meta", str(path)], relative)
            text = prepare(_run(["pdftotext", "-enc", "UTF-8", str(path), "-"], relative))
            whole_metadata = prepare(info + "\n" + meta)
            value_metadata = prepare(pdf_metadata_for_values(info, meta))
            label = relative + " metadata"
            yield relative, "document", [
                Segment(label, whole_metadata, Locator(whole_metadata), values=False),
                Segment(label, value_metadata, Locator(value_metadata), values=values),
                Segment(relative, text, Locator(text, pages=True), values=values),
            ]
        elif suffix == ".docx":
            text = prepare(docx_text(path, relative))
            yield relative, "document", [Segment(relative, text, Locator(text, prefix="paragraph"),
                                                 values=values)]
        elif suffix in crp.TEXT_SUFFIXES or suffix in {".htm", ".rmd", ".r", ".ndjson"}:
            if path.stat().st_size > MAX_RELEASE_TEXT_BYTES:
                raise InspectionFailure(f"cannot inspect {relative}: larger than the text limit")
            data = path.read_bytes()
            if suffix == ".json" and relative in {"run.json", "comment.json"}:
                masked = crp.apply_declared_redactions(data)
                if masked is not None:
                    data = masked
            text = decode_text(data)
            segments = None
            if suffix in {".json", ".jsonl", ".ndjson", ".ipynb"}:
                segments = _json_segments(relative, text)
            if segments is None and suffix in {".html", ".htm", ".xml"}:
                # Two readings, line for line: the visible text with inline markup joined, and
                # the text with every tag replaced by its attribute values (an alt text, a title,
                # an address in an href).
                visible = prepare(strip_markup(text, inline=suffix != ".xml"))
                attributes = prepare(tag_attribute_text(text))
                segments = [Segment(relative, visible, Locator(visible), values=values),
                            Segment(relative, attributes, Locator(attributes), values=values)]
            elif segments is None:
                text = prepare(text)
                segments = [Segment(relative, text, Locator(text), values=values, mask="text")]
            yield relative, "text", segments


def paper_texts(audit: pathlib.Path) -> list[str]:
    paper = audit / "paper"
    texts = []
    if paper.is_dir():
        for path in sorted(paper.glob("*.txt")):
            try:
                texts.append(decode_text(path.read_bytes()))
            except OSError:
                continue
        if not texts:
            for path in sorted(paper.glob("*.pdf")):
                try:
                    texts.append(_run(["pdftotext", "-enc", "UTF-8", str(path), "-"], "paper"))
                except InspectionFailure:
                    continue
    return texts


_REFERENCES_HEADING = re.compile(
    r"^(#{1,6})[ \t]*(?:\d+[.)]?[ \t]*)?(?:references?|bibliography|reference list|works cited|"
    r"literatur(?:verzeichnis)?|références|referencias|riferimenti(?: bibliografici)?|"
    r"referências|referenties|literatuur)[ \t]*(?:\{[^}\n]*\})?[ \t]*$", re.IGNORECASE | re.MULTILINE)
_HEADING = re.compile(r"^(#{1,6})[ \t]", re.MULTILINE)


def references_section(text: str) -> str:
    """The comment's own reference list: every References section of a Markdown report."""
    parts = []
    for match in _REFERENCES_HEADING.finditer(text):
        level = len(match.group(1))
        end = len(text)
        for heading in _HEADING.finditer(text, match.end()):
            if len(heading.group(1)) <= level:
                end = heading.start()
                break
        parts.append(text[match.end():end])
    return "\n".join(parts)


def public_texts(audit: pathlib.Path, release: pathlib.Path | None) -> list[str]:
    """Text whose every value is public or the comment's own: the skill's house citations, the
    whole front matter, and the comment's References sections."""
    texts: list[str] = []
    candidates = [HOUSE_CITATIONS]
    for base in (audit, release):
        if base is not None:
            candidates.append(base / "report/front_matter.md")
    for path in candidates:
        try:
            if path.is_file():
                texts.append(decode_text(path.read_bytes()))
        except OSError:
            continue
    for base in (audit, release):
        if base is None:
            continue
        report = base / "report/REPORT_final.md"
        try:
            if report.is_file():
                section = references_section(decode_text(report.read_bytes()))
                if section.strip():
                    texts.append(section)
        except OSError:
            continue
    return texts


@dataclass
class ScanResult:
    hits: list[Hit]
    warnings: list[Hit]
    texts: int
    documents: int
    # every refused match, before locations are merged (what ``mask_records`` masks); never printed
    matches: list[Hit] = field(default_factory=list)


def scan_release(audit: pathlib.Path, release: pathlib.Path, store: dict[str, Any]) -> ScanResult:
    matcher = Matcher(store)
    papers = paper_texts(audit)
    for text in papers:
        matcher.allow_text(text, paper=True)
    if papers:
        matcher.allow_byline(papers[0].split("\f", 1)[0][:FIRST_PAGE_CHARS])
    for text in public_texts(audit, release):
        matcher.allow_text(text)
    found: list[Hit] = []
    texts = documents = 0
    for relative, kind, segments in release_files(release):
        if kind == "document":
            documents += 1
        else:
            texts += 1
        if pathlib.PurePosixPath(relative).name == STORE_NAME:
            found.append(Hit(relative, "a deposit-value hash store from a pre-release build (it "
                                       "must never be staged)", file=relative))
            continue
        for segment in segments:
            found.extend(dataclasses.replace(item, file=relative) for item in matcher.scan(segment))
    unique: dict[tuple[str, str, str], Hit] = {}
    for item in found:
        unique.setdefault((item.location, item.kind, item.severity), item)
    refused = [item for item in unique.values() if item.severity == REFUSE]
    refused_at = {item.location for item in refused}
    warnings = [item for item in unique.values()
                if item.severity == WARN and item.location not in refused_at]
    return ScanResult(refused, warnings, texts, documents,
                      [item for item in found if item.severity == REFUSE])


# ------------------------------------------------------------------------------------------------
# Records nobody can reword: their STAGED copies are masked, never the audit's originals (2.6.9)
# ------------------------------------------------------------------------------------------------
# A refusal whose only remedy is "reword the finding" blocks an audit forever when the text is a record
# that cannot be reworded: a Phase-1 auditor report is hash-bound (SETTLED.json's reviewer evidence
# records; editing it fails finalize), a critic's output is model output that is never edited, and a work
# record or evidence script is bound into its role's evidence manifest. For those, stage_release.sh
# (``--mask-records``) replaces each matched span in the STAGED copy with a placeholder naming only the
# value's class, and lists every masking in RELEASE_VALUE_REDACTION.txt (file, line, class; never the
# value), as the audit-path neutraliser does for paths. The audit's own files are never touched. The
# comment's own outputs -- the report, the comment PDF and DOCX, comment.json, the verification notebook,
# the claims script, the front matter and captions -- are never masked: a value there still refuses, and
# the finding is reworded. Nothing masked is ever filed: publish_completed.sh files only the comment
# outputs, and runs this check on them again.
REDACTION_NOTE = "RELEASE_VALUE_REDACTION.txt"
RECORD_DIRS = ("critique/", "work/", "phase1/", "coordinator/")
RECORD_FILES = frozenset({"report/ADJUDICATION.md", "report/retired_claims.txt", "PAPER.txt", "BRIEF.md"})
_PHASE1_REPORT = re.compile(r"\Areport/0[0-9]_[^/]+\.md\Z")
RECORD_REMEDY = (
    "stage_release.sh masks the staged copy of a record nobody can reword (a Phase-1 auditor report, a "
    "critique record, a work record or evidence script) and lists it in " + REDACTION_NOTE + "; restage "
    "into a fresh directory, and never edit the audit's own copy (it is hash-bound)"
)
OTHER_REMEDY = (
    "edit the text in the audit's own copy of the file so that it describes the identifier by kind, file, "
    "column and count, never its value; then restage"
)


def record_file(relative: str) -> bool:
    """A release path that holds a record nobody can reword (its staged copy may be masked)."""
    if provenance_file(relative):
        return False
    return (relative in RECORD_FILES or relative.startswith(RECORD_DIRS)
            or bool(_PHASE1_REPORT.match(relative)))


_COMMENT_OUTPUTS = re.compile(
    r"\A(?:comment\.json|run\.json|verification\.Rmd|verification_summary\.csv|claims_script\.[^/]+"
    r"|[^/]+\.(?:pdf|docx))\Z")


def comment_output(relative: str) -> bool:
    """A release path the comment itself is built or filed from: a value there is reworded, never masked.
    Every file under report/ that is not a record (REPORT_final.md, REPORT_vN.md, LEDGER.md, the front matter,
    the captions, the bound PDF and DOCX) and, at the top, comment.json, run.json, the verification notebook
    and summary, the claims script and any PDF or DOCX."""
    return (relative.startswith("report/") and not record_file(relative)) or bool(_COMMENT_OUTPUTS.match(relative))


def _value_class(kind: str) -> str:
    for label in sorted(CLASS_LABEL.values(), key=len, reverse=True):
        if kind.startswith(label):
            return label
    return {"an IPv4 address": CLASS_LABEL["ip"], "an IPv6 address": CLASS_LABEL["ip"]}.get(kind, "a value")


def _placeholder(kind: str) -> str:
    return f"[masked at release: {_value_class(kind)}]"


def _merge_spans(spans: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    merged: list[tuple[int, int, str]] = []
    for start, end, kind in sorted(spans):
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end), merged[-1][2] or kind)
        else:
            merged.append((start, end, kind))
    return merged


def _mask_text(text: str, spans: list[tuple[int, int, str]]) -> str:
    """``text`` (one JSON string value) with each span replaced by its placeholder."""
    out = text
    for start, end, kind in reversed(_merge_spans(spans)):
        out = out[:start] + _placeholder(kind) + out[end:]
    return out


def _decode_for_mask(data: bytes) -> tuple[str, Callable[[str], bytes]] | None:
    """The file's text as the scan decoded it, and how to write it back in the same encoding; None for a
    file this masking does not rewrite (UTF-16)."""
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return None
    bom = data.startswith(codecs.BOM_UTF8)
    try:
        text = data.decode("utf-8-sig")
        return text, lambda value: (codecs.BOM_UTF8 if bom else b"") + value.encode("utf-8")
    except UnicodeDecodeError:
        text = decode_text(data)

        def encode(value: str) -> bytes:
            out = bytearray()
            for character in value:
                for codec in ("cp1252", "latin-1", "utf-8"):
                    try:
                        out += character.encode(codec)
                        break
                    except UnicodeEncodeError:
                        continue
            return bytes(out)
        return text, encode


def mask_records(release: pathlib.Path, matches: list[Hit]) -> tuple[list[Hit], list[str]]:
    """Mask the refused matches in the staged copies of records (``record_file``). Returns the masked
    matches (one per location and class) and the files that could not be masked, whose matches still
    refuse. The audit's files are never opened for writing."""
    by_file: dict[str, list[Hit]] = {}
    for item in matches:
        if item.file and record_file(item.file) and item.segment is not None and item.start >= 0:
            by_file.setdefault(item.file, []).append(item)
    masked: list[Hit] = []
    unmaskable: list[str] = []
    for relative, items in sorted(by_file.items()):
        path = release / relative
        if path.is_symlink() or not path.is_file() or any(not item.segment.mask for item in items):
            unmaskable.append(relative)
            continue
        decoded = _decode_for_mask(path.read_bytes())
        if decoded is None:
            unmaskable.append(relative)
            continue
        raw, encode = decoded
        if items[0].segment.mask == "text":
            prepared = prepare(raw)
            raw_lines, prepared_lines = raw.split("\n"), prepared.split("\n")
            if len(raw_lines) != len(prepared_lines):
                unmaskable.append(relative)
                continue
            starts = [0]
            for line in prepared_lines[:-1]:
                starts.append(starts[-1] + len(line) + 1)
            per_line: dict[int, list[tuple[int, int, str]]] = {}
            for item in items:
                first = bisect.bisect_right(starts, item.start) - 1
                last = bisect.bisect_right(starts, max(item.start, item.end - 1)) - 1
                for number in range(first, last + 1):
                    line_start, line = starts[number], prepared_lines[number]
                    begin = max(item.start, line_start) - line_start
                    finish = min(item.end, line_start + len(line)) - line_start
                    if line.endswith("\r") and finish == len(line):
                        finish -= 1
                    # a span that continues onto this line: its tail is removed, the placeholder stands once
                    kind = item.kind if number == first else ""
                    per_line.setdefault(number, []).append((begin, max(begin, finish), kind))
            for number, spans in per_line.items():
                base = raw_lines[number] if raw_lines[number] == prepared_lines[number] else prepared_lines[number]
                text = base
                for begin, finish, kind in reversed(_merge_spans(spans)):
                    text = text[:begin] + (_placeholder(kind) if kind else "") + text[finish:]
                raw_lines[number] = text
            path.write_bytes(encode("\n".join(raw_lines)))
        else:   # JSON: each string literal re-encoded with its masked value, every other byte kept
            if any(item.segment.token is None for item in items):
                unmaskable.append(relative)
                continue
            tokens: dict[tuple[int, int], list[Hit]] = {}
            for item in items:
                tokens.setdefault(item.segment.token, []).append(item)
            text = raw
            for (start, end), token_items in sorted(tokens.items(), reverse=True):
                value = _mask_text(token_items[0].segment.text,
                                   [(item.start, item.end, item.kind) for item in token_items])
                ascii_only = all(ord(character) < 128 for character in text[start:end])
                text = text[:start] + json.dumps(value, ensure_ascii=ascii_only) + text[end:]
            path.write_bytes(encode(text))
        seen: set[tuple[str, str]] = set()
        for item in items:
            key = (item.location, _value_class(item.kind))
            if key not in seen:
                seen.add(key)
                masked.append(item)
    return masked, unmaskable


def write_redaction_note(release: pathlib.Path, masked: list[Hit]) -> pathlib.Path | None:
    """Record the maskings in the release. A rewrite nobody can see is a silent edit."""
    if not masked:
        return None
    lines = [
        "Deposit values masked at release staging (stage check 3c)",
        "",
        "These staged files are copies of records nobody can reword: Phase-1 auditor reports, critique",
        "records, work records and evidence scripts. Each span below reproduced an identifying value from",
        "the deposit (or carried an e-mail or IP address) and was replaced with a placeholder naming only",
        "its class, '[masked at release: <class>]'; nothing else in the files was changed. The audit-side",
        "originals are unmodified and still match the hashes that bind them. The comment's own outputs are",
        "never masked: a value there refuses the release until the finding is reworded.",
        "",
    ]
    for item in sorted(masked, key=lambda hit: (hit.location, hit.kind)):
        lines.append(f"  {item.location}  ({_value_class(item.kind)})")
    note = release / REDACTION_NOTE
    note.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return note


# ------------------------------------------------------------------------------------------------
# Reporting
# ------------------------------------------------------------------------------------------------
def deposit_summary_line(store: dict[str, Any]) -> str:
    deposit = store["deposit"]
    columns = deposit["pii_columns"]
    column_text = ", ".join(f"{cls} {count}" for cls, count in columns.items()) or "none"
    dismissed = deposit.get("pii_columns_dismissed") or {}
    if dismissed:
        column_text += " (dismissed as variable names or identifiers: " + ", ".join(
            f"{cls} {count}" for cls, count in dismissed.items()) + ")"
    not_scanned = len(deposit["not_scanned"])
    truncated = len(deposit.get("truncated", []))
    values = sum(store["values"].values())
    return (f"deposit ({deposit['source']}): {deposit['files']} files, {deposit['scanned']} read "
            f"({deposit['read_by_r']} through R), {not_scanned} not scanned, {truncated} read in "
            f"part, {deposit['skipped']} not data; personal-data columns: {column_text}; "
            f"{values} distinct values hashed in memory under a one-run key (nothing value-derived "
            "is written)")


def _print_rows(out: Callable[[str], None], rows: list[str], limit: int, what: str) -> None:
    for row in rows[:limit]:
        out(row)
    if len(rows) > limit:
        out(f"    ... and {len(rows) - limit} more {what}")


def run_check(audit: pathlib.Path, release: pathlib.Path, *, json_path: pathlib.Path | None = None,
              out: Callable[[str], None] = print, mask: bool = False) -> int:
    store = build_store(audit)
    deposit = store["deposit"]
    out("  " + deposit_summary_line(store))
    if deposit.get("stale_value_store"):
        out(f"    notice: work/{STORE_NAME} is a value store left by a pre-release build of this "
            "check; it holds keyed hashes and their key, so treat it as personal data and delete it "
            "(nothing reads it)")
    _print_rows(out, [f"    not scanned (never a refusal): {row['file']} -- {row['reason']}"
                      for row in deposit["not_scanned"]], MAX_PRINTED_NOTICES, "not scanned")
    _print_rows(out, [f"    read in part (never a refusal): {row['file']} -- {row['reason']}"
                      for row in deposit["truncated"]], MAX_PRINTED_NOTICES, "read in part")
    _print_rows(out, [f"    notice (never a refusal): column {row['column']!r} in {row['file']} "
                      f"looks like {CLASS_LABEL.get(row['class'], row['class'])} column but yielded "
                      "no value this check can match (blank, too short, or a format it does not "
                      "read)" for row in deposit["pii_columns_without_values"]],
                MAX_PRINTED_NOTICES, "columns without values")
    summary: dict[str, Any] = {
        "schema_version": SCHEMA,
        "deposit": deposit,
        "values": store["values"],
    }
    masked: list[Hit] = []
    try:
        result = scan_release(audit, release, store)
        if mask and result.matches:
            # Staging only (stage_release.sh --mask-records): mask the staged copies of records nobody can
            # reword, list them, then scan again so that what is certified is what is on disk.
            masked, _unmaskable = mask_records(release, result.matches)
            if masked:
                write_redaction_note(release, masked)
                result = scan_release(audit, release, store)
    except InspectionFailure as exc:
        out(f"  FAIL (not inspectable): {exc}; a release document that cannot be read is not "
            "certified")
        out(f"    Remedy: {INSPECTION_REMEDY}.")
        if json_path is not None:
            summary.update({"inspection_failure": str(exc), "passed": False})
            json_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n",
                                 encoding="utf-8")
        return 2
    summary.update({
        "release": {"texts_scanned": result.texts, "documents_scanned": result.documents},
        "hits": [{"location": item.location, "kind": item.kind, "source": item.source}
                 for item in result.hits],
        "masked": [{"location": item.location, "kind": item.kind, "source": item.source}
                   for item in masked],
        "warnings": [{"location": item.location, "kind": item.kind, "source": item.source}
                     for item in result.warnings],
        "passed": not result.hits,
    })
    if json_path is not None:
        json_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if result.warnings:
        out(f"  warning: {len(result.warnings)} release location(s) hold one word that matches a "
            "one-word deposit value (values are never printed; never a refusal):")
        _print_rows(out, [f"    {item.location}: {item.kind}"
                          + (f" ({item.source})" if item.source else "")
                          for item in result.warnings], MAX_PRINTED_WARNINGS, "warnings")
        out(f"    Review: {WARNING_NOTE}.")
    if masked:
        out(f"  masked: {len(masked)} location(s) in the staged copies of records nobody can reword (the "
            f"audit's own files are untouched; listed in {REDACTION_NOTE}; never a refusal):")
        _print_rows(out, [f"    {item.location}: {item.kind}"
                          + (f" ({item.source})" if item.source else "")
                          for item in masked], MAX_PRINTED_HITS, "masked locations")
    if result.hits:
        out(f"  FAIL: {len(result.hits)} release location(s) reproduce an identifying value "
            "(values are never printed):")
        _print_rows(out, [f"    {item.location}: {item.kind}"
                          + (f" ({item.source})" if item.source else "")
                          for item in result.hits], MAX_PRINTED_HITS, "locations")
        # The remedy depends on the kind of file: a comment output is reworded, a record nobody can
        # reword is masked in its staged copy (only staging does that), anything else is edited.
        files = {item.file or item.location.split(" ", 1)[0] for item in result.hits}
        if any(comment_output(name) for name in files):
            out(f"    Remedy: {REMEDY} (for the comment's own outputs: the report, the comment PDF and DOCX, "
                "comment.json, the verification notebook, the claims script, the front matter and captions).")
        if any(record_file(name) for name in files):
            out(f"    Remedy: {RECORD_REMEDY} (for the records listed above"
                + ("; a staged record that still matches is in a format the masking does not rewrite, such "
                   "as HTML: report it" if mask else "") + ").")
        if any(not comment_output(name) and not record_file(name) for name in files):
            out(f"    Remedy: {OTHER_REMEDY} (for any other staged file).")
        return 1
    out(f"  ok: no deposit value, e-mail address or IP address in {result.texts} release text(s) "
        f"and {result.documents} document(s)")
    return 0


# ------------------------------------------------------------------------------------------------
# Self-test: synthetic, out-of-range controls (invented names, 1901 and 1902 birth dates, reserved
# domains, documentation and reserved address ranges). The generic e-mail and IP positives are
# assembled at run time, so this file never carries one literally.
# ------------------------------------------------------------------------------------------------
def _minimal_xlsx(rows: list[list[str]], sheet_name: str = "Roster") -> bytes:
    strings: list[str] = []
    sheet_rows = []
    for number, row in enumerate(rows, 1):
        cells = []
        for column, value in enumerate(row):
            reference = chr(65 + column) + str(number)
            if value not in strings:
                strings.append(value)
            cells.append(f'<c r="{reference}" t="s"><v>{strings.index(value)}</v></c>')
        sheet_rows.append(f'<row r="{number}">{"".join(cells)}</row>')
    main = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    rel = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", f'<workbook {main} {rel}><sheets>'
                         f'<sheet name="{html.escape(sheet_name)}" sheetId="1" r:id="rId1"/>'
                         '</sheets></workbook>')
        archive.writestr("xl/_rels/workbook.xml.rels",
                         '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
                         'relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/>'
                         '</Relationships>')
        archive.writestr("xl/sharedStrings.xml", f"<sst {main}>" + "".join(
            f"<si><t>{html.escape(value)}</t></si>" for value in strings) + "</sst>")
        archive.writestr("xl/worksheets/sheet1.xml",
                         f"<worksheet {main}><sheetData>{''.join(sheet_rows)}</sheetData></worksheet>")
    return buffer.getvalue()


def _pdf_string(text: str) -> bytes:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").encode("latin-1")


def _minimal_pdf(text: str, *, info: dict[str, str] | None = None, xmp: str | None = None) -> bytes:
    """A one-page PDF; ``info`` fills its Info dictionary and ``xmp`` its XMP metadata stream
    (pdfinfo -custom and pdfinfo -meta print them)."""
    stream = b"BT /Fnt 12 Tf 72 720 Td (" + _pdf_string(text) + b") Tj ET\n"
    catalog = b"<< /Type /Catalog /Pages 2 0 R" + (b" /Metadata 6 0 R" if xmp else b"") + b" >>"
    objects = [
        catalog,
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /Fnt 5 0 R "
        b">> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    if xmp:
        packet = xmp.encode("utf-8")
        objects.append(b"<< /Type /Metadata /Subtype /XML /Length " + str(len(packet)).encode()
                       + b" >>\nstream\n" + packet + b"\nendstream")
    info_number = None
    if info:
        objects.append(b"<< " + b" ".join(b"/" + key.encode("ascii") + b" (" + _pdf_string(value)
                                          + b")" for key, value in info.items()) + b" >>")
        info_number = len(objects)
    output = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, obj in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    trailer = f"<< /Size {len(objects) + 1} /Root 1 0 R"
    trailer += f" /Info {info_number} 0 R >>" if info_number else " >>"
    output.extend(f"trailer\n{trailer}\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(output)


def _minimal_docx(paragraphs: list[str]) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{html.escape(text)}</w:t></w:r></w:p>" for text in paragraphs)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml",
                         '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/'
                         f'2006/main"><w:body>{body}</w:body></w:document>')
    return buffer.getvalue()


def _xmp_packet(namespace: str, attributes: str = "") -> str:
    return ('<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
            '<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
            f'<rdf:RDF xmlns:rdf="{namespace}">\n'
            f'<rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/" {attributes}>\n'
            '<dc:format>application/pdf</dc:format>\n'
            '</rdf:Description>\n</rdf:RDF>\n</x:xmpmeta>\n<?xpacket end="w"?>')


def house_citation_pair() -> str | None:
    """Two adjacent words of the house citations (each three letters or more, capitalised), as a
    synthetic deposit name: a comment that carries the citation must still pass."""
    try:
        text = HOUSE_CITATIONS.read_text(encoding="utf-8")
    except OSError:
        return None
    start = text.find("## Reference entries")
    tokens = TOKEN.findall(prepare(text[start if start >= 0 else 0:]))
    for first, second in zip(tokens, tokens[1:]):
        if (first.isalpha() and second.isalpha() and len(first) >= 3 and len(second) >= 3
                and first[:1].isupper() and second[:1].isupper()):
            return f"{first} {second}"
    return None


SELF_TEST_VALUES = {
    # Invented names that are no one's, and birth dates outside any plausible sample.
    "full_name": "Qorvelline Xandrithow",
    "single_surname": "Brummelstedtova",
    "first": "Ilkavette",
    "last": "Wendrasshausen",
    # Two ordinary words as first and last name: only the pair may count.
    "ordinary_first": "Cider",
    "ordinary_last": "Tower",
    "last_comma_first": "Wendelgrave, Ossiandra Pellumine",
    # A surname beside a one-letter given name: code such as "zenthal(x, y)" is not that name.
    "initial_surname": "Zenthalbrook",
    "ordinary_surname": "Young",
    "pseudonym": "Respondent 1917",
    # Public or the comment's own: in the front matter, the reference list, the paper.
    "byline_name": "Orlavinne Quessart",
    "reference_name": "Marvolette Quistrand",
    "paper_name": "Qorvel Authorsen",
    "birth_dmy": "03.04.1901",
    "birth_iso": "1901-04-03",
    "birth_mon": "07-Aug-1902",
    "deposit_email_user": "qorvelline.x",
    "ip_doc": "198.51.100.77",
    "ipv6_doc": "2001:db8:85a3::8a2e:370:7334",
    "phone": "+49 30 9999 1901",
    # A survey definition's variable name and a column of identifiers: not people.
    "dictionary_value": "Vendelquistor",
    "identifier_column_value": "Harrowmickle",
    # A sheet named like a person: never printed.
    "sheet_name": "Vendelquist Oranamoor",
    "zip_name": "Ostravinsky Quarrelane",
    "gz_first": "Idrunella",
    "gz_last": "Pelvratsky",
    "json_first": "Quilmara",
    "json_last": "Dostrevna",
    "fr_nom": "Xorbelline",
    "fr_prenom": "Quiravette",
    "fr_birth": "05/06/1902",
    "latin1_name": "Brümmelstädt Orvanique",
    "year_file_name": "Tessaly Vornquist",
    "dated_file_name": "Zelvarine Oquastrom",
}


def self_test(out: Callable[[str], None] = print) -> bool:
    values = SELF_TEST_VALUES
    at = "@"
    deposit_email = values["deposit_email_user"] + at + "nowhere.invalid"
    generic_email = "zq.roe" + at + "uni-" + "nowhere.edu"
    front_email = "corresponding.author" + at + "uni-" + "somewhere.edu"
    # The authors' contact line in their deposited code names a byline author; the second address
    # in the same file does not.
    contact_email = "q.authorsen" + at + "uni-" + "nowhere.edu"
    office_email = "lab.office" + at + "uni-" + "nowhere.edu"
    # 240.0.0.0/4 is reserved and never assigned, and 3ffe::/16 (the 6bone) was returned to IANA
    # in 2006, so these controls can be no one's address.
    generic_ip = ".".join(["240", "201", "202", "203"])
    generic_ipv6 = ":".join(["3ffe", "b00", "0", "0", "1", "0", "0", "a"])
    version_quad = ".".join(["240", "2", "4", "2"])
    private_ip = ".".join(["10", "200", "100", "50"])
    kernel_version = ".".join(["240", "15", "153", "1"])
    ipv6_other_form = ":".join(["2001", "0DB8", "85A3", "0000", "0000", "8A2E", "0370", "7334"])
    house_pair = house_citation_pair()
    failures: list[str] = []
    checks = 0

    def expect(label: str, condition: bool) -> None:
        nonlocal checks
        checks += 1
        if not condition:
            failures.append(label)

    capability = r_capability()
    with tempfile.TemporaryDirectory(prefix="i4r-deposit-values-selftest-") as scratch:
        base = pathlib.Path(scratch)
        audit, release = base / "audit", base / "release"
        for directory in ("pkg/data", "pkg/code", "report", "paper", "work"):
            (audit / directory).mkdir(parents=True)
        survey_rows = [
            [values["full_name"], deposit_email, values["birth_dmy"], values["ip_doc"],
             values["phone"]],
            [values["single_surname"], "", values["birth_mon"], values["ipv6_doc"], ""],
            [values["ordinary_surname"]], [values["pseudonym"]], [values["last_comma_first"]],
            [values["byline_name"]], [values["reference_name"]], [values["paper_name"]],
        ] + ([[house_pair]] if house_pair else [])
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(["Name", "E-Mail", "Geburtsdatum", "IPAddress", "Telefon", "score",
                         "study_name"])
        for number, row in enumerate(survey_rows, 3):
            writer.writerow(row + [""] * (5 - len(row)) + [str(number), "Placebo Arm"])
        (audit / "pkg/data/survey.csv").write_text(buffer.getvalue(), encoding="utf-8")
        (audit / "pkg/data/roster.xlsx").write_bytes(_minimal_xlsx(
            [["Vorname", "Nachname", "Gruppe"], [values["first"], values["last"], "A"],
             [values["ordinary_first"], values["ordinary_last"], "B"],
             ["X", values["initial_surname"], "C"]], values["sheet_name"]))
        # No identifier among the variable names: only the dictionary rule dismisses this column.
        (audit / "pkg/data/definition.csv").write_text(
            "type,name,label\ntext,Consentquary,Consent\n"
            f"text,{values['dictionary_value']},Question one\n", encoding="utf-8")
        (audit / "pkg/data/items.csv").write_text(
            "name,score\nitem_1,1\nitem_2,2\n"
            f"{values['identifier_column_value']},3\n", encoding="utf-8")
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr(f"inner/{values['zip_name']}.csv", f"Name\n{values['zip_name']}\n")
        (audit / "pkg/data/archive.zip").write_bytes(archive.getvalue())
        (audit / "pkg/data/names.csv.gz").write_bytes(gzip.compress(
            f"Surname,Forename\n{values['gz_last']},{values['gz_first']}\n".encode()))
        (audit / "pkg/data/records.json").write_text(json.dumps(
            [{"firstName": values["json_first"], "lastName": values["json_last"], "age": 30}]),
            encoding="utf-8")
        (audit / "pkg/data/fr_roster.csv").write_text(
            f"Nom;Prénom;Date de naissance\n{values['fr_nom']};{values['fr_prenom']};"
            f"{values['fr_birth']}\n", encoding="utf-8")
        # An ASCII head longer than any encoding sniff, then a Windows-1252 name.
        (audit / "pkg/data/latin1.csv").write_bytes(
            ("Name,comment\n" + ",padding that keeps the name past the head\n" * 3000).encode()
            + f"{values['latin1_name']},x\n".encode("cp1252"))
        (audit / "pkg/data/cohort 1901.csv").write_text(f"Name\n{values['year_file_name']}\n",
                                                        encoding="utf-8")
        (audit / f"pkg/data/export {values['birth_dmy']}.csv").write_text(
            f"Name\n{values['dated_file_name']}\n", encoding="utf-8")
        (audit / "pkg/code/analysis.R").write_text(
            f"# contact: {contact_email}; data office: {office_email}\nx <- 1\n", encoding="utf-8")
        (audit / "pkg/data/table.parquet").write_bytes(b"PAR1synthetic")
        (audit / "pkg/data/broken.xlsx").write_bytes(b"not a workbook")
        (audit / "paper/paper.txt").write_text(
            f"A synthetic article\n{values['paper_name']} and Synthetic Author\n"
            "Correspondence: " + front_email + "\n\fSecond page.\n", encoding="utf-8")
        (audit / "report/front_matter.md").write_text(
            f'::: {{custom-style="Authors"}}\nSynthetic Author and {values["byline_name"]}\n:::\n'
            "\\* Correspondence: Synthetic Author, " + front_email + "\n", encoding="utf-8")
        store = build_store(audit)
        deposit = store["deposit"]
        expect("unsupported and unreadable deposit files are reported as not scanned",
               len(deposit["not_scanned"]) == 2)
        expect("personal-data headers are recognised (English, German, French, JSON, zip, gz)",
               deposit["pii_columns"] == {"birthdate": 2, "email": 1, "ip": 1, "name": 13,
                                          "phone": 1})
        expect("a dictionary's and an identifier list's name columns are dismissed",
               deposit["pii_columns_dismissed"] == {"name": 2})
        expect("every kept personal-data column yielded values",
               deposit["pii_columns_without_values"] == [])
        expect("study_name is not a person's name", classify_header("study_name") is None)
        expect("IPAQ_total is not an IP column", classify_header("IPAQ_total") is None)
        expect("RecipientLastName is a last-name column",
               classify_header("RecipientLastName") == ("name", "last"))
        expect("clientIP is an IP column", classify_header("clientIP") == ("ip", ""))
        expect("Adresse électronique is an e-mail column",
               classify_header("Adresse électronique") == ("email", ""))
        expect("fecha_nacimiento, data di nascita and geboortedatum are birth-date columns",
               all(classify_header(header) == ("birthdate", "")
                   for header in ("fecha_nacimiento", "data di nascita", "geboortedatum")))
        expect("Apellido, Cognome, Sobrenome and Achternaam are last-name columns",
               all(classify_header(header) == ("name", "last")
                   for header in ("Apellido", "Cognome", "Sobrenome", "Achternaam")))

        clean = ("The file data/survey.csv holds a column Name with 9 participant names and a "
                 "Geburtsdatum column with 2 dates of birth.\nYoung athletes were tested; "
                 "respondent 1917 withdrew.\nContact: " + front_email + " and "
                 "someone" + at + "example.org; server 192.0.2.10.\n"
                 f"Built with SomeTool {version_quad}; router {private_ip}; host OS Linux "
                 f"{kernel_version}-generic.\n"
                 f"The variable {values['dictionary_value']} and the item "
                 f"{values['identifier_column_value']} are described.\n"
                 "Run with Python 3.4.01 and pandoc v3.4.01; schema http://ns.invalid/?d=1901-04-03; "
                 "path data/1901/04/03.\n"
                 "Speeds 3/4/01 km/h and 3/4/01 (km/h); group sizes 3/4/01/17; items FIND-3/4/01.\n"
                 "Documentation addresses 2001:db8::10 and fe80::1; code std::vector, dplyr::filter; "
                 "time 12:30:45.\n")
        matrix_code = ("beta = X.T" + at + "X.values\nfit = M" + at + "model.params\n"
                       "z = x" + at + "W.to(device)\n")
        run_json = json.dumps({"invocations": [{"error": values["full_name"]}],
                               "notes": values["full_name"]}, indent=2) + "\n"
        notes_line = next(number for number, line in enumerate(run_json.splitlines(), 1)
                          if '"notes"' in line)
        cases: dict[str, tuple[str, str, str]] = {
            "clean": ("report/clean.md", clean, "pass"),
            "authors' contact line": ("work/analysis_copy.R", f"# contact: {contact_email}\n",
                                      "pass"),
            "matrix code is not an e-mail address": ("work/model.py", matrix_code, "pass"),
            "a surname beside an initial is one word": (
                "work/fit.py", f"r = {values['initial_surname'].lower()}(x, y)\n", "pass"),
            "a byline name (front matter)": (
                "report/byline.md", f"{values['byline_name']} wrote this comment.\n", "pass"),
            "a name the comment's reference list cites": (
                "report/cited.md", f"As {values['reference_name']} showed.\n", "pass"),
            "a name the paper prints": (
                "report/paper_name.md", f"{values['paper_name']} reported it.\n", "pass"),
            "a provenance JSON file is not value-matched": (
                "SETTLED.json", json.dumps({"python_version": values["full_name"]}) + "\n", "pass"),
            "a provenance text file is not value-matched": (
                "SETTLED.md", f"- Confirmed by: {values['full_name']}\n", "pass"),
            "the skill's own scripts are not value-matched": (
                "scripts/helper.py", f"# {values['full_name']} born {values['birth_iso']}\n", "pass"),
            "an ordinary word alone": ("report/c4.md", f"{values['ordinary_first']} was served.\n",
                                       "pass"),
            "full name in upper case": (
                "report/a.md", f"One row reads {values['full_name'].upper()}.\n", "refuse"),
            "full name in lower case": (
                "report/a_lower.md", f"one row reads {values['full_name'].lower()}.\n", "refuse"),
            "surname part of a full name warns": (
                "report/a2.md", f"Mr {values['full_name'].split()[-1]} declined.\n", "warn"),
            "single surname warns": (
                "report/b.md", f"The name {values['single_surname']} appears.\n", "warn"),
            "first plus last from paired columns": (
                "report/c.md", f"Listed as {values['first']} {values['last']}.\n", "refuse"),
            "ordinary-word first plus last": (
                "report/c2.md", f"Listed as {values['ordinary_first']} {values['ordinary_last']}.\n",
                "refuse"),
            "ordinary-word pair as Last, First": (
                "report/c3.md", f"Listed as {values['ordinary_last']}, {values['ordinary_first']}.\n",
                "refuse"),
            "a Last, First Middle deposit value": (
                "report/c5.md", "Ossiandra Pellumine Wendelgrave agreed.\n", "refuse"),
            "birth date reformatted": ("report/d.md", f"Born {values['birth_iso']}.\n", "refuse"),
            "dotted two-digit-year birth date": ("report/d2.md", "Born 3.4.01.\n", "refuse"),
            "dd-Mon-yyyy deposit birth date": ("report/d3.md", "Born 7 August 1902.\n", "refuse"),
            "French roster: Prénom plus Nom": (
                "report/d4.md", f"{values['fr_prenom']} {values['fr_nom']} withdrew.\n", "refuse"),
            "French roster: a French-written birth date": (
                "report/d5.md", "Née le 5 juin 1902.\n", "refuse"),
            "deposit ip in a documentation range": (
                "report/e.md", f"From {values['ip_doc']}.\n", "refuse"),
            "deposit ipv6 in another form": ("report/e2.md", f"From {ipv6_other_form}.\n", "refuse"),
            "generic ipv6": ("report/e3.md", f"Logged from {generic_ipv6}.\n", "refuse"),
            "deposit phone": ("report/f.md", f"Call {values['phone']}.\n", "refuse"),
            "generic e-mail": ("report/g.md", f"Write to {generic_email}.\n", "refuse"),
            "generic ipv4": ("report/h.md", f"Logged from {generic_ip}.\n", "refuse"),
            "version-shaped quad after a network word": (
                "report/i.md", f"The server at {version_quad} answered.\n", "refuse"),
            "a code-file address that names no author": (
                "report/j.md", f"Data office: {office_email}.\n", "refuse"),
            "deposit e-mail in comment.json": (
                "comment.json", json.dumps({"note": deposit_email}) + "\n", "refuse"),
            "a \\u-escaped name in JSON": (
                "report/escaped.json", '{"observed": "\\u0051orvelline Xandrithow"}\n', "refuse"),
            "a name split by HTML inline markup": (
                "report/page.html", "<p>One row: Qor<b>velline</b> Xandrithow</p>\n", "refuse"),
            "a name in an HTML attribute": (
                "report/figure.html", f'<img alt="Row {values["full_name"]}" src="f.png">\n',
                "refuse"),
            "an address in an HTML link": (
                "report/link.html", f'<a href="mailto:{generic_email}">the office</a>\n', "refuse"),
            "a name across a hyphenated line end": (
                "report/hyphen.md", "The row reads Qorvelline Xan-\ndrithow here.\n", "refuse"),
            "a zip member's name": ("report/zip.md", f"{values['zip_name']} left.\n", "refuse"),
            "a gz table's first plus last": (
                "report/gz.md", f"{values['gz_first']} {values['gz_last']} left.\n", "refuse"),
            "a JSON deposit's first plus last": (
                "report/json.md", f"{values['json_last']} {values['json_first']} left.\n", "refuse"),
            "a Windows-1252 name after an ASCII head": (
                "report/latin.md", f"{values['latin1_name']} withdrew.\n", "refuse"),
            "a name from a file whose name holds a year": (
                "report/year.md", f"{values['year_file_name']} withdrew.\n", "refuse"),
            "a name from a file whose name holds a date": (
                "report/dated.md", f"{values['dated_file_name']} withdrew.\n", "refuse"),
            "a provenance file still meets the e-mail detector": (
                "ENV.txt", f"maintainer {generic_email}\n", "refuse"),
            # 2.6.9: check 4's per-line escape for code that looks like an address, honoured here too
            # (only in a code file, only on its own line, and never for a deposit address)
            "a code line marked not-an-email": (
                "work/marked.py", "z = q" + at + "keys.data  # i4r-privacy: not-an-email\n", "pass"),
            "the same code without the marker": (
                "work/unmarked.py", "z = q" + at + "keys.data\n", "refuse"),
            "the marker outside a code file": (
                "report/marked.md", f"Write to {generic_email}  # i4r-privacy: not-an-email\n", "refuse"),
            "a deposit address on a marked code line": (
                "work/marked_deposit.py", f"x = '{deposit_email}'  # i4r-privacy: not-an-email\n", "refuse"),
        }
        if house_pair:
            cases["the house citations"] = (
                "report/house.md", HOUSE_CITATIONS.read_text(encoding="utf-8"), "pass")
        for relative, text, _expected in cases.values():
            target = release / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        (release / "run.json").write_text(run_json, encoding="utf-8")
        (release / "report/REPORT_final.md").write_text(
            "# Comment\n\nText.\n\n## References\n\n"
            f"{values['reference_name'].split()[1]}, {values['reference_name'].split()[0]} (1901). "
            "A synthetic study. *Journal of Nothing*.\n", encoding="utf-8")
        pdf_available = shutil.which("pdftotext") is not None and shutil.which("pdfinfo") is not None
        if pdf_available:
            (release / "comment.pdf").write_bytes(_minimal_pdf(f"Row: {values['full_name']}"))
            (release / "meta.pdf").write_bytes(_minimal_pdf(
                "Clean text.", info={"Subject": f"Row {values['full_name']}"}))
            (release / "xmp.pdf").write_bytes(_minimal_pdf(
                "Clean text.", xmp=_xmp_packet("tag:1901-04-03:rdf-syntax-ns#")))
            (release / "xmp_attr.pdf").write_bytes(_minimal_pdf("Clean text.", xmp=_xmp_packet(
                "tag-rdf-syntax-ns#", f'dc:title="Row {values["full_name"]}"')))
        (release / "comment.docx").write_bytes(_minimal_docx(
            ["Clean paragraph.", f"Row: {values['full_name']}"]))
        result = scan_release(audit, release, store)
        refused = {item.location for item in result.hits}
        warned = {item.location for item in result.warnings}

        def verdict(relative: str) -> str:
            prefix = relative + " "
            if any(location.startswith(prefix) for location in refused):
                return "refuse"
            if any(location.startswith(prefix) for location in warned):
                return "warn"
            return "pass"

        for label, (relative, _text, expected) in cases.items():
            expect(f"{label}: {expected}", verdict(relative) == expected)
        expect("run.json telemetry is not value-matched, its notes are",
               sorted(location for location in refused if location.startswith("run.json ")) ==
               [f"run.json line {notes_line}"])
        expect("DOCX text caught with a paragraph location",
               "comment.docx paragraph 2" in refused)
        if pdf_available:
            expect("PDF text caught with a page location",
                   any(location.startswith("comment.pdf page 1 line") for location in refused))
            expect("PDF Info metadata caught",
                   any(location.startswith("meta.pdf metadata line") for location in refused))
            expect("an XMP namespace is not a birth date", verdict("xmp.pdf") == "pass"
                   and not any(location.startswith("xmp.pdf metadata") for location in refused))
            expect("an XMP property in attribute form is read",
                   any(location.startswith("xmp_attr.pdf metadata line") for location in refused))
        sources = {item.location.split(" ", 1)[0]: item.source for item in result.hits}
        expect("a sheet is named by its position", "roster.xlsx sheet 1" in sources.get(
            "report/c.md", ""))
        expect("a zip member is named by its position", "archive.zip member 1" in sources.get(
            "report/zip.md", ""))
        expect("a file name with a year is printed as its path",
               "in data/cohort 1901.csv" in sources.get("report/year.md", ""))
        expect("a file name with a date is printed as its index",
               "deposit file #" in sources.get("report/dated.md", ""))

        lines: list[str] = []
        summary_path = base / "summary.json"
        status = run_check(audit, release, json_path=summary_path, out=lines.append)
        printed = "\n".join(lines)
        planted = [str(value) for value in values.values()] + [
            deposit_email, contact_email, office_email, generic_email, generic_ip, generic_ipv6,
            version_quad, kernel_version, ipv6_other_form, "Ossiandra", "Wendelgrave", "Pellumine"]
        planted += [house_pair] if house_pair else []
        expect("the check refuses the planted release", status == 1)
        expect("no planted value is printed",
               not any(value.casefold() in printed.casefold() for value in planted))
        summary_text = summary_path.read_text(encoding="utf-8").casefold()
        expect("the --json summary holds no planted value",
               not any(value.casefold() in summary_text for value in planted))
        expect("the remedy is printed", "Remedy: reword" in printed)
        expect("one-word matches are printed as warnings", "warning:" in printed)
        expect("nothing value-derived is written to the audit",
               not any((audit / "work").iterdir()))

        # Warnings alone never refuse, and a clean release passes.
        for path in release.rglob("*"):
            if path.is_file():
                path.unlink()
        (release / "report").mkdir(exist_ok=True)
        (release / "report/clean.md").write_text(clean, encoding="utf-8")
        lines = []
        expect("a clean release passes",
               run_check(audit, release, out=lines.append) == 0)
        (release / "report/b.md").write_text(cases["single surname warns"][1], encoding="utf-8")
        lines = []
        expect("a release with warnings only passes",
               run_check(audit, release, out=lines.append) == 0 and "warning:" in "\n".join(lines))
        # 2.6.9: staging masks the STAGED copy of a record nobody can reword (a critic's output, a
        # Phase-1 report, a notebook under work/), lists it, and scans again; a comment output still
        # refuses with the reword remedy.
        record_md = release / "critique/round_1/critic_stats.md"
        record_md.parent.mkdir(parents=True, exist_ok=True)
        record_md.write_text(f"Intro.\nRow {values['full_name']} and {generic_email} here.\n"
                             f"Split Qorvelline Xan-\ndrithow end.\n", encoding="utf-8")
        phase1 = release / "report/02_data_integrity.md"
        phase1.write_text(f"Born {values['birth_iso']}.\n", encoding="utf-8")
        notebook = release / "work/a2/check.ipynb"
        notebook.parent.mkdir(parents=True, exist_ok=True)
        notebook.write_text(json.dumps({"cells": [{"source": [f"# row {values['full_name']}\n", "x = 1\n"]}]},
                                       indent=1) + "\n", encoding="utf-8")
        lines = []
        status = run_check(audit, release, out=lines.append, mask=True)
        note = release / REDACTION_NOTE
        note_text = note.read_text(encoding="utf-8") if note.is_file() else ""
        masked_texts = [path.read_text(encoding="utf-8") for path in (record_md, phase1, notebook)]
        expect("staging masks records and certifies the masked copies", status == 0)
        expect("the masking note lists each location by line and class, never the value",
               "critique/round_1/critic_stats.md line 2  (a name)" in note_text
               and "critique/round_1/critic_stats.md line 2  (an e-mail address)" in note_text
               and "report/02_data_integrity.md line 1  (a date of birth)" in note_text
               and not any(value.casefold() in note_text.casefold()
                           for value in (values["full_name"], generic_email, values["birth_iso"])))
        expect("the masked copies hold placeholders, keep their line numbers and stay valid JSON",
               "[masked at release: a name]" in masked_texts[0]
               and masked_texts[0].count("\n") == 4
               and values["full_name"].split()[0] not in masked_texts[0]
               and "drithow" not in masked_texts[0]
               and json.loads(masked_texts[2])["cells"][0]["source"][1] == "x = 1\n"
               and not any(values["full_name"] in text for text in masked_texts))
        expect("a masked value is never printed", values["full_name"] not in "\n".join(lines))
        (release / "report/REPORT_final.md").write_text(f"Row {values['full_name']}.\n", encoding="utf-8")
        record_md.write_text(f"Row {values['full_name']}.\n", encoding="utf-8")
        lines = []
        status = run_check(audit, release, out=lines.append, mask=True)
        expect("a comment output still refuses, with the reword remedy, beside a masked record",
               status == 1 and "Remedy: reword" in "\n".join(lines)
               and "report/REPORT_final.md line 1" in "\n".join(lines)
               and values["full_name"] not in record_md.read_text(encoding="utf-8"))
        lines = []
        record_md.write_text(f"Row {values['full_name']}.\n", encoding="utf-8")
        (release / "report/REPORT_final.md").unlink()
        status = run_check(audit, release, out=lines.append)
        expect("without --mask-records a record still refuses, and the remedy says staging masks it",
               status == 1 and "stage_release.sh masks the staged copy" in "\n".join(lines))
        for path in (record_md, phase1, notebook, note):
            path.unlink()
        # A document that cannot be read is not certified, with its own exit status.
        (release / "broken.docx").write_bytes(b"not a document")
        lines = []
        expect("an unreadable document is not certified (exit 2)",
               run_check(audit, release, out=lines.append) == 2
               and "not inspectable" in "\n".join(lines))
        (release / "broken.docx").unlink()
        # R reader, when this machine has it: an SPSS file written by haven itself, whose IP column
        # is camelCase and whose name column is found by its French variable label only.
        if capability["rscript"] and capability["haven"]:
            sav = audit / "pkg/data/extra.sav"
            script = ('df <- data.frame(ID = 1L, clientIP = "10.201.7.9", v7 = "Talvorine Esquimant",'
                      ' Nachname = "Vorsterkampff", stringsAsFactors = FALSE);'
                      ' attr(df$v7, "label") <- "Quel est votre nom ?";'
                      ' haven::write_sav(df, commandArgs(TRUE)[[1]])')
            written = subprocess.run([capability["rscript"], "--vanilla", "-e", script, str(sav)],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     timeout=120, check=False).returncode == 0
            expect("haven wrote the SPSS control", written and sav.is_file())
            if written:
                (release / "report/r_ip.md").write_text("Seen from 10.201.7.9.\n", encoding="utf-8")
                (release / "report/r_label.md").write_text("Talvorine Esquimant left.\n",
                                                           encoding="utf-8")
                (release / "report/r_single.md").write_text("Vorsterkampff left.\n",
                                                            encoding="utf-8")
                result = scan_release(audit, release, build_store(audit))
                refused = {item.location.split(" ", 1)[0] for item in result.hits}
                warned = {item.location.split(" ", 1)[0] for item in result.warnings}
                expect("a camelCase IP column read through R is caught", "report/r_ip.md" in refused)
                expect("a column found by its French label through R is caught",
                       "report/r_label.md" in refused)
                expect("a one-word SPSS value read through R warns", "report/r_single.md" in warned)
                lines = []
                run_check(audit, release, out=lines.append)
                expect("the SPSS values are not printed",
                       not any(value in "\n".join(lines).casefold()
                               for value in ("vorsterkampff", "talvorine", "esquimant", "10.201.7.9")))
    if failures:
        out(f"  FAIL: deposit-value detector self-test failed {len(failures)} of {checks} controls:")
        for label in failures:
            out(f"    - {label}")
        return False
    notes = []
    if not (capability["rscript"] and capability["haven"]):
        notes.append("R reader controls skipped: Rscript with haven is not available")
    if not (shutil.which("pdftotext") and shutil.which("pdfinfo")):
        notes.append("PDF controls skipped: pdftotext and pdfinfo are not on PATH")
    if house_pair is None:
        notes.append("house-citation control skipped: reference/house_citations.md is missing")
    note = f" ({'; '.join(notes)})" if notes else ""
    out(f"  ok: deposit-value detector self-tested ({checks} synthetic controls){note}")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("audit", nargs="?", type=pathlib.Path)
    parser.add_argument("release", nargs="?", type=pathlib.Path)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--json", type=pathlib.Path,
                        help="write a summary here (counts, locations and value kinds; no value)")
    parser.add_argument("--mask-records", action="store_true",
                        help="staging only: mask matches in the STAGED copies of records nobody can reword "
                             "(Phase-1 reports, critique records, work records) and list them in "
                             + REDACTION_NOTE + "; the comment's own outputs still refuse")
    args = parser.parse_args(argv)
    try:
        if args.self_test:
            return 0 if self_test() else 1
        if args.audit is None or args.release is None:
            parser.error("an audit directory and a release directory are required")
        audit, release = args.audit.resolve(), args.release.resolve()
        if not audit.is_dir() or not release.is_dir():
            print("  FAIL (not inspectable): the audit and the release must both be directories",
                  file=sys.stderr)
            return 2
        if args.mask_records and (release == audit or audit in release.parents or release in audit.parents):
            print("  FAIL (not inspectable): --mask-records rewrites the staged copy only; the release must be "
                  "a staged directory outside the audit", file=sys.stderr)
            return 2
        return run_check(audit, release, json_path=args.json, mask=args.mask_records)
    except Exception as exc:  # noqa: BLE001 -- never a traceback: it could quote a value
        # Only the exception's class is printed: a message could quote a cell or a line.
        print(f"  FAIL (internal error): check 3c stopped on {exc.__class__.__name__}; "
              f"{INTERNAL_REMEDY}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
