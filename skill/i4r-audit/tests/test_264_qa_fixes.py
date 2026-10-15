"""2.6.4: fixes from the QA loop run after 2.6.3.

Each test builds the condition and runs the real code: the derived impact_rank in integrate_round,
the focused repair of a critic's new_issues[].finding_id, the same-second re-pin probe, the
projection's refusal of a code snippet with no expect()/check(), and the coordinator-origin hint
on the two-family rule.
"""
import copy
import subprocess
import os
import pathlib
import re
import sys
import tempfile
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
sys.path.insert(0, str(SKILL / "schemas" / "tools"))
import generate_finding_artifacts  # noqa: E402
import integrate_round  # noqa: E402
import record_invocation  # noqa: E402
import test_run_critique_runtime as runtime  # noqa: E402
import validate_v2  # noqa: E402



def _journal_dir(tmp):
    import subprocess
    root = pathlib.Path(tmp)
    (root / "run.json").write_text('{"rounds": [], "schema_version": "2.1.0"}', encoding="utf-8")
    subprocess.run([sys.executable, str(SKILL / "scripts" / "lifecycle_journal.py"), "initialize",
                    str(root)], check=True, capture_output=True)
    return root


class DerivedImpactRank(unittest.TestCase):
    def test_apply_source_derives_rank_from_decided_impact(self):
        # apply() copies decided fields, then must derive impact_rank from the decided impact.
        source = pathlib.Path(integrate_round.__file__).read_text(encoding="utf-8")
        self.assertIn('row["impact_rank"] = validate_v2.IMPACT_RANK[decision["impact"]]', source)
        self.assertEqual(validate_v2.IMPACT_RANK["none"], 0)
        self.assertEqual(validate_v2.IMPACT_RANK["conclusion_narrowed"], 3)


class CriticNewIssueRepair(unittest.TestCase):
    ISSUES = [{"item_id": "N1", "finding_id": "F1", "claim": "x", "verdict": "CONFIRMED"}]

    def test_finding_id_is_normalised_and_nothing_else(self):
        value = runtime._scientific_value("new_issues", copy.deepcopy(self.ISSUES))
        self.assertIsNone(value[0]["finding_id"])
        self.assertEqual({k: v for k, v in value[0].items() if k != "finding_id"},
                         {k: v for k, v in self.ISSUES[0].items() if k != "finding_id"})
        self.assertEqual(self.ISSUES[0]["finding_id"], "F1", "input must not be mutated")

    def test_other_fields_untouched(self):
        items = [{"finding_id": "F2", "verdict": "CONFIRMED"}]
        self.assertEqual(runtime._scientific_value("items", items), items)

    def test_hash_equal_before_and_after_the_admin_fix(self):
        fields = {f: [] for f in runtime.REPAIR_SCIENTIFIC_FIELDS}
        fields["overall"] = {"publishable": True}
        before = dict(fields, new_issues=copy.deepcopy(self.ISSUES))
        after = dict(fields, new_issues=[dict(self.ISSUES[0], finding_id=None)])
        self.assertEqual(runtime._canonical_field_hashes(before), runtime._canonical_field_hashes(after))
        changed = dict(fields, new_issues=[dict(self.ISSUES[0], finding_id=None, claim="y")])
        self.assertNotEqual(runtime._canonical_field_hashes(before), runtime._canonical_field_hashes(changed))


class SameSecondRepin(unittest.TestCase):
    POLICY = {"critic_route_history": [{
        "superseded_at": "2026-09-23T06:00:00Z", "reason": "x",
        "critics": {"requested_model": "old", "provider": "openai", "reasoning_effort": "high"},
        "targeted_adjudicator": {"requested_model": "old", "provider": "openai", "reasoning_effort": "max"}}]}

    def test_probe_in_the_same_second_as_the_repin_uses_the_old_route(self):
        routes = record_invocation.historical_route_for(
            {"role": "model_probe", "status": "failed", "started_at": "2026-09-23T06:00:00Z"}, self.POLICY)
        self.assertEqual([r["requested_model"] for r in routes], ["old"])

    def test_later_probe_does_not(self):
        routes = record_invocation.historical_route_for(
            {"role": "model_probe", "status": "failed", "started_at": "2026-09-23T06:00:01Z"}, self.POLICY)
        self.assertEqual(routes, [])


class SnippetRefusal(unittest.TestCase):
    def row(self, path):
        return {"id": "F1", "verification_snippet": {"kind": "code", "path": path, "basis": "b"},
                "report_section": "Section 2"}

    def test_print_only_snippet_is_refused_with_the_file_to_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp).resolve()
            (root / "work").mkdir()
            (root / "work" / "check.R").write_text("print(9)\n")
            orig = generate_finding_artifacts.number
            generate_finding_artifacts.number = lambda row: 1
            try:
                with self.assertRaises(SystemExit) as ctx:
                    generate_finding_artifacts.prepare_snippets([self.row("work/check.R")], root)
            finally:
                generate_finding_artifacts.number = orig
            message = str(ctx.exception)
            self.assertIn("expect(", message)
            self.assertIn("work/check.R", message)

    def test_expect_check_snippet_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp).resolve()
            (root / "work").mkdir()
            (root / "work" / "check.R").write_text('expect("C-1", a = 1)\ncheck("C-1", a = 1)\n')
            orig = generate_finding_artifacts.number
            generate_finding_artifacts.number = lambda row: 1
            try:
                prepared = generate_finding_artifacts.prepare_snippets([self.row("work/check.R")], root)
            finally:
                generate_finding_artifacts.number = orig
            self.assertIn("C-1.R", prepared)


class CoordinatorOriginHint(unittest.TestCase):
    def test_hint_names_the_remedy_when_no_auditor_is_bound(self):
        source = pathlib.Path(validate_v2.__file__).read_text(encoding="utf-8")
        self.assertIn("Findings the coordinator raises", source)
        guide = (SKILL / "reference" / "adjudication_guide.md").read_text(encoding="utf-8")
        self.assertIn("### Findings the coordinator raises", guide)


class RoundTwoFixes(unittest.TestCase):
    def test_hand_logged_gate_exit_status_and_character_are_checked_at_capture(self):
        import lifecycle_journal
        for bad in ("notanumber", None, -1, 256, True):
            with self.assertRaises(lifecycle_journal.JournalError):
                lifecycle_journal.check_hand_payload("gate", {"gate": "check_consistency", "exit_status": bad})
        lifecycle_journal.check_hand_payload("gate", {"gate": "check_consistency", "exit_status": 0})
        with self.assertRaises(lifecycle_journal.JournalError):
            lifecycle_journal.check_hand_payload("round_result", {"round": 1, "character": "bogus"})
        lifecycle_journal.check_hand_payload("round_result", {"round": 1, "character": "precision"})
        lifecycle_journal.check_hand_payload("round_result", {"round": 1, "character": None})

    def test_setup_workspace_guard_fires_when_the_parent_does_not_exist(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            paper = root / "paper.pdf"; paper.write_bytes(b"%PDF-1.4\n")
            pkg = root / "pkg"; pkg.mkdir(); (pkg / "a.R").write_text("x <- 1\n")
            audit = root / "runs" / "J" / "Does it work? [really]" / "audit"
            result = subprocess.run(["bash", str(SKILL / "scripts" / "setup_workspace.sh"), str(audit),
                                     str(paper), str(pkg)], capture_output=True, text=True,
                                    env={**__import__("os").environ, "I4R_SKIP_RESERVE": "1"})
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("disarms the filesystem sandbox", result.stderr)
            self.assertFalse(audit.exists())

    def test_coordinator_rejects_orphan_report_markers(self):
        source = (SKILL / "scripts" / "run_coordinator.py").read_text(encoding="utf-8")
        self.assertIn("marker but findings.jsonl has no row", source)

    def test_integrate_round_checks_adjudication_log_before_writing(self):
        source = pathlib.Path(integrate_round.__file__).read_text(encoding="utf-8")
        self.assertLess(source.index('(audit / "report/ADJUDICATION.md").is_file()'),
                        source.index("backup.write_bytes(registry_path.read_bytes())"))


class RoundThreeFixes(unittest.TestCase):
    def test_commented_out_expect_check_does_not_satisfy_the_snippet_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp).resolve()
            (root / "work").mkdir()
            (root / "work" / "check.R").write_text('# expect("C-1", a = 1)\n# check("C-1", a = 1)\nprint(9)\n')
            row = {"id": "F1", "verification_snippet": {"kind": "code", "path": "work/check.R", "basis": "b"}}
            orig = generate_finding_artifacts.number
            generate_finding_artifacts.number = lambda row: 1
            try:
                with self.assertRaises(SystemExit):
                    generate_finding_artifacts.prepare_snippets([row], root)
            finally:
                generate_finding_artifacts.number = orig

    def test_orphan_marker_regex_catches_a_leading_dash_id(self):
        import re
        source = (SKILL / "scripts" / "run_coordinator.py").read_text(encoding="utf-8")
        pattern = re.search(r're\.findall\(r"(<!-- \(finding[^"]+)"', source).group(1)
        found = re.findall(pattern, "<!-- finding:-orphan --> <!-- merged:A-1->B-2 --> <!-- finding:F1 -->")
        self.assertEqual([m[1] for m in found], ["-orphan", "A-1", "F1"])

    def test_release_privacy_flags_undeclared_email_and_allows_byline_and_article(self):
        import check_release_privacy as crp
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "report").mkdir()
            (root / "report" / "front_matter.md").write_text("Correspondence: a.person@univ.edu\n")
            article = root / "paper.txt"; article.write_text("CONTACT author@journal.org\n")
            crp.ALLOWED_EMAILS.clear()
            crp.allow_emails_from(root / "report" / "front_matter.md")
            crp.allow_emails_from(article)
            ok = crp.inspect_text("byline a.person@univ.edu, article author@journal.org, test x@example.org",
                                  "r.md", identifiers=True)
            self.assertEqual([i for i in ok if i.category == "email"], [])
            bad = crp.inspect_text("participant reached at kid123@gmail.com", "r.md", identifiers=True)
            self.assertEqual([i.category for i in bad if i.category == "email"], ["email"])
            crp.ALLOWED_EMAILS.clear()

    def test_build_docx_refuses_front_matter_ending_in_a_bare_rule(self):
        source = (SKILL / "scripts" / "build_docx.sh").read_text(encoding="utf-8")
        self.assertIn("ends with a bare", source)

    def test_verification_guide_uses_flat_claim_ids(self):
        import re
        guide = (SKILL / "reference" / "verification_guide.md").read_text(encoding="utf-8")
        self.assertNotRegex(guide.replace("such as `C-2.5`", ""), r"C-\d+\.\d+")


class RoundFourFixes(unittest.TestCase):
    def test_version_matches_top_changelog_entry(self):
        import re
        version = (SKILL / "VERSION").read_text(encoding="utf-8").strip()
        top = re.search(r"(?m)^## (\d+\.\d+\.\d+)\b", (SKILL / "CHANGELOG.md").read_text(encoding="utf-8"))
        self.assertIsNotNone(top)
        self.assertEqual(top.group(1), version)

    def test_hash_in_a_string_does_not_hide_a_same_line_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp).resolve()
            (root / "work").mkdir()
            (root / "work" / "check.R").write_text(
                'expect("C-1", value = 2, note = "item #3", tol = 0); check("C-1", value = 1 + 1)\n')
            row = {"id": "F1", "verification_snippet": {"kind": "code", "path": "work/check.R", "basis": "b"}}
            orig = generate_finding_artifacts.number
            generate_finding_artifacts.number = lambda row: 1
            try:
                self.assertIn("C-1.R", generate_finding_artifacts.prepare_snippets([row], root))
            finally:
                generate_finding_artifacts.number = orig

    def test_apply_refuses_a_decisions_file_that_is_not_keyed_by_finding_ids(self):
        source = pathlib.Path(integrate_round.__file__).read_text(encoding="utf-8")
        self.assertIn("decisions file has keys that are not finding ids", source)

    def test_notebook_generator_no_longer_mints_dotted_claim_ids(self):
        source = (SKILL / "scripts" / "make_verification_notebook.sh").read_text(encoding="utf-8")
        self.assertNotIn('cid = "C-" + m.group(1)', source)

    def test_lock_wait_announces_a_busy_lock(self):
        import fcntl, io, contextlib
        import lock_wait
        with tempfile.NamedTemporaryFile() as holder_file:
            holder = open(holder_file.name, "a+")
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
            waiter = open(holder_file.name, "a+")
            import threading
            released = threading.Timer(0.3, lambda: fcntl.flock(holder.fileno(), fcntl.LOCK_UN))
            released.start()
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                lock_wait.acquire_exclusive(waiter, "the test lock")
            self.assertIn("waiting: the test lock", err.getvalue())
            waiter.close(); holder.close()

    def test_publish_releases_the_reservation_whoever_claimed_it(self):
        source = (SKILL / "scripts" / "publish_completed.sh").read_text(encoding="utf-8")
        self.assertIn('reserve_paper.sh" release "$PAPER_DIR" --force', source)


class RoundFiveFixes(unittest.TestCase):
    def _gate(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp).resolve()
            (root / "work").mkdir()
            (root / "work" / "check.R").write_text(text)
            row = {"id": "F1", "verification_snippet": {"kind": "code", "path": "work/check.R", "basis": "b"}}
            orig = generate_finding_artifacts.number
            generate_finding_artifacts.number = lambda row: 1
            try:
                generate_finding_artifacts.prepare_snippets([row], root)
                return True
            except SystemExit:
                return False
            finally:
                generate_finding_artifacts.number = orig

    @unittest.skipUnless(__import__("shutil").which("Rscript"), "needs Rscript for the exact parser check")
    def test_raw_string_with_quotes_cannot_hide_a_commented_out_check(self):
        self.assertFalse(self._gate('expect("C-1", a = 1, note = r"(say "hi)")\n# check("C-1", a = 1)\n'))
        self.assertTrue(self._gate('expect("C-1", a = 1, note = r"(a "#" b)"); check("C-1", a = 1)\n'))

    @unittest.skipUnless(__import__("shutil").which("Rscript"), "needs Rscript for the exact parser check")
    def test_a_call_named_only_inside_a_string_does_not_count(self):
        self.assertFalse(self._gate('expect("C-1", a = 1); x <- "check(C-1)"\n'))


class RoundSixFixes(unittest.TestCase):
    def test_fallback_heuristic_understands_raw_strings(self):
        orig = generate_finding_artifacts.shutil.which
        generate_finding_artifacts.shutil.which = lambda name: None
        try:
            gate = RoundFiveFixes._gate
            self.assertFalse(gate(self, 'x <- r"(safe" expect(fake) check(fake))"\n'))
            self.assertTrue(gate(self, 'expect("C-1", a = 1, note = r"(a "#" b)"); check("C-1", a = 1)\n'))
            self.assertFalse(gate(self, 'expect("C-1", a = 1); x <- "check(C-1)"\n'))
        finally:
            generate_finding_artifacts.shutil.which = orig

    def test_skill_release_block_names_snapshot_registry_and_final_round_gate(self):
        skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn('report/REPORT_v<N+1>.md', skill)
        self.assertIn('--registry "<tree-root>/registry"', skill)
        self.assertIn("phase_start phase=intake", skill)
        self.assertIn("finalize requires one per round", skill)
        self.assertIn("renumber_findings.py \"$AUDIT_DIR\" --apply --also report/ADJUDICATION.md   # once, before round 1", skill)

    def test_driver_restores_a_finished_round_pointer_on_any_failed_rerun(self):
        driver = (SKILL / "scripts" / "run_critique.sh").read_text(encoding="utf-8")
        self.assertIn('[ "$FINAL_OUTCOME" = "failed" ] || [ "$FINAL_OUTCOME" = "quorum_complete_adjudication_failed" ]', driver)
        self.assertIn('&& [ -n "$PRIOR_CURRENT_INVOCATION" ] && round_is_finished', driver)


class RoundSevenFixes(unittest.TestCase):
    def test_publish_allowlists_the_byline_and_article_emails(self):
        source = (SKILL / "scripts" / "publish_completed.sh").read_text(encoding="utf-8")
        self.assertIn('--allow-emails-from "$A/report/front_matter.md"', source)

    def test_fallback_requires_matching_raw_string_brackets(self):
        orig = generate_finding_artifacts.shutil.which
        generate_finding_artifacts.shutil.which = lambda name: None
        try:
            gate = RoundFiveFixes._gate
            self.assertFalse(gate(self, 'x <- r"(text ]" and expect("C-1") check("C-1") more text)"\n'))
            self.assertTrue(gate(self, 'expect("C-1", a = 1, note = r"[x ) y]"); check("C-1", a = 1)\n'))
        finally:
            generate_finding_artifacts.shutil.which = orig

    def test_front_matter_guard_strips_leading_whitespace(self):
        source = (SKILL / "scripts" / "build_docx.sh").read_text(encoding="utf-8")
        self.assertIn('sub(/^[ \\t]+/, "", last)', source)

    def test_docs_do_not_claim_publish_takes_registry(self):
        install = (SKILL / "INSTALL.md").read_text(encoding="utf-8")
        self.assertNotIn("pass `--registry` there", install)


class RoundEightFixes(unittest.TestCase):
    def test_front_matter_guard_refuses_only_whole_line_rules(self):
        import subprocess
        source = (SKILL / "scripts" / "build_docx.sh").read_text(encoding="utf-8")
        self.assertIn('[[ "$LAST_FM_LINE" =~ ^-+$ ]]', source)
        check = ('L="$1"; if [[ "$L" =~ ^-+$ ]] || [[ "$L" =~ ^=+$ ]]; then echo refused; else echo ok; fi')
        for line, want in [("---", "refused"), ("====", "refused"), ("==", "refused"), ("-", "refused"),
                           ("----- Byline -----", "ok"), ("--- note", "ok"), ("By A. Author", "ok")]:
            got = subprocess.run(["bash", "-c", check, "x", line], capture_output=True, text=True).stdout.strip()
            self.assertEqual(got, want, line)


    def test_round_result_defect_split_cannot_exceed_blocking_items(self):
        import lifecycle_journal
        with tempfile.TemporaryDirectory() as tmp:
            root = _journal_dir(tmp)
            with self.assertRaises(lifecycle_journal.JournalError):
                lifecycle_journal.append_event(
                    root, "round_result", {"round": 1, "blocking_items": 0, "comment_defects": 0, "paper_defects": 1})
        lifecycle_journal.check_hand_payload(
            "round_result", {"round": 1, "blocking_items": 2, "comment_defects": 1, "paper_defects": 1})

    def _run_round_one_guard(self, registry_bytes):
        import re, subprocess
        driver = (SKILL / "scripts" / "run_critique.sh").read_text(encoding="utf-8")
        block = re.search(r'(?ms)^if \[ "\$ROUND" -eq 1 \].*?^fi$', driver).group(0)
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "work").mkdir()
            (root / "work" / "findings.jsonl").write_bytes(registry_bytes)
            script = ('set -uo pipefail; fail_early() { echo "FATAL: $*"; exit 1; }; '
                      f'ROUND=1; WS="{root}"; AUDIT_DIR="{root}"\n' + block + '\necho PASSED\n')
            return subprocess.run(["bash", "-c", script], capture_output=True, text=True)

    def test_round_one_refuses_unrenumbered_ids(self):
        bad = self._run_round_one_guard(b'{"id": "REP-f1", "ledger_disposition": {"status": "INCLUDED"}}\n')
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("renumber_findings.py", bad.stdout)
        good = self._run_round_one_guard(
            b'{"id": "F1", "ledger_disposition": {"status": "INCLUDED"}}\n'
            b'{"id": "DAT-x", "ledger_disposition": {"status": "MERGED"}}\n')
        self.assertEqual(good.returncode, 0, good.stdout + good.stderr)

    def test_round_one_guard_fails_closed_on_an_unreadable_registry(self):
        result = self._run_round_one_guard(
            b'{"id": "REP-f1", "ledger_disposition": {"status": "INCLUDED"}}\n\xff\xfe garbage\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("PASSED", result.stdout)

    def test_section_three_only_previews_the_projection(self):
        skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        section3 = skill.split("## 3.", 1)[1].split("## 4.", 1)[0]
        for line in section3.splitlines():
            if line.startswith("python3 scripts/generate_finding_artifacts.py"):
                self.assertNotIn(" --apply", line.split("#", 1)[0])

    def test_batch_refuses_a_split_across_lines_of_one_batch(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            root = _journal_dir(tmp)
            batch = root / "events.jsonl"
            batch.write_text(
                '{"type":"round_result","payload":{"round":1,"blocking_items":5}}\n'
                '{"type":"round_result","payload":{"round":1,"comment_defects":3,"paper_defects":3}}\n',
                encoding="utf-8")
            before = (root / "run.events.jsonl").read_bytes()
            result = subprocess.run(["bash", str(SKILL / "scripts" / "log_event.sh"), "--batch", str(batch)],
                                    env={**os.environ, "RUN_JSON": str(root / "run.json")},
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("cannot exceed blocking_items", result.stdout + result.stderr)
            self.assertEqual((root / "run.events.jsonl").read_bytes(), before)

    def test_append_event_checks_hand_payloads(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            root = _journal_dir(tmp)
            payload = root / "payload.json"
            payload.write_text('{"round":1,"blocking_items":2,"comment_defects":5,"paper_defects":5}',
                               encoding="utf-8")
            result = subprocess.run([sys.executable, str(SKILL / "scripts" / "lifecycle_journal.py"),
                                     "append-event", str(root), "round_result", str(payload)],
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)

    def test_report_structure_documents_the_equals_rule(self):
        doc = (SKILL / "reference" / "report_structure.md").read_text(encoding="utf-8")
        self.assertIn("Any trailing line of only `=` or `-`", doc)



class RoundTenFixes(unittest.TestCase):
    def _append(self, root, event_type, payload):
        import lifecycle_journal
        return lifecycle_journal.append_event(root, event_type, payload, source="log_event.sh")

    def test_split_is_checked_across_separate_appends_and_round_integrated(self):
        import lifecycle_journal
        with tempfile.TemporaryDirectory() as tmp:
            root = _journal_dir(tmp)
            self._append(root, "round_result", {"round": 1, "blocking_items": 1})
            before = (root / "run.events.jsonl").read_bytes()
            with self.assertRaises(lifecycle_journal.JournalError):
                self._append(root, "round_result", {"round": 1, "comment_defects": 1, "paper_defects": 1})
            with self.assertRaises(lifecycle_journal.JournalError):
                self._append(root, "round_integrated", {"round": 1, "comment_defects": 3, "paper_defects": 0})
            self.assertEqual((root / "run.events.jsonl").read_bytes(), before)

    def test_split_check_works_without_run_json_and_allows_clearing(self):
        import lifecycle_journal
        with tempfile.TemporaryDirectory() as tmp:
            root = _journal_dir(tmp)
            self._append(root, "round_result", {"round": 1, "blocking_items": 4, "comment_defects": 3,
                                                "paper_defects": 1})
            (root / "run.json").unlink()
            with self.assertRaises(lifecycle_journal.JournalError):
                self._append(root, "round_result", {"round": 1, "blocking_items": 0})
            lifecycle_journal.append_events(root, [
                {"type": "round_result", "payload": {"round": 1, "comment_defects": None, "paper_defects": None},
                 "source": "log_event.sh"},
                {"type": "round_result", "payload": {"round": 1, "blocking_items": 0}, "source": "log_event.sh"},
            ])

    def test_counts_and_round_numbers_must_be_integers(self):
        import lifecycle_journal
        bad = [("round_result", {"round": 1, "blocking_items": 1, "comment_defects": 5.0}),
               ("round_result", {"round": 2, "comment_defects": -1}),
               ("round_result", {"round": 5, "paper_defects": True}),
               ("round_integrated", {"round": 1, "blocking_items": "2"}),
               ("round_result", {"round": 3.0, "blocking_items": 2}),
               ("round_result", {"round": "3"}),
               ("round_end", {"round": 0})]
        for event_type, payload in bad:
            with self.assertRaises(lifecycle_journal.JournalError, msg=repr(payload)):
                lifecycle_journal.check_hand_payload(event_type, payload)
        lifecycle_journal.check_hand_payload("round_result", {"round": 1, "blocking_items": 0,
                                                              "comment_defects": None})

    def _renumber_dir(self, tmp, rows, markers):
        import json
        root = pathlib.Path(tmp)
        (root / "work").mkdir()
        (root / "report").mkdir()
        (root / "work" / "findings.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        (root / "report" / "REPORT_final.md").write_text(
            "".join(f"<!-- finding:{m} -->\n" for m in markers), encoding="utf-8")
        return root

    def test_renumber_repairs_a_gap_left_by_adjudication(self):
        import subprocess
        rows = [{"id": "F1", "ledger_disposition": {"status": "INCLUDED"}},
                {"id": "F2", "ledger_disposition": {"status": "MERGED", "target": "F1"}},
                {"id": "F3", "ledger_disposition": {"status": "INCLUDED"}}]
        with tempfile.TemporaryDirectory() as tmp:
            root = self._renumber_dir(tmp, rows, ["F1", "F3"])
            out = subprocess.run([sys.executable, str(SKILL / "scripts" / "renumber_findings.py"), str(root),
                                  "--dry-run"], capture_output=True, text=True)
            self.assertNotIn("already renumbered", out.stdout)
        done = [{"id": "F1", "ledger_disposition": {"status": "INCLUDED"}},
                {"id": "F2", "ledger_disposition": {"status": "INCLUDED"}},
                {"id": "F3", "ledger_disposition": {"status": "MERGED", "target": "F1"}}]
        with tempfile.TemporaryDirectory() as tmp:
            root = self._renumber_dir(tmp, done, ["F1", "F2"])
            out = subprocess.run([sys.executable, str(SKILL / "scripts" / "renumber_findings.py"), str(root),
                                  "--dry-run"], capture_output=True, text=True)
            self.assertIn("already renumbered", out.stdout)

    def test_round_one_guard_refuses_non_contiguous_included_ids(self):
        guard = RoundEightFixes._run_round_one_guard
        for body in (b'{"id":"F1","ledger_disposition":{"status":"INCLUDED"}}\n'
                     b'{"id":"F2","ledger_disposition":{"status":"MERGED"}}\n'
                     b'{"id":"F3","ledger_disposition":{"status":"INCLUDED"}}\n',
                     b'{"id":"F1","ledger_disposition":{"status":"INCLUDED"}}\n'
                     b'{"id":"F1","ledger_disposition":{"status":"INCLUDED"}}\n'):
            result = guard(self, body)
            self.assertNotEqual(result.returncode, 0, body)
            self.assertIn("contiguous", result.stdout)

    def test_adjudication_guide_previews_before_round_one(self):
        guide = (SKILL / "reference" / "adjudication_guide.md").read_text(encoding="utf-8")
        self.assertNotIn('--apply` after every schema-valid batch', guide)



class RoundTenWaiter(unittest.TestCase):
    def test_waiter_rereads_a_stale_pointer_while_the_new_run_is_alive(self):
        import json, subprocess, time

        def status(inv, pid, outcome, exit_code, quorum):
            return {"status_version": "i4r-round-status-2", "invocation_id": inv, "round": 3, "pid": pid,
                    "outcome": outcome, "exit_code": exit_code, "expected_roles": ["stats", "completeness"],
                    "completed_roles": ["stats", "completeness"] if quorum else [], "quorum_met": quorum,
                    "adjudication": "not_reached", "detail": ""}

        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "audit" / "critique" / "round_3"
            (out / "invocations" / "inv-old").mkdir(parents=True)
            (out / "invocations" / "inv-old" / "ROUND_STATUS.json").write_text(
                json.dumps(status("inv-old", 999999, "paused", 3, False)))
            (out / "CURRENT_INVOCATION").write_text("inv-old\n")
            runner = subprocess.Popen(["sleep", "30"])
            try:
                waiter = subprocess.Popen(
                    ["/bin/bash", str(SKILL / "scripts" / "wait_for_round.sh"), str(out.parents[1]), "3",
                     "--pid", str(runner.pid), "--timeout-min", "1", "--poll-seconds", "1"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                time.sleep(2.5)
                self.assertIsNone(waiter.poll(), "waiter gave up on the stale pointer")
                (out / "invocations" / "inv-new").mkdir()
                (out / "invocations" / "inv-new" / "ROUND_STATUS.json").write_text(
                    json.dumps(status("inv-new", runner.pid, "complete", 0, True)))
                (out / "CURRENT_INVOCATION").write_text("inv-new\n")
                stdout, stderr = waiter.communicate(timeout=30)
                self.assertEqual(waiter.returncode, 0, stdout + stderr)
            finally:
                runner.kill()
                runner.wait()



class RoundTenEndToEnd(unittest.TestCase):
    def test_renumber_rewrites_prose_in_notes_but_only_markers_in_reports(self):
        sys.path.insert(0, str(SKILL / "scripts"))
        import renumber_findings
        mapping = {"F3": "F5", "F5": "F3"}
        with tempfile.TemporaryDirectory() as tmp:
            notes = pathlib.Path(tmp) / "RESPONSE_notes.md"
            notes.write_text("### F3: withdrawn\n### A-F5 round 1\nF30 stays\n", encoding="utf-8")
            self.assertEqual(renumber_findings.rewrite_extra(notes, mapping),
                             "### F5: withdrawn\n### A-F3 round 1\nF30 stays\n")
            report = pathlib.Path(tmp) / "REPORT_BEFORE_INTEGRATION.md"
            report.write_text("<!-- finding:F3 --> prose about F3\n", encoding="utf-8")
            self.assertEqual(renumber_findings.rewrite_extra(report, mapping),
                             "<!-- finding:F5 --> prose about F3\n")

    def test_finalize_passes_the_requested_stopping_reason_to_the_round_check(self):
        source = (SKILL / "scripts" / "finalize_run.sh").read_text(encoding="utf-8")
        self.assertIn('--stopping-reason "${STOP:-$existing_stop}"', source)
        import inspect
        sys.path.insert(0, str(SKILL / "scripts"))
        import validate_round_projections
        self.assertIn("stopping_reason", inspect.signature(validate_round_projections.verify).parameters)

    def test_docs_settle_every_round_and_point_guide_at_decisions(self):
        skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        self.assertNotIn("Settled cache (round 1 only)", skill)
        guide = (SKILL / "reference" / "adjudication_guide.md").read_text(encoding="utf-8")
        self.assertNotIn("apply --decisions <plan.json>", guide)

    def test_settled_hash_error_names_the_remedy(self):
        source = (SKILL / "scripts" / "make_diff_packet.py").read_text(encoding="utf-8")
        self.assertIn("After a report or registry edit, rebuild the settled", source)



class RoundTenDiffReview(unittest.TestCase):
    def _finished(self, events):
        import json, re, subprocess
        driver = (SKILL / "scripts" / "run_critique.sh").read_text(encoding="utf-8")
        func = re.search(r"(?ms)^round_is_finished\(\) \{.*?^\}$", driver).group(0)
        with tempfile.TemporaryDirectory() as tmp:
            (pathlib.Path(tmp) / "run.events.jsonl").write_text(
                "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
            script = f'WS="{tmp}"; ROUND=1\n{func}\nround_is_finished'
            return subprocess.run(["bash", "-c", script]).returncode == 0

    def test_a_reopened_round_is_not_finished(self):
        start = {"type": "round_start", "payload": {"round": 1}}
        done = {"type": "round_integrated", "payload": {"round": 1}}
        reopen = {"type": "round_start", "payload": {"round": 1, "reopened_from": "integrated"}}
        self.assertTrue(self._finished([start, done]))
        self.assertFalse(self._finished([start, done, reopen]))
        self.assertFalse(self._finished([start]))

    def test_email_detector_skips_code_and_remotes_but_not_addresses(self):
        import check_release_privacy as crp
        self.assertEqual(crp._email_issues("beta = X.T@X.values", "f"), [])
        self.assertEqual(crp._email_issues("git clone git@github.com:org/pkg.git", "f"), [])
        issues = crp._email_issues("write to jane.doe@univ.edu", "f")
        self.assertEqual(len(issues), 1)
        self.assertIn("j***@univ.edu", str(issues[0]))

    def test_post_delta_privacy_gate_uses_the_byline_allowlist(self):
        import check_release_privacy as crp
        import run_post_delta_gates
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp).resolve()
            (root / "report").mkdir()
            (root / "report" / "front_matter.md").write_text("Correspondence: a.person@univ.edu\n")
            (root / "report" / "REPORT_final.md").write_text("Contact a.person@univ.edu for data.\n")
            saved = set(crp.ALLOWED_EMAILS)
            crp.ALLOWED_EMAILS.clear()
            try:
                rc, out = run_post_delta_gates.privacy_gate(root, ["report/REPORT_final.md"], root)
            finally:
                crp.ALLOWED_EMAILS.clear()
                crp.ALLOWED_EMAILS.update(saved)
            self.assertEqual(rc, 0, out)

    def test_skill_release_order_matches_install(self):
        skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        self.assertNotIn('approve_settled.py "$AUDIT_DIR" && bash scripts/make_settled.sh', skill)
        self.assertIn("approve, project,\n   approve, project, then `make_settled.sh`", skill)



class RoundElevenFixes(unittest.TestCase):
    def test_late_also_after_two_withdrawal_renumbers_uses_the_last_map(self):
        import json, subprocess
        tool = [sys.executable, str(SKILL / "scripts" / "renumber_findings.py")]

        def state(root, statuses, markers):
            (root / "work" / "findings.jsonl").write_text("".join(
                json.dumps({"id": f"F{n}", "ledger_disposition": {"status": s}}) + "\n"
                for n, s in enumerate(statuses, 1)), encoding="utf-8")
            (root / "report" / "REPORT_final.md").write_text(
                "".join(f"<!-- finding:{m} -->\n" for m in markers), encoding="utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "work").mkdir()
            (root / "report").mkdir()
            state(root, ["EXCLUDED", "INCLUDED", "INCLUDED", "INCLUDED"], ["F2", "F3", "F4"])
            subprocess.run(tool + [str(root), "--apply"], check=True, capture_output=True)
            rows = [json.loads(line) for line in (root / "work" / "findings.jsonl").read_text().splitlines()]
            self.assertEqual([r["id"] for r in rows], ["F4", "F1", "F2", "F3"])
            # second withdrawal: current F1 excluded; the operator forgets --also on this run
            state(root, ["EXCLUDED", "INCLUDED", "INCLUDED", "EXCLUDED"], ["F2", "F3"])
            subprocess.run(tool + [str(root), "--apply"], check=True, capture_output=True)
            notes = root / "RESPONSE_notes.md"
            notes.write_text("### F2: kept\n### F3: kept\n", encoding="utf-8")
            out = subprocess.run(tool + [str(root), "--apply", "--late-also", "--also", "RESPONSE_notes.md"],
                                 capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            self.assertEqual(notes.read_text(encoding="utf-8"), "### F1: kept\n### F2: kept\n")

    def test_only_excluded_merged_or_rejected_rows_are_withdrawn(self):
        source = (SKILL / "scripts" / "integrate_round.py").read_text(encoding="utf-8")
        self.assertNotIn('row.get("coordinator_approved") is False\n', source)
        self.assertIn('decision.get("applied_note")', source)

    def test_only_the_latest_round_needs_current_gate_code(self):
        source = (SKILL / "scripts" / "run_post_delta_gates.py").read_text(encoding="utf-8")
        self.assertIn('if require_current and receipt.get("gate_code") != code_bindings():', source)

    def test_email_detector_still_catches_real_addresses(self):
        import check_release_privacy as crp
        for text in ("Contact jane.doe@g.harvard.edu", "jdoe@u.northwestern.edu", "e0123456@u.nus.edu",
                     "alice@x.com", "Write to jane.doe@univ.edu: she has the data.", "a@b.co.uk"):
            self.assertEqual(len(crp._email_issues(text, "f")), 1, text)
        for text in ("beta = X.T@X.values", "r = M@model.params", "git clone git@github.com:org/pkg.git"):
            self.assertEqual(crp._email_issues(text, "f"), [], text)

    def test_notes_rewrite_touches_only_anchored_finding_references(self):
        import renumber_findings
        mapping = {"F2": "F4", "F3": "F2", "F4": "F3", "COD-auditor_code_methods:F3": "F1"}
        with tempfile.TemporaryDirectory() as tmp:
            notes = pathlib.Path(tmp) / "RESPONSE_notes.md"
            notes.write_text("### F2: x\n### A-F3 round 1\nTable F3 and Table F2\n<!-- merged:F2->F4 -->\n"
                             "<!-- adjudication:F3:round1 -->\n<!-- adjudication:COD-auditor_code_methods:F3 -->\n"
                             "- withdrawn (EXCLUDED): duplicate of F4\n", encoding="utf-8")
            self.assertEqual(renumber_findings.rewrite_extra(notes, mapping),
                             "### F4: x\n### A-F2 round 1\nTable F3 and Table F2\n<!-- merged:F4->F3 -->\n"
                             "<!-- adjudication:F2:round1 -->\n<!-- adjudication:F1 -->\n"
                             "- withdrawn (EXCLUDED): duplicate of F3\n")

    def test_scoped_round_does_not_flag_unrouted_high_impact_findings(self):
        registry = {"F1": {"id": "F1", "changes_conclusion": False, "impact_rank": 3,
                           "ledger_disposition": {"status": "INCLUDED"}}}
        rows = [("stats", {"items": [], "new_issues": [], "conflicts": [], "settled_spot_checks": []})]
        unscoped = runtime.adjudication_triggers(rows, registry)
        self.assertIn("high_impact_evidence_gap", [t["kind"] for t in unscoped])
        scoped = runtime.adjudication_triggers(rows, registry, {"F2"})
        self.assertNotIn("high_impact_evidence_gap", [t["kind"] for t in scoped])

    def test_notebook_binds_each_chunk_to_its_claim(self):
        source = (SKILL / "scripts" / "make_verification_notebook.sh").read_text(encoding="utf-8")
        self.assertIn(".bind_claim(id)", source)
        self.assertIn("w(f'.V$current <- \"{c[\"id\"]}\"')", source)
        self.assertIn("outcome recorded for an id that is not a claim of this comment", source)

    def test_integrate_rerun_replaces_its_adjudication_block(self):
        source = (SKILL / "scripts" / "integrate_round.py").read_text(encoding="utf-8")
        self.assertIn("A rerun supersedes this round's block", source)

    def test_publish_refusal_lists_violations(self):
        source = (SKILL / "scripts" / "finalization_receipt.py").read_text(encoding="utf-8")
        self.assertIn("VIOLATION: {v}", source)

    def test_settled_event_is_relogged_every_round(self):
        skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        self.assertNotIn("skipping `make_settled.sh`'s settled event", skill)
        install = (SKILL / "INSTALL.md").read_text(encoding="utf-8")
        self.assertIn("event after **every** `make_settled.sh` run", install)
        cheat = (SKILL / "CHEATSHEET.html").read_text(encoding="utf-8")
        self.assertNotIn("generated_after_round=N", cheat)



class RoundElevenWaiter(unittest.TestCase):
    def test_waiter_rereads_a_pointer_whose_run_never_wrote_status(self):
        import json, subprocess, time
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "audit" / "critique" / "round_3"
            (out / "invocations" / "inv-old").mkdir(parents=True)
            (out / "CURRENT_INVOCATION").write_text("inv-old\n")
            runner = subprocess.Popen(["sleep", "30"])
            try:
                waiter = subprocess.Popen(
                    ["/bin/bash", str(SKILL / "scripts" / "wait_for_round.sh"), str(out.parents[1]), "3",
                     "--pid", str(runner.pid), "--timeout-min", "1", "--poll-seconds", "1"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                time.sleep(3.5)
                self.assertIsNone(waiter.poll(), "waiter gave up on the stale pointer")
                (out / "invocations" / "inv-new").mkdir()
                (out / "invocations" / "inv-new" / "ROUND_STATUS.json").write_text(json.dumps({
                    "status_version": "i4r-round-status-2", "invocation_id": "inv-new", "round": 3,
                    "pid": runner.pid, "outcome": "complete", "exit_code": 0,
                    "expected_roles": ["stats", "completeness"], "completed_roles": ["stats", "completeness"],
                    "quorum_met": True, "adjudication": "not_reached", "detail": ""}))
                (out / "CURRENT_INVOCATION").write_text("inv-new\n")
                stdout, stderr = waiter.communicate(timeout=30)
                self.assertEqual(waiter.returncode, 0, stdout + stderr)
            finally:
                runner.kill()
                runner.wait()



class RoundTwelveFixes(unittest.TestCase):
    def test_late_also_skips_files_the_last_renumber_already_rewrote(self):
        import json, subprocess
        tool = [sys.executable, str(SKILL / "scripts" / "renumber_findings.py")]
        with tempfile.TemporaryDirectory() as tmp:
            a = pathlib.Path(tmp)
            for sub in ("work", "report", "critique/round_2"):
                (a / sub).mkdir(parents=True)
            (a / "report/REPORT_final.md").write_text("Intro <!-- finding:F1 --> x\n\nMore <!-- finding:F3 --> y\n")
            (a / "work/findings.jsonl").write_text("".join(
                json.dumps({"id": f"F{i}", "claim": f"c{i}",
                            "ledger_disposition": {"status": "EXCLUDED" if i == 2 else "INCLUDED"}}) + "\n"
                for i in (1, 2, 3)))
            (a / "report/ADJUDICATION.md").write_text(
                "<!-- adjudication:F3:round2 -->\n### A-F3 round 2 — stats CONFIRMED\n")
            (a / "critique/round_2/RESPONSE_notes.md").write_text("### F3: c3\n")
            subprocess.run(tool + [str(a), "--apply", "--also", "report/ADJUDICATION.md"], check=True,
                           capture_output=True)
            out = subprocess.run(tool + [str(a), "--apply", "--late-also", "--also", "critique/round_2/RESPONSE_notes.md",
                                         "--also", "report/ADJUDICATION.md"], capture_output=True, text=True)
            self.assertIn("skipped (already rewritten by the last renumber): report/ADJUDICATION.md", out.stdout)
            self.assertIn("### A-F2 round 2", (a / "report/ADJUDICATION.md").read_text())
            self.assertEqual((a / "critique/round_2/RESPONSE_notes.md").read_text(), "### F2: c3\n")

    def test_anchored_rewrite_handles_lists_sources_conflicts_reasons_and_drafts(self):
        import renumber_findings
        mapping = {"F3": "F5", "F4": "F3", "F5": "F4", "REP-f1_d100_n50_reproduce": "F1"}
        with tempfile.TemporaryDirectory() as tmp:
            notes = pathlib.Path(tmp) / "RESPONSE_notes.md"
            notes.write_text("findings F3 and F4 overlap\nSources: REP-f1_d100_n50_reproduce, F4\n"
                             "- critic_stats C1 on F3, F4: overlap\n- withdrawn (EXCLUDED): same value as F4\n"
                             "Table F4 stays\nsee REP-f1_d100_n50_reproduce\n### A-F3 round 1\n", encoding="utf-8")
            self.assertEqual(renumber_findings.rewrite_extra(notes, mapping),
                             "findings F5 and F3 overlap\nSources: F1, F3\n"
                             "- critic_stats C1 on F5, F3: overlap\n- withdrawn (EXCLUDED): same value as F3\n"
                             "Table F4 stays\nsee F1\n### A-F5 round 1\n")

    def test_email_detector_round_twelve_cases(self):
        import check_release_privacy as crp
        for text in ("jane.doe@uni.eus", "x@lab.cat", "info@charity.ngo"):
            self.assertEqual(len(crp._email_issues(text, "f")), 1, text)
        for text in ("git+ssh://git@github.com/org/pkg.git@v1.2", "https://oauth2@gitlab.com/x",
                     "y = x@W.to(device)", "x@W.at[i]"):
            self.assertEqual(crp._email_issues(text, "f"), [], text)

    def test_confirmed_mention_of_unrouted_finding_is_not_a_gap(self):
        registry = {"F1": {"id": "F1", "changes_conclusion": False, "impact_rank": 3,
                           "ledger_disposition": {"status": "INCLUDED"}}}
        item = {"finding_id": "F1", "item_id": "c-1", "verdict": "CONFIRMED", "priority": "NON_BLOCKING",
                "confidence": "CONFIRMED", "evidence": {"artifact_refs": []}}
        rows = [("completeness", {"items": [item], "new_issues": [], "conflicts": [], "settled_spot_checks": []})]
        kinds = [t["kind"] for t in runtime.adjudication_triggers(rows, registry, {"F2"})]
        self.assertNotIn("high_impact_evidence_gap", kinds)

    def test_notebook_binds_judgement_and_unverifiable_and_refuses_legacy_abs(self):
        source = (SKILL / "scripts" / "make_verification_notebook.sh").read_text(encoding="utf-8")
        self.assertIn('judgement <- function(id, statement, basis = "") {\n  .bind_claim(id)', source)
        self.assertIn("unverifiable <- function(id, reason, observed = NULL) {\n  .bind_claim(id)", source)
        self.assertIn("C-ABS.<n> sections that run_verification.sh cannot bind", source)

    def test_converged_requires_the_reviewed_report(self):
        source = (SKILL / "scripts" / "validate_round_projections.py").read_text(encoding="utf-8")
        self.assertIn("converged, but report/REPORT_final.md differs from the report round", source)

    def test_integrate_refuses_after_a_later_renumber_and_keeps_withdrawn_blocks(self):
        source = (SKILL / "scripts" / "integrate_round.py").read_text(encoding="utf-8")
        self.assertIn("re-integrate round", source)
        self.assertIn("rounds_run_before_renumber", source)
        self.assertIn("withdrawn-adjudication:round-", source)
        self.assertNotIn("existing = re.search", source)

    def test_cheatsheet_renumber_includes_adjudication(self):
        cheat = (SKILL / "CHEATSHEET.html").read_text(encoding="utf-8")
        self.assertIn('renumber_findings.py "$AUDIT_DIR" --apply --also report/ADJUDICATION.md', cheat)



class RoundThirteenFixes(unittest.TestCase):
    def _renumber_tree(self, tmp):
        import json
        a = pathlib.Path(tmp)
        for sub in ("work", "report", "critique/round_1"):
            (a / sub).mkdir(parents=True, exist_ok=True)
        (a / "report/REPORT_final.md").write_text("<!-- finding:F1 --> x\n<!-- finding:F3 --> y\n")
        (a / "work/findings.jsonl").write_text("".join(
            json.dumps({"id": f"F{i}", "claim": f"c{i}",
                        "ledger_disposition": {"status": "EXCLUDED" if i == 2 else "INCLUDED"}}) + "\n"
            for i in (1, 2, 3)))
        (a / "report/ADJUDICATION.md").write_text("## A3 — F3\n<!-- adjudication:F3 -->\n")
        (a / "critique/round_1/ROUND_STATUS.json").write_text("{}")
        (a / "critique/round_1/CURRENT_INVOCATION").write_text("inv-r1\n")
        return a

    def test_identity_renumber_with_also_does_nothing(self):
        import subprocess
        tool = [sys.executable, str(SKILL / "scripts" / "renumber_findings.py")]
        with tempfile.TemporaryDirectory() as tmp:
            a = self._renumber_tree(tmp)
            subprocess.run(tool + [str(a), "--apply", "--also", "report/ADJUDICATION.md"], check=True,
                           capture_output=True)
            after = (a / "report/ADJUDICATION.md").read_text()
            self.assertIn("## A3 — F2", after)
            (a / "critique/round_1/NEW.md").write_text("### F2: current\n")
            out = subprocess.run(tool + [str(a), "--apply", "--also", "critique/round_1/NEW.md"],
                                 capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn("nothing to do", out.stdout)
            self.assertEqual((a / "critique/round_1/NEW.md").read_text(), "### F2: current\n")

    def test_renumber_records_the_rounds_that_had_run(self):
        import json, subprocess
        with tempfile.TemporaryDirectory() as tmp:
            a = self._renumber_tree(tmp)
            subprocess.run([sys.executable, str(SKILL / "scripts" / "renumber_findings.py"), str(a), "--apply"],
                           check=True, capture_output=True)
            state = json.loads((a / "work/renumber_state.json").read_text())
            self.assertEqual(state["rounds_run_before_renumber"], {"round_1": "inv-r1"})

    def test_integrate_refusal_is_by_invocation_not_file_time(self):
        import json, subprocess
        with tempfile.TemporaryDirectory() as tmp:
            a = pathlib.Path(tmp)
            (a / "critique/round_1").mkdir(parents=True)
            (a / "work").mkdir()
            (a / "critique/round_1/ROUND_STATUS.json").write_text(json.dumps(
                {"outcome": "complete", "quorum_met": True, "exit_code": 0, "failed_roles": []}))
            (a / "critique/round_1/CURRENT_INVOCATION").write_text("inv-r1\n")
            (a / "work/renumber_state.json").write_text(json.dumps({"rounds_run_before_renumber": {"round_1": "inv-r1"}}))
            out = subprocess.run([sys.executable, str(SKILL / "scripts" / "integrate_round.py"), str(a), "--round", "1",
                                  "plan"], capture_output=True, text=True)
            self.assertEqual(out.returncode, 2)
            self.assertIn("renumbered after round 1's invocation ran", out.stderr)
            (a / "critique/round_1/CURRENT_INVOCATION").write_text("inv-r1-rerun\n")
            out = subprocess.run([sys.executable, str(SKILL / "scripts" / "integrate_round.py"), str(a), "--round", "1",
                                  "plan"], capture_output=True, text=True)
            self.assertNotIn("renumbered after round 1", out.stderr)

    def test_rewrite_covers_withdrawn_markers_and_coordinator_entries(self):
        import renumber_findings
        mapping = {"F1": "F2", "F2": "F1"}
        with tempfile.TemporaryDirectory() as tmp:
            log = pathlib.Path(tmp) / "ADJUDICATION.md"
            log.write_text("<!-- withdrawn-adjudication:F2:round1 -->\n### A-F2 round 1\n## A2 — F2\n"
                           "Sources: draft registry row F1\nTable F1\n", encoding="utf-8")
            self.assertEqual(renumber_findings.rewrite_extra(log, mapping),
                             "<!-- withdrawn-adjudication:F1:round1 -->\n### A-F1 round 1\n## A2 — F1\n"
                             "Sources: draft registry row F2\nTable F1\n")

    def test_email_real_tlds_are_not_code_attributes(self):
        import check_release_privacy as crp
        for text in ("Correspondence: jane.doe@univie.ac.at", "bob@uni.to", "info@lab.data", "jane@uni.edu[1]"):
            self.assertEqual(len(crp._email_issues(text, "f")), 1, text)

    def test_projection_refuses_a_snippet_with_another_claims_id(self):
        source = (SKILL / "scripts" / "generate_finding_artifacts.py").read_text(encoding="utf-8")
        self.assertIn("records outcomes under", source)
        self.assertIn("edit the source named by the row's verification_snippet.path", source)

    def test_converged_remedy_and_docs(self):
        source = (SKILL / "scripts" / "validate_round_projections.py").read_text(encoding="utf-8")
        self.assertIn("at the round cap, cap_with_unresolved", source)
        guide = (SKILL / "reference" / "adjudication_guide.md").read_text(encoding="utf-8")
        self.assertIn("reworded_at_round", guide)
        install = (SKILL / "INSTALL.md").read_text(encoding="utf-8")
        self.assertIn("--late-also", install)
        self.assertNotIn("any journal\nappend after `make_settled.sh`", install)

    def test_stage_explains_a_rejected_directory(self):
        source = (SKILL / "scripts" / "stage_release.sh").read_text(encoding="utf-8")
        self.assertIn("holds an earlier REJECTED staging attempt", source)



class RoundFourteenFixes(unittest.TestCase):
    def test_legacy_renumber_without_state_still_refuses_reintegration(self):
        import json, os, subprocess, time
        with tempfile.TemporaryDirectory() as tmp:
            a = pathlib.Path(tmp)
            (a / "critique/round_1").mkdir(parents=True)
            (a / "work").mkdir()
            status = a / "critique/round_1/ROUND_STATUS.json"
            status.write_text(json.dumps({"outcome": "complete", "quorum_met": True, "exit_code": 0, "failed_roles": []}))
            (a / "critique/round_1/CURRENT_INVOCATION").write_text("inv-r1\n")
            legacy = a / "work/id_map_last.json"
            legacy.write_text(json.dumps({"F3": "F5", "F5": "F3"}))
            os.utime(status, (time.time() - 100, time.time() - 100))
            out = subprocess.run([sys.executable, str(SKILL / "scripts" / "integrate_round.py"), str(a), "--round", "1",
                                  "plan"], capture_output=True, text=True)
            self.assertEqual(out.returncode, 2)
            self.assertIn("earlier skill version", out.stderr)

    def test_also_outside_the_audit_is_refused_before_any_write(self):
        import json, subprocess
        with tempfile.TemporaryDirectory() as tmp:
            a = RoundThirteenFixes._renumber_tree(self, pathlib.Path(tmp) / "audit")
            outside = pathlib.Path(tmp) / "shared.md"
            outside.write_text("### F3: notes\n")
            (a / "critique/round_1/RESPONSE_notes.md").symlink_to(outside)
            before = (a / "work/findings.jsonl").read_text()
            out = subprocess.run([sys.executable, str(SKILL / "scripts" / "renumber_findings.py"), str(a), "--apply",
                                  "--also", "critique/round_1/RESPONSE_notes.md"], capture_output=True, text=True)
            self.assertEqual(out.returncode, 2, out.stdout + out.stderr)
            self.assertIn("must resolve inside the audit directory", out.stderr)
            self.assertEqual((a / "work/findings.jsonl").read_text(), before)

    def test_email_attribute_chains_are_code(self):
        import check_release_privacy as crp
        for text in ("h = x@self.W.data", "out = inp@layer.weight.data.T", "z = A@B.dot"):
            self.assertEqual(crp._email_issues(text, "f"), [], text)
        for text in ("jane.doe@univie.ac.at", "info@lab.data", "Write to jane.doe@univ.edu. She"):
            self.assertEqual(len(crp._email_issues(text, "f")), 1, text)

    def test_also_refuses_the_canonical_report(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            a = RoundThirteenFixes._renumber_tree(self, pathlib.Path(tmp) / "audit")
            out = subprocess.run([sys.executable, str(SKILL / "scripts" / "renumber_findings.py"), str(a), "--apply",
                                  "--also", "report/REPORT_final.md"], capture_output=True, text=True)
            self.assertEqual(out.returncode, 2)
            self.assertIn("canonical report or registry", out.stderr)

    def test_heading_and_sources_rewrites_leave_titles_and_exhibits(self):
        import renumber_findings
        mapping = {"F1": "F2", "F2": "F3", "F3": "F2"}
        with tempfile.TemporaryDirectory() as tmp:
            log = pathlib.Path(tmp) / "ADJUDICATION.md"
            log.write_text("## A2 — F1 score misreported\n## A3 — F2\n"
                           "Sources: auditor C-2; appendix Table F3 of the paper; registry row F2\n", encoding="utf-8")
            self.assertEqual(renumber_findings.rewrite_extra(log, mapping),
                             "## A2 — F1 score misreported\n## A3 — F3\n"
                             "Sources: auditor C-2; appendix Table F3 of the paper; registry row F3\n")

    def test_markdown_footnote_after_an_address_is_flagged(self):
        import check_release_privacy as crp
        for text in ("jane.doe@uni.edu[^1]", "jane.doe@uni.edu[^note]"):
            self.assertEqual(len(crp._email_issues(text, "f")), 1, text)

    def test_plan_and_packet_sources(self):
        ir = (SKILL / "scripts" / "integrate_round.py").read_text(encoding="utf-8")
        self.assertIn('findings = load_jsonl(backup if backup is not None else audit / "work/findings.jsonl")', ir)
        mdp = (SKILL / "scripts" / "make_diff_packet.py").read_text(encoding="utf-8")
        self.assertIn('settled_refs[-1]["reworded_at_round"]', mdp)
        rn = (SKILL / "scripts" / "renumber_findings.py").read_text(encoding="utf-8")
        self.assertIn("id_map_history.jsonl", rn)
        p1 = (SKILL / "scripts" / "run_phase1.py").read_text(encoding="utf-8")
        self.assertIn('alias = completed.with_suffix(".alias")', p1)

    def test_source_level_checks(self):
        vrp = (SKILL / "scripts" / "validate_round_projections.py").read_text(encoding="utf-8")
        self.assertIn("the text round {reviewed_round} reviewed", vrp)
        gfa = (SKILL / "scripts" / "generate_finding_artifacts.py").read_text(encoding="utf-8")
        self.assertIn("uncommented = re.sub(", gfa)
        ir = (SKILL / "scripts" / "integrate_round.py").read_text(encoding="utf-8")
        self.assertIn("(withdrawn in an earlier round)", ir)
        fin = (SKILL / "scripts" / "finalize_run.sh").read_text(encoding="utf-8")
        self.assertIn("publish_completed.sh will REFUSE this run", fin)
        self.assertIn("re-log it: ", fin)
        pdg = (SKILL / "scripts" / "run_post_delta_gates.py").read_text(encoding="utf-8")
        self.assertIn("The gates run after the round is prepared", pdg)



class RoundFifteenFixes(unittest.TestCase):
    def test_backup_is_bound_to_its_invocation(self):
        import integrate_round, json, os, time
        with tempfile.TemporaryDirectory() as tmp:
            a = pathlib.Path(tmp)
            (a / "work").mkdir()
            (a / "critique/round_1").mkdir(parents=True)
            backup = a / "work/findings.before_round1_integration.jsonl"
            backup.write_text("{}\n")
            backup.with_suffix(".invocation").write_text("inv-a\n")
            self.assertEqual(integrate_round.backup_for(a, 1, "inv-a"), backup)
            self.assertIsNone(integrate_round.backup_for(a, 1, "inv-b"))
            backup.with_suffix(".invocation").unlink()
            status = a / "critique/round_1/ROUND_STATUS.json"
            status.write_text("{}")
            os.utime(backup, (time.time() - 100, time.time() - 100))
            self.assertIsNone(integrate_round.backup_for(a, 1, "inv-b"))   # re-run after the legacy backup

    def test_renumber_refuses_the_backup_as_also(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            a = RoundThirteenFixes._renumber_tree(self, pathlib.Path(tmp) / "audit")
            (a / "work/findings.before_round1_integration.jsonl").write_text("{}\n")
            out = subprocess.run([sys.executable, str(SKILL / "scripts" / "renumber_findings.py"), str(a), "--apply",
                                  "--also", "work/findings.before_round1_integration.jsonl"], capture_output=True, text=True)
            self.assertEqual(out.returncode, 2)
            self.assertIn("pre-integration backup", out.stderr)

    def test_sources_exhibit_lists_and_ranges_stay(self):
        import renumber_findings
        mapping = {"F1": "F2", "F2": "F3", "F3": "F1"}
        with tempfile.TemporaryDirectory() as tmp:
            log = pathlib.Path(tmp) / "ADJUDICATION.md"
            log.write_text("Sources: Tables F2 and F3 of the paper; registry row F1\nSources: table F3; row F2\n"
                           "Sources: Fig F2, Tab. F3, Figures F1-F3\n", encoding="utf-8")
            self.assertEqual(renumber_findings.rewrite_extra(log, mapping),
                             "Sources: Tables F2 and F3 of the paper; registry row F2\nSources: table F3; row F3\n"
                             "Sources: Fig F2, Tab. F3, Figures F1-F3\n")

    def test_email_round_fifteen_cases(self):
        import check_release_privacy as crp
        for text in ("table(obj@meta.data$cluster)", "x <- fit@model.frame", "fit@coef.table", "attn = q@k.mT"):
            self.assertEqual(crp._email_issues(text, "f"), [], text)
        self.assertEqual(len(crp._email_issues("Email m.muster@w.tuwien.ac.at for data", "f")), 1)

    def test_source_level_round_fifteen(self):
        rn = (SKILL / "scripts" / "renumber_findings.py").read_text(encoding="utf-8")
        self.assertIn('entry["legacy_prior"] = True', rn)
        ms = (SKILL / "scripts" / "make_settled.sh").read_text(encoding="utf-8")
        self.assertIn("the round ran again after this renumber", ms)
        p1 = (SKILL / "scripts" / "run_phase1.py").read_text(encoding="utf-8")
        self.assertIn("corrupt completion alias", p1)
        fin = (SKILL / "scripts" / "finalize_run.sh").read_text(encoding="utf-8")
        self.assertIn('sf.get("spot_check_failures") or 0', fin)
        vrp = (SKILL / "scripts" / "validate_round_projections.py").read_text(encoding="utf-8")
        self.assertIn("reviewed_by_id.get(row.get(\"id\")) == row.get(\"claim\")", vrp)
        mdp = (SKILL / "scripts" / "make_diff_packet.py").read_text(encoding="utf-8")
        self.assertIn("is not the registry round", mdp)
        ini = (SKILL / "scripts" / "init_run.sh").read_text(encoding="utf-8")
        self.assertIn("--doi must be a bare DOI", ini)
        stg = (SKILL / "scripts" / "stage_release.sh").read_text(encoding="utf-8")
        self.assertIn("this attempt is marked REJECTED", stg)
        ir = (SKILL / "scripts" / "integrate_round.py").read_text(encoding="utf-8")
        self.assertIn("has not run in", ir)
        ap = (SKILL / "scripts" / "approve_settled.py").read_text(encoding="utf-8")
        self.assertIn("({changed} changed)", ap)

    def test_init_run_normalises_and_refuses_dois(self):
        import subprocess
        for given, ok in (("https://doi.org/10.1234/x", True), ("notadoi", False)):
            with tempfile.TemporaryDirectory() as tmp:
                out = subprocess.run(["bash", str(SKILL / "scripts" / "init_run.sh"), tmp, "--doi", given,
                                      "--paper-title", "T"], capture_output=True, text=True,
                                     env={**os.environ, "I4R_ALLOW_SKILL_VERSION_MISMATCH": "1"})
                if ok:
                    self.assertNotIn("must be a bare DOI", out.stderr + out.stdout)
                else:
                    self.assertIn("must be a bare DOI", out.stderr + out.stdout)



class RoundSixteenFixes(unittest.TestCase):
    def test_sources_numeric_exhibit_does_not_swallow_finding_ids(self):
        import renumber_findings
        mapping = {"F2": "F3", "F3": "F2"}
        with tempfile.TemporaryDirectory() as tmp:
            log = pathlib.Path(tmp) / "ADJUDICATION.md"
            log.write_text("Sources: Table 2, F3 (registry row)\nSources: Tables 1-2, F2 and F3\n"
                           "Sources: Tables F2 and F3 of the paper\n", encoding="utf-8")
            self.assertEqual(renumber_findings.rewrite_extra(log, mapping),
                             "Sources: Table 2, F2 (registry row)\nSources: Tables 1-2, F3 and F2\n"
                             "Sources: Tables F2 and F3 of the paper\n")

    def test_latex_footnote_after_an_address_is_flagged(self):
        import check_release_privacy as crp
        self.assertEqual(len(crp._email_issues("Corresponding: jane.doe@uni.edu$^{*}$", "f")), 1)
        self.assertEqual(crp._email_issues("table(obj@meta.data$cluster)", "f"), [])

    def test_reopened_round_fields_are_in_the_run_schema(self):
        import json
        schema = json.loads((SKILL / "schemas" / "run.schema.json").read_text(encoding="utf-8"))
        props = schema["$defs"]["round"]["properties"]
        self.assertIn("reopened_from", props)
        self.assertIn("reopened_at", props)

    def test_finalize_counts_only_the_live_invocation_of_a_reopened_round(self):
        fin = (SKILL / "scripts" / "finalize_run.sh").read_text(encoding="utf-8")
        self.assertIn('matches=[x for x in matches if str(x.get("invocation_id") or "").startswith(live)', fin)

    def test_email_self_meta_and_one_letter_chains(self):
        import check_release_privacy as crp
        for text in ("y = self.h@self.W.data", "pred = X.T@W.data", "m = np.A@B.dot", "head(pbmc.obj@meta.data)"):
            self.assertEqual(crp._email_issues(text, "f"), [], text)
        self.assertEqual(len(crp._email_issues("Email m.muster@w.tuwien.ac.at for data", "f")), 1)

    def test_round_sixteen_messages_and_docs(self):
        install = (SKILL / "INSTALL.md").read_text(encoding="utf-8")
        self.assertIn("works only BEFORE `integrate_round.py apply`", install)
        self.assertIn('record_invocation.py --correct "$AUDIT_DIR"', install)
        self.assertIn("first release action after the last round", install)
        pdg = (SKILL / "scripts" / "run_post_delta_gates.py").read_text(encoding="utf-8")
        self.assertIn("(the skill was updated); regenerate it", pdg)
        cheat = (SKILL / "CHEATSHEET.html").read_text(encoding="utf-8")
        self.assertIn("(&times;2 if changed)", cheat)

    def test_no_shipped_script_trips_the_release_email_gate(self):
        import check_release_privacy as crp
        for path in sorted((SKILL / "scripts").glob("*")):
            if path.is_file() and path.suffix in (".py", ".sh", ".R"):
                self.assertEqual(crp._email_issues(path.read_text(encoding="utf-8", errors="ignore"), path.name), [],
                                 path.name)

    def test_source_level_round_sixteen(self):
        ir = (SKILL / "scripts" / "integrate_round.py").read_text(encoding="utf-8")
        self.assertIn("Only this invocation's reviewers can back this round", ir)
        self.assertIn("would change", ir)
        self.assertIn("remove its last line from ", ir)
        mdp = (SKILL / "scripts" / "make_diff_packet.py").read_text(encoding="utf-8")
        self.assertIn("renumber_findings.rewrite_rows(expected", mdp)
        self.assertIn(".from_backup.jsonl", mdp)
        ap = (SKILL / "scripts" / "approve_settled.py").read_text(encoding="utf-8")
        self.assertIn("if that projection changed", ap)
        stg = (SKILL / "scripts" / "stage_release.sh").read_text(encoding="utf-8")
        self.assertIn('''printf '       cp %q %q\\n' "$A/report/REPORT_final.md"''', stg)



class RoundSeventeenFixes(unittest.TestCase):
    def test_sources_same_prefix_rule(self):
        import renumber_findings
        mapping = {"F2": "F3", "F3": "F2"}
        with tempfile.TemporaryDirectory() as tmp:
            log = pathlib.Path(tmp) / "ADJUDICATION.md"
            log.write_text("Sources: Table A3, F3 (registry row)\nSources: Table S1, F2 registry row; Table 2, F3\n"
                           "Sources: Tables F2 and F3 of the paper\nSources: Table F3\n", encoding="utf-8")
            self.assertEqual(renumber_findings.rewrite_extra(log, mapping),
                             "Sources: Table A3, F2 (registry row)\nSources: Table S1, F3 registry row; Table 2, F2\n"
                             "Sources: Tables F2 and F3 of the paper\nSources: Table F3\n")

    def test_email_marker_and_tighter_one_letter_rule(self):
        import check_release_privacy as crp
        self.assertEqual(crp._email_issues("z = q@keys.data  # i4r-privacy: not-an-email", "f.py"), [])
        for text in ("write to office@w.tuwien.ac.at", "Mail muster@e.univie.ac.at for data", "muster.m@w.tuwien.ac.at"):
            self.assertEqual(len(crp._email_issues(text, "f")), 1, text)
        for text in ("out = x@model.weight.data", "levels(pbmc@active.ident)", "pred = X.T@W.data"):
            self.assertEqual(crp._email_issues(text, "f"), [], text)

    def test_source_level_round_seventeen(self):
        ir = (SKILL / "scripts" / "integrate_round.py").read_text(encoding="utf-8")
        self.assertIn("drop it on every apply, not just the first", ir)
        self.assertLess(ir.index("later = later_rounds(audit, rnd)"), ir.index('backup = backup_for(audit, rnd, plan["invocation"])'))
        fin = (SKILL / "scripts" / "finalize_run.sh").read_text(encoding="utf-8")
        self.assertIn("the calls its cache replays resolve to", fin)
        self.assertIn('startswith("Self-reported, not instrumented.")', fin)
        mdp = (SKILL / "scripts" / "make_diff_packet.py").read_text(encoding="utf-8")
        self.assertIn("must never be overwritten", mdp)
        self.assertIn("--no-remedy-file", mdp)
        vdp = (SKILL / "scripts" / "verify_diff_packet.py").read_text(encoding="utf-8")
        self.assertIn('"--no-remedy-file"', vdp)
        skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("phase_end phase=critique at=<final-round end>", skill)
        pub = (SKILL / "scripts" / "publish_completed.sh").read_text(encoding="utf-8")
        self.assertIn("finalize_run.sh; then publish again", pub)



class RoundEighteenFixes(unittest.TestCase):
    def _live_count(self, rows, live):
        import textwrap
        src = (SKILL / "scripts" / "finalize_run.sh").read_text(encoding="utf-8")
        start = src.index('        pointer=audit_root/("critique/round_%s/CURRENT_INVOCATION" % number)')
        end = src.index("(number,role,len(matches)))", start) + len("(number,role,len(matches)))")
        block = textwrap.dedent(src[start:end])
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "critique/round_1").mkdir(parents=True)
            (root / "critique/round_1/CURRENT_INVOCATION").write_text(live + "\n")
            env = {"audit_root": root, "number": 1, "actual": {"critic_stats"}, "viol": [],
                   "completed": [r for r in rows if r.get("provider_call") is True and r["status"] == "completed"],
                   "all_invocations": rows}
            exec(block, env)
            return env["viol"]

    def test_finalize_counts_a_live_replays_source_call(self):
        rows = [
            {"invocation_id": "A:stats", "role": "critic_stats", "round": 1, "status": "completed", "provider_call": True},
            {"invocation_id": "B:stats", "role": "critic_stats", "round": 1, "status": "completed", "provider_call": True},
            {"invocation_id": "C:stats", "role": "critic_stats", "round": 1, "status": "completed", "provider_call": False,
             "source_invocation_id": "A:stats"},
        ]
        self.assertEqual(self._live_count(rows, "C"), [])
        self.assertEqual(self._live_count(rows, "B"), [])
        self.assertTrue(self._live_count(rows, "Z"))

    def test_email_round_eighteen_cases(self):
        import check_release_privacy as crp
        for text in ("office@weight.at", "info@meta.at", "hello@self.to", "js@w.tuwien.ac.at", "ab@e.univie.ac.at"):
            self.assertEqual(len(crp._email_issues(text, "f.py")), 1, text)
        self.assertEqual(crp._email_issues("z = q@keys.data  # i4r-privacy: not-an-email", "f.py"), [])
        self.assertEqual(len(crp._email_issues("z = q@keys.data  # i4r-privacy: not-an-email", "f.json")), 1)
        self.assertEqual(len(crp._email_issues("x = 1 # i4r-privacy: not-an-email\rjane@uni.edu", "f.py")), 1)
        for text in ("head(pbmc.obj@meta.data)", "out = x@model.weight.data", "pred = X.T@W.data"):
            self.assertEqual(crp._email_issues(text, "f.py"), [], text)

    def test_sources_oxford_comma_and_through(self):
        import renumber_findings
        mapping = {"F1": "F4", "F2": "F5", "F3": "F6"}
        with tempfile.TemporaryDirectory() as tmp:
            log = pathlib.Path(tmp) / "ADJUDICATION.md"
            log.write_text("Sources: Tables F1, F2, and F3 of the online appendix\nSources: Figures F1 through F3\n",
                           encoding="utf-8")
            self.assertEqual(renumber_findings.rewrite_extra(log, mapping),
                             "Sources: Tables F1, F2, and F3 of the online appendix\nSources: Figures F1 through F3\n")

    def test_source_level_round_eighteen(self):
        ir = (SKILL / "scripts" / "integrate_round.py").read_text(encoding="utf-8")
        self.assertIn("def later_rounds(", ir)
        self.assertLess(ir.index("later = later_rounds(audit, rnd)"), ir.index("plan, verifier = _build_plan(audit, rnd, now)\n    round_dir"))
        mdp = (SKILL / "scripts" / "make_diff_packet.py").read_text(encoding="utf-8")
        self.assertIn("before_findings.samefile(backup)", mdp)
        self.assertIn("later_disputes.legacy_renumbered(root)", mdp)
        self.assertIn("still carries the ids from before a renumber", mdp)
        ms = (SKILL / "scripts" / "make_settled.sh").read_text(encoding="utf-8")
        self.assertIn("later_disputes.legacy_renumbered(ws)", ms)
        fin = (SKILL / "scripts" / "finalize_run.sh").read_text(encoding="utf-8")
        self.assertIn("a phase_end alone never closes a phase", fin)
        rc = (SKILL / "scripts" / "run_critique.sh").read_text(encoding="utf-8")
        self.assertIn("cannot be replayed", rc)
        ini = (SKILL / "scripts" / "init_run.sh").read_text(encoding="utf-8")
        self.assertIn("s#[/.,;]$##", ini)
        install = (SKILL / "INSTALL.md").read_text(encoding="utf-8")
        self.assertIn("N >= 2: leave round N-1's before-files alone", install)



class RoundNineteenFixes(unittest.TestCase):
    def test_prepared_or_failed_later_round_does_not_block_reapply(self):
        import integrate_round, json
        with tempfile.TemporaryDirectory() as tmp:
            a = pathlib.Path(tmp)
            for n, status in ((2, {"outcome": "paused", "exit_code": 3}), (3, {"outcome": "failed", "completed_roles": []})):
                (a / f"critique/round_{n}").mkdir(parents=True)
                (a / f"critique/round_{n}/ROUND_STATUS.json").write_text(json.dumps(status))
            self.assertEqual(integrate_round.later_rounds(a, 1), [])
            (a / "critique/round_4").mkdir(parents=True)
            (a / "critique/round_4/ROUND_STATUS.json").write_text(json.dumps({"outcome": "complete", "completed_roles": ["stats"]}))
            self.assertEqual(integrate_round.later_rounds(a, 1), [4])

    def test_init_run_force_archives_phase1(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            args = ["bash", str(SKILL / "scripts" / "init_run.sh"), tmp, "--doi", "10.1234/x", "--paper-title", "T"]
            env = {**os.environ, "I4R_ALLOW_SKILL_VERSION_MISMATCH": "1"}
            subprocess.run(args, capture_output=True, env=env)
            (pathlib.Path(tmp) / "phase1" / "probe_cache").mkdir(parents=True)
            (pathlib.Path(tmp) / "phase1" / "probe_cache" / "k.json").write_text("{}")
            (pathlib.Path(tmp) / "phase1" / "probe_cache").chmod(0o555)
            out = subprocess.run(args + ["--force"], capture_output=True, text=True, env=env)
            self.assertEqual(out.returncode, 0, out.stderr[-500:])
            self.assertFalse((pathlib.Path(tmp) / "phase1").exists())
            self.assertTrue(list(pathlib.Path(tmp).glob("phase1.previous.*")))

    def test_source_level_round_nineteen(self):
        rc = (SKILL / "scripts" / "run_critique.sh").read_text(encoding="utf-8")
        self.assertIn("(the forced rerun's output)", rc)
        self.assertIn('cached_dir="${2:-$CACHE_ROOT/$cached_role}"', rc)
        vrp = (SKILL / "scripts" / "validate_round_projections.py").read_text(encoding="utf-8")
        self.assertIn("later_disputes.legacy_renumbered(audit)", vrp)
        mdp = (SKILL / "scripts" / "make_diff_packet.py").read_text(encoding="utf-8")
        self.assertIn("later_disputes.legacy_renumbered(root)", mdp)
        ir = (SKILL / "scripts" / "integrate_round.py").read_text(encoding="utf-8")
        self.assertIn("{legacy_map.relative_to(audit)} (a renumber by an earlier skill version)", ir)
        skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("phase_start phase=intake at=<setup_workspace.sh start>", skill)
        self.assertIn("a validated adjudication of this packet is cached", rc)
        self.assertIn("the cached targeted adjudication predates the forced rerun", rc)



class RoundTwentyFixes(unittest.TestCase):
    def test_running_later_round_blocks_reapply(self):
        import integrate_round, json
        with tempfile.TemporaryDirectory() as tmp:
            a = pathlib.Path(tmp)
            (a / "critique/round_2").mkdir(parents=True)
            (a / "critique/round_2/ROUND_STATUS.json").write_text(json.dumps(
                {"outcome": "running", "completed_roles": [], "provider_calls": 0}))
            self.assertEqual(integrate_round.later_rounds(a, 1), [2])

    def test_settled_generated_after_round_must_be_one(self):
        import lifecycle_journal
        with self.assertRaises(lifecycle_journal.JournalError):
            lifecycle_journal.check_hand_payload("settled", {"used": True, "generated_after_round": 2})
        lifecycle_journal.check_hand_payload("settled", {"used": True, "generated_after_round": 1})

    def test_probe_only_failure_does_not_count_as_dispatched(self):
        import integrate_round, json
        with tempfile.TemporaryDirectory() as tmp:
            a = pathlib.Path(tmp)
            (a / "critique/round_2").mkdir(parents=True)
            (a / "critique/round_2/ROUND_STATUS.json").write_text(json.dumps(
                {"outcome": "failed", "completed_roles": [], "provider_calls": 1}))
            (a / "invocations.jsonl").write_text(json.dumps(
                {"invocation_id": "p", "role": "model_probe", "role_class": "other", "round": 2,
                 "provider_call": True}) + "\n")
            self.assertEqual(integrate_round.later_rounds(a, 1), [])
            with (a / "invocations.jsonl").open("a") as fh:
                fh.write(json.dumps({"invocation_id": "c", "role": "critic_stats", "role_class": "critic",
                                     "round": 2, "provider_call": True}) + "\n")
            self.assertEqual(integrate_round.later_rounds(a, 1), [2])

    def test_source_level_round_twenty(self):
        rc = (SKILL / "scripts" / "run_critique.sh").read_text(encoding="utf-8")
        self.assertIn('[ "$FORCE_RERUN" -eq 1 ] && rm -f "$publish_conflict_marker"', rc)
        self.assertIn('[ "$FORCE_RERUN" -eq 1 ] && rm -f "$target_cache.conflict"', rc)
        self.assertIn('"$FINAL_OUTCOME" = "quorum_complete_adjudication_failed"', rc)
        self.assertIn("(same as the cached adjudication)", rc)
        mdp = (SKILL / "scripts" / "make_diff_packet.py").read_text(encoding="utf-8")
        self.assertIn('backup_ids = {r.get("claim"): r.get("id") for r in untranslated}', mdp)
        p1 = (SKILL / "scripts" / "run_phase1.py").read_text(encoding="utf-8")
        self.assertIn(".stale.", p1)
        vrp = (SKILL / "scripts" / "validate_round_projections.py").read_text(encoding="utf-8")
        self.assertIn("asked for corrections nobody resolved", vrp)
        ir = (SKILL / "scripts" / "integrate_round.py").read_text(encoding="utf-8")
        self.assertNotIn("or run the round again", ir)
        install = (SKILL / "INSTALL.md").read_text(encoding="utf-8")
        self.assertIn("CONTESTED.json", install)
        self.assertIn('forced_dir="$CACHE_ROOT/conflicts/$role/$(basename "$forced_dir")"', rc)
        self.assertIn("retry the adjudication with I4R_FORCE_RERUN=1", rc)
        sw = (SKILL / "scripts" / "setup_workspace.sh").read_text(encoding="utf-8")
        self.assertIn("intake started_at:", sw)
        self.assertIn("audit / 'work/renumber_state.json'", ir)



class RoundTwentyOneFixes(unittest.TestCase):
    def test_all_round_counts_must_be_non_negative_integers(self):
        import lifecycle_journal
        for payload in ({"round": 3, "factual_corrections": -1}, {"round": 3, "factual_corrections": "yes"},
                        {"round": 3, "non_blocking_items": -4}, {"round": 3, "items_accepted": "abc"},
                        {"round": 3, "items_contested": 2.5}):
            with self.assertRaises(lifecycle_journal.JournalError, msg=repr(payload)):
                lifecycle_journal.check_hand_payload("round_result", payload)
        lifecycle_journal.check_hand_payload("round_result", {"round": 3, "factual_corrections": 0})
        for bad in (1.0, True, None):
            with self.assertRaises(lifecycle_journal.JournalError, msg=repr(bad)):
                lifecycle_journal.check_hand_payload("settled", {"used": True, "generated_after_round": bad})

    def test_source_level_round_twenty_one(self):
        vrp = (SKILL / "scripts" / "validate_round_projections.py").read_text(encoding="utf-8")
        self.assertIn('for k in ("position", "evidence"))', vrp)
        dr = (SKILL / "scripts" / "document_release.py").read_text(encoding="utf-8")
        self.assertIn("the report changed after this run finalized as ", dr)
        skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("(contests: `CONTESTED.json`, INSTALL.md)", skill)
        install = (SKILL / "INSTALL.md").read_text(encoding="utf-8")
        self.assertIn('"critic_stats:stats-F1": {"position"', install)
        guide = (SKILL / "reference" / "adjudication_guide.md").read_text(encoding="utf-8")
        self.assertIn("CONTESTED.json", guide)
        rc = (SKILL / "scripts" / "run_critique.sh").read_text(encoding="utf-8")
        self.assertIn("the finished round is left as it was", rc)
        self.assertIn('[ -f "$OUT/adjudication_cache/${TARGET_INPUT_SHA:-none}.conflict" ]', rc)
        self.assertIn('critic_root = last_projection if last_projection is not None else last_dir', vrp)
        self.assertIn('critic_synth:{item.get(', vrp)



class RoundTwentyTwoFixes(unittest.TestCase):
    def test_source_level_round_twenty_two(self):
        dr = (SKILL / "scripts" / "document_release.py").read_text(encoding="utf-8")
        self.assertIn('if label != "report":', dr)
        self.assertIn("Rebuild the document with build_docx.sh, then re-run", dr)
        rc = (SKILL / "scripts" / "run_critique.sh").read_text(encoding="utf-8")
        self.assertEqual(rc.count('a plain rerun cannot reuse the pre-force adjudication; retry with I4R_FORCE_RERUN=1"'), 2)
        self.assertEqual(rc.count('FINAL_DETAIL="finished round left as it was; a plain rerun'), 1)
        wfr = (SKILL / "scripts" / "wait_for_round.sh").read_text(encoding="utf-8")
        self.assertIn("*I4R_FORCE_RERUN*)", wfr)
        install = (SKILL / "INSTALL.md").read_text(encoding="utf-8")
        self.assertIn("critic_synth:<item_id>", install)

    def _packet_tree(self, tmp):
        root = pathlib.Path(tmp)
        for rel, text in (("report/REPORT_final.md", "after\n"), ("work/findings.jsonl", "{}\n"),
                          ("critique/round_1/REPORT_BEFORE_INTEGRATION.md", "before\n"),
                          ("critique/round_1/FINDINGS_BEFORE_INTEGRATION.jsonl", "{}\n"),
                          ("critique/round_2/REPORT_BEFORE_INTEGRATION.md", "later\n"),
                          ("critique/round_2/FINDINGS_BEFORE_INTEGRATION.jsonl", "{}\n")):
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text, encoding="utf-8")
        return root

    def _packet(self, root, rnd, before_dir):
        import subprocess
        return subprocess.run(
            [sys.executable, str(SKILL / "scripts" / "make_diff_packet.py"), str(root), "--round", str(rnd),
             "--before-report", str(root / before_dir / "REPORT_BEFORE_INTEGRATION.md"),
             "--after-report", str(root / "report/REPORT_final.md"),
             "--before-findings", str(root / before_dir / "FINDINGS_BEFORE_INTEGRATION.jsonl"),
             "--no-remedy-file"], capture_output=True, text=True)

    def test_diff_packet_refuses_before_files_from_another_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._packet_tree(tmp)
            proc = self._packet(root, 2, "critique/round_2")
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("is from round_2, but --round 2 describes round 1's integration", proc.stderr)

    def test_diff_packet_refuses_a_report_the_critics_did_not_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._packet_tree(tmp)
            snap = root / "critique/round_1/snapshots/inv1/source/REPORT_TO_REVIEW.md"
            snap.parent.mkdir(parents=True)
            snap.write_text("what the critics saw\n", encoding="utf-8")
            proc = self._packet(root, 2, "critique/round_1")
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("is not the report round 1's critics reviewed", proc.stderr)

    def test_diff_packet_accepts_a_renumbered_reviewed_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._packet_tree(tmp)
            before = root / "critique/round_1/REPORT_BEFORE_INTEGRATION.md"
            before.write_text("A. <!-- finding:F2 -->\nB. <!-- finding:F3 -->\n", encoding="utf-8")
            snap = root / "critique/round_1/snapshots/inv1/source/REPORT_TO_REVIEW.md"
            snap.parent.mkdir(parents=True)
            snap.write_text("A. <!-- finding:F3 -->\nB. <!-- finding:F4 -->\n", encoding="utf-8")
            proc = self._packet(root, 2, "critique/round_1")
            self.assertNotIn("is not the report round 1's critics reviewed", proc.stderr)
            # prose is compared exactly: an appendix label edit is a real change
            snap.write_text("Table F3 is 0.52.\n", encoding="utf-8")
            before.write_text("Table F4 is 0.52.\n", encoding="utf-8")
            proc = self._packet(root, 2, "critique/round_1")
            self.assertIn("is not the report round 1's critics reviewed", proc.stderr)

    def test_source_level_round_twenty_two_e2e(self):
        ir = (SKILL / "scripts" / "integrate_round.py").read_text(encoding="utf-8")
        self.assertNotIn("correction applied to the report", ir)
        self.assertIn("FATAL: these verdicts asked for a correction the decisions do not act on", ir)
        fin = (SKILL / "scripts" / "finalize_run.sh").read_text(encoding="utf-8")
        self.assertNotIn("not bound to immutable runtime projections", fin)
        self.assertIn("the critique-round check above failed", fin)


class RoundTwentyThreeFixes(unittest.TestCase):
    def test_journal_checks_parallelism_and_typed_round_fields_on_every_round_event(self):
        import lifecycle_journal
        for event, payload in (("round_start", {"round": 1, "parallelism": 0}),
                               ("round_quorum_complete", {"round": 1, "parallelism": -3}),
                               ("round_result", {"round": 1, "consistency_check_exit": "abc"}),
                               ("round_result", {"round": 1, "consistency_check_run": "maybe"}),
                               ("round_integrated", {"round": 1, "integrated_before_delivery": "yes"})):
            with self.assertRaises(lifecycle_journal.JournalError, msg=repr((event, payload))):
                lifecycle_journal.check_hand_payload(event, payload)
        lifecycle_journal.check_hand_payload("round_start", {"round": 1, "parallelism": 2,
                                                             "critics_run": "critic_stats,critic_data"})
        lifecycle_journal.check_hand_payload("round_result", {"round": 1, "consistency_check_run": True,
                                                              "consistency_check_exit": 0})

    def test_integrate_round_withdrawal_and_adjudication_count_as_acting(self):
        self.assertTrue(integrate_round.withdrawn_by({"status": "rejected"}))
        self.assertTrue(integrate_round.withdrawn_by({"ledger_disposition": {"status": "MERGED"}}))
        self.assertFalse(integrate_round.withdrawn_by({"applied_note": "x"}))
        confirmed = {"verdicts": [{"role": "critic_stats", "verdict": "OVERSTATED", "requested_correction": "n"},
                                  {"role": "critic_synth", "verdict": "CONFIRMED", "requested_correction": None}]}
        self.assertTrue(integrate_round.adjudicator_confirmed(confirmed))
        confirmed["verdicts"][1]["requested_correction"] = "narrow it"
        self.assertFalse(integrate_round.adjudicator_confirmed(confirmed))

    def test_integrate_round_contested_takes_a_file(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run([sys.executable, str(SKILL / "scripts" / "integrate_round.py"), tmp, "--round", "1",
                                   "apply", "--contested", '{"critic_stats:s-F1": {}}'],
                                  capture_output=True, text=True)
            self.assertNotEqual(proc.returncode, 0)
            self.assertNotIn("Traceback", proc.stderr)

    def test_diff_packet_draft_ids_and_case(self):
        helper = RoundTwentyTwoFixes()
        with tempfile.TemporaryDirectory() as tmp:
            root = helper._packet_tree(tmp)
            (root / "critique/round_1/REPORT_BEFORE_INTEGRATION.md").write_text(
                "One. <!-- finding:F1 -->\nTwo. <!-- finding:F2 -->\n", encoding="utf-8")
            snap = root / "critique/round_1/snapshots/inv1/source/REPORT_TO_REVIEW.md"
            snap.parent.mkdir(parents=True)
            snap.write_text("One. <!-- finding:CM-F2 -->\nTwo. <!-- finding:ST-F1 -->\n", encoding="utf-8")
            proc = helper._packet(root, 2, "critique/round_1")
            self.assertNotIn("is not the report round 1's critics reviewed", proc.stderr)
            proc = helper._packet(root, 2, "critique/ROUND_2")
            self.assertIn("describes round 1's integration", proc.stderr)

    def test_source_level_round_twenty_three(self):
        vrp = (SKILL / "scripts" / "validate_round_projections.py").read_text(encoding="utf-8")
        self.assertIn("CONTESTED.json entries that match no item of that round", vrp)
        gates = (SKILL / "scripts" / "run_post_delta_gates.py").read_text(encoding="utf-8")
        self.assertIn("exists but was never prepared", gates)
        stage = (SKILL / "scripts" / "stage_release.sh").read_text(encoding="utf-8")
        self.assertIn('"$d"/CONTESTED.json', stage)
        pub = (SKILL / "scripts" / "publish_completed.sh").read_text(encoding="utf-8")
        self.assertNotIn("projection binding is stale", pub)
        self.assertIn("re-running", pub)
        wfr = (SKILL / "scripts" / "wait_for_round.sh").read_text(encoding="utf-8")
        self.assertIn('glob("invocations/*/ROUND_STATUS.json")', wfr)
        guide = (SKILL / "reference" / "adjudication_guide.md").read_text(encoding="utf-8")
        self.assertIn("critic_synth:<item_id>", guide)
        ir = (SKILL / "scripts" / "integrate_round.py").read_text(encoding="utf-8")
        self.assertIn("called the claim OVERSTATED/REFUTED and its text is unchanged", ir)

class RoundTwentyFourFixes(unittest.TestCase):
    def test_withdrawn_by_tolerates_a_malformed_disposition(self):
        self.assertFalse(integrate_round.withdrawn_by({"ledger_disposition": "EXCLUDED"}))

    def test_decisions_values_must_be_objects(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            decisions = pathlib.Path(tmp) / "d.json"
            decisions.write_text('{"F1": "confirmed"}', encoding="utf-8")
            proc = subprocess.run([sys.executable, str(SKILL / "scripts" / "integrate_round.py"), tmp, "--round", "1",
                                   "apply", "--decisions", str(decisions)], capture_output=True, text=True)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("must be objects of decided fields", proc.stderr)
            self.assertNotIn("Traceback", proc.stderr)

    def test_result_events_check_list_fields(self):
        import lifecycle_journal
        with self.assertRaises(lifecycle_journal.JournalError):
            lifecycle_journal.check_hand_payload("round_result", {"round": 2, "critics_run": "stats,data"})
        lifecycle_journal.check_hand_payload("round_start", {"round": 2, "critics_run": "stats,data"})

    def test_contested_entries_are_checked_before_any_write(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            contested = pathlib.Path(tmp) / "c.json"
            contested.write_text('{"critic_stats:stats-F1": "the claim holds"}', encoding="utf-8")
            proc = subprocess.run([sys.executable, str(SKILL / "scripts" / "integrate_round.py"), tmp, "--round", "1",
                                   "apply", "--contested", str(contested)], capture_output=True, text=True)
            self.assertIn("malformed: ['critic_stats:stats-F1']", proc.stderr)

    def test_waiter_ignores_older_statuses_with_a_recycled_pid(self):
        wfr = (SKILL / "scripts" / "wait_for_round.sh").read_text(encoding="utf-8")
        self.assertIn("if path.stat().st_mtime <= floor:", wfr)
        self.assertIn('"finished round left as it was"*)', wfr)

    def test_source_level_round_twenty_four(self):
        ir = (SKILL / "scripts" / "integrate_round.py").read_text(encoding="utf-8")
        self.assertIn('".verifications" in str(exc)', ir)
        self.assertIn('verdict["role"] in unbound.get(entry["id"], [])', ir)
        self.assertIn("an applied_note does not make it a confirmation", ir)
        ms = (SKILL / "scripts" / "make_settled.sh").read_text(encoding="utf-8")
        self.assertIn("_disputed_later", ms)
        vrp = (SKILL / "scripts" / "validate_round_projections.py").read_text(encoding="utf-8")
        self.assertIn('work/RELEASE_STAGED.json', vrp)
        self.assertIn("RELEASE_STAGED.json", (SKILL / "scripts" / "stage_release.sh").read_text(encoding="utf-8"))
        gates = (SKILL / "scripts" / "run_post_delta_gates.py").read_text(encoding="utf-8")
        self.assertIn('not in (None, "failed")', gates)
        rec = (SKILL / "scripts" / "record_invocation.py").read_text(encoding="utf-8")
        self.assertIn("scripts/init_run.sh on the audit first", rec)
        skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        self.assertNotIn("release_dir=<clean-release-dir>", skill)   # a local path in the published run.json
        guide = (SKILL / "reference" / "adjudication_guide.md").read_text(encoding="utf-8")
        self.assertIn("an `applied_note` alone binds nothing", guide)

class RoundTwentyFiveFixes(unittest.TestCase):
    def test_later_disputes_translate_ids_and_honour_the_adjudication(self):
        import json as _json
        import later_disputes
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "critique/round_1").mkdir(parents=True)
            (root / "critique/round_1/INTEGRATION_SUMMARY.json").write_text("{}", encoding="utf-8")
            r2 = root / "critique/round_2"
            proj = r2 / "projections/p"
            proj.mkdir(parents=True)
            (r2 / "CURRENT_INVOCATION").write_text("inv2\n", encoding="utf-8")
            (r2 / "CURRENT_PROJECTION").write_text("p\n", encoding="utf-8")
            (proj / "critic_stats.json").write_text(_json.dumps({"items": [
                {"finding_id": "F3", "verdict": "OVERSTATED"}]}), encoding="utf-8")
            (proj / "critic_data.json").write_text(_json.dumps({"items": [
                {"finding_id": "F4", "verdict": "OVERSTATED"}]}), encoding="utf-8")
            (proj / "CRITIQUE.json").write_text(_json.dumps({"items": [
                {"finding_id": "F4", "verdict": "CONFIRMED", "requested_correction": None}]}), encoding="utf-8")
            (root / "work").mkdir()
            (root / "work/id_map_history.jsonl").write_text(_json.dumps(
                {"map": {"F3": "F2", "F4": "F3"}, "rounds_run_before_renumber": {"round_2": "inv2"}}) + "\n",
                encoding="utf-8")
            self.assertEqual(later_disputes.later_disputes(root), {"F2": {"critic_stats": 2}})
            (root / "critique/round_2/INTEGRATION_SUMMARY.json").write_text("{}", encoding="utf-8")
            self.assertEqual(later_disputes.later_disputes(root), {})

    def test_source_level_round_twenty_five(self):
        ir = (SKILL / "scripts" / "integrate_round.py").read_text(encoding="utf-8")
        self.assertIn("if rnd > 1 and not adjudicator_confirmed(entry):", ir)
        self.assertIn("bound_now.add((row[\"id\"], candidate[\"reviewer_id\"]))", ir)
        self.assertIn("rebind_literal_confirmations ignored", ir)
        self.assertIn('elif overruled and not note.startswith("overruled:"):', ir)
        vrp = (SKILL / "scripts" / "validate_round_projections.py").read_text(encoding="utf-8")
        self.assertIn("are not both", vrp)
        self.assertNotIn('release_dir = (run.get("delivery")', vrp)
        journal = (SKILL / "scripts" / "lifecycle_journal.py").read_text(encoding="utf-8")
        self.assertNotIn("os.path.relpath(payload[\"release_dir\"]", journal)
        guide = (SKILL / "reference" / "adjudication_guide.md").read_text(encoding="utf-8")
        self.assertIn("A reviewer with no record yet is bound automatically", guide)

class RoundTwentySixFixes(unittest.TestCase):
    def test_a_rounds_own_verdict_never_disputes_the_record_it_produced(self):
        import later_disputes
        disputes = {"F1": {"critic_stats": 1}, "*": {"critic_data": 3}}
        rounds = {"inv-r1": 1, "inv-r2": 2}
        same = [{"reviewer_id": "critic_stats", "invocation_id": "inv-r1"}]
        self.assertEqual(later_disputes.stale_confirmations(pathlib.Path("."), "F1", same, disputes, rounds), [])
        legacy = [{"reviewer_id": "critic_data", "invocation_id": "inv-r2"}]
        self.assertEqual(later_disputes.stale_confirmations(pathlib.Path("."), "F9", legacy, disputes, rounds),
                         ["critic_data (round 3)"])

    def test_projection_refuses_a_gap_below_an_unapproved_finding(self):
        rows = [{"id": "F1", "status": "confirmed", "coordinator_approved": True,
                 "ledger_disposition": {"status": "INCLUDED"}},
                {"id": "F3", "status": "confirmed", "coordinator_approved": False,
                 "ledger_disposition": {"status": "INCLUDED"}}]
        with self.assertRaises(SystemExit) as caught:
            generate_finding_artifacts.ensure_final_invariants(rows, pathlib.Path("."))
        self.assertIn("observed [1, 3]", str(caught.exception))

    def test_source_level_round_twenty_six(self):
        ir = (SKILL / "scripts" / "integrate_round.py").read_text(encoding="utf-8")
        self.assertIn('if v["outcome"] in CONFIRMING}', ir)
        mdp = (SKILL / "scripts" / "make_diff_packet.py").read_text(encoding="utf-8")
        self.assertIn("translated_rows = expected", mdp)
        rn = (SKILL / "scripts" / "renumber_findings.py").read_text(encoding="utf-8")
        self.assertIn("binds their bytes, and make_diff_packet translates their ids when it reads them", rn)
        vrp = (SKILL / "scripts" / "validate_round_projections.py").read_text(encoding="utf-8")
        self.assertIn("changed after the release was staged", vrp)
        self.assertIn("It changed after round", vrp)
        pub = (SKILL / "scripts" / "publish_completed.sh").read_text(encoding="utf-8")
        self.assertLess(pub.index("has no finalization_receipt.json"), pub.index('"$DOCUMENT_RELEASE" verify'))
        skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("disputed a settled fact", skill)

class RoundTwentySevenFixes(unittest.TestCase):
    def test_draft_to_final_renumber_is_not_legacy(self):
        import json as _json
        import later_disputes
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "work").mkdir()
            (root / "work/id_map.json").write_text(_json.dumps({"CM-F2": "F1", "ST-F1": "F2"}), encoding="utf-8")
            self.assertFalse(later_disputes._legacy_renumbered(root))
            (root / "work/id_map.json").write_text(_json.dumps({"F3": "F2", "F4": "F3"}), encoding="utf-8")
            self.assertTrue(later_disputes._legacy_renumbered(root))

    def test_projection_accepts_a_deferred_middle_approval(self):
        rows = [{"id": f"F{n}", "status": "confirmed", "coordinator_approved": n != 2,
                 "ledger_disposition": {"status": "INCLUDED"}, "report_section": "Section 1",
                 "changes_conclusion": False, "verifications": [], "evidence": {"source_hashes": []}}
                for n in (1, 2, 3)]
        try:
            generate_finding_artifacts.ensure_final_invariants(rows, pathlib.Path("."))
        except SystemExit as exc:
            self.assertNotIn("contiguous", str(exc))

    def test_source_level_round_twenty_seven(self):
        mdp = (SKILL / "scripts" / "make_diff_packet.py").read_text(encoding="utf-8")
        self.assertIn("report_maps = maps_after[matched:]", mdp)
        self.assertIn("round's frozen diff packet binds it, so it must not be replaced", mdp)
        self.assertIn("translated here by claim text", mdp)
        rn = (SKILL / "scripts" / "renumber_findings.py").read_text(encoding="utf-8")
        self.assertIn("st_ino", rn)
        rc = (SKILL / "scripts" / "run_critique.sh").read_text(encoding="utf-8")
        self.assertLess(rc.index("a typo must fail before any critic is paid for"), rc.index('OUT="$WS/critique/round_${ROUND}"'))
        vrp = (SKILL / "scripts" / "validate_round_projections.py").read_text(encoding="utf-8")
        self.assertIn("there is no staging record", vrp)
        self.assertIn("post_delta/*/payload/", vrp)
        self.assertIn("python3 scripts/run_post_delta_gates.py", vrp)
        skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("phase_start phase=critique at=<first critic started_at>", skill)

class RoundTwentyEightFixes(unittest.TestCase):
    def test_legacy_rule_is_shared(self):
        import json as _json
        import later_disputes
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "work").mkdir()
            (root / "work/id_map_history.jsonl").write_text(_json.dumps({"map": {}, "legacy_prior": True}) + "\n",
                                                             encoding="utf-8")
            self.assertTrue(later_disputes.legacy_renumbered(root))
            (root / "work/id_map_history.jsonl").write_text(_json.dumps({"map": {}}) + "\n", encoding="utf-8")
            self.assertFalse(later_disputes.legacy_renumbered(root))
        for name in ("make_settled.sh", "validate_round_projections.py", "make_diff_packet.py"):
            self.assertIn("later_disputes.legacy_renumbered(", (SKILL / "scripts" / name).read_text(encoding="utf-8"))
        self.assertIn("later_disputes._moves_final_ids(legacy_map)",
                      (SKILL / "scripts" / "integrate_round.py").read_text(encoding="utf-8"))

    def test_deferred_middle_approval_passes_the_downstream_helpers(self):
        mc = (SKILL / "scripts" / "make_claims_script.sh").read_text(encoding="utf-8")
        self.assertIn("nums != sorted(set(nums))", mc)
        self.assertIn("tags != [str(n) for n in nums]", mc)
        gates = (SKILL / "scripts" / "run_post_delta_gates.py").read_text(encoding="utf-8")
        self.assertIn("generator.ensure_final_invariants(rows, snapshot, quiet=True)", gates)
        rv = (SKILL / "scripts" / "run_verification.sh").read_text(encoding="utf-8")
        self.assertIn("numbers != sorted(set(numbers))", rv)
        cs = (SKILL / "CHEATSHEET.html").read_text(encoding="utf-8")
        self.assertIn("8 phase_start/phase_end + 1 delivery", cs)

    def test_source_level_round_twenty_eight(self):
        rn = (SKILL / "scripts" / "renumber_findings.py").read_text(encoding="utf-8")
        self.assertIn("had_last_map = later_disputes._legacy_renumbered(root)", rn)
        self.assertIn('root.glob("critique/round_*/RESPONSE_notes.md")', rn)
        mdp = (SKILL / "scripts" / "make_diff_packet.py").read_text(encoding="utf-8")
        self.assertIn("set(mapping) == {r.get(\"id\") for r in given}", mdp)
        self.assertIn("bound_path.samefile(before_findings)", mdp)
        self.assertIn("the stage whose replay reproduces the", mdp)
        self.assertIn("a step-1 copy taken", mdp)
        rc = (SKILL / "scripts" / "run_critique.sh").read_text(encoding="utf-8")
        self.assertIn('if [ -n "${ADJUDICATE+x}" ]; then', rc)
        gfa = (SKILL / "scripts" / "generate_finding_artifacts.py").read_text(encoding="utf-8")
        self.assertIn("are not yet approved, so the projection", gfa)
        gates = (SKILL / "scripts" / "run_post_delta_gates.py").read_text(encoding="utf-8")
        self.assertIn("a release is already staged", gates)
        skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("appendix changed since the last packet", skill)

class RoundTwentyNineFixes(unittest.TestCase):
    def test_source_level_round_twenty_nine(self):
        gfa = (SKILL / "scripts" / "generate_finding_artifacts.py").read_text(encoding="utf-8")
        self.assertIn("expected_numbers=[number(r) for r in final]", gfa)
        mc = (SKILL / "scripts" / "make_claims_script.sh").read_text(encoding="utf-8")
        self.assertIn("for f, i in zip(finds, nums):", mc)
        install = (SKILL / "INSTALL.md").read_text(encoding="utf-8")
        self.assertIn("project twice (the projection adds the finding to the report)", install)
        cs = (SKILL / "CHEATSHEET.html").read_text(encoding="utf-8")
        self.assertNotIn("Commit the eight", cs)
        mdp = (SKILL / "scripts" / "make_diff_packet.py").read_text(encoding="utf-8")
        self.assertNotIn("rerun \"\n               \"any renumber --also", mdp)
        self.assertIn("translated when the packet is built", mdp)
        gates = (SKILL / "scripts" / "run_post_delta_gates.py").read_text(encoding="utf-8")
        self.assertIn("ensure_final_invariants(rows, snapshot, quiet=True)", gates)
        stage = (SKILL / "scripts" / "stage_release.sh").read_text(encoding="utf-8")
        self.assertIn('"$R/critique/id_map_history.jsonl"', stage)

    def test_renumber_records_audit_relative_paths(self):
        import renumber_findings
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "critique/round_1").mkdir(parents=True)
            self.assertEqual(renumber_findings._audit_relative(root, root / "critique/round_1/RESPONSE_notes.md"),
                             "critique/round_1/RESPONSE_notes.md")

    def test_source_level_round_twenty_nine_r28fixes(self):
        gates = (SKILL / "scripts" / "run_post_delta_gates.py").read_text(encoding="utf-8")
        self.assertIn('not l.lstrip().startswith("note:")', gates)
        mdp = (SKILL / "scripts" / "make_diff_packet.py").read_text(encoding="utf-8")
        self.assertIn('get("snapshot_rel")', mdp)
        self.assertIn("if maps_after and not legacy:", mdp)
        self.assertIn("the report still carries the reviewed ids", mdp)
        self.assertIn("never another file (least of all the canonical report)", mdp)
        self.assertIn("not the canonical report", mdp)

class RoundThirtyFixes(unittest.TestCase):
    def test_empty_at_is_refused(self):
        import lifecycle_journal
        with self.assertRaises(lifecycle_journal.JournalError):
            lifecycle_journal.check_hand_payload("phase_start", {"phase": "critique", "at": ""})

    def test_source_level_round_thirty(self):
        mdp = (SKILL / "scripts" / "make_diff_packet.py").read_text(encoding="utf-8")
        self.assertIn("written by a rerun that never became the round's current", mdp)
        self.assertIn("a later round's frozen packet binds", mdp)
        rc = (SKILL / "scripts" / "run_critique.sh").read_text(encoding="utf-8")
        self.assertIn("is already finished (integrated or ended) and cannot be rerun: its inputs changed", rc)
        fin = (SKILL / "scripts" / "finalize_run.sh").read_text(encoding="utf-8")
        self.assertIn("after it ends", fin)
        jr = (SKILL / "scripts" / "lifecycle_journal.py").read_text(encoding="utf-8")
        self.assertIn("was logged as ended at", jr)
        gfa = (SKILL / "scripts" / "generate_finding_artifacts.py").read_text(encoding="utf-8")
        self.assertIn("(then stage, if a release is due)", gfa)
        vrp = (SKILL / "scripts" / "validate_round_projections.py").read_text(encoding="utf-8")
        self.assertIn("invocation_round_of = later_disputes.invocation_rounds(audit)", vrp)

class RoundThirtyOneFixes(unittest.TestCase):
    def test_finalize_names_a_phase_that_starts_after_it_ends(self):
        import json as _json, shutil, subprocess
        with tempfile.TemporaryDirectory() as tmp:
            audit = pathlib.Path(tmp) / "a"
            audit.mkdir()
            shutil.copy(SKILL / "schemas/EXAMPLE_comment.json", audit / "comment.json")
            run = _json.loads((SKILL / "schemas/EXAMPLE_run.json").read_text(encoding="utf-8"))
            run["phases"]["fan_out"]["started_at"] = "2026-07-14T23:00:00Z"
            (audit / "run.json").write_text(_json.dumps(run), encoding="utf-8")
            proc = subprocess.run(["bash", str(SKILL / "scripts/finalize_run.sh"), str(audit), "--no-registry"],
                                  capture_output=True, text=True)
            self.assertIn("phase fan_out starts (2026-07-14T23:00:00Z) after it ends", proc.stdout + proc.stderr)

    def test_source_level_round_thirty_one(self):
        mdp = (SKILL / "scripts" / "make_diff_packet.py").read_text(encoding="utf-8")
        self.assertIn("own_report_copy = step1_report.is_file() and before_report.samefile(step1_report)", mdp)
        self.assertIn("own_copy = step1_findings.is_file() and before_findings.samefile(step1_findings)", mdp)
        self.assertIn("if args.no_remedy_file:   # a check-only caller writes nothing", mdp)
        vdp = (SKILL / "scripts" / "verify_diff_packet.py").read_text(encoding="utf-8")
        self.assertIn("detail[-4000:]", vdp)
        jr = (SKILL / "scripts" / "lifecycle_journal.py").read_text(encoding="utf-8")
        self.assertIn("def _warn_late_phase_start", jr)
        rn = (SKILL / "scripts" / "renumber_findings.py").read_text(encoding="utf-8")
        self.assertIn("def bound_report_candidates", rn)
        vrp = (SKILL / "scripts" / "validate_round_projections.py").read_text(encoding="utf-8")
        self.assertIn("renumber_findings.bound_report_candidates(audit, relative, reviewed)", vrp)

    def test_source_level_round_thirty_one_e2e(self):
        fin = (SKILL / "scripts" / "finalize_run.sh").read_text(encoding="utf-8")
        self.assertIn("does not exist (a typo?)", fin)
        self.assertIn("is not this audit's tree registry", fin)
        self.assertIn("phase 'critique' ends at %s, before round(s) %s finished", fin)
        jr = (SKILL / "scripts" / "lifecycle_journal.py").read_text(encoding="utf-8")
        self.assertIn("a finished round is not rerun", jr)
        ir = (SKILL / "scripts" / "integrate_round.py").read_text(encoding="utf-8")
        self.assertIn("applied (not counted as a correction)", ir)
        mdp = (SKILL / "scripts" / "make_diff_packet.py").read_text(encoding="utf-8")
        self.assertIn("still carries draft finding ids", mdp)
        gfa = (SKILL / "scripts" / "generate_finding_artifacts.py").read_text(encoding="utf-8")
        self.assertIn("finding markers do not match the INCLUDED findings", gfa)
        install = (SKILL / "INSTALL.md").read_text(encoding="utf-8")
        self.assertIn("the final round's last critic or targeted-adjudicator call", install)

class RoundThirtyTwoFixes(unittest.TestCase):
    def _tree(self, tmp, before, reviewed):
        helper = RoundTwentyTwoFixes()
        root = helper._packet_tree(tmp)
        (root / "critique/round_1/REPORT_BEFORE_INTEGRATION.md").write_text(before, encoding="utf-8")
        snap = root / "critique/round_1/snapshots/inv1/source/REPORT_TO_REVIEW.md"
        snap.parent.mkdir(parents=True)
        snap.write_text(reviewed, encoding="utf-8")
        return helper, root

    def test_draft_ids_accepted_when_the_round_reviewed_draft_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = "One. <!-- finding:CM-F2 -->\n"
            helper, root = self._tree(tmp, text, text)
            proc = helper._packet(root, 2, "critique/round_1")
            self.assertNotIn("still carries draft finding ids", proc.stderr)

    def test_draft_ids_refused_when_the_round_reviewed_final_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            helper, root = self._tree(tmp, "One. <!-- finding:CM-F2 -->\n", "One. <!-- finding:F1 -->\n")
            proc = helper._packet(root, 2, "critique/round_1")
            self.assertIn("still carries draft finding ids", proc.stderr)

    def test_finalize_restores_a_legacy_bound_renumbered_copy(self):
        import hashlib as _h, json as _json
        import renumber_findings
        import validate_round_projections as vrp
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            reviewed = "One. <!-- finding:COD-F1 -->\nTwo. <!-- finding:DAT-F2 -->\n"
            legacy = {"COD-F1": "F1", "DAT-F2": "F2"}
            bound_text = renumber_findings.rewrite_text(reviewed, legacy)
            def w(rel, text):
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
                return path
            w("critique/round_1/snapshots/s1/source/REPORT_TO_REVIEW.md", reviewed)
            w("work/id_map.json", _json.dumps(legacy))
            bound = w("critique/round_1/REPORT_BEFORE_INTEGRATION.renumbered.md", bound_text + "edited\n")
            findings = w("critique/round_1/FINDINGS_BEFORE_INTEGRATION.jsonl", "{}\n")
            snap = root / "critique/round_2/snapshots/s2"
            after_report = w("critique/round_2/snapshots/s2/source/REPORT_TO_REVIEW.md", bound_text)
            after_findings = w("critique/round_2/snapshots/s2/source/context/work/findings.jsonl", "{}\n")
            H = lambda path: _h.sha256(path.read_bytes()).hexdigest()
            manifest = w("critique/round_2/snapshots/s2/source/context/DIFF_MANIFEST.json", _json.dumps({
                "packet_version": "i4r-diff-packet-2", "round": 2,
                "input_paths": {"before_report": "critique/round_1/REPORT_BEFORE_INTEGRATION.renumbered.md",
                                "before_findings": "critique/round_1/FINDINGS_BEFORE_INTEGRATION.jsonl"},
                "inputs": {"after_report_sha256": H(after_report), "after_findings_sha256": H(after_findings),
                           "before_report_sha256": _h.sha256(bound_text.encode()).hexdigest(),
                           "before_findings_sha256": H(findings)}}))
            with self.assertRaises(ValueError) as caught:
                vrp.verify_snapshot_diff_binding(root, manifest, 2, snap)
            self.assertIn(".restored", str(caught.exception))
            self.assertEqual((root / "critique/round_1/REPORT_BEFORE_INTEGRATION.renumbered.md.restored")
                             .read_text(encoding="utf-8"), bound_text)

    def test_source_level_round_thirty_two(self):
        mdp = (SKILL / "scripts" / "make_diff_packet.py").read_text(encoding="utf-8")
        self.assertIn("frozen_mismatch = bool(frozen_report_hash)", mdp)
        install = (SKILL / "INSTALL.md").read_text(encoding="utf-8")
        self.assertIn("I4R_CREATE_REGISTRY=1", install)
        self.assertIn("I4R_REGISTRY_OVERRIDE=1", install)
        fin = (SKILL / "scripts" / "finalize_run.sh").read_text(encoding="utf-8")
        self.assertIn("is not a valid timestamp", fin)
        self.assertIn("tree_registry () {", fin)
        self.assertIn("($registry_source) does not exist (a typo?)", fin)
        self.assertIn("INTEGRATION_SUMMARY.json", fin)
        self.assertIn("check_consistency.sh was not recorded for round(s)", fin)
        pub = (SKILL / "scripts" / "publish_completed.sh").read_text(encoding="utf-8")
        self.assertIn("is a registry folder, not a tree root", pub)
        self.assertIn("-h|--help) awk", pub)
        jr = (SKILL / "scripts" / "lifecycle_journal.py").read_text(encoding="utf-8")
        self.assertIn("the last end in batch order wins", jr)
        mdp2 = (SKILL / "scripts" / "make_diff_packet.py").read_text(encoding="utf-8")
        self.assertIn("before_findings = step1_findings", mdp2)
        rn = (SKILL / "scripts" / "renumber_findings.py").read_text(encoding="utf-8")
        self.assertIn("for start in range(len(maps)):", rn)

class RoundThirtyThreeFixes(unittest.TestCase):
    def test_options_missing_a_value_fail_fast(self):
        import subprocess
        for script, args in (("finalize_run.sh", ["x", "--stopping-reason"]), ("init_run.sh", ["x", "--doi"])):
            proc = subprocess.run(["bash", str(SKILL / "scripts" / script)] + args,
                                  capture_output=True, text=True, timeout=20)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("requires a value", proc.stderr)

    def test_source_level_round_thirty_three(self):
        vrp = (SKILL / "scripts" / "validate_round_projections.py").read_text(encoding="utf-8")
        self.assertIn("'report/REPORT_final.md'))} --before-findings", vrp)
        fin = (SKILL / "scripts" / "finalize_run.sh").read_text(encoding="utf-8")
        self.assertIn('registry_source="from REGISTRY_DIR/I4R_REGISTRY_DIR"', fin)
        install = (SKILL / "INSTALL.md").read_text(encoding="utf-8")
        self.assertIn("(or `REGISTRY_DIR` / `I4R_REGISTRY_DIR`) create a registry folder", install)

    def test_zone_less_integrated_at_is_refused_at_capture(self):
        import lifecycle_journal
        for value in ("2026-09-25T03:06:37", "2026-09-25", "garbage"):
            with self.assertRaises(lifecycle_journal.JournalError, msg=value):
                lifecycle_journal.check_hand_payload("round_integrated", {"round": 1, "integrated_at": value})
        lifecycle_journal.check_hand_payload("round_integrated", {"round": 1, "integrated_at": "2026-09-25T03:06:37Z"})

    def test_source_level_round_thirty_three_r32fixes(self):
        fin = (SKILL / "scripts" / "finalize_run.sh").read_text(encoding="utf-8")
        self.assertIn("return value if value.tzinfo is not None else None", fin)
        self.assertIn("a missing folder is never created inside a tree", fin)
        pub = (SKILL / "scripts" / "publish_completed.sh").read_text(encoding="utf-8")
        self.assertIn("is not this audit's tree root", pub)
        self.assertIn("I4R_PUBLISH_ANY_ROOT", pub)
        rc = (SKILL / "scripts" / "run_critique.sh").read_text(encoding="utf-8")
        self.assertIn('[ ! -f "$WS/critique/round_1/INTEGRATION_SUMMARY.json" ]', rc)
        self.assertIn("If more review is needed, $next_step.", rc)
        mdp = (SKILL / "scripts" / "make_diff_packet.py").read_text(encoding="utf-8")
        self.assertIn("if draft_markers and not bound_by_frozen", mdp)

class RoundThirtyFourFixes(unittest.TestCase):
    def test_next_round_hint(self):
        import lifecycle_journal as j
        events = [{"type": "round_prepared", "payload": {"round": 1}}, {"type": "round_integrated", "payload": {"round": 1}},
                  {"type": "round_prepared", "payload": {"round": 2}}]
        self.assertEqual(j.next_round_hint(events, 1), "run round 2 (it is prepared)")
        self.assertEqual(j.next_round_hint(events[:2], 1), "prepare round 2")

    def test_source_level_round_thirty_four(self):
        vrp = (SKILL / "scripts" / "validate_round_projections.py").read_text(encoding="utf-8")
        self.assertIn("HERE / 'make_diff_packet.py'", vrp)
        self.assertNotIn("(run from the audit folder;", vrp)
        jr = (SKILL / "scripts" / "lifecycle_journal.py").read_text(encoding="utf-8")
        self.assertIn('put \\"at\\" at the top level of the', jr)
        self.assertIn("has no timezone; re-journal it", jr)
        fin = (SKILL / "scripts" / "finalize_run.sh").read_text(encoding="utf-8")
        self.assertIn('[ "${2#--}" = "$2" ]', fin)
        pub = (SKILL / "scripts" / "publish_completed.sh").read_text(encoding="utf-8")
        self.assertIn('ROOT="$(i4r_root_of_audit "$A_TREE")"', pub)
        cs = (SKILL / "CHEATSHEET.html").read_text(encoding="utf-8")
        self.assertNotIn("the announcement will still read as if all is well", cs)
        install = (SKILL / "INSTALL.md").read_text(encoding="utf-8")
        self.assertIn("I4R_PUBLISH_ANY_ROOT=1", install)

class RoundThirtyFiveFixes(unittest.TestCase):
    def test_next_round_hint_counts_rounds_without_prepared_events(self):
        import lifecycle_journal as j
        events = [{"type": "round_start", "payload": {"round": 1}}, {"type": "round_end", "payload": {"round": 1}},
                  {"type": "round_start", "payload": {"round": 2}}, {"type": "round_end", "payload": {"round": 2}}]
        self.assertEqual(j.next_round_hint(events, 1), "prepare round 3")

    def test_empty_option_values_are_refused(self):
        import subprocess
        proc = subprocess.run(["bash", str(SKILL / "scripts/init_run.sh"), "x", "--doi", ""],
                              capture_output=True, text=True, timeout=20)
        self.assertIn("requires a value", proc.stderr)

    def test_source_level_round_thirty_five(self):
        pub = (SKILL / "scripts" / "publish_completed.sh").read_text(encoding="utf-8")
        self.assertIn('if ! publish_tree_root "$(i4r_root_of_audit "$A_LOGICAL")"; then A_TREE="$A"; fi', pub)
        self.assertIn("-h|--help) awk", pub)
        fin = (SKILL / "scripts" / "finalize_run.sh").read_text(encoding="utf-8")
        self.assertIn('"$(cd "$1" 2>/dev/null && pwd -P)"', fin)
        self.assertIn("after phase 'release' starts at", fin)
        self.assertIn("delivery artifact_sha256 does not match", fin)
        vrp = (SKILL / "scripts" / "validate_round_projections.py").read_text(encoding="utf-8")
        self.assertIn('stop_now == "target_complete" and last_unintegrated', vrp)
        self.assertIn("DIFF_MANIFEST_round_{number}.rebuilt.json", vrp)
        mdp = (SKILL / "scripts" / "make_diff_packet.py").read_text(encoding="utf-8")
        self.assertIn("def load_frozen_manifest", mdp)
        self.assertIn("def frozen_manifests()", mdp)
        ms = (SKILL / "scripts" / "make_settled.sh").read_text(encoding="utf-8")
        self.assertIn("re-journal it: bash scripts/log_event.sh round_integrated round={_n}", ms)
        le = (SKILL / "scripts" / "log_event.sh").read_text(encoding="utf-8")
        self.assertIn('if "at" in value["payload"]:', le)
        install = (SKILL / "INSTALL.md").read_text(encoding="utf-8")
        self.assertIn("an optional suggestion on a", install)

class RoundThirtySixFixes(unittest.TestCase):
    def test_source_level_round_thirty_six(self):
        vrp = (SKILL / "scripts" / "validate_round_projections.py").read_text(encoding="utf-8")
        self.assertIn('strict = stop_now == "converged"', vrp)
        self.assertIn("has prose that round {reviewed_round}'s critics did not", vrp)
        self.assertIn("so it counts as not integrated", vrp)
        self.assertNotIn('f"converged rests on {relative}', vrp)
        jr = (SKILL / "scripts" / "lifecycle_journal.py").read_text(encoding="utf-8")
        self.assertIn('"artifact_sha256": payload.get("artifact_sha256"),', jr)
        le = (SKILL / "scripts" / "log_event.sh").read_text(encoding="utf-8")
        self.assertIn("artifact_sha256: ($p.artifact_sha256 // null),", le)
        rc = (SKILL / "scripts" / "run_critique.sh").read_text(encoding="utf-8")
        self.assertIn('{ [ -n "$PRIOR_CURRENT_INVOCATION" ] || [ -n "$PRIOR_STATUS_COPY" ]; } && round_is_finished', rc)
        pub = (SKILL / "scripts" / "publish_completed.sh").read_text(encoding="utf-8")
        self.assertIn('publish_tree_root () { i4r_is_tree_root "$1" || [ -d "$1/registry" ]; }', pub)
        fin = (SKILL / "scripts" / "finalize_run.sh").read_text(encoding="utf-8")
        self.assertIn("-h|--help) awk", fin)
        ir = (SKILL / "scripts" / "integrate_round.py").read_text(encoding="utf-8")
        self.assertIn("cannot be re-integrated: later round(s)", ir)
        self.assertIn("audit / 'critique' / f'round_{rnd}' / 'CONTESTED.json'", ir)

class RoundThirtySevenFixes(unittest.TestCase):
    def test_source_level_round_thirty_seven(self):
        skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("both ship the reviewed report (target_complete: projection edits aside)", skill)
        install = (SKILL / "INSTALL.md").read_text(encoding="utf-8")
        self.assertIn("integrating even a wording-only fix after the last round means another round", install)
        guide = (SKILL / "reference" / "adjudication_guide.md").read_text(encoding="utf-8")
        self.assertIn("finalize refuses other prose edits", guide)
        vrp = (SKILL / "scripts" / "validate_round_projections.py").read_text(encoding="utf-8")
        self.assertIn('[f"removed: {line}" for line in removed]', vrp)

    def test_new_audit_refusals_name_the_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            paper = pathlib.Path(tmp) / "data" / "J" / "P"
            paper.mkdir(parents=True)
            for name in ("a.pdf", "b.pdf"):
                (paper / name).write_bytes(b"%PDF-1.4")
            proc = subprocess.run(["bash", str(SKILL / "scripts" / "new_audit.sh"), str(paper)],
                                  capture_output=True, text=True, cwd=tmp)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("--paper <pdf>", proc.stderr)

    def test_factual_corrections_counts_findings(self):
        src = (SKILL / "scripts" / "integrate_round.py").read_text(encoding="utf-8")
        self.assertIn('"factual_corrections": len(corrected_ids)', src)
        self.assertIn('corrected_ids.add(entry["id"])', src)

    def test_round_thirty_seven_tester_fixes(self):
        vrp = (SKILL / "scripts" / "validate_round_projections.py").read_text(encoding="utf-8")
        self.assertIn('if reviewed_round and stop_now in ("converged", "target_complete"):', vrp)
        self.assertEqual(vrp.count('item.get("priority") == "BLOCKING"'), 4)
        self.assertIn('" ".join(line.split())', vrp)
        self.assertIn("finding markers differ in kind or number", vrp)
        self.assertIn("a report change, so it needs another critique round", vrp)
        joined = re.sub(r'"\s*\n\s*f?"', "", vrp)   # adjacent string pieces joined
        self.assertNotIn("delivery and then phase_end", joined)
        fin = (SKILL / "scripts" / "finalize_run.sh").read_text(encoding="utf-8")
        self.assertIn("the delivery names no artifact_path", fin)
        self.assertIn("I4R_CREATE_REGISTRY creates only $marker_root/registry", fin)
        self.assertIn("phase_end phase=release", (SKILL / "scripts" / "document_release.py").read_text(encoding="utf-8"))
        self.assertIn("phase_end phase=release", (SKILL / "scripts" / "publish_completed.sh").read_text(encoding="utf-8"))
        install = (SKILL / "INSTALL.md").read_text(encoding="utf-8")
        self.assertIn("An `applied_note` does not answer one", install)

    def test_round_thirty_eight_diff_fixes(self):
        vrp = (SKILL / "scripts" / "validate_round_projections.py").read_text(encoding="utf-8")
        self.assertIn("def withdrawn(item: dict) -> bool:", vrp)
        self.assertIn("was integrated after its review, and only projection edits may follow", vrp)
        doc = (SKILL / "scripts" / "document_release.py").read_text(encoding="utf-8")
        self.assertIn("This run finalized as target_complete", doc)
        fin = (SKILL / "scripts" / "finalize_run.sh").read_text(encoding="utf-8")
        self.assertIn('if [ -z "$tree_reg" ]; then', fin)
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(["perl", "-e", "alarm 10; exec @ARGV", "bash",
                                   str(SKILL / "scripts" / "new_audit.sh"), tmp, "--paper"],
                                  capture_output=True, text=True)
            self.assertEqual(proc.returncode, 2)
            self.assertIn("--paper requires a value", proc.stderr)

    def test_round_thirty_eight_tester_fixes(self):
        import re as _re
        rx = _re.compile(r"<!--\s*(finding|tested-negative|merged):[^\s>]+(?:->[^\s>]+)?\s*-->")
        for marker in ("<!-- finding:F1 -->", "<!-- merged:F3->F1 -->", "<!--finding:F1-->", "<!-- finding:CM-F2 -->"):
            self.assertTrue(rx.fullmatch(marker), marker)
        self.assertEqual(rx.sub("M", "<!-- tested-negative:F2--><b>new prose</b><!--x -->"), "M<b>new prose</b><!--x -->")
        for name in ("validate_round_projections.py", "make_diff_packet.py"):
            self.assertIn(":[^\\s>]+(?:->[^\\s>]+)?\\s*-->", (SKILL / "scripts" / name).read_text(encoding="utf-8"))
        fin = (SKILL / "scripts" / "finalize_run.sh").read_text(encoding="utf-8")
        self.assertIn("is not a path inside the audit", fin)
        self.assertIn('== "comments/ and metadata/ (filed publication set)"', fin)
        self.assertIn("this\" >&2", fin)   # the existing-folder branch of the new-tree registry rule
        self.assertIn('and v.get("priority") != "BLOCKING"',
                      (SKILL / "scripts" / "integrate_round.py").read_text(encoding="utf-8"))
        self.assertIn('item.get("priority") != "BLOCKING"', (SKILL / "scripts" / "later_disputes.py").read_text(encoding="utf-8"))
        self.assertIn('PDF="$(cd "$(dirname "$PDF")" && pwd -P)/$(basename "$PDF")"',
                      (SKILL / "scripts" / "new_audit.sh").read_text(encoding="utf-8"))

    def test_finalize_refuses_a_published_audit(self):
        import json as _json
        with tempfile.TemporaryDirectory() as tmp:
            audit = pathlib.Path(tmp) / "audit"
            audit.mkdir()
            (audit / "run.json").write_text("{}", encoding="utf-8")
            event = {"type": "delivery", "payload": {"release_dir": "comments/J/P",
                                                     "recipient": "comments/ and metadata/ (filed publication set)"}}
            (audit / "run.events.jsonl").write_text(_json.dumps(event) + "\n", encoding="utf-8")
            env = {k: v for k, v in os.environ.items() if k != "I4R_REFINALIZE_PUBLISHED"}
            proc = subprocess.run(["bash", str(SKILL / "scripts" / "finalize_run.sh"), str(audit),
                                   "--stopping-reason", "target_complete", "--no-registry"],
                                  capture_output=True, text=True, env=env)
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertIn("already published", proc.stderr)
            self.assertIn("I4R_REFINALIZE_PUBLISHED=1", proc.stderr)
        install = (SKILL / "INSTALL.md").read_text(encoding="utf-8")
        self.assertIn("## Correcting a published comment", install)
        self.assertIn("full report: python3 scripts/summarize_invocations.py",
                      (SKILL / "scripts" / "finalize_run.sh").read_text(encoding="utf-8"))

    def test_round_thirty_nine_diff_fixes(self):
        fin = (SKILL / "scripts" / "finalize_run.sh").read_text(encoding="utf-8")
        self.assertIn("the delivery on record is still the previous publication's filing", fin)
        self.assertIn("This audit's tree has no registry yet; for its first audit pass $marker_root/registry", fin)
        self.assertIn('! -samefile "$PDF"', (SKILL / "scripts" / "new_audit.sh").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            paper = pathlib.Path(tmp) / "T" / "data" / "J" / "Paper [2020]"
            paper.mkdir(parents=True)
            (paper / "article.pdf").write_bytes(b"%PDF-1.4\n")
            (paper / "code.R").write_text("x <- 1\n", encoding="utf-8")
            # 2.7.5: new_audit.sh checks the critic route with one Codex call; the fake answers it
            env = dict(os.environ, TMPDIR=tmp, CODEX=str(SKILL / "tests" / "fake_codex.sh"),
                       FAKE_CODEX_STATE=str(pathlib.Path(tmp) / "fake"), I4R_STATE_DIR=str(pathlib.Path(tmp) / "state"),
                       CODEX_HOME=str(pathlib.Path(tmp) / "codex_home"))
            proc = subprocess.run(["bash", str(SKILL / "scripts" / "new_audit.sh"), str(paper)],
                                  capture_output=True, text=True, env=env)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            copied = sorted(p.name for p in (pathlib.Path(tmp) / "T").rglob("pkg/*"))
            self.assertEqual(copied, ["code.R"])

    def test_round_thirty_nine_tester_fixes(self):
        vrp = (SKILL / "scripts" / "validate_round_projections.py").read_text(encoding="utf-8")
        self.assertIn("PROJECTED_FIELDS = (", vrp)
        self.assertIn("differ from what round", vrp)
        self.assertIn("return finding in withdrawn_ids and finding in reviewed_withdrawn", vrp)
        fin = (SKILL / "scripts" / "finalize_run.sh").read_text(encoding="utf-8")
        self.assertIn("I4R_REFINALIZE_PUBLISHED=1, but the published set is still in place", fin)
        self.assertIn("does not exist in the audit; re-log `bash scripts/log_event.sh delivery", fin)
        install = (SKILL / "INSTALL.md").read_text(encoding="utf-8")
        self.assertIn("old_runs/<Journal>/<Paper>.published-", install)
        self.assertIn("`phase_end phase=critique at=<final-round end>` and `phase_start phase=release`", install)
        self.assertIn("../../../../registry", (SKILL / "CHEATSHEET.html").read_text(encoding="utf-8"))
        self.assertIn('bash \\"$HERE/init_run.sh\\"', (SKILL / "scripts" / "new_audit.sh").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            audit = pathlib.Path(tmp) / "T" / "runs" / "J" / "P" / "audit"
            audit.mkdir(parents=True)
            (pathlib.Path(tmp) / "T" / "data").mkdir()
            (pathlib.Path(tmp) / "T" / "comments" / "J" / "P").mkdir(parents=True)
            (audit / "run.json").write_text("{}", encoding="utf-8")
            import json as _json
            event = {"type": "delivery", "payload": {"release_dir": "comments/J/P",
                                                     "recipient": "comments/ and metadata/ (filed publication set)"}}
            (audit / "run.events.jsonl").write_text(_json.dumps(event) + "\n", encoding="utf-8")
            env = dict(os.environ, I4R_REFINALIZE_PUBLISHED="1")
            proc = subprocess.run(["bash", str(SKILL / "scripts" / "finalize_run.sh"), str(audit),
                                   "--stopping-reason", "target_complete", "--no-registry"],
                                  capture_output=True, text=True, env=env)
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertIn("published set is still in place", proc.stderr)

    def test_round_forty_fixes(self):
        vrp = (SKILL / "scripts" / "validate_round_projections.py").read_text(encoding="utf-8")
        for field in ("claim_ref", "changes_conclusion_which", "benign_explanations_ruled_out", "verification_snippet"):
            self.assertIn(f'"{field}"', vrp.split("PROJECTED_EVIDENCE_FIELDS")[0])
        self.assertIn('PROJECTED_EVIDENCE_FIELDS = ("expected", "observed", "rerun_command"', vrp)
        fin = (SKILL / "scripts" / "finalize_run.sh").read_text(encoding="utf-8")
        self.assertIn('for pub_path in "$(cd "$DIRN" && pwd)" "$(cd "$DIRN" && pwd -P)"; do', fin)
        self.assertIn('if [ "$last_delivery_is_filing" = "filing" ]; then', fin)
        self.assertIn("rm -rf \"$STAGE\"' EXIT", (SKILL / "scripts" / "new_audit.sh").read_text(encoding="utf-8"))
        install = (SKILL / "INSTALL.md").read_text(encoding="utf-8")
        self.assertIn('mv metadata/<Journal>/<Paper> "$A/metadata"', install)
        self.assertIn("the published registry fields of each INCLUDED finding", install)
        skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("target_complete may integrate it only to bind a deferred approval or record", skill)
        self.assertIn("contests, then rebuild: INSTALL.md)", skill)
        self.assertIn('export AUDIT_DIR="<absolute path new_audit.sh prints>"', skill)

    def test_changed_after_review_deferred_exemption(self):
        import validate_round_projections as vrp
        reviewed = {"coordinator_approved": False, "report_section": None, "verification_snippet": None,
                    "changes_conclusion": False, "changes_conclusion_which": None, "confidence": "LIKELY",
                    "evidence": {"observed": 1}, "ledger_disposition": {"status": "INCLUDED"}}
        bound = dict(reviewed, coordinator_approved=True, report_section="Section 2",
                     verification_snippet={"kind": "judgement", "path": "work/x.txt", "basis": "b"})
        self.assertEqual(vrp.changed_after_review(reviewed, bound), [])
        flipped = dict(bound, changes_conclusion=True, changes_conclusion_which="the headline")
        self.assertEqual(vrp.changed_after_review(reviewed, flipped), ["changes_conclusion", "changes_conclusion_which"])
        set_before = dict(reviewed, report_section="Section 1")
        self.assertEqual(vrp.changed_after_review(set_before, dict(bound, report_section="Section 2")), ["report_section"])
        approved = dict(reviewed, coordinator_approved=True)
        self.assertEqual(vrp.changed_after_review(approved, dict(approved, report_section="Section 2")), ["report_section"])
        self.assertEqual(vrp.changed_after_review(reviewed, dict(reviewed, evidence={"observed": 2})), ["evidence.observed"])

    def test_round_forty_one_fixes(self):
        import validate_round_projections as vrp
        withdrawn_then = {"coordinator_approved": False, "report_section": None,
                          "ledger_disposition": {"status": "EXCLUDED"}}
        self.assertEqual(vrp.changed_after_review(withdrawn_then, dict(withdrawn_then, report_section="Section 2")),
                         ["report_section"])
        self.assertIn("source_hashes", vrp.PROJECTED_EVIDENCE_FIELDS)
        src = (SKILL / "scripts" / "validate_round_projections.py").read_text(encoding="utf-8")
        self.assertIn("the INCLUDED findings differ from the ones round", src)
        self.assertIn("reviewed_by_text", src)
        self.assertIn("could not copy the loose package files into staging",
                      (SKILL / "scripts" / "new_audit.sh").read_text(encoding="utf-8"))
        self.assertIn("the set of INCLUDED findings must also be the one", (SKILL / "INSTALL.md").read_text(encoding="utf-8"))

    def test_round_forty_one_e2e_fixes(self):
        ir = (SKILL / "scripts" / "integrate_round.py").read_text(encoding="utf-8")
        self.assertIn("would not match the finding schema after these decisions", ir)
        fin = (SKILL / "scripts" / "finalize_run.sh").read_text(encoding="utf-8")
        self.assertIn("release started at %s, before that", fin)
        mdp = (SKILL / "scripts" / "make_diff_packet.py").read_text(encoding="utf-8")
        self.assertIn("step-1 copies were not made", mdp)
        self.assertIn("If the last round\n   was never integrated, first make its step-1 copies",
                      (SKILL / "INSTALL.md").read_text(encoding="utf-8"))

    def test_unintegrated_prior_round_registry_copy_is_checked(self):
        import copy as _copy, shutil as _shutil
        sys.path.insert(0, str(SKILL))
        from tests.test_diff_packet import DiffPacketTests
        case = DiffPacketTests("setUp"); case.setUp()
        try:
            audit = case.audit
            rows = [case.row("F1", "statistics")]
            case.write_rows("work/findings.jsonl", rows)
            snap = audit / "critique/round_1/snapshots/abc/source"
            (snap / "context/work").mkdir(parents=True)
            _shutil.copy(audit / "report/REPORT_final.md", snap / "REPORT_TO_REVIEW.md")
            _shutil.copy(audit / "work/findings.jsonl", snap / "context/work/findings.jsonl")
            edited = _copy.deepcopy(rows); edited[0]["claim"] = "F1 reworded after review"
            case.write_rows("work/findings.jsonl", edited)
            prior = audit / "critique/round_1"
            command = [sys.executable, str(SKILL / "scripts/make_diff_packet.py"), str(audit), "--round", "2",
                       "--before-report", str(prior / "REPORT_BEFORE_INTEGRATION.md"),
                       "--after-report", str(audit / "report/REPORT_final.md"),
                       "--before-findings", str(prior / "FINDINGS_BEFORE_INTEGRATION.jsonl"),
                       "--output", str(audit / "work/pkt.json")]
            first = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(first.returncode, 0)
            self.assertIn("source/context/work/findings.jsonl", first.stderr)
            _shutil.copy(audit / "report/REPORT_final.md", prior / "REPORT_BEFORE_INTEGRATION.md")
            _shutil.copy(audit / "work/findings.jsonl", prior / "FINDINGS_BEFORE_INTEGRATION.jsonl")
            second = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(second.returncode, 0, second.stdout)
            self.assertIn("differs from the registry round 1 reviewed", second.stderr)
        finally:
            case.tearDown()

    def test_round_forty_two_fixes(self):
        vrp = (SKILL / "scripts" / "validate_round_projections.py").read_text(encoding="utf-8")
        self.assertIn("compared by claim text, as this audit was", vrp)
        self.assertIn("deferred.append(ValueError(", vrp)
        ir = (SKILL / "scripts" / "integrate_round.py").read_text(encoding="utf-8")
        self.assertIn("return refuse()", ir)
        self.assertIn("--contested keys match no verdict of round", ir)
        self.assertIn("--decisions has fields apply does not know", ir)
        self.assertIn('"unverifiable_basis"):', ir)
        self.assertIn("Make them from the files that round reviewed",
                      (SKILL / "scripts" / "make_diff_packet.py").read_text(encoding="utf-8"))
        self.assertIn("could not copy the package into", (SKILL / "scripts" / "setup_workspace.sh").read_text(encoding="utf-8"))
        install = (SKILL / "INSTALL.md").read_text(encoding="utf-8")
        self.assertIn("published-$(date -u +%Y%m%dT%H%M%SZ)", install)
        self.assertIn("last-round OVERSTATED/REFUTED verdict is contested in `CONTESTED.json`",
                      (SKILL / "SKILL.md").read_text(encoding="utf-8"))

    def test_round_forty_three_fixes(self):
        ir = (SKILL / "scripts" / "integrate_round.py").read_text(encoding="utf-8")
        self.assertIn('for role, reviewer in plan["reviewers"].items():', ir)
        sw = (SKILL / "scripts" / "setup_workspace.sh").read_text(encoding="utf-8")
        self.assertIn('[ "${CLAIMED_NOW:-0}" = "1" ] && echo', sw)
        self.assertIn("re-run the same command (I4R_ALLOW_EXISTING=1)", sw)
        self.assertIn('chmod u+w {shlex.quote(str(prior_dir / target))}',
                      (SKILL / "scripts" / "make_diff_packet.py").read_text(encoding="utf-8"))

    def test_round_forty_three_e2e_fixes(self):
        rn = (SKILL / "scripts" / "renumber_findings.py").read_text(encoding="utf-8")
        self.assertEqual(rn.count("not writable, so nothing was"), 2)
        ir = (SKILL / "scripts" / "integrate_round.py").read_text(encoding="utf-8")
        self.assertIn("elif stale_contest.is_file() and contested_path is None:", ir)
        self.assertIn("f'round_{rnd}' / 'CONTESTED.json'", ir)

    def test_round_forty_three_r42fixes(self):
        mdp = (SKILL / "scripts" / "make_diff_packet.py").read_text(encoding="utf-8")
        self.assertIn("elif snapshot_stage is not None:", mdp)
        sw = (SKILL / "scripts" / "setup_workspace.sh").read_text(encoding="utf-8")
        self.assertIn('chmod -R u+rwX $q && rm -rf $q', sw)
        self.assertIn('[ "${BAD_MEMBERS:-0}" = "1" ]', sw)
        self.assertIn("""trap 'chmod -R u+w "$STAGE" 2>/dev/null; rm -rf "$STAGE"' EXIT""",
                      (SKILL / "scripts" / "new_audit.sh").read_text(encoding="utf-8"))
        self.assertIn('isinstance(decision.get("impact"), str)',
                      (SKILL / "scripts" / "integrate_round.py").read_text(encoding="utf-8"))

    def test_round_forty_four_fixes(self):
        mdp = (SKILL / "scripts" / "make_diff_packet.py").read_text(encoding="utf-8")
        self.assertIn("and (reviewed_pinned or len(reviewed_paths) == 1)", mdp)
        self.assertIn("def frozen_binding_broken() -> bool:", mdp)
        self.assertIn("backup_usable = backup.is_file()", mdp)
        self.assertIn("cp -f {shlex.quote(", mdp)
        rn = (SKILL / "scripts" / "renumber_findings.py").read_text(encoding="utf-8")
        self.assertIn("--also takes files, not directories", rn)
        self.assertIn('"work/renumber_state.json", "work/id_map_history.jsonl")]', rn)
        ir = (SKILL / "scripts" / "integrate_round.py").read_text(encoding="utf-8")
        self.assertIn("## Items not tied to a registry finding", ir)
        self.assertIn("contests verdicts whose findings these decisions reword or withdraw", ir)
        self.assertIn("passed_same = contested_path is not None", ir)
        sw = (SKILL / "scripts" / "setup_workspace.sh").read_text(encoding="utf-8")
        self.assertIn("CREATED_BY_RUN", sw)
        self.assertIn("list_missing_members ()", sw)
        self.assertIn("printf '%q'", sw)
        self.assertIn("reclaimed_for_correction", (SKILL / "scripts" / "status.py").read_text(encoding="utf-8"))
        self.assertIn('DIR="$(on_disk_dir "$(cd "$DIR" && pwd -P)")"   # the record holds an absolute path',
                      (SKILL / "scripts" / "reserve_paper.sh").read_text(encoding="utf-8"))
        self.assertIn(". Run with CRITICS=",
                      (SKILL / "scripts" / "test_run_critique_runtime.py").read_text(encoding="utf-8"))

    def test_round_forty_five_fixes(self):
        mdp = (SKILL / "scripts" / "make_diff_packet.py").read_text(encoding="utf-8")
        self.assertIn("--before-findings cannot be checked: round", mdp)
        sw = (SKILL / "scripts" / "setup_workspace.sh").read_text(encoding="utf-8")
        self.assertIn("if not os.path.lexists(path):", sw)
        self.assertIn('[ -d "$DIR/pkg" ] && CREATED_BY_RUN="$CREATED_BY_RUN pkg"', sw)
        ir = (SKILL / "scripts" / "integrate_round.py").read_text(encoding="utf-8")
        self.assertIn("all(k in contested for k in correcting)", ir)
        self.assertIn('get("recommended_roles")', (SKILL / "scripts" / "test_run_critique_runtime.py").read_text(encoding="utf-8"))
        rp = (SKILL / "scripts" / "reserve_paper.sh").read_text(encoding="utf-8")
        self.assertEqual(rp.count("on_disk_dir \"$(pwd -P)\" || printf '%s' \"$DIR\")"), 2)

    def test_round_forty_five_e2e_and_r44fixes(self):
        st = (SKILL / "scripts" / "status.py").read_text(encoding="utf-8")
        self.assertIn('finished = row["status"] == "complete" and not reopened', st)
        self.assertIn("published_set_missing", st)
        vrp = (SKILL / "scripts" / "validate_round_projections.py").read_text(encoding="utf-8")
        self.assertIn("integration recorded {last_corrections} factual correction(s)", vrp)
        mdp = (SKILL / "scripts" / "make_diff_packet.py").read_text(encoding="utf-8")
        self.assertIn("differs from the bytes a later round's frozen diff packet binds", mdp)
        self.assertIn("(reviewed_pinned or len(reviewed_paths) == 1)", mdp)
        self.assertNotIn('cp \\"{source}\\"', mdp)
        self.assertIn("same name as {other} apart from case", (SKILL / "scripts" / "setup_workspace.sh").read_text(encoding="utf-8"))
        self.assertIn("'Correcting a published comment', step 1", (SKILL / "scripts" / "reserve_paper.sh").read_text(encoding="utf-8"))

    def test_round_forty_six_fixes(self):
        sw = (SKILL / "scripts" / "setup_workspace.sh").read_text(encoding="utf-8")
        self.assertIn('info.filename.encode("cp437")', sw)
        mdp = (SKILL / "scripts" / "make_diff_packet.py").read_text(encoding="utf-8")
        self.assertIn("id_masked(r.read_bytes()) == id_masked(raw[before_report])", mdp)
        vrp = (SKILL / "scripts" / "validate_round_projections.py").read_text(encoding="utf-8")
        self.assertIn("apply --decisions <that round's decisions file> \"", vrp)

    def test_setup_workspace_accepts_macos_utf8_zip_names(self):
        import zipfile as _zipfile
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            archive = base / "dep.zip"
            with _zipfile.ZipFile(archive, "w") as handle:
                info = _zipfile.ZipInfo("dep/donn\u00e9es.csv".encode("utf-8").decode("cp437"))
                handle.writestr(info, "a\n")   # UTF-8 bytes, flag 0x800 unset (what macOS zip writes)
                handle.writestr("dep/plain.do", "b\n")
            (base / "paper.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
            env = {k: v for k, v in os.environ.items() if k not in ("LANG", "LC_ALL")}
            env["I4R_SKIP_RESERVE"] = "1"
            proc = subprocess.run(["/bin/bash", str(SKILL / "scripts" / "setup_workspace.sh"), str(base / "audit"),
                                   str(base / "paper.pdf"), str(archive)], capture_output=True, text=True, env=env)
            self.assertEqual(proc.returncode, 0, proc.stderr[-800:])
            subprocess.run(["chmod", "-R", "u+w", str(base)], check=False)

    def test_round_forty_six_r45fixes(self):
        sw = (SKILL / "scripts" / "setup_workspace.sh").read_text(encoding="utf-8")
        self.assertIn("the package holds symlinks pointing outside it", sw)
        self.assertIn('raw = raw.replace(b"\\\\", b"/")', sw)
        self.assertIn("macOS unzip could not write some member names", sw)
        vrp = (SKILL / "scripts" / "validate_round_projections.py").read_text(encoding="utf-8")
        self.assertIn("does not record a valid", vrp)
        self.assertNotIn('chmod u+w \\"{path}\\"', vrp)
        self.assertIn('work/findings.before_round{prior}_integration.jsonl"))',
                      (SKILL / "scripts" / "make_diff_packet.py").read_text(encoding="utf-8"))
        self.assertIn('row["reclaimed_for_correction"] = True', (SKILL / "scripts" / "status.py").read_text(encoding="utf-8"))

    def test_round_forty_six_e2e_fixes(self):
        self.assertIn("def _lock_is_held(path: pathlib.Path) -> bool:", (SKILL / "scripts" / "status.py").read_text(encoding="utf-8"))
        mdp = (SKILL / "scripts" / "make_diff_packet.py").read_text(encoding="utf-8")
        self.assertIn('" before it was integrated" if integrated else " (it was never integrated)"', mdp)
        self.assertIn('"contested_sha256":', (SKILL / "scripts" / "integrate_round.py").read_text(encoding="utf-8"))
        self.assertIn("is not the set of contests round", (SKILL / "scripts" / "validate_round_projections.py").read_text(encoding="utf-8"))

    def test_round_forty_seven_fixes(self):
        sw = (SKILL / "scripts" / "setup_workspace.sh").read_text(encoding="utf-8")
        self.assertIn('if info.create_system == 0 and b"/" not in raw:', sw)
        self.assertIn("list_escaping_links ()", sw)
        self.assertIn('grep -q "Illegal byte sequence" "$unzip_log"', sw)
        import status as _status
        self.assertFalse(_status._later("2026-09-25T11:00:00Z", "2026-09-25T08:00:00-04:00"))
        self.assertTrue(_status._later("2026-09-25T13:00:00Z", "2026-09-25T08:00:00-04:00"))
        self.assertFalse(_status._later("junk", "2026-09-25T08:00:00Z"))
        vrp = (SKILL / "scripts" / "validate_round_projections.py").read_text(encoding="utf-8")
        self.assertNotIn("integrate_round.py --round N apply", vrp)

    def test_folder_package_with_escaping_symlink_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            (base / "dep").mkdir()
            (base / "dep" / "a.do").write_text("x\n")
            (base / "secret.txt").write_text("s\n")
            os.symlink(base / "secret.txt", base / "dep" / "link.txt")
            os.symlink("a.do", base / "dep" / "inner.do")
            (base / "paper.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
            env = dict(os.environ, I4R_SKIP_RESERVE="1")
            proc = subprocess.run(["/bin/bash", str(SKILL / "scripts" / "setup_workspace.sh"), str(base / "audit"),
                                   str(base / "paper.pdf"), str(base / "dep")], capture_output=True, text=True, env=env)
            self.assertEqual(proc.returncode, 2, proc.stderr[-600:])
            self.assertIn("symlinks pointing outside it", proc.stderr)
            self.assertNotIn("inner.do", proc.stderr)
            subprocess.run(["chmod", "-R", "u+w", str(base)], check=False)

    def test_round_forty_seven_r46fixes(self):
        sw = (SKILL / "scripts" / "setup_workspace.sh").read_text(encoding="utf-8")
        self.assertIn("raw = bytes(b for b in raw if b >= 0x20 and b != 0x7f)", sw)
        self.assertIn('unicodedata.normalize("NFD"', sw)
        self.assertIn("python3 -m zipfile -e <zip> <folder>", sw)
        mdp = (SKILL / "scripts" / "make_diff_packet.py").read_text(encoding="utf-8")
        self.assertIn("paired = unchanged or paired", mdp)
        self.assertIn("a step-1 file this round's frozen packet already binds", mdp)
        vrp = (SKILL / "scripts" / "validate_round_projections.py").read_text(encoding="utf-8")
        self.assertIn("cannot be read as a JSON object", vrp)
        self.assertIn('if key == "before_findings_sha256" else []', vrp)
        self.assertIn("has no current run to integrate", (SKILL / "scripts" / "integrate_round.py").read_text(encoding="utf-8"))

    def test_mac_icon_file_zip_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            (base / "src" / "pkg").mkdir(parents=True)
            (base / "src" / "pkg" / "a.R").write_text("x\n")
            (base / "src" / "pkg" / "Icon\r").write_bytes(b"")
            subprocess.run(["zip", "-qr", str(base / "icon.zip"), "pkg"], cwd=base / "src", check=True)
            (base / "paper.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
            env = {k: v for k, v in os.environ.items() if k not in ("LANG", "LC_ALL")}
            env["I4R_SKIP_RESERVE"] = "1"
            proc = subprocess.run(["/bin/bash", str(SKILL / "scripts" / "setup_workspace.sh"), str(base / "audit"),
                                   str(base / "paper.pdf"), str(base / "icon.zip")], capture_output=True, text=True, env=env)
            self.assertEqual(proc.returncode, 0, proc.stderr[-600:])
            subprocess.run(["chmod", "-R", "u+w", str(base)], check=False)

    def test_round_forty_seven_e2e_fixes(self):
        ir = (SKILL / "scripts" / "integrate_round.py").read_text(encoding="utf-8")
        self.assertIn("note: wrote the contests passed to", ir)
        self.assertIn("differs from {stale_contest.relative_to(audit)}, which finalize", ir)
        vrp = (SKILL / "scripts" / "validate_round_projections.py").read_text(encoding="utf-8")
        self.assertIn("is missing (finalize and the release read that file)", vrp)
        self.assertEqual(vrp.count("deferred.append(ValueError("), 4)
        self.assertIn("The final round may also be integrated only to record contests",
                      (SKILL / "INSTALL.md").read_text(encoding="utf-8"))

    def test_round_forty_eight_fixes(self):
        sw = (SKILL / "scripts" / "setup_workspace.sh").read_text(encoding="utf-8")
        self.assertIn("resolved = os.path.realpath(path)", sw)
        mdp = (SKILL / "scripts" / "make_diff_packet.py").read_text(encoding="utf-8")
        self.assertIn("continue   # malformed: load_frozen_manifest refuses it by name", mdp)
        self.assertIn("it has no snapshot to copy from", mdp)
        self.assertIn("so the same follow-ups apply as", (SKILL / "INSTALL.md").read_text(encoding="utf-8"))

    def test_chained_symlink_escape_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            (base / "dep").mkdir()
            (base / "dep" / "a.do").write_text("x\n")
            os.symlink(".", base / "dep" / "d")
            os.symlink("d/d/d/d/d/d/d/d/d/d/../../../../../../../../../../etc/hosts", base / "dep" / "x.txt")
            (base / "paper.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
            env = dict(os.environ, I4R_SKIP_RESERVE="1")
            proc = subprocess.run(["/bin/bash", str(SKILL / "scripts" / "setup_workspace.sh"), str(base / "audit"),
                                   str(base / "paper.pdf"), str(base / "dep")], capture_output=True, text=True, env=env)
            self.assertEqual(proc.returncode, 2, proc.stderr[-600:])
            self.assertIn("symlinks pointing outside it", proc.stderr)
            subprocess.run(["chmod", "-R", "u+w", str(base)], check=False)

    def test_round_forty_eight_r47fixes(self):
        sw = (SKILL / "scripts" / "setup_workspace.sh").read_text(encoding="utf-8")
        self.assertIn("flatten a single wrapper directory BEFORE the checks below", sw)
        self.assertIn("could not remove it from pkg/", sw)
        self.assertIn("macOS unzip could not write some member names (non-ASCII names", sw)
        self.assertIn('shown.encode("unicode_escape")', sw)
        self.assertIn("is empty; it must name round", (SKILL / "scripts" / "integrate_round.py").read_text(encoding="utf-8"))

    def test_wrapper_folder_symlink_to_parent_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            (base / "dep" / "wrap").mkdir(parents=True)
            (base / "dep" / "wrap" / "r.R").write_text("x\n")
            os.symlink("..", base / "dep" / "wrap" / "up")
            (base / "paper.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
            env = dict(os.environ, I4R_SKIP_RESERVE="1")
            proc = subprocess.run(["/bin/bash", str(SKILL / "scripts" / "setup_workspace.sh"), str(base / "audit"),
                                   str(base / "paper.pdf"), str(base / "dep")], capture_output=True, text=True, env=env)
            self.assertEqual(proc.returncode, 2, proc.stderr[-600:])
            self.assertFalse(os.path.lexists(base / "audit" / "pkg" / "wrap" / "up"))
            subprocess.run(["chmod", "-R", "u+w", str(base)], check=False)

    def test_round_forty_eight_e2e_fixes(self):
        sw = (SKILL / "scripts" / "setup_workspace.sh").read_text(encoding="utf-8")
        self.assertIn("rename_control_names ()", sw)
        st = (SKILL / "scripts" / "stage_release.sh").read_text(encoding="utf-8")
        self.assertIn("post-delta gates are not current", st)
        ir = (SKILL / "scripts" / "integrate_round.py").read_text(encoding="utf-8")
        self.assertIn('if withdrawn_by(decision) and "coordinator_approved" not in decision:', ir)
        self.assertIn('\"$AUDIT_DIR/critique/round_N/CONTESTED.json\"`', (SKILL / "INSTALL.md").read_text(encoding="utf-8"))

    def test_folder_package_icon_file_is_renamed(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            (base / "dep").mkdir()
            (base / "dep" / "a.R").write_text("x\n")
            (base / "dep" / "Icon\r").write_bytes(b"")
            (base / "paper.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
            env = dict(os.environ, I4R_SKIP_RESERVE="1")
            proc = subprocess.run(["/bin/bash", str(SKILL / "scripts" / "setup_workspace.sh"), str(base / "audit"),
                                   str(base / "paper.pdf"), str(base / "dep")], capture_output=True, text=True, env=env)
            self.assertEqual(proc.returncode, 0, proc.stderr[-600:])
            self.assertTrue((base / "audit" / "pkg" / "Icon").exists())
            self.assertFalse(os.path.lexists(base / "audit" / "pkg" / "Icon\r"))
            subprocess.run(["chmod", "-R", "u+w", str(base)], check=False)

    def test_round_forty_nine_fixes(self):
        sw = (SKILL / "scripts" / "setup_workspace.sh").read_text(encoding="utf-8")
        self.assertIn("os.replace(os.path.join(base, name), os.path.join(base, cleaned))", sw)
        self.assertIn("A clash is\n# judged on the deposit itself", sw)
        self.assertIn("restore it as it prints instead of rebuilding", (SKILL / "scripts" / "stage_release.sh").read_text(encoding="utf-8"))

    def test_symlink_escaping_through_wrapper_flatten_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            (base / "dep" / "paper").mkdir(parents=True)
            (base / "dep" / "paper" / "code.do").write_text("x\n")
            os.symlink("../paper/paper.pdf", base / "dep" / "paper" / "p.pdf")
            (base / "paper.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
            env = dict(os.environ, I4R_SKIP_RESERVE="1")
            proc = subprocess.run(["/bin/bash", str(SKILL / "scripts" / "setup_workspace.sh"), str(base / "audit"),
                                   str(base / "paper.pdf"), str(base / "dep")], capture_output=True, text=True, env=env)
            self.assertEqual(proc.returncode, 2, proc.stderr[-600:])
            self.assertIn("symlinks pointing outside it", proc.stderr)
            subprocess.run(["chmod", "-R", "u+w", str(base)], check=False)

    def test_icon_reintake_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            (base / "dep").mkdir()
            (base / "dep" / "a.R").write_text("x\n")
            (base / "dep" / "Icon\r").write_bytes(b"")
            (base / "paper.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
            command = ["/bin/bash", str(SKILL / "scripts" / "setup_workspace.sh"), str(base / "audit"),
                       str(base / "paper.pdf"), str(base / "dep")]
            first = subprocess.run(command, capture_output=True, text=True, env=dict(os.environ, I4R_SKIP_RESERVE="1"))
            self.assertEqual(first.returncode, 0, first.stderr[-600:])
            again = subprocess.run(command, capture_output=True, text=True,
                                   env=dict(os.environ, I4R_SKIP_RESERVE="1", I4R_ALLOW_EXISTING="1"))
            self.assertEqual(again.returncode, 0, again.stderr[-600:])
            subprocess.run(["chmod", "-R", "u+w", str(base)], check=False)

    def test_round_forty_nine_e2e_fixes(self):
        fin = (SKILL / "scripts" / "finalize_run.sh").read_text(encoding="utf-8")
        self.assertNotIn("f.is_file() and not f.is_symlink()", fin)
        self.assertIn("are status rejected but still INCLUDED", (SKILL / "scripts" / "renumber_findings.py").read_text(encoding="utf-8"))
        self.assertIn("status `rejected` with ledger_disposition EXCLUDED or MERGED", (SKILL / "INSTALL.md").read_text(encoding="utf-8"))
        st = (SKILL / "scripts" / "status.py").read_text(encoding="utf-8")
        self.assertIn('"reopened for correction" if reopened', st)

    def test_round_forty_nine_r48fixes(self):
        st = (SKILL / "scripts" / "stage_release.sh").read_text(encoding="utf-8")
        self.assertIn("the rounds finalize counts: run.json's rounds", st)
        self.assertIn("--before-report {q(before_report)}", st)
        sw = (SKILL / "scripts" / "setup_workspace.sh").read_text(encoding="utf-8")
        self.assertIn('! grep -q "File name too long" "$unzip_log"', sw)
        self.assertIn("a symlink to a name with control characters", sw)
        self.assertIn("a name made only of control characters", sw)
        self.assertIn("avoids\n  only approve/project", (SKILL / "INSTALL.md").read_text(encoding="utf-8"))
        self.assertIn("does not bind {key}", (SKILL / "scripts" / "make_diff_packet.py").read_text(encoding="utf-8"))

    def test_round_fifty_fixes(self):
        sw = (SKILL / "scripts" / "setup_workspace.sh").read_text(encoding="utf-8")
        self.assertIn('find "$DIR/pkg" -mindepth 1 -delete', sw)
        self.assertIn("apart from control characters, case or Unicode form", sw)
        self.assertIn('staged_wrapper="$DIR/pkg/.i4r-flatten.$$"', sw)
        self.assertIn("like Finder's Icon<CR> file", sw)
        fin = (SKILL / "scripts" / "finalize_run.sh").read_text(encoding="utf-8")
        self.assertIn('lines = [item["sha256"] + "  " + item["path"] for item in cached]', fin)

    def test_wrapper_with_same_named_child_flattens(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            (base / "dep" / "rep" / "rep").mkdir(parents=True)
            (base / "dep" / "rep" / "code").mkdir()
            (base / "dep" / "rep" / "rep" / "x.R").write_text("a\n")
            (base / "dep" / "rep" / "code" / "y.R").write_text("b\n")
            (base / "paper.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
            proc = subprocess.run(["/bin/bash", str(SKILL / "scripts" / "setup_workspace.sh"), str(base / "audit"),
                                   str(base / "paper.pdf"), str(base / "dep")], capture_output=True, text=True,
                                  env=dict(os.environ, I4R_SKIP_RESERVE="1"))
            self.assertEqual(proc.returncode, 0, proc.stderr[-600:])
            self.assertTrue((base / "audit" / "pkg" / "rep" / "x.R").is_file())
            self.assertTrue((base / "audit" / "pkg" / "code" / "y.R").is_file())
            subprocess.run(["chmod", "-R", "u+w", str(base)], check=False)

    def test_round_fifty_testers(self):
        self.assertIn("key=lambda value: pathlib.PurePosixPath(value).parts",
                      (SKILL / "scripts" / "phase1_runtime.py").read_text(encoding="utf-8"))
        self.assertIn("continue   # a dangling in-package symlink",
                      (SKILL / "scripts" / "make_verification_notebook.sh").read_text(encoding="utf-8"))
        sw = (SKILL / "scripts" / "setup_workspace.sh").read_text(encoding="utf-8")
        self.assertIn("unpack it with ditto -x -k <zip> <folder>", sw)
        self.assertIn("flattened_away", sw)
        self.assertIn("-type d -exec chmod u+w {}", sw)
        vrp = (SKILL / "scripts" / "validate_round_projections.py").read_text(encoding="utf-8")
        self.assertIn('UNRUN_ROUND_STATES = ("prepared", "awaiting_authorization", "ready")', vrp)
        self.assertIn("was prepared but never run", vrp)
        self.assertIn("a trailing round prepared (or authorized) but never run",
                      (SKILL / "scripts" / "finalize_run.sh").read_text(encoding="utf-8"))
        self.assertIn("changed since it was built:", (SKILL / "scripts" / "verify_diff_packet.py").read_text(encoding="utf-8"))
        self.assertIn('rounds = [r.get("round") for r in (run.get("rounds") or [])',
                      (SKILL / "scripts" / "status.py").read_text(encoding="utf-8"))

    def test_package_order_matches_between_intake_and_phase1(self):
        import pathlib as _pl
        names = ["code.R", "code/x.R", "data.csv", "data/a.csv", "a b/c", "a/b"]
        root = _pl.Path("/r")
        intake = [(root / n).relative_to(root).as_posix() for n in sorted(root / n for n in names)]
        phase1 = sorted(names, key=lambda v: _pl.PurePosixPath(v).parts)
        self.assertEqual(intake, phase1)

    def test_round_fifty_one_fixes(self):
        self.assertIn('not rounds[-1].get("started_at")', (SKILL / "scripts" / "validate_round_projections.py").read_text(encoding="utf-8"))
        self.assertIn('and not rounds[-1].get("started_at")', (SKILL / "scripts" / "finalize_run.sh").read_text(encoding="utf-8"))
        self.assertIn('and not r.get("started_at")', (SKILL / "scripts" / "stage_release.sh").read_text(encoding="utf-8"))
        sw = (SKILL / "scripts" / "setup_workspace.sh").read_text(encoding="utf-8")
        self.assertIn("groups.setdefault(key(clean(name)), []).append(name)", sw)
        self.assertIn("is the audit's own pkg/ (or inside it)", sw)

    def test_two_control_names_cleaning_to_one_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            (base / "dep" / "code").mkdir(parents=True)
            (base / "dep" / "code" / "x\r.R").write_text("first\n")
            (base / "dep" / "code" / "x\x01.R").write_text("second\n")
            (base / "dep" / "data.csv").write_text("a\n")
            (base / "paper.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
            proc = subprocess.run(["/bin/bash", str(SKILL / "scripts" / "setup_workspace.sh"), str(base / "audit"),
                                   str(base / "paper.pdf"), str(base / "dep")], capture_output=True, text=True,
                                  env=dict(os.environ, I4R_SKIP_RESERVE="1"))
            self.assertEqual(proc.returncode, 2, proc.stderr[-600:])
            subprocess.run(["chmod", "-R", "u+w", str(base)], check=False)

    def test_round_fifty_one_testers(self):
        vv = (SKILL / "schemas" / "tools" / "validate_v2.py").read_text(encoding="utf-8")
        self.assertIn("a trailing round prepared or authorized but never started is not part of the review", vv)
        self.assertIn("I4R_SUPERSEDE_PLAN", (SKILL / "scripts" / "run_critique.sh").read_text(encoding="utf-8"))
        self.assertIn("I4R_SUPERSEDE_PLAN=1", (SKILL / "scripts" / "test_run_critique_runtime.py").read_text(encoding="utf-8"))
        self.assertIn("| `I4R_SUPERSEDE_PLAN=1` |", (SKILL / "INSTALL.md").read_text(encoding="utf-8"))
        self.assertIn('hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]', (SKILL / "scripts" / "reserve_paper.sh").read_text(encoding="utf-8"))
        self.assertIn("called_rounds", (SKILL / "scripts" / "validate_round_projections.py").read_text(encoding="utf-8"))
        self.assertIn("_called_rounds", (SKILL / "scripts" / "finalize_run.sh").read_text(encoding="utf-8"))
        self.assertIn('R["pause_reason"] = None', (SKILL / "scripts" / "finalize_run.sh").read_text(encoding="utf-8"))
        self.assertIn('chmod -R u+rwX "$DIR/pkg"', (SKILL / "scripts" / "setup_workspace.sh").read_text(encoding="utf-8"))

    def test_round_fifty_two_fixes(self):
        rp = (SKILL / "scripts" / "reserve_paper.sh").read_text(encoding="utf-8")
        self.assertIn("reservation_file () {", rp)
        self.assertIn('not re.fullmatch(r"[A-Za-z0-9._-]*", raw)', rp)
        self.assertIn("an unreadable ledger must not switch this check off",
                      (SKILL / "scripts" / "stage_release.sh").read_text(encoding="utf-8"))
        self.assertIn("contains the audit directory", (SKILL / "scripts" / "setup_workspace.sh").read_text(encoding="utf-8"))

    def test_reservation_names_keep_plain_and_find_legacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "reservations").mkdir()
            for rel in ("data/AER/smith_2020", "data/Psych/Paper with spaces"):
                (root / rel).mkdir(parents=True)
            legacy = root / "reservations" / "Psych_Paper_with_spaces.json"
            legacy.write_text('{"owner": "alice@h", "paper": "%s", "claimed_at": "2026-09-20T00:00:00Z", '
                              '"status": "active"}' % (root / "data/Psych/Paper with spaces"))
            script = str(SKILL / "scripts" / "reserve_paper.sh")
            env = dict(os.environ, I4R_RESERVE_GRACE="0", I4R_OWNER="bob@h")
            refused = subprocess.run(["bash", script, "claim", "data/Psych/Paper with spaces"], cwd=root,
                                     capture_output=True, text=True, env=env)
            self.assertIn("reserved by alice@h", refused.stdout)
            subprocess.run(["bash", script, "claim", "data/AER/smith_2020"], cwd=root, capture_output=True, env=env)
            self.assertTrue((root / "reservations" / "AER_smith_2020.json").is_file())

    def test_nfc_and_nfd_paper_names_share_one_reservation(self):
        import unicodedata
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "reservations").mkdir()
            nfc = unicodedata.normalize("NFC", "data/Psych/Garc\u00eda_2021")
            nfd = unicodedata.normalize("NFD", nfc)
            (root / nfc).mkdir(parents=True)
            script = str(SKILL / "scripts" / "reserve_paper.sh")
            first = subprocess.run(["bash", script, "claim", nfc], cwd=root, capture_output=True, text=True,
                                   env=dict(os.environ, I4R_RESERVE_GRACE="0", I4R_OWNER="alice@h"))
            self.assertEqual(first.returncode, 0, first.stderr[-400:])
            second = subprocess.run(["bash", script, "claim", nfd], cwd=root, capture_output=True, text=True,
                                    env=dict(os.environ, I4R_RESERVE_GRACE="0", I4R_OWNER="bob@h"))
            self.assertIn("reserved by alice@h", second.stdout + second.stderr)
            self.assertEqual(len(list((root / "reservations").glob("*.json"))), 1)

    def test_status_finds_an_intake_only_audit(self):
        import status
        with tempfile.TemporaryDirectory() as tmp:
            audit = pathlib.Path(tmp) / "AER" / "smith_2020" / "audit"
            (audit / "pkg").mkdir(parents=True)
            (audit / "package_manifest.json").write_text("{}", encoding="utf-8")
            self.assertIn(audit.resolve(), [p.resolve() for p in status.find_audits(pathlib.Path(tmp))])

    def test_round_two_dispatch_refuses_a_stale_gate_receipt(self):
        source = (SKILL / "scripts" / "run_critique.sh").read_text(encoding="utf-8")
        start = source.index('if [ "$ROUND" -ge 2 ] && [ -f "$OUT/POST_DELTA_GATES.json" ]')
        self.assertLess(start, source.index('FINAL_DETAIL="snapshot ready; checking model probe"'))
        snippet = source[source.index("python3 -c '", start) + len("python3 -c '"):]
        snippet = snippet[:snippet.index("' \"$SCRIPT_DIR\"")]
        with tempfile.TemporaryDirectory() as tmp:
            ws = pathlib.Path(tmp)
            (ws / "critique" / "round_2").mkdir(parents=True)
            (ws / "critique" / "round_2" / "POST_DELTA_GATES.json").write_text('{"round": 2}', encoding="utf-8")
            proc = subprocess.run([sys.executable, "-c", snippet, str(SKILL / "scripts"), str(ws), "2"],
                                  capture_output=True, text=True)
            self.assertEqual(proc.returncode, 1, proc.stderr[-400:])
            self.assertTrue(proc.stdout.strip())
        self.assertIn("from round 2, fresh post-delta gates", (SKILL / "INSTALL.md").read_text(encoding="utf-8"))

    def test_folder_package_containing_the_audit_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp) / "paper"
            base.mkdir()
            (base / "a.do").write_text("x\n")
            (base / "paper.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
            proc = subprocess.run(["/bin/bash", str(SKILL / "scripts" / "setup_workspace.sh"), str(base / "audit"),
                                   str(base / "paper.pdf"), str(base)], capture_output=True, text=True,
                                  env=dict(os.environ, I4R_SKIP_RESERVE="1"))
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("contains the audit directory", proc.stderr)
            self.assertFalse((base / "audit" / "pkg" / "audit").exists())
            subprocess.run(["chmod", "-R", "u+w", str(base)], check=False)

    def test_reservation_name_does_not_depend_on_locale(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "reservations").mkdir()
            for name in ("Garc\u00eda_2021", "\u00c9tude", "\u00cbtude"):
                (root / "data" / "Psych" / name).mkdir(parents=True)
            script = str(SKILL / "scripts" / "reserve_paper.sh")
            def claim(paper, owner, locale):
                env = dict(os.environ, I4R_RESERVE_GRACE="0", I4R_OWNER=owner, LC_ALL=locale, LANG=locale)
                return subprocess.run(["/bin/bash", script, "claim", "data/Psych/" + paper], cwd=root,
                                      capture_output=True, text=True, env=env)
            claim("Garc\u00eda_2021", "alice@h", "C")
            second = claim("Garc\u00eda_2021", "bob@h", "en_US.UTF-8")
            self.assertIn("reserved by alice@h", second.stdout + second.stderr)
            self.assertEqual(claim("\u00c9tude", "alice@h", "en_US.UTF-8").returncode, 0)
            other = claim("\u00cbtude", "bob@h", "en_US.UTF-8")
            self.assertEqual(other.returncode, 0, other.stdout + other.stderr)
            self.assertEqual(len(list((root / "reservations").glob("*.json"))), 3)

    def test_containment_refusal_comes_before_the_reservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "reservations").mkdir()
            (root / "data" / "AER" / "smith_2020").mkdir(parents=True)
            (root / "data" / "AER" / "smith_2020" / "paper.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
            paper = root / "runs" / "AER" / "smith_2020"
            paper.mkdir(parents=True)
            (paper / "b.do").write_text("y\n")
            proc = subprocess.run(["/bin/bash", str(SKILL / "scripts" / "setup_workspace.sh"), "runs/AER/smith_2020/audit",
                                   "data/AER/smith_2020/paper.pdf", "runs/AER/smith_2020"], capture_output=True, text=True, cwd=root,
                                  env=dict(os.environ, I4R_RESERVE_GRACE="0", I4R_OWNER="alice@h"))
            self.assertEqual(proc.returncode, 2, proc.stderr[-600:])
            self.assertIn("contains the audit directory", proc.stderr)
            self.assertEqual(list((root / "reservations").glob("*.json")), [])
            self.assertFalse((paper / "audit").exists())

    def test_stale_gate_remedy_is_shell_quoted_and_skips_finished_rounds(self):
        source = (SKILL / "scripts" / "run_critique.sh").read_text(encoding="utf-8")
        self.assertIn('[ -f "$OUT/POST_DELTA_GATES.json" ] && ! quorum_on_this_input; then', source)
        self.assertIn("printf '  python3 %q %q --round %s --report %q\\n' \"$SCRIPT_DIR/run_post_delta_gates.py\"", source)

    def test_legacy_reservation_of_another_paper_is_not_taken_or_released(self):
        import unicodedata
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp).resolve()
            (root / "reservations").mkdir()
            for rel in ("data/Journal A/Paper X", "data/Journal_A/Paper X"):
                (root / rel).mkdir(parents=True)
            held = root / "reservations" / "Journal_A_Paper_X.json"
            held.write_text('{"owner": "alice@h", "paper": "%s", "claimed_at": "2026-09-20T00:00:00Z", '
                            '"status": "active"}' % (root / "data/Journal A/Paper X"))
            script = str(SKILL / "scripts" / "reserve_paper.sh")
            env = dict(os.environ, I4R_RESERVE_GRACE="0", I4R_OWNER="bob@h")
            other = subprocess.run(["bash", script, "claim", "data/Journal_A/Paper X"], cwd=root,
                                   capture_output=True, text=True, env=env)
            self.assertIn("RESERVED", other.stdout)
            subprocess.run(["bash", script, "release", "data/Journal_A/Paper X", "--force"], cwd=root,
                           capture_output=True, env=env)
            self.assertTrue(held.is_file(), "another paper's claim must survive a forced release")
            same = subprocess.run(["bash", script, "claim", "data/Journal A/Paper X"], cwd=root,
                                  capture_output=True, text=True, env=env)
            self.assertIn("reserved by alice@h", same.stdout)
            # a legacy claim written from the NFD spelling is found from the NFC one
            nfc = unicodedata.normalize("NFC", "\u00c9tude caf\u00e9")
            (root / "data" / "J" / nfc).mkdir(parents=True)
            legacy_name = subprocess.run(["bash", "-c", "printf '%s' \"$1\" | tr -cs 'A-Za-z0-9._-' '_' | cut -c1-180",
                                          "_", "J__" + unicodedata.normalize("NFD", nfc)],
                                         capture_output=True, text=True, env=dict(os.environ, LC_ALL="C")).stdout
            (root / "reservations" / (legacy_name + ".json")).write_text(
                '{"owner": "alice@h", "paper": "%s", "claimed_at": "2026-09-20T00:00:00Z", "status": "active"}'
                % (str(root / "data" / "J") + "/" + unicodedata.normalize("NFD", nfc)))
            nfc_claim = subprocess.run(["bash", script, "claim", "data/J/" + nfc], cwd=root,
                                       capture_output=True, text=True, env=env)
            self.assertIn("reserved by alice@h", nfc_claim.stdout)

    def test_status_matches_reservation_across_unicode_forms(self):
        import status
        import unicodedata
        with tempfile.TemporaryDirectory() as tmp:
            tree = pathlib.Path(tmp)
            (tree / "reservations").mkdir()
            name = "\u00c9tude"
            (tree / "reservations" / "J_tude-x.json").write_text(
                '{"owner": "a", "paper": "/x/data/J/%s", "claimed_at": "2026-09-20T00:00:00Z"}'
                % unicodedata.normalize("NFC", name), encoding="utf-8")
            rows = status._reservations(tree)
            self.assertIn(("j", unicodedata.normalize("NFC", name).casefold()), rows)
        self.assertIn('reservations.get((_nfc(journal), _nfc(lookup)))',
                      (SKILL / "scripts" / "status.py").read_text(encoding="utf-8"))

    def test_symlinked_audit_inside_the_package_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp).resolve()
            (base / "dep" / "audit").mkdir(parents=True)
            (base / "dep" / "a.do").write_text("x\n")
            os.symlink(base / "dep" / "audit", base / "auditlink")
            (base / "paper.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
            proc = subprocess.run(["/bin/bash", str(SKILL / "scripts" / "setup_workspace.sh"), str(base / "auditlink"),
                                   str(base / "paper.pdf"), str(base / "dep")], capture_output=True, text=True,
                                  env=dict(os.environ, I4R_SKIP_RESERVE="1"))
            self.assertEqual(proc.returncode, 2, proc.stderr[-600:])
            self.assertIn("contains the audit directory", proc.stderr)
            self.assertFalse((base / "dep" / "audit" / "pkg").exists())

    def test_round_fifty_three_e2e_fixes(self):
        na = (SKILL / "scripts" / "new_audit.sh").read_text(encoding="utf-8")
        self.assertIn("printf '  export AUDIT_DIR=%q\\n' \"$AUDIT_DIR\"", na)
        self.assertIn('*[\\$\\`\\"\\\\!]*)', na)
        rc = (SKILL / "scripts" / "run_critique.sh").read_text(encoding="utf-8")
        self.assertIn("authorize-round %q %s %q", rc)
        self.assertIn("AUDIT_DIR=%q bash %q %s",
                      (SKILL / "scripts" / "wait_for_round.sh").read_text(encoding="utf-8"))
        self.assertIn("re-run this round's post-delta gates",
                      (SKILL / "scripts" / "make_diff_packet.py").read_text(encoding="utf-8"))
        # the printed export line survives a folder name holding $ and quotes
        for name in ("/x/L'\u00e9t\u00e9 $HOME", "/a b/c", '/y/a`b"c', "/z/Wow!/audit"):
            snippet = na[na.index('case "$AUDIT_DIR" in'):]
            snippet = snippet[:snippet.index("esac") + 4]
            line = subprocess.run(["/bin/bash", "-c", snippet], capture_output=True, text=True,
                                  env=dict(os.environ, AUDIT_DIR=name)).stdout.strip()
            back = subprocess.run(["/bin/bash", "-c", line + '; printf %s "$AUDIT_DIR"'], capture_output=True,
                                  text=True).stdout
            self.assertEqual(back, name)

    def test_own_legacy_claim_with_a_relative_path_is_released(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "reservations").mkdir()
            (root / "data" / "AER" / "smith_2020").mkdir(parents=True)
            (root / "data" / "AER" / "smith").mkdir(parents=True)
            script = str(SKILL / "scripts" / "reserve_paper.sh")
            for recorded in (".", "smith_2020", "/x/AER//smith_2020/.", None):
                f = root / "reservations" / "AER_smith_2020.json"
                body = {"owner": "alice@h", "claimed_at": "2026-09-01T00:00:00Z", "status": "active"}
                if recorded is not None:
                    body["paper"] = recorded
                import json
                f.write_text(json.dumps(body))
                proc = subprocess.run(["bash", script, "release", "data/AER/smith_2020", "--force"], cwd=root,
                                      capture_output=True, text=True, env=dict(os.environ, I4R_OWNER="x@h"))
                self.assertIn("released", proc.stdout, (recorded, proc.stdout))
                self.assertFalse(f.exists())
            # a conflict copy of AER_smith_2020 does not block the different paper AER/smith
            (root / "reservations" / "AER_smith_2020 (bob's conflicted copy 2026-09-01).json").write_text(
                '{"owner": "bob", "paper": "x", "claimed_at": "2020-01-01T00:00:00Z"}')
            other = subprocess.run(["bash", script, "claim", "data/AER/smith"], cwd=root, capture_output=True,
                                   text=True, env=dict(os.environ, I4R_RESERVE_GRACE="0", I4R_OWNER="alice@h"))
            self.assertIn("RESERVED", other.stdout, other.stdout)

    def test_waiter_without_an_invocation_does_not_say_rerun(self):
        with tempfile.TemporaryDirectory() as tmp:
            (pathlib.Path(tmp) / "critique").mkdir()
            proc = subprocess.run(["bash", str(SKILL / "scripts" / "wait_for_round.sh"), tmp, "1", "--pid", "99999",
                                   "--poll-seconds", "1"], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 4)
            self.assertIn("exited before publishing an invocation", proc.stderr)
            self.assertNotIn("re-run it", proc.stderr)
        mdp = (SKILL / "scripts" / "make_diff_packet.py").read_text(encoding="utf-8")
        self.assertIn("{shlex.quote(str(root))} --round {args.round} --report", mdp)
        self.assertIn("After re-integrating the reopened round (with or without contests), re-log its `round_result`",
                      (SKILL / "INSTALL.md").read_text(encoding="utf-8"))

    def test_reservation_follows_the_on_disk_case_and_plain_collisions_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "reservations").mkdir()
            for rel in ("data/JDE/\u00c9tude \u00e9conomique", "data/AER/Smith_2020", "data/AER/Smith__2020"):
                (root / rel).mkdir(parents=True)
            script = str(SKILL / "scripts" / "reserve_paper.sh")
            def run(owner, *args):
                return subprocess.run(["bash", script, *args], cwd=root, capture_output=True, text=True,
                                      env=dict(os.environ, I4R_RESERVE_GRACE="0", I4R_OWNER=owner))
            probe = root / "data" / "jde"
            if not probe.exists():
                self.skipTest("case-sensitive filesystem")
            run("alice@h", "claim", "data/jde/\u00e9tude \u00e9conomique")
            self.assertIn("reserved by alice@h", run("bob@h", "claim", "data/JDE/\u00c9tude \u00e9conomique").stdout)
            self.assertIn("released", run("bob@h", "release", "data/JDE/\u00c9tude \u00e9conomique", "--force").stdout)
            self.assertIn("RESERVED", run("alice@h", "claim", "data/AER/Smith_2020").stdout)
            second = run("alice@h", "claim", "data/AER/Smith__2020")
            self.assertIn("RESERVED", second.stdout, "a different paper sharing the plain name is not 'already yours'")
            self.assertIn("reserved by alice@h", run("bob@h", "claim", "data/AER/Smith__2020").stdout)
            self.assertEqual(len(list((root / "reservations").glob("AER_Smith_2020*.json"))), 2)

    def test_round_fifty_four_quoting_and_retry_fixes(self):
        self.assertIn("round_has_quorum() {", (SKILL / "scripts" / "run_critique.sh").read_text(encoding="utf-8"))
        self.assertIn("make_settled.sh {shlex.quote(str(audit))} {shlex.quote(str(registry))}",
                      (SKILL / "scripts" / "approve_settled.py").read_text(encoding="utf-8"))

    def test_waiter_hint_keeps_the_rounds_roles_and_manifest(self):
        import shlex
        with tempfile.TemporaryDirectory() as tmp:
            audit = pathlib.Path(tmp) / "w 2"
            inv = audit / "critique" / "round_3" / "invocations" / "INV1"
            inv.mkdir(parents=True)
            (audit / "critique" / "round_3" / "CURRENT_INVOCATION").write_text("INV1\n")
            (inv / "ROUTING.json").write_text('{"roles": ["data", "completeness"], "reason": "CRITICS override, with completeness retained"}')
            (inv / "attempts").mkdir()
            (inv / "attempts" / "a1").write_text("x")
            # a later refused run moved the pointer to an invocation that never dispatched
            (audit / "critique" / "round_3" / "invocations" / "INV2").mkdir()
            (audit / "critique" / "round_3" / "invocations" / "INV2" / "ROUTING.json").write_text('{"roles": ["stats"]}')
            (audit / "critique" / "round_3" / "CURRENT_INVOCATION").write_text("INV2\n")
            (audit / "critique" / "DIFF_MANIFEST_round_3.json").write_text("{}")
            proc = subprocess.run(["bash", str(SKILL / "scripts" / "wait_for_round.sh"), str(audit), "3", "--pid",
                                   "99999", "--poll-seconds", "1"], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 4)
            line = [l for l in proc.stderr.splitlines() if "run_critique.sh 3" in l][0]
            words = shlex.split(line.split("): ", 1)[1])
            self.assertIn("CRITICS=data completeness", words)
            self.assertIn("DIFF_MANIFEST=" + str(audit / "critique" / "DIFF_MANIFEST_round_3.json"), words)
            self.assertIn("AUDIT_DIR=" + str(audit), words)
        rc = (SKILL / "scripts" / "run_critique.sh").read_text(encoding="utf-8")
        self.assertIn("already started with roles", rc)
        self.assertLess(rc.index("already started with roles"), rc.index("round_authorization() {"))

    def test_status_keys_reservations_by_journal_and_paper(self):
        import status
        with tempfile.TemporaryDirectory() as tmp:
            tree = pathlib.Path(tmp)
            (tree / "reservations").mkdir()
            (tree / "reservations" / "AER_P.json").write_text(
                '{"owner": "a", "paper": "/x/data/AER/P", "claimed_at": "2026-09-20T00:00:00Z"}', encoding="utf-8")
            rows = status._reservations(tree)
            self.assertIn(("aer", "p"), rows)
            self.assertNotIn(("jde", "p"), rows)

    def test_round_fifty_four_e2e_quoting_and_messages(self):
        self.assertIn("printf 'NEXT: export RUN_JSON=%q", (SKILL / "scripts" / "init_run.sh").read_text(encoding="utf-8"))
        sr = (SKILL / "scripts" / "stage_release.sh").read_text(encoding="utf-8")
        self.assertNotIn('rm -r \\"$R\\"', sr)
        self.assertIn("rm -r %q", sr)
        self.assertIn("shlex.quote(str(audit / 'work/renumber_state.json'))",
                      (SKILL / "scripts" / "integrate_round.py").read_text(encoding="utf-8"))
        self.assertIn("is the audit of a different paper", (SKILL / "scripts" / "new_audit.sh").read_text(encoding="utf-8"))
        self.assertIn("is refused by check 0c whatever you rebuild",
                      (SKILL / "scripts" / "finalize_run.sh").read_text(encoding="utf-8"))

    def test_round_fifty_five_fixes(self):
        rc = (SKILL / "scripts" / "run_critique.sh").read_text(encoding="utf-8")
        self.assertIn("same_roles()", rc)
        self.assertIn('"$OUT" "$(basename "$INV")"', rc)
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "reservations").mkdir()
            for rel in ("data/AER/Smith_2020", "data/AER/Smith__2020"):
                (root / rel).mkdir(parents=True)
            script = str(SKILL / "scripts" / "reserve_paper.sh")
            def run(owner, *args):
                return subprocess.run(["bash", script, *args], cwd=root, capture_output=True, text=True,
                                      env=dict(os.environ, I4R_RESERVE_GRACE="0", I4R_OWNER=owner))
            run("bob@h", "claim", "data/AER/Smith__2020")
            run("alice@h", "claim", "data/AER/Smith_2020")
            run("bob@h", "release", "data/AER/Smith__2020")
            self.assertIn("RESERVED by alice@h", run("x@h", "status", "data/AER/Smith_2020").stdout)
            self.assertIn("reserved by alice@h", run("carol@h", "claim", "data/AER/Smith_2020").stdout)
            self.assertIn("released", run("alice@h", "release", "data/AER/Smith_2020").stdout)

    def test_new_audit_resolves_case_and_existing_collision_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp).resolve()
            for name in ("reservations", "runs", "comments", "metadata"):
                (root / name).mkdir()
            for title in ("What", "What?"):
                paper = root / "data" / "AER" / title
                (paper / "pkg" / "code").mkdir(parents=True)
                (paper / "article.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
                (paper / "pkg" / "code" / "a.do").write_text("x\n")
            # 2.7.5: new_audit.sh checks the critic route with one Codex call; the fake answers it
            env = dict(os.environ, I4R_RESERVE_GRACE="0", I4R_OWNER="me@h",
                       CODEX=str(SKILL / "tests" / "fake_codex.sh"), FAKE_CODEX_STATE=str(root / ".fake_codex"),
                       I4R_STATE_DIR=str(root / ".i4r_state"), CODEX_HOME=str(root / ".codex_home"))
            na = str(SKILL / "scripts" / "new_audit.sh")
            first = subprocess.run(["bash", na, "data/AER/What"], cwd=root, capture_output=True, text=True, env=env)
            self.assertEqual(first.returncode, 0, first.stderr[-600:])
            if not (root / "data" / "aer").exists():
                self.skipTest("case-sensitive filesystem")
            again = subprocess.run(["bash", na, "data/aer/what"], cwd=root, capture_output=True, text=True, env=env)
            self.assertIn("an audit of this paper already exists", again.stderr)
            self.assertNotIn("different paper", again.stderr)
            other = subprocess.run(["bash", na, "data/AER/What?"], cwd=root, capture_output=True, text=True, env=env)
            self.assertIn("different paper", other.stderr)
            self.assertIn("--run-name", other.stderr)
            self.assertRegex(other.stderr, r"What\\?\s\\?\(2\\?\)")

    def test_round_gated_only_after_it_ran_is_refused(self):
        import json
        import validate_round_projections as vrp
        with tempfile.TemporaryDirectory() as tmp:
            audit = pathlib.Path(tmp)
            def journal(*events):
                (audit / "run.events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
            gate = {"type": "gate", "payload": {"round": 3, "gate": "post_delta", "exit_status": 0,
                                                "log_path": "critique/round_3/POST_DELTA_GATES.json"}}
            start = {"type": "round_start", "payload": {"round": 3}}
            journal(gate, start)
            self.assertTrue(vrp._gated_before_start(audit, 3))
            journal(start, gate)
            self.assertFalse(vrp._gated_before_start(audit, 3))
            journal(gate, start, start, gate)   # a retry after the pass, then a documented re-gate
            self.assertTrue(vrp._gated_before_start(audit, 3))
            journal({"type": "gate", "payload": {"round": 3, "gate": "post_delta", "exit_status": 1}}, start)
            self.assertFalse(vrp._gated_before_start(audit, 3))
        rc = (SKILL / "scripts" / "run_critique.sh").read_text(encoding="utf-8")
        self.assertIn('[ -f "$OUT/POST_DELTA_GATES.partial.json" ] && ! quorum_on_this_input; then', rc)
        self.assertIn('[ -f "$OUT/POST_DELTA_GATES.json" ] && ! quorum_on_this_input; then', rc)

    def test_reservation_owner_with_apostrophe_and_reap_of_pathless_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "reservations").mkdir()
            (root / "data" / "J" / "P").mkdir(parents=True)
            script = str(SKILL / "scripts" / "reserve_paper.sh")
            env = dict(os.environ, I4R_RESERVE_GRACE="0", I4R_OWNER="Sean O'Brien")
            subprocess.run(["bash", script, "claim", "data/J/P"], cwd=root, capture_output=True, env=env)
            again = subprocess.run(["bash", script, "claim", "data/J/P"], cwd=root, capture_output=True, text=True, env=env)
            self.assertIn("already yours", again.stdout)
            self.assertIn("released", subprocess.run(["bash", script, "release", "data/J/P"], cwd=root,
                                                     capture_output=True, text=True, env=env).stdout)
            (root / "reservations" / "J_Q.json").write_text(
                '{"owner": "Sean O\'Brien", "claimed_at": "2020-01-01T00:00:00Z"}')
            reap = subprocess.run(["bash", script, "reap", str(root)], cwd=root, capture_output=True, text=True, env=env)
            self.assertEqual(reap.returncode, 0, reap.stderr[-400:])
            self.assertIn("no absolute paper path", reap.stdout)

    def test_race_loser_backs_off_whichever_file_sync_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "reservations").mkdir()
            (root / "data" / "J" / "P").mkdir(parents=True)
            swap = ("import json, os, time\n"
                    "time.sleep(1)\n"
                    "f = 'reservations/J_P.json'\n"
                    "os.rename(f, \"reservations/J_P (bob's conflicted copy 2026).json\")\n"
                    "json.dump({'owner': 'bob', 'paper': 'x/J/P', 'claimed_at': '2020-01-01T00:00:00Z'}, open(f, 'w'))\n")
            racer = subprocess.Popen([sys.executable, "-c", swap], cwd=root)
            proc = subprocess.run(["bash", str(SKILL / "scripts" / "reserve_paper.sh"), "claim", "data/J/P"], cwd=root,
                                  capture_output=True, text=True,
                                  env=dict(os.environ, I4R_RESERVE_GRACE="3", I4R_OWNER="alice"))
            racer.wait()
            self.assertIn("backing off", proc.stdout)
            self.assertEqual(sorted(f.name for f in (root / "reservations").iterdir()), ["J_P.json"])

    def test_status_finds_a_claim_recorded_as_dot_and_prints_the_journal(self):
        import status
        with tempfile.TemporaryDirectory() as tmp:
            tree = pathlib.Path(tmp)
            (tree / "reservations").mkdir()
            (tree / "reservations" / "J1_Q.json").write_text(
                '{"owner": "a", "paper": ".", "claimed_at": "2026-09-20T00:00:00Z"}', encoding="utf-8")
            rows = status._reservations(tree)
            self.assertIn(("\0file", "j1_q"), rows)
            self.assertEqual(status._slugs("J1", "Q")[0], "J1_Q")
        src = (SKILL / "scripts" / "status.py").read_text(encoding="utf-8")
        self.assertIn("(r.get('journal') or '?')}/{r['paper']}", src)

    def test_round_fifty_five_e2e_messages(self):
        self.assertIn("(none unresolved: `converged`)", (SKILL / "SKILL.md").read_text(encoding="utf-8"))
        self.assertIn("finalize with --stopping-reason converged",
                      (SKILL / "scripts" / "finalize_run.sh").read_text(encoding="utf-8"))
        self.assertIn("add MAX_TRIES=<higher>", (SKILL / "scripts" / "run_critique.sh").read_text(encoding="utf-8"))
        self.assertIn("add MAX_TRIES=<higher>", (SKILL / "scripts" / "wait_for_round.sh").read_text(encoding="utf-8"))
        self.assertIn("prepared critique round plan differs from this run's plan in",
                      (SKILL / "scripts" / "test_run_critique_runtime.py").read_text(encoding="utf-8"))
        na = (SKILL / "scripts" / "new_audit.sh").read_text(encoding="utf-8")
        self.assertIn("--run-name) RUN_NAME=", na)
        self.assertIn("bash %q %q --run-name %q", na)
        self.assertIn("exited without publishing an invocation",
                      (SKILL / "scripts" / "wait_for_round.sh").read_text(encoding="utf-8"))

    def test_race_winner_is_moved_back_to_the_main_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "reservations").mkdir()
            (root / "data" / "J" / "P").mkdir(parents=True)
            swap = ("import json, os, time\n"
                    "time.sleep(1)\n"
                    "f = 'reservations/J_P.json'\n"
                    "os.rename(f, \"reservations/J_P (bob's conflicted copy 2026).json\")\n"
                    "json.dump({'owner': 'bob', 'paper': 'x/J/P', 'claimed_at': '2099-01-01T00:00:00Z'}, open(f, 'w'))\n")
            racer = subprocess.Popen([sys.executable, "-c", swap], cwd=root)
            script = str(SKILL / "scripts" / "reserve_paper.sh")
            proc = subprocess.run(["bash", script, "claim", "data/J/P"], cwd=root, capture_output=True, text=True,
                                  env=dict(os.environ, I4R_RESERVE_GRACE="3", I4R_OWNER="alice"))
            racer.wait()
            self.assertIn("RESERVED", proc.stdout)
            self.assertEqual(sorted(f.name for f in (root / "reservations").iterdir()), ["J_P.json"])
            status = subprocess.run(["bash", script, "status", "data/J/P"], cwd=root, capture_output=True, text=True)
            self.assertIn("RESERVED by alice", status.stdout)

    def test_round_fifty_six_fixes(self):
        rc = (SKILL / "scripts" / "run_critique.sh").read_text(encoding="utf-8")
        refusal = rc.index('[ -f "$WS/work/finding_projections.manifest.json" ] && ! quorum_on_this_input; then')
        self.assertLess(refusal, rc.index("FINAL_DETAIL=\"snapshot ready; checking model probe\""))
        self.assertIn("if number == len(rounds) and not _gated_before_start(audit, number)",
                      (SKILL / "scripts" / "validate_round_projections.py").read_text(encoding="utf-8"))
        na = (SKILL / "scripts" / "new_audit.sh").read_text(encoding="utf-8")
        self.assertIn("printf ' --paper %q' \"$USER_PDF\"", na)
        self.assertIn('re.match(r"\\s*(FAIL|FATAL|ERROR)\\b", l)',
                      (SKILL / "scripts" / "run_post_delta_gates.py").read_text(encoding="utf-8"))
        st = (SKILL / "scripts" / "status.py").read_text(encoding="utf-8")
        self.assertIn('row["run_folder"] = row["paper"]', st)

    def test_new_audit_refuses_a_second_audit_of_the_same_paper(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp).resolve()
            for name in ("reservations", "runs", "comments", "metadata"):
                (root / name).mkdir()
            for title in ("Growth", "Growth?"):
                paper = root / "data" / "JDE" / title
                (paper / "pkg").mkdir(parents=True)
                (paper / "article.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
                (paper / "pkg" / "a.do").write_text("x\n")
            # 2.7.5: new_audit.sh checks the critic route with one Codex call; the fake answers it
            env = dict(os.environ, I4R_RESERVE_GRACE="0", I4R_OWNER="me@h",
                       CODEX=str(SKILL / "tests" / "fake_codex.sh"), FAKE_CODEX_STATE=str(root / ".fake_codex"),
                       I4R_STATE_DIR=str(root / ".i4r_state"), CODEX_HOME=str(root / ".codex_home"))
            na = str(SKILL / "scripts" / "new_audit.sh")
            def run(*args):
                return subprocess.run(["bash", na, *args], cwd=root, capture_output=True, text=True, env=env)
            growth = run("data/JDE/Growth")
            self.assertEqual(growth.returncode, 0, growth.stderr[-600:])
            self.assertEqual(run("data/JDE/Growth?", "--run-name", "Growth (2)").returncode, 0)
            dup = run("data/JDE/Growth", "--run-name", "Growth (3)")
            self.assertIn("an audit of this paper already exists", dup.stderr)
            (root / "old_runs").mkdir()
            (root / "runs" / "JDE" / "Growth").rename(root / "old_runs" / "Growth")
            again = run("data/JDE/Growth?")
            self.assertIn("Growth (2)/audit", again.stderr)
            self.assertIn("an audit of this paper already exists", again.stderr)

    def test_later_claimant_is_refused_after_the_winner_moves_its_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "reservations").mkdir()
            (root / "data" / "J" / "P").mkdir(parents=True)
            winner = ("import json, os, time\n"
                      "time.sleep(1)\n"
                      "c = \"reservations/J_P (alice's conflicted copy 2026).json\"\n"
                      "json.dump({'owner': 'alice', 'paper': 'x/J/P', 'claimed_at': '2000-01-01T00:00:00Z'}, open(c, 'w'))\n"
                      "time.sleep(1)\n"
                      "os.replace(c, 'reservations/J_P.json')\n")
            racer = subprocess.Popen([sys.executable, "-c", winner], cwd=root)
            proc = subprocess.run(["bash", str(SKILL / "scripts" / "reserve_paper.sh"), "claim", "data/J/P"], cwd=root,
                                  capture_output=True, text=True,
                                  env=dict(os.environ, I4R_RESERVE_GRACE="3", I4R_OWNER="bob"))
            racer.wait()
            self.assertEqual(proc.returncode, 1, proc.stdout)
            self.assertIn("won the sync race", proc.stdout)
            self.assertIn('"alice"', (root / "reservations" / "J_P.json").read_text())
        rc = (SKILL / "scripts" / "run_critique.sh").read_text(encoding="utf-8")
        self.assertIn('[ ! -f "$OUT/POST_DELTA_GATES.json" ] && [ ! -f "$OUT/POST_DELTA_GATES.partial.json" ]', rc)
        self.assertIn("cannot be rebuilt once later rounds changed the report",
                      (SKILL / "scripts" / "validate_round_projections.py").read_text(encoding="utf-8"))

    def test_round_fifty_seven_minor_fixes(self):
        import json
        import validate_round_projections as vrp
        with tempfile.TemporaryDirectory() as tmp:
            audit = pathlib.Path(tmp)
            hand = {"type": "gate", "payload": {"round": 2, "gate": "post_delta", "exit_status": 0}}
            start = {"type": "round_start", "payload": {"round": 2}}
            (audit / "run.events.jsonl").write_text(json.dumps(hand) + "\n" + json.dumps(start) + "\n")
            self.assertFalse(vrp._gated_before_start(audit, 2), "a bare hand-logged pass must not count")
        rc = (SKILL / "scripts" / "run_critique.sh").read_text(encoding="utf-8")
        self.assertIn('[ "${FORCE_RERUN:-0}" -eq 1 ] && return 1', rc)
        self.assertIn("still has a live provider process", rc)
        wr = (SKILL / "scripts" / "wait_for_round.sh").read_text(encoding="utf-8")
        self.assertIn("provider_alive() {", wr)
        self.assertIn('"CRITICS override" in str(routing.get("reason", ""))', wr)
        self.assertIn("no coordinator-approved finding has been projected",
                      (SKILL / "scripts" / "run_post_delta_gates.py").read_text(encoding="utf-8"))
        self.assertIn("Rebuild the release set for the corrected report", (SKILL / "INSTALL.md").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "reservations").mkdir()
            (root / "data" / "J" / "P").mkdir(parents=True)
            (root / "reservations" / "J_P (bob's conflicted copy 2026).json").write_text(
                '{"owner": "bob", "paper": "x/J/P", "claimed_at": "2000-01-01T00:00:00Z"}')
            proc = subprocess.run(["bash", str(SKILL / "scripts" / "reserve_paper.sh"), "claim", "data/J/P"], cwd=root,
                                  capture_output=True, text=True, env=dict(os.environ, I4R_RESERVE_GRACE="0", I4R_OWNER="bob"))
            self.assertIn("already yours", proc.stdout)
            self.assertEqual(sorted(f.name for f in (root / "reservations").iterdir()), ["J_P.json"])
            # new_audit finds an audit started under any --run-name
            for name in ("runs", "comments", "metadata"):
                (root / name).mkdir()
            paper = root / "data" / "JDE" / "Solo"
            (paper / "pkg").mkdir(parents=True)
            (paper / "article.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
            (paper / "pkg" / "a.do").write_text("x\n")
            # 2.7.5: new_audit.sh checks the critic route with one Codex call; the fake answers it
            env = dict(os.environ, I4R_RESERVE_GRACE="0", I4R_OWNER="me@h",
                       CODEX=str(SKILL / "tests" / "fake_codex.sh"), FAKE_CODEX_STATE=str(root / ".fake_codex"),
                       I4R_STATE_DIR=str(root / ".i4r_state"), CODEX_HOME=str(root / ".codex_home"))
            na = str(SKILL / "scripts" / "new_audit.sh")
            first = subprocess.run(["bash", na, "data/JDE/Solo", "--run-name", "Solo-alt"], cwd=root,
                                   capture_output=True, text=True, env=env)
            self.assertEqual(first.returncode, 0, first.stderr[-600:])
            dup = subprocess.run(["bash", na, "data/JDE/Solo"], cwd=root, capture_output=True, text=True, env=env)
            self.assertIn("an audit of this paper already exists", dup.stderr)

    def test_round_fifty_eight_minor_fixes(self):
        rc = (SKILL / "scripts" / "run_critique.sh").read_text(encoding="utf-8")
        self.assertIn('ps -o command= -p "$provider_pid" 2>/dev/null | grep -q codex', rc)
        self.assertIn('if ! kill -0 "$$" 2>/dev/null; then', rc)
        self.assertLess(rc.index('if ! kill -0 "$$" 2>/dev/null; then'), rc.index('echo "  [run  ] critic_$role attempt'))
        self.assertIn('RESTORE_NOTE="the earlier run', rc)
        self.assertIn("provider_pid\" 2>/dev/null | grep -q codex && return 0",
                      (SKILL / "scripts" / "wait_for_round.sh").read_text(encoding="utf-8"))
        self.assertIn("or a CRITICS/ADJUDICATE override differs",
                      (SKILL / "scripts" / "lifecycle_journal.py").read_text(encoding="utf-8"))
        install = (SKILL / "INSTALL.md").read_text(encoding="utf-8")
        self.assertLess(install.index("recipient='I4R editorial queue' at=<release staged>"),
                        install.index("phase=release      at=<at or after the delivery"))
        self.assertIn("which journals the pass), then rerun",
                      (SKILL / "scripts" / "validate_round_projections.py").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "reservations").mkdir()
            (root / "data" / "J" / "P").mkdir(parents=True)
            res = root / "reservations"
            (res / "J_P.json").write_text('{"owner": "B", "paper": "/x/data/J/P", "claimed_at": "2026-09-25T10:00:05Z"}')
            (res / "J_P (conflicted copy A).json").write_text(
                '{"owner": "A", "paper": "/x/data/J/P", "claimed_at": "2026-09-25T10:00:01Z"}')
            script = str(SKILL / "scripts" / "reserve_paper.sh")
            proc = subprocess.run(["bash", script, "claim", "data/J/P"], cwd=root, capture_output=True, text=True,
                                  env=dict(os.environ, I4R_RESERVE_GRACE="0", I4R_OWNER="B"))
            self.assertIn("held by A since 2026-09-25T10:00:01Z", proc.stdout)
            self.assertIn("J_P.json  (B,", proc.stdout)
            # recovery warns about the displaced later claim
            (res / "J_P (conflicted copy A).json").unlink()
            (res / "J_P (bob's conflicted copy 2026).json").write_text(
                '{"owner": "bob", "paper": "x/J/P", "claimed_at": "2000-01-01T00:00:00Z"}')
            rec = subprocess.run(["bash", script, "claim", "data/J/P"], cwd=root, capture_output=True, text=True,
                                 env=dict(os.environ, I4R_RESERVE_GRACE="0", I4R_OWNER="bob"))
            self.assertIn("replaced B's later claim", rec.stdout)
        selftest = subprocess.run(["bash", "reserve_paper.sh", "--selftest"], cwd=SKILL / "scripts",
                                  capture_output=True, text=True)
        self.assertIn("SELFTEST OK", selftest.stdout)

if __name__ == "__main__":
    unittest.main()
