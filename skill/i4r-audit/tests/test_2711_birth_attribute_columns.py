"""2.7.11: a birth word that qualifies another attribute is not a date-of-birth column.

One audit's staging and publication logs listed "personal-data columns: birthdate N":
SPSS 'Sex' columns whose label asks which sex the respondent was assigned at birth, each with a
"looks like a date of birth column" notice. gender_at_birth, birth_weight, country_of_birth and birth
order headers were classified the same way. Runs the real check_deposit_values.py on a deposit built on
disk; on 2.7.10 the summary reports {'birthdate': 5} for the CSV below. A real date-of-birth column, and a
header whose birth word is itself a date (dob, birthday), are still collected, and a release text that
quotes a date of birth still refuses.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
CHECK = SKILL / "scripts/check_deposit_values.py"


class BirthAttributeColumns(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="i4r-2711-birth-")
        base = pathlib.Path(self.tmp.name)
        self.audit, self.release, self.summary = base / "audit", base / "release", base / "summary.json"
        for directory in ("pkg/data", "report", "paper", "work"):
            (self.audit / directory).mkdir(parents=True)
        self.release.mkdir()
        (self.audit / "paper/paper.txt").write_text("A synthetic article\n\fSecond page.\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def check(self) -> tuple[subprocess.CompletedProcess, dict]:
        result = subprocess.run([sys.executable, str(CHECK), str(self.audit), str(self.release),
                                 "--json", str(self.summary)], capture_output=True, text=True,
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        return result, json.loads(self.summary.read_text())

    def test_birth_attribute_headers_are_not_birth_dates(self) -> None:
        (self.audit / "pkg/data/survey.csv").write_text(
            "pid,gender_at_birth,birth_weight,country_of_birth,SexAtBirth,BirthOrder,score\n"
            "1,1,3500,A,1,1,4\n2,2,3100,B,2,2,5\n3,1,2900,A,1,3,3\n", encoding="utf-8")
        result, summary = self.check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("birthdate", summary["deposit"]["pii_columns"])
        self.assertNotIn("looks like a date of birth column", result.stdout)

    def test_a_real_birth_date_column_still_refuses_its_value(self) -> None:
        (self.audit / "pkg/data/survey.csv").write_text(
            "pid,date_of_birth,score\n1,1987-03-14,4\n2,1990-11-02,5\n", encoding="utf-8")
        (self.release / "note.md").write_text("One respondent was born on 1987-03-14.\n", encoding="utf-8")
        result, summary = self.check()
        self.assertEqual(summary["deposit"]["pii_columns"].get("birthdate"), 1)
        self.assertNotEqual(result.returncode, 0, "a quoted date of birth still refuses the release")

    def test_a_birth_word_that_is_itself_a_date_is_still_a_birth_date(self) -> None:
        (self.audit / "pkg/data/survey.csv").write_text(
            "pid,dob_place,birthday_country,score\n1,1987-03-14,1990-11-02,4\n2,1990-11-02,1987-03-14,5\n",
            encoding="utf-8")
        result, summary = self.check()
        self.assertEqual(summary["deposit"]["pii_columns"].get("birthdate"), 2, result.stdout + result.stderr)

    def test_a_date_format_hint_beside_a_birth_attribute_is_still_a_birth_date(self) -> None:
        # Review EDGE-1: a birth noun with an attribute word and a date-format hint (no date word) named a date of
        # birth on 2.7.10 and must still; the candidate dropped these and check 3c passed a quoted date.
        (self.audit / "pkg/data/survey.csv").write_text(
            'pid,Birth (dd/mm/yyyy) and sex,"Birth (yyyy-mm-dd), sex",birth_ddmmyyyy_country,birth_mmddyy_sex,'
            '"Birth dd.mm.yy, place",Birth (month/year) and sex,score\n'
            "1,14/03/1990,1990-03-14,14031990,031490,14.03.90,03/1990,4\n"
            "2,02/11/1988,1988-11-02,02111988,110288,02.11.88,11/1988,5\n", encoding="utf-8")
        (self.release / "note.md").write_text("One respondent was born on 14/03/1990.\n", encoding="utf-8")
        result, summary = self.check()
        self.assertEqual(summary["deposit"]["pii_columns"].get("birthdate"), 6, result.stdout + result.stderr)
        self.assertNotEqual(result.returncode, 0, "a quoted date of birth still refuses the release")
        self.assertIn("date of birth", result.stdout + result.stderr)

    def test_a_variable_label_that_asks_the_sex_assigned_at_birth_is_not_a_birth_date(self) -> None:
        # The SPSS/Stata label path (classify_column with a label), as one audit's 'Sex' columns
        # reached it; building a .sav needs R's haven, so the classifier the reader calls is run directly.
        sys.path.insert(0, str(CHECK.parent))
        try:
            import check_deposit_values as cdv
        finally:
            sys.path.remove(str(CHECK.parent))
        self.assertIsNone(cdv.classify_column("Sex", "What sex were you assigned at birth?"))
        self.assertIsNone(cdv.classify_column("q12", "Country of birth"))
        self.assertIsNone(cdv.classify_column("q15", "Weight at birth"))
        self.assertEqual(cdv.classify_column("q16", "Birth (dd/mm/yyyy) and sex"), ("birthdate", ""))
        self.assertEqual(cdv.classify_column("q13", "Date of birth"), ("birthdate", ""))
        self.assertEqual(cdv.classify_column("q14", "Place and date of birth"), ("birthdate", ""))


if __name__ == "__main__":
    unittest.main()
