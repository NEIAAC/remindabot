"""How the bot reaches production: the CI and release workflows, and the way the documents tell the
server to run it.

GitHub runs the workflows, not these tests, so they cannot prove a workflow works. They prove the
properties that make a release safe, read from the files themselves (the standard library has no YAML
reader, and the files are written in one plain style):

  * every action is pinned to a commit, and nothing runs on a pull request with the power to publish;
  * only the publish job can write packages, and only after the tests and the version check passed;
  * the tag is the version in pyproject.toml, and no `latest` image exists;
  * the Python the tests run on is the Python of the image;
  * what the documents tell the server to run is a published, hardened image.
"""
from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
CI = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
RELEASE = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")


def job(text: str, name: str) -> str:
    """The text of one job of a workflow."""
    match = re.search(rf"(?ms)^  {re.escape(name)}:\n(.*?)(?=^  \S|\Z)", text.split("\njobs:\n", 1)[1])
    assert match, f"no job {name}"
    return match.group(1)


class Pinning(unittest.TestCase):
    def test_every_action_is_pinned_to_a_commit_and_says_which_version_it_is(self):
        for name, text in (("ci.yml", CI), ("release.yml", RELEASE)):
            for line in text.splitlines():
                match = re.match(r"\s*(?:- )?uses: (\S+)(.*)$", line)
                if not match or match.group(1).startswith("./"):
                    continue
                with self.subTest(workflow=name, action=match.group(1)):
                    self.assertRegex(match.group(1), r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$", "pin to a commit, not a tag")
                    self.assertRegex(match.group(2), r"^\s+# v\d+\.\d+\.\d+$", "say which version the commit is")

    def test_dependabot_keeps_the_pins_up_to_date(self):
        text = (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
        self.assertIn('package-ecosystem: "github-actions"', text)


class Safety(unittest.TestCase):
    def test_the_default_power_of_a_workflow_is_to_read(self):
        for name, text in (("ci.yml", CI), ("release.yml", RELEASE)):
            with self.subTest(workflow=name):
                self.assertRegex(text, r"(?m)^permissions:\n  contents: read\n")

    def test_nothing_that_runs_on_a_pull_request_can_publish_or_see_a_secret(self):
        self.assertNotIn("pull_request_target", CI + RELEASE, "that trigger runs a fork's code with secrets")
        self.assertNotIn("packages: write", CI)
        self.assertNotIn("secrets.", CI)
        self.assertRegex(CI, r"(?m)^  pull_request:$")

    def test_only_the_publish_job_can_write_packages_and_it_uses_the_runs_own_token(self):
        self.assertEqual(RELEASE.count("packages: write"), 1)
        self.assertIn("packages: write", job(RELEASE, "publish"))
        self.assertEqual(re.findall(r"secrets\.(\w+)", RELEASE), ["GITHUB_TOKEN"])

    def test_checkouts_do_not_leave_the_token_in_the_workspace(self):
        for name, text in (("ci.yml", CI), ("release.yml", RELEASE)):
            checkouts = text.count("actions/checkout@")
            with self.subTest(workflow=name):
                self.assertEqual(text.count("persist-credentials: false"), checkouts)

    def test_a_tag_name_never_reaches_a_shell_as_text(self):
        """A tag name is typed by whoever pushes it: it goes through the environment, not into the script."""
        for line in RELEASE.splitlines():
            if "${{" in line and "github.ref_name" in line:
                self.assertRegex(line, r"^\s+TAG: \$\{\{ github\.ref_name \}\}$", "only as an environment variable")


class Release(unittest.TestCase):
    def test_it_runs_for_version_tags_only(self):
        self.assertRegex(RELEASE, r'(?ms)^on:\n  push:\n    tags: \["v\[0-9\]\+\.\[0-9\]\+\.\[0-9\]\+"\]\n')
        self.assertNotIn("branches:", RELEASE)

    def test_publishing_waits_for_the_tests_and_the_version_check(self):
        self.assertRegex(job(RELEASE, "publish"), r"(?m)^    needs: \[version, ci\]$")
        self.assertIn("uses: ./.github/workflows/ci.yml", job(RELEASE, "ci"))
        self.assertRegex(CI, r"(?m)^  workflow_call:$")

    def test_the_tag_must_be_the_version_in_pyproject(self):
        step = job(RELEASE, "version")
        self.assertIn("pyproject.toml", step)
        self.assertIn('[ "v$version" = "$TAG" ]', step)
        version = re.search(r'(?m)^version = "(\d+\.\d+\.\d+)"$', (ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertIsNotNone(version, "pyproject.toml needs a plain X.Y.Z version for the check to work")

    def test_the_image_is_named_by_its_version_and_has_no_latest(self):
        publish = job(RELEASE, "publish")
        self.assertIn("images: ghcr.io/${{ github.repository }}", publish)
        self.assertIn("type=semver,pattern={{version}}", publish)
        self.assertIn("flavor: latest=false", publish)
        self.assertIn("push: true", publish)

    def test_it_logs_in_to_the_registry_it_publishes_to(self):
        publish = job(RELEASE, "publish")
        self.assertIn("registry: ghcr.io", publish)
        self.assertIn("username: ${{ github.actor }}", publish)
        self.assertIn("password: ${{ secrets.GITHUB_TOKEN }}", publish)


class CiMatchesTheImage(unittest.TestCase):
    def test_the_tests_run_on_the_python_the_dockerfile_names_and_nobody_has_to_keep_two_numbers_equal(self):
        """The version is READ from the Dockerfile by the workflow (a copy of the number once went stale, and
        CI tested on 3.12 an image that runs 3.14), so this checks that the very command it runs finds it."""
        tests = job(CI, "test")
        self.assertIn("python-version: ${{ steps.python.outputs.version }}", tests)
        self.assertNotRegex(tests, r'python-version: "?\d', "a typed-in version would go stale")
        command = re.search(r"version=\$\(sed -n '([^']+)' Dockerfile\)", tests)
        self.assertIsNotNone(command, "the step that reads the version from the Dockerfile")
        found = subprocess.run(["sed", "-n", command.group(1), str(ROOT / "Dockerfile")], capture_output=True, text=True)
        base = re.search(r"(?m)^FROM python:(\d+\.\d+)-slim$", (ROOT / "Dockerfile").read_text(encoding="utf-8")).group(1)
        self.assertEqual(found.stdout.strip(), base)
        self.assertRegex(tests, r"\[ -n \"\$version\" \] \|\|", "an empty version must stop the job, not pick a default")

    def test_the_image_is_built_on_every_pull_request_but_never_pushed_there(self):
        image = job(CI, "image")
        self.assertIn("push: false", image)
        self.assertNotIn("push: true", CI)

    def test_the_tests_see_the_time_zone_they_were_written_for(self):
        self.assertIn("TZ: Europe/Lisbon", job(CI, "test"))


class WhatTheServerRuns(unittest.TestCase):
    setup = (ROOT / "docs" / "setup.md").read_text(encoding="utf-8")

    def test_the_documented_service_is_a_published_image_with_the_hardening_the_stack_tests(self):
        snippet = self.setup.split("```yaml", 1)[1].split("```", 1)[0]
        self.assertRegex(snippet, r"image: ghcr\.io/neiaac/remindabot:\d+\.\d+\.\d+")
        self.assertNotIn("build:", snippet, "the server pulls the published image, it does not build")
        self.assertNotIn("pull_policy", snippet)
        for line in ("read_only: true", "cap_drop:", "- ALL", "no-new-privileges:true", "- /tmp"):
            with self.subTest(line=line):
                self.assertIn(line, snippet)
        self.assertNotRegex(snippet, r"\bports:", "the chat-command port is never published")

    def test_the_documented_version_exists_in_the_project(self):
        documented = re.search(r"remindabot:(\d+\.\d+\.\d+)", self.setup).group(1)
        version = re.search(r'(?m)^version = "(\d+\.\d+\.\d+)"$', (ROOT / "pyproject.toml").read_text(encoding="utf-8")).group(1)
        self.assertEqual(documented, version, "the example in docs/setup.md names the current version")


if __name__ == "__main__":
    unittest.main()
