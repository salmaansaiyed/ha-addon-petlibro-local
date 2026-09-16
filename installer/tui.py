"""Small dependency-free terminal helpers for the guided installer."""

from __future__ import annotations

import os
import select as select_module
import sys
import termios
import tty
from collections.abc import Sequence


def clear_screen() -> None:
    """Clear an interactive terminal without affecting redirected output."""

    if sys.stdout.isatty():
        print("\x1b[2J\x1b[H", end="", flush=True)


def _read_key() -> str:
    descriptor = sys.stdin.fileno()
    previous = termios.tcgetattr(descriptor)
    try:
        tty.setraw(descriptor)
        key = os.read(descriptor, 1)
        if key == b"\x1b":
            for _ in range(2):
                readable, _, _ = select_module.select([descriptor], [], [], 0.05)
                if not readable:
                    break
                key += os.read(descriptor, 1)
        return key.decode("ascii", errors="ignore")
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, previous)


def select(title: str, options: Sequence[str], *, default: int = 0) -> int:
    """Select an option with arrows, falling back to numbered input."""

    if not options:
        raise ValueError("menu requires at least one option")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print(title)
        for index, option in enumerate(options, 1):
            print(f"  {index}. {option}")
        while True:
            answer = input(f"Choose [{default + 1}]: ").strip()
            if not answer:
                return default
            if answer.isdecimal() and 1 <= int(answer) <= len(options):
                return int(answer) - 1
            print("Enter one of the displayed numbers.")

    selected = default
    print(title)
    print("Use ↑/↓ and Enter; q or Esc selects the last option.")
    while True:
        for index, option in enumerate(options):
            marker = "❯" if index == selected else " "
            print(f"  {marker} {option}")
        key = _read_key()
        print(f"\x1b[{len(options)}A", end="")
        if key in {"\x1b[A", "k"}:
            selected = (selected - 1) % len(options)
        elif key in {"\x1b[B", "j"}:
            selected = (selected + 1) % len(options)
        elif key in {"\r", "\n"}:
            print(f"\x1b[{len(options)}B", end="")
            return selected
        elif key in {"q", "\x03", "\x1b"}:
            print(f"\x1b[{len(options)}B", end="")
            return len(options) - 1
