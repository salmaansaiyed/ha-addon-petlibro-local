from __future__ import annotations

import unittest
from unittest.mock import patch

from installer.tui import _read_key, clear_screen, select


class TerminalMenuTests(unittest.TestCase):
    @patch("installer.tui.print")
    @patch("installer.tui.sys.stdout.isatty", return_value=True)
    def test_clear_screen_uses_ansi_in_terminal(self, _isatty, output) -> None:
        clear_screen()
        output.assert_called_once_with("\x1b[2J\x1b[H", end="", flush=True)

    @patch("installer.tui.print")
    @patch("installer.tui.sys.stdout.isatty", return_value=False)
    def test_clear_screen_preserves_redirected_output(self, _isatty, output) -> None:
        clear_screen()
        output.assert_not_called()

    @patch("builtins.input", return_value="")
    def test_noninteractive_menu_uses_default(self, _input) -> None:
        self.assertEqual(1, select("Choose", ("one", "two"), default=1))

    @patch("builtins.input", side_effect=["invalid", "2"])
    def test_noninteractive_menu_accepts_numbered_choice(self, _input) -> None:
        self.assertEqual(1, select("Choose", ("one", "two")))

    def test_menu_requires_an_option(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least one"):
            select("Choose", ())

    @patch("installer.tui.termios.tcsetattr")
    @patch("installer.tui.tty.setraw")
    @patch("installer.tui.termios.tcgetattr", return_value=[object()])
    @patch("installer.tui.sys.stdin.fileno", return_value=7)
    @patch("installer.tui.select_module.select", return_value=([], [], []))
    @patch("installer.tui.os.read", return_value=b"\x1b")
    def test_bare_escape_does_not_block(
        self, _read, wait_for_input, _fileno, _getattr, _setraw, _setattr
    ) -> None:
        self.assertEqual("\x1b", _read_key())
        wait_for_input.assert_called_once_with([7], [], [], 0.05)


if __name__ == "__main__":
    unittest.main()
