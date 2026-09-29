# Copyright (C) 2026 SUSE Linux Products GmbH / Adrian Schröter <adrian@suse.de>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Name and email used for .changes entries.

Stored in ``$XDG_CONFIG_HOME/pbuild-ai/config`` (default
``~/.config/pbuild-ai/config``)::

    [changelog]
    name = Jane Doe
    email = jane@example.org

When it is not set, a proposal is derived from the osc config, git config and
the environment; interactive runs ask the user to confirm it and store it.
"""

import configparser
import os
import pwd
import re
import subprocess
from pathlib import Path

CONFIG_SECTION = "changelog"

_ADDR_RE = re.compile(r'^\s*(.*?)\s*<([^<>\s]+@[^<>\s]+)>\s*$')
_EMAIL_RE = re.compile(r'^[^@\s<>]+@[^@\s<>]+$')


def config_path():
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "pbuild-ai" / "config"


def split_address(value):
    """Split ``'Name <email>'`` or a bare email into ``(name, email)``."""
    value = (value or "").strip()
    m = _ADDR_RE.match(value)
    if m:
        return m.group(1).strip().strip('"'), m.group(2)
    if _EMAIL_RE.match(value):
        return "", value
    return "", ""


def format_author(name, email):
    return f"{name} <{email}>" if name else email


def load_identity(path=None):
    """Return ``(name, email)`` from the config file ('' when unset)."""
    cp = configparser.ConfigParser(interpolation=None)
    try:
        cp.read(path or config_path(), encoding="utf-8")
    except (configparser.Error, OSError):
        return "", ""
    if not cp.has_section(CONFIG_SECTION):
        return "", ""
    return (cp.get(CONFIG_SECTION, "name", fallback="").strip(),
            cp.get(CONFIG_SECTION, "email", fallback="").strip())


def save_identity(name, email, path=None):
    """Store name and email, keeping any other settings in the file."""
    path = Path(path or config_path())
    cp = configparser.ConfigParser(interpolation=None)
    try:
        cp.read(path, encoding="utf-8")
    except (configparser.Error, OSError):
        pass
    if not cp.has_section(CONFIG_SECTION):
        cp.add_section(CONFIG_SECTION)
    cp.set(CONFIG_SECTION, "name", name)
    cp.set(CONFIG_SECTION, "email", email)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        cp.write(f)
    return path


def _osc_identity(home=None):
    """(name, email) from the osc config for the default API server."""
    home = Path(home or Path.home())
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(home / ".config")
    for rc in (Path(xdg) / "osc" / "oscrc", home / ".oscrc"):
        if not rc.is_file():
            continue
        cp = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            cp.read(rc, encoding="utf-8")
        except (configparser.Error, OSError):
            continue
        apiurl = cp.get("general", "apiurl", fallback="https://api.opensuse.org").rstrip("/")
        sections = [s for s in cp.sections() if s.rstrip("/") == apiurl]
        sections += [s for s in cp.sections() if s != "general" and s not in sections]
        for s in sections:
            email = cp.get(s, "email", fallback="").strip()
            if email:
                return cp.get(s, "realname", fallback="").strip(), email
    return "", ""


def _git_identity():
    values = []
    for key in ("user.name", "user.email"):
        try:
            r = subprocess.run(["git", "config", "--global", key],
                               capture_output=True, text=True, timeout=5)
            values.append(r.stdout.strip() if r.returncode == 0 else "")
        except (OSError, subprocess.SubprocessError):
            values.append("")
    return values[0], values[1]


def _env_identity(env=None):
    env = os.environ if env is None else env
    name, email = split_address(env.get("EMAIL", ""))
    email = email or split_address(env.get("DEBEMAIL", ""))[1]
    name = name or env.get("DEBFULLNAME", "") or env.get("NAME", "")
    return name.strip(), email


def _passwd_name():
    try:
        gecos = pwd.getpwuid(os.getuid()).pw_gecos
    except (KeyError, AttributeError):
        return ""
    return (gecos or "").split(",")[0].strip()


def propose_identity(sources=None):
    """Best guess for ``(name, email)``: osc config first (it is what the
    openSUSE build service knows about the user), then git, then the
    environment, then the passwd entry for the name."""
    if sources is None:
        sources = (_osc_identity, _git_identity, _env_identity)
    name, email = "", ""
    for source in sources:
        try:
            n, e = source()
        except Exception:
            continue
        name = name or n
        email = email or e
        if name and email:
            break
    if not name:
        name = _passwd_name()
    return name, email


def _ask(label, default, input_fn):
    prompt = f"[CHANGELOG] {label} [{default}]: " if default else f"[CHANGELOG] {label}: "
    answer = input_fn(prompt).strip()
    return answer or default


def resolve_changelog_author(cli_value="", interactive=False, path=None,
                             input_fn=input, sources=None):
    """Return the ``'Name <email>'`` to use for new .changes entries, or ''
    when nothing is known (non-interactive run without config or proposal).

    Order: --email (a bare address is combined with the configured name),
    the config file, then — interactive — ask with a proposal and store the
    answer, or — non-interactive — use the proposal without storing it.
    """
    name, email = load_identity(path)
    cli_name, cli_email = split_address(cli_value)
    if cli_email:
        return format_author(cli_name or name or propose_identity(sources)[0], cli_email)
    if name and email:
        return format_author(name, email)

    p_name, p_email = propose_identity(sources)
    name, email = name or p_name, email or p_email
    target = path or config_path()
    if interactive:
        print(f"[CHANGELOG] No name/email for .changes entries configured yet ({target}).")
        try:
            while True:
                name = _ask("Name", name, input_fn)
                if name:
                    break
            while True:
                email = _ask("Email", email, input_fn)
                if _EMAIL_RE.match(email):
                    break
                print("[CHANGELOG] Please enter an email address like jane@example.org.")
                email = ""
        except (EOFError, KeyboardInterrupt):
            print()
            return format_author(name, email) if email else ""
        try:
            saved = save_identity(name, email, target)
            print(f"[CHANGELOG] Saved to {saved}.")
        except OSError as e:
            print(f"[CHANGELOG] Could not save {target}: {e}")
        return format_author(name, email)
    if email:
        print(f"[CHANGELOG] Using {format_author(name, email)} for .changes entries "
              f"(guessed; set it in {target} or run interactively once to store it).")
        return format_author(name, email)
    return ""
