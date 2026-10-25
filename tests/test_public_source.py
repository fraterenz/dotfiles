"""Public-source privacy tripwires, not a substitute for reviewing each commit."""

from pathlib import Path
import re
import subprocess
import unittest


SOURCE = Path(__file__).resolve().parents[1]
EMAIL = re.compile(r"\b[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
SECRET_PATTERNS = [
    re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{24,}|AKIA[A-Z0-9]{16})"),
    re.compile(r"AGE-SECRET-KEY-1[A-Z0-9]{20,}"),
]


def text_files():
    for path in SOURCE.rglob("*"):
        if not path.is_file() or set(path.relative_to(SOURCE).parts) & {".git", "__pycache__"}:
            continue
        try:
            yield path.relative_to(SOURCE), path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue


def is_no_reply(email):
    return email.endswith("@users.noreply.github.com") or email == "noreply@github.com"


class PublicSourceTests(unittest.TestCase):
    def test_ssh_directory_is_not_imported_into_source(self):
        for name in (".ssh", "dot_ssh", "private_dot_ssh"):
            with self.subTest(path=name):
                path = SOURCE / name
                self.assertFalse(path.exists() or path.is_symlink())

    def test_git_identity_is_local_template_data(self):
        template = (SOURCE / "dot_gitconfig.tmpl").read_text()
        self.assertIn("{{ .gitName }}", template)
        self.assertIn("{{ .gitEmail }}", template)
        self.assertFalse((SOURCE / "dot_gitconfig").exists())

    def test_no_personal_email_literals(self):
        failures = []
        for path, text in text_files():
            for email in EMAIL.findall(text):
                if email == "git@github.com" or email.endswith(".invalid") or is_no_reply(email):
                    continue
                failures.append(str(path))
        self.assertEqual(failures, [], "Review email literals in the listed files; values omitted")

    def test_no_common_secret_patterns(self):
        failures = [
            str(path)
            for path, text in text_files()
            if any(pattern.search(text) for pattern in SECRET_PATTERNS)
        ]
        self.assertEqual(failures, [], "Review possible credentials in the listed files; values omitted")

    def test_no_absolute_host_home_paths(self):
        failures = []
        for path, text in text_files():
            for match in re.finditer(r"/(?:var/)?home/([A-Za-z0-9_.-]+|\$\{[A-Za-z0-9_]+\})", text):
                # These are intentional container paths, not a host identity.
                if match.group(1) not in ("dev", "${USER_NAME}"):
                    failures.append(str(path))
        self.assertEqual(failures, [], "Use portable paths rather than a literal host username")

    def test_commit_emails_are_no_reply(self):
        result = subprocess.run(
            ["git", "-C", str(SOURCE), "log", "--all", "--format=%ae%n%ce"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, "Cannot inspect commit metadata: " + result.stderr)
        if not result.stdout.strip():
            self.skipTest("No baseline commit yet; check metadata again before the first push")
        emails = result.stdout.splitlines()
        self.assertTrue(emails)
        self.assertTrue(all(is_no_reply(email) for email in emails))

    def test_no_reply_policy_accepts_github_merge_identity_not_personal_email(self):
        self.assertTrue(is_no_reply("noreply@github.com"))
        self.assertTrue(is_no_reply("example@users.noreply.github.com"))
        self.assertFalse(is_no_reply("example@example.invalid"))


if __name__ == "__main__":
    unittest.main()
