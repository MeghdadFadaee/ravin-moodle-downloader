"""Persistent, repeatable shell completion setup."""

from pathlib import Path
import os
import shlex
import sys


def install_completion(shell: str) -> Path:
    if shell == "zsh":
        startup = Path(os.environ.get("ZDOTDIR") or Path.home()) / ".zshrc"
    else:
        startup = Path.home() / (".bash_profile" if sys.platform == "darwin" else ".bashrc")
    start = "# >>> ravin completion >>>"
    end = "# <<< ravin completion <<<"
    original = startup.read_text() if startup.exists() else ""
    if start in original or end in original:
        if original.count(start) != 1 or original.count(end) != 1:
            raise ValueError(f"Invalid Ravin completion markers in {startup}")
        first = original.index(start)
        last = original.index(end)
        if last < first:
            raise ValueError(f"Invalid Ravin completion markers in {startup}")
        before = original[:first]
        after = original[last + len(end):].lstrip("\n")
    else:
        before = original + ("\n" if original and not original.endswith("\n") else "")
        after = ""
    python = shlex.quote(sys.executable)
    initialize = (
        "  if ! (( $+functions[compdef] )); then\n"
        "    autoload -Uz compinit\n"
        "    compinit\n"
        "  fi\n"
    ) if shell == "zsh" else ""
    block = (
        f"{start}\n"
        f"if [ -x {python} ]; then\n"
        f"{initialize}"
        f'  eval "$({python} -m ravin completion {shell})"\n'
        f"fi\n{end}\n"
    )
    updated = before + block + after
    if updated != original:
        startup.parent.mkdir(parents=True, exist_ok=True)
        startup.write_text(updated)
    return startup
