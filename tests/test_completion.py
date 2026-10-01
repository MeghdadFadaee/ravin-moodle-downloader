"""Exercise shell completion through argcomplete's shell protocol."""

import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ravin.cli import main


class CompletionTests(unittest.TestCase):
    def complete(self, line):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "completions"
            env = dict(os.environ, COMP_LINE=line, COMP_POINT=str(len(line)),
                       _ARGCOMPLETE="1", _ARGCOMPLETE_IFS="\n",
                       _ARGCOMPLETE_STDOUT_FILENAME=str(output))
            result = subprocess.run(
                [sys.executable, "-c", "from ravin.cli import main; main()"],
                env=env, capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return [candidate.rstrip(" ") for candidate in output.read_text().splitlines()]

    def test_commands_and_aliases(self):
        self.assertIn("scan", self.complete("ravin sc"))
        self.assertIn("recordings", self.complete("ravin recording"))

    def test_command_options_and_choices(self):
        self.assertIn("--no-keep-awake", self.complete("ravin transcribe --no-k"))
        self.assertIn("cuda", self.complete("ravin transcribe --device cu"))
        self.assertNotIn("--device", self.complete("ravin serve --d"))

    def test_file_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "questions.md"
            path.touch()
            self.assertIn(str(path), self.complete(f"ravin questions 44 12 {directory}/qu"))

    def test_setup_requires_no_authentication(self):
        for shell in ("bash", "zsh"):
            with self.subTest(shell=shell), patch("ravin.cli._authenticate") as authenticate:
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(main(["completion", shell]), 0)
                self.assertIn("ravin", output.getvalue())
                authenticate.assert_not_called()


class PersistentCompletionTests(unittest.TestCase):
    def test_install_preserves_settings_and_is_repeatable(self):
        from ravin.completion import install_completion
        for shell, filename in (("bash", ".bashrc"), ("zsh", ".zshrc")):
            with self.subTest(shell=shell), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                startup = home / filename
                startup.write_text("# existing settings\nexport EXAMPLE=yes\n")
                with patch("ravin.completion.Path.home", return_value=home), \
                     patch("ravin.completion.sys.platform", "linux"), \
                     patch.dict(os.environ, {"ZDOTDIR": ""}):
                    self.assertEqual(install_completion(shell), startup)
                    first = startup.read_text()
                    install_completion(shell)
                self.assertEqual(startup.read_text(), first)
                self.assertTrue(first.startswith("# existing settings\nexport EXAMPLE=yes\n"))
                self.assertEqual(first.count("# >>> ravin completion >>>"), 1)
                result = subprocess.run([shell, "-n", str(startup)], capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_zdotdir(self):
        from ravin.completion import install_completion
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"ZDOTDIR": directory}):
            self.assertEqual(install_completion("zsh"), Path(directory) / ".zshrc")

    def test_invalid_markers_preserve_file(self):
        from ravin.completion import install_completion
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"ZDOTDIR": directory}):
            startup = Path(directory) / ".zshrc"
            original = "# >>> ravin completion >>>\n# user settings\n"
            startup.write_text(original)
            with self.assertRaises(ValueError):
                install_completion("zsh")
            self.assertEqual(startup.read_text(), original)
