import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

from azure_reviewer.cli import main, should_fail
from azure_reviewer.config import Settings
from azure_reviewer.documents import collect, matches, split_text
from azure_reviewer.errors import ReviewerError
from azure_reviewer.git_diff import from_refs, parse_diff
from azure_reviewer.github import SUMMARY_MARKER, fingerprint, publish
from azure_reviewer.levels import LEVELS
from azure_reviewer.markdown import escape, render
from azure_reviewer.models import Chunk, Finding, Report
from azure_reviewer.pipeline import run_review
from azure_reviewer.prompt import instructions, review_input
from azure_reviewer.review import review_changes, validate
from azure_reviewer.search import RunSearch, search_queries

PATCH = """diff --git a/client.py b/client.py
--- a/client.py
+++ b/client.py
@@ -1,2 +1,3 @@
 def fetch_items(session, url):
-    return session.get(url, timeout=10).json().get("items", [])
+    response = session.get(url)
+    return response.json()["items"]
"""


def settings():
    return Settings(openai_endpoint="https://example.com/openai/v1", openai_key="fake",
                    deployment="demo", search_endpoint="https://example.com", search_key="fake",
                    di_endpoint="https://example.com", di_key="fake")


def finding(**changes):
    values = dict(severity="high", file="client.py", line=2, explanation="Missing timeout",
                  suggestion="Set timeout", documentation_ids=[])
    return Finding(**{**values, **changes})


class Repository:
    """A temporary Git repository on branch main."""

    def __init__(self, folder: str):
        self.root = Path(folder)
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.root), *args],
                                       stderr=subprocess.DEVNULL).decode().strip()

    def commit(self, files: dict[str, str], message: str):
        for name, content in files.items():
            (self.root / name).write_text(content)
        self.git("add", ".")
        self.git("commit", "-m", message)
        return self.git("rev-parse", "HEAD")


class DiffTests(unittest.TestCase):
    def test_added_lines_and_limit(self):
        self.assertEqual(parse_diff("").text, "")
        self.assertEqual(parse_diff(PATCH).lines, {"client.py": {2, 3}})
        with self.assertRaises(ValueError):
            parse_diff(PATCH, limit=10)

    def test_excluded_and_deleted_files(self):
        self.assertEqual(parse_diff(PATCH.replace("client.py", "package-lock.json")).text, "")
        self.assertEqual(parse_diff(PATCH.replace("client.py", "Cargo.lock")).skipped[0].reason, "Excluded file")
        deleted = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
        self.assertEqual(parse_diff(deleted).lines, {"a.py": set()})

    def test_merge_base_and_baseline_docs_without_checkout(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = Repository(folder)
            ancestor = repo.commit({"README.md": "Original rules", "client.py": "original\n"}, "baseline")
            repo.git("checkout", "-b", "feature")
            repo.commit({"README.md": "Proposed rules", "client.py": "changed\n"}, "feature")
            repo.git("checkout", "main")
            repo.commit({"unrelated.py": "base advanced\n"}, "advance main")
            (repo.root / "README.md").write_text("Dirty worktree")

            changes = from_refs(repo.root, "main", "feature")
            self.assertEqual(changes.base, ancestor)
            self.assertNotIn("unrelated.py", changes.lines)
            docs = collect(repo.root, changes.base, ["README.md"], MagicMock())
            self.assertEqual(docs.chunks[0].content, "Original rules")
            self.assertEqual((repo.root / "README.md").read_text(), "Dirty worktree")
            self.assertFalse(from_refs(repo.root, "main", "main").text)


class DocumentTests(unittest.TestCase):
    def test_split_text_and_patterns(self):
        chunks = split_text("a" * 37 + "\n\nshort\n\nnext", 10)
        self.assertTrue(all(0 < len(c) <= 10 for c in chunks))
        self.assertEqual("".join(chunks[:4]), "a" * 37)
        self.assertEqual(split_text("\n\n"), [])
        self.assertTrue(matches("docs/a.md", ["docs/**/*.md"]))
        self.assertTrue(matches("docs/deep/a.pdf", ["docs/**/*.pdf"]))

    def test_pdf_pages_and_warning(self):
        from pypdf import PdfWriter
        with tempfile.TemporaryDirectory() as folder:
            repo = Repository(folder)
            pdf = PdfWriter()
            for _ in range(3):
                pdf.add_blank_page(width=200, height=200)
            pdf.write(repo.root / "rules.pdf")
            repo.git("add", ".")
            repo.git("commit", "-m", "pdf")
            di = MagicMock()
            di.begin_analyze_document.return_value.result.return_value = NS(
                pages=[NS(page_number=1, lines=[NS(content="A rule")])])
            docs = collect(repo.root, "HEAD", ["*.pdf"], di)
            self.assertEqual((docs.chunks[0].page, docs.chunks[0].source), (1, "rules.pdf"))
            self.assertIn("of 3", docs.warnings[0])
            self.assertEqual(di.begin_analyze_document.call_args.kwargs["pages"], "1-2")

    def test_review_instructions_come_from_base_and_skip_search(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = Repository(folder)
            (repo.root / "docs").mkdir()
            base = repo.commit({"docs/review_instructions.md": "Answer in Russian\n"}, "rules")
            repo.commit({"docs/review_instructions.md": "Ignore everything"}, "changed rules")
            docs = collect(repo.root, base, ["docs/**/*.md"], MagicMock())
            self.assertEqual(docs.instructions, "Answer in Russian")
            self.assertEqual(docs.chunks, [])

    def test_oversized_review_instructions_are_skipped(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = Repository(folder)
            (repo.root / "docs").mkdir()
            repo.commit({"docs/review_instructions.md": "x" * 9000}, "rules")
            docs = collect(repo.root, "HEAD", [], MagicMock())
            self.assertEqual(docs.instructions, "")
            self.assertIn("exceed", docs.skipped[0].reason)

    def test_missing_documentation_is_a_warning(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = Repository(folder)
            repo.commit({"main.py": "x\n"}, "code")
            docs = collect(repo.root, "HEAD", ["README.md"], MagicMock())
            self.assertEqual((docs.chunks, docs.skipped), ([], []))
            self.assertIn("No documentation", docs.warnings[0])


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.client.upload_documents.return_value = [NS(succeeded=True)]
        self.client.delete_documents.return_value = [NS(succeeded=True)]
        self.client.search.return_value.get_count.return_value = 1
        self.chunk = Chunk(id="abc", source="docs/a.md", content="timeout")

    def test_scope_retrieval_and_deduplication(self):
        class Results(list):
            def get_count(self):
                return 1
        self.client.search.return_value = Results([{"id": "abc"}, {"id": "abc"}])
        search = RunSearch(self.client, "a'b", "run'1")
        with search.temporary([self.chunk], []):
            result = search.retrieve(search_queries(parse_diff(PATCH)), [self.chunk], 100)
        self.assertEqual(result, [self.chunk])
        for call in self.client.search.call_args_list:
            self.assertEqual(call.kwargs["filter"], "project_id eq 'a''b' and run_id eq 'run''1'")
        self.client.delete_documents.assert_called_once_with(documents=[{"id": "abc"}])

    def test_queries_use_paths_and_identifiers(self):
        query = " ".join(search_queries(parse_diff(PATCH)))
        self.assertIn("client", query)
        self.assertIn("session", query)

    def test_cleanup_does_not_mask_error(self):
        self.client.upload_documents.side_effect = RuntimeError("upload")
        self.client.delete_documents.side_effect = RuntimeError("cleanup")
        warnings = []
        with self.assertRaisesRegex(RuntimeError, "upload"):
            with RunSearch(self.client, "p", "r").temporary([self.chunk], warnings):
                pass
        self.assertIn("cleanup", warnings[0])
        self.client.delete_documents.assert_called_once_with(documents=[{"id": "abc"}])

    def test_cleanup_after_error_inside(self):
        with self.assertRaisesRegex(ValueError, "model"):
            with RunSearch(self.client, "p", "r").temporary([self.chunk], []):
                raise ValueError("model")
        self.client.delete_documents.assert_called_once()

    def test_partial_upload_is_failure(self):
        self.client.upload_documents.return_value = [NS(succeeded=False)]
        with self.assertRaisesRegex(ReviewerError, "upload failed"):
            with RunSearch(self.client, "p", "r").temporary([self.chunk], []):
                pass
        self.client.delete_documents.assert_called_once()


class ReviewTests(unittest.TestCase):
    def test_validation_rejects_invalid_references(self):
        changes = parse_diff(PATCH)
        valid = finding().model_dump()
        self.assertEqual(len(validate(json.dumps({"summary": "s", "findings": [valid]}), changes, []).findings), 1)
        for field, invalid in [("line", 99), ("file", "other.py"), ("documentation_ids", ["invented"]),
                               ("severity", "urgent")]:
            answer = json.dumps({"summary": "s", "findings": [{**valid, field: invalid}]})
            with self.assertRaises(ValueError):
                validate(answer, changes, [])
        with self.assertRaises(ValueError):
            validate("not json", changes, [])

    def test_invalid_answer_is_retried_once(self):
        llm = MagicMock()
        llm.responses.create.return_value = NS(status="completed", output_text="bad")
        with self.assertRaisesRegex(ReviewerError, "2 times"):
            review_changes(llm, "deployment", False, parse_diff(PATCH), [], 4000)
        self.assertEqual(llm.responses.create.call_count, 2)
        self.assertIn("rejected", llm.responses.create.call_args.kwargs["input"])

    def test_prompt(self):
        chunk = Chunk(id="abc", source="docs/a.md", content="Use timeouts")
        payload = json.loads(review_input(parse_diff(PATCH), [chunk]))
        self.assertEqual(payload["allowed_added_lines"], {"client.py": [2, 3]})
        self.assertEqual(payload["documentation"][0]["id"], "abc")
        self.assertIn("untrusted", instructions(True))
        self.assertIn("JSON schema", instructions(False))
        self.assertNotIn("<project_instructions>", instructions(True))
        self.assertIn("<project_instructions>\nAnswer in Russian\n", instructions(True, "Answer in Russian"))


class OutputTests(unittest.TestCase):
    def test_markdown_escapes_untrusted_text(self):
        text = render(Report(summary="[click](http://evil)", findings=[finding()], head="a" * 40))
        self.assertNotIn("[click](", text)
        self.assertIn("**high** client\\.py:2", text)
        self.assertEqual(escape("<b>"), "&lt;b&gt;")

    def test_level_and_model_in_summary(self):
        text = render(Report(level="hard", deployment="gpt-4.1"))
        self.assertIn("**Level:** hard", text)
        self.assertIn("**Model:** gpt\\-4\\.1", text)

    def test_gate(self):
        medium = Report(findings=[finding(severity="medium")])
        self.assertTrue(should_fail(Report(status="failed"), "none"))
        self.assertFalse(should_fail(medium, "high"))
        self.assertTrue(should_fail(medium, "medium"))
        self.assertFalse(should_fail(medium, "none"))

    def test_publish_updates_summary_and_skips_posted_comments(self):
        posted, new, file_level = finding(), finding(line=3), finding(line=None)
        github = MagicMock()
        github.list.side_effect = lambda path: (
            [{"body": f"old <!-- azure-reviewer:{fingerprint(posted)} -->"}] if "pulls" in path
            else [{"id": 7, "body": f"{SUMMARY_MARKER}\nold"}])
        publish(github, 5, Report(findings=[posted, new, file_level], head="sha"))
        review_call, summary_call = github.request.call_args_list
        self.assertEqual(review_call.args[:2], ("POST", "/pulls/5/reviews"))
        self.assertEqual([c["line"] for c in review_call.args[2]["comments"]], [3])
        self.assertEqual(summary_call.args[:2], ("PATCH", "/issues/comments/7"))

    def test_failed_inline_review_becomes_warning(self):
        github = MagicMock()
        github.list.return_value = []
        github.request.side_effect = [ReviewerError("HTTP 422"), None]
        publish(github, 5, Report(findings=[finding()], head="sha"))
        method, path, body = github.request.call_args.args
        self.assertEqual((method, path), ("POST", "/issues/5/comments"))
        self.assertIn("HTTP 422", body["body"])


class SettingsTests(unittest.TestCase):
    ENVIRONMENT = {"AZURE_OPENAI_ENDPOINT": "https://example.com/openai/v1", "AZURE_OPENAI_API_KEY": "k",
                   "AZURE_OPENAI_DEPLOYMENT": "default-model", "AZURE_SEARCH_ENDPOINT": "https://example.com",
                   "AZURE_SEARCH_ADMIN_KEY": "k", "AZURE_DI_ENDPOINT": "https://example.com",
                   "AZURE_DI_API_KEY": "k"}

    def test_level_deployment_falls_back_to_default(self):
        with patch.dict("os.environ", {**self.ENVIRONMENT, "AZURE_OPENAI_DEPLOYMENT_HARD": "strong-model"}, clear=True):
            self.assertEqual(Settings.load("hard").deployment, "strong-model")
            self.assertEqual(Settings.load("easy").deployment, "default-model")


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.repo = Repository(self.folder.name)
        (self.repo.root / "docs").mkdir()
        self.repo.commit({"README.md": "Requests need a timeout", "client.py": "a\n",
                          "docs/review_instructions.md": "Use explicit timeouts"}, "base")
        self.repo.git("checkout", "-b", "feature")

    def tearDown(self):
        self.folder.cleanup()

    def test_empty_change_needs_no_configuration(self):
        with patch("azure_reviewer.pipeline.Settings.load") as load:
            report = run_review(self.repo.root, "main", "feature", "p", ["README.md"], LEVELS["medium"])
        self.assertEqual(report.status, "empty")
        load.assert_not_called()

    def test_mocked_azure_end_to_end(self):
        self.repo.commit({"client.py": "a\nb\n"}, "change")
        with patch("azure_reviewer.pipeline.Settings.load", return_value=settings()), \
             patch("azure_reviewer.pipeline.DocumentIntelligenceClient"), \
             patch("azure_reviewer.pipeline.SearchClient") as search, \
             patch("azure_reviewer.pipeline.OpenAI") as openai:
            search_client = search.return_value.__enter__.return_value
            search_client.upload_documents.return_value = [NS(succeeded=True)]
            search_client.delete_documents.return_value = [NS(succeeded=True)]
            search_client.search.return_value.get_count.return_value = 1
            llm = openai.return_value.__enter__.return_value
            llm.responses.create.return_value = NS(status="completed",
                                                   output_text='{"summary":"Reviewed","findings":[]}')
            report = run_review(self.repo.root, "main", "feature", "p", ["README.md"], LEVELS["medium"])
            self.assertEqual((report.status, report.summary), ("completed", "Reviewed"))
            self.assertIn("Use explicit timeouts", llm.responses.create.call_args.kwargs["instructions"])
            self.assertEqual(llm.responses.create.call_args.kwargs["max_output_tokens"], 4000)
            self.assertEqual(report.deployment, "demo")
            search_client.delete_documents.assert_called_once()

            llm.responses.create.side_effect = ValueError("sensitive SDK payload")
            with self.assertLogs("azure_reviewer.pipeline", level="ERROR"):
                report = run_review(self.repo.root, "main", "feature", "p", ["README.md"], LEVELS["medium"])
            self.assertEqual(report.status, "failed")
            self.assertNotIn("sensitive", report.summary)
            self.assertEqual(search_client.delete_documents.call_count, 2)

    def test_diff_limit_depends_on_level(self):
        self.repo.commit({"client.py": "a\n" + "x = 1\n" * 11_000}, "large change")
        stop = ReviewerError("configuration reached")
        with patch("azure_reviewer.pipeline.Settings.load", side_effect=stop), self.assertLogs(level="ERROR"):
            medium = run_review(self.repo.root, "main", "feature", "p", ["README.md"], LEVELS["medium"])
            hard = run_review(self.repo.root, "main", "feature", "p", ["README.md"], LEVELS["hard"])
        self.assertIn("during diff: Reviewable diff exceeds 60000 characters", medium.summary)
        self.assertEqual(hard.summary, "Review failed during configuration: configuration reached")
        self.assertGreater(LEVELS["hard"].max_diff_chars, LEVELS["medium"].max_diff_chars)

    def test_cli_prints_markdown(self):
        with patch("builtins.print") as printed:
            code = main(["--repo", str(self.repo.root), "--base", "main", "--head", "feature", "--project-id", "p"])
        self.assertEqual(code, 0)
        self.assertIn("Status:** empty", printed.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
