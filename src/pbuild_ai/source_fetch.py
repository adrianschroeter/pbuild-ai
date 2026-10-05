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

"""Fetch a source archive the spec names without a URL.

``Source: %{name}-%{version}.tar.gz`` only names the local file. After a
version update the new archive still has to be put next to the spec; it is
derived from the project URL of the spec (the tag archives of GitHub,
GitLab, Gitea/Forgejo/Codeberg) and stored under the name the spec expects.
"""

import re
import tarfile
from pathlib import Path
from urllib.parse import urlparse

_URL_RE = re.compile(r'https?://[^\s"\'<>()]+')
_GITLAB_HOSTS = ("gitlab.com", "gitlab.gnome.org", "gitlab.freedesktop.org", "invent.kde.org",
                 "salsa.debian.org", "code.opensuse.org")
_GITEA_HOSTS = ("codeberg.org", "gitea.com", "src.opensuse.org")
# Archive types tarfile can write after downloading a .tar.gz.
_RECOMPRESS = {".tar.xz": "w:xz", ".txz": "w:xz", ".tar.bz2": "w:bz2", ".tbz2": "w:bz2"}


def expand_spec_macros(text, spec_text, **overrides):
    """Expand the simple macros of *spec_text* (Name, Version, %define, %global) in *text*.

    Handles %{x}, %{?x} and %x. Keyword *overrides* (e.g. version='1.0')
    replace the values found in the spec. Unknown macros are left alone."""
    macros = {}
    for m in re.finditer(r'^%(?:define|global)\s+(\w+)\s+(\S+)', spec_text or '', re.M):
        macros.setdefault(m.group(1), m.group(2))
    for m in re.finditer(r'^(Name|Version|Release):\s*(\S+)', spec_text or '', re.M | re.I):
        macros.setdefault(m.group(1).lower(), m.group(2))
    macros.update({k: v for k, v in overrides.items() if v})
    for _ in range(5):
        new = re.sub(r'%\{\??(\w+)\}|%(\w+)',
                     lambda m: macros.get(m.group(1) or m.group(2), m.group(0)), text)
        if new == text:
            break
        text = new
    return text


def add_archive_name(source, name):
    """*source* with '#/%{name}-%{version}<ext>' when the URL basename does not
    name the package (tag archives like .../archive/refs/tags/v%{version}.tar.gz),
    so the archive is stored as <name>-<version><ext> instead of v1.0.tar.gz."""
    if '://' not in source or '#' in source:
        return source
    base = urlparse(source).path.rsplit('/', 1)[-1]
    if name.lower() in base.lower() or '%{name}' in base or '%name' in base:
        return source
    for ext in ('.tar.gz', '.tar.xz', '.tar.bz2', '.tar.zst', '.tgz', '.zip'):
        if base.endswith(ext):
            return f"{source}#/%{{name}}-%{{version}}{ext}"
    return source


def _project_urls(spec_text):
    """Project URLs of the spec, the URL: tag first."""
    urls = []
    m = re.search(r'^URL:\s*(\S+)', spec_text or '', re.M | re.I)
    if m:
        urls.append(m.group(1))
    urls += _URL_RE.findall(spec_text or '')
    seen, result = set(), []
    for url in urls:
        url = url.rstrip('/.,;')
        if url not in seen and '%' not in url:
            seen.add(url)
            result.append(url)
    return result


def archive_url_candidates(spec_text, version):
    """Tag archive URLs (.tar.gz) to try for *version*, best guess first."""
    candidates = []
    for url in _project_urls(spec_text):
        p = urlparse(url)
        host = (p.hostname or '').lower()
        parts = [x for x in p.path.split('/') if x]
        if len(parts) < 2:
            continue
        if host == "github.com":
            base = f"https://github.com/{parts[0]}/{parts[1].removesuffix('.git')}"
            candidates += [f"{base}/archive/refs/tags/v{version}.tar.gz",
                           f"{base}/archive/refs/tags/{version}.tar.gz"]
        elif host in _GITLAB_HOSTS or "gitlab" in host:
            if '-' in parts:
                parts = parts[:parts.index('-')]
            proj = parts[-1].removesuffix('.git')
            base = f"https://{host}/{'/'.join(parts[:-1] + [proj])}"
            candidates += [f"{base}/-/archive/v{version}/{proj}-v{version}.tar.gz",
                           f"{base}/-/archive/{version}/{proj}-{version}.tar.gz"]
        elif host in _GITEA_HOSTS:
            base = f"https://{host}/{parts[0]}/{parts[1].removesuffix('.git')}"
            candidates += [f"{base}/archive/v{version}.tar.gz",
                           f"{base}/archive/{version}.tar.gz"]
    return list(dict.fromkeys(candidates))


def _target_mode(filename):
    if filename.endswith((".tar.gz", ".tgz")):
        return ""
    for ext, mode in _RECOMPRESS.items():
        if filename.endswith(ext):
            return mode
    return None


def _recompress(src, dest, mode):
    with tarfile.open(src, "r:*") as tin, tarfile.open(dest, mode) as tout:
        for member in tin:
            tout.addfile(member, tin.extractfile(member) if member.isfile() else None)


def fetch_missing_source(spec_path, filename, version, download, log=print):
    """Download *filename* (a bare Source name of the spec) for *version*.

    *download(url, dest_path)* fetches one URL to an absolute path and
    returns True on success. Returns True when the file is in place."""
    spec_path = Path(spec_path)
    dest = spec_path.parent / filename
    if dest.exists():
        return True
    mode = _target_mode(filename)
    if mode is None:
        log(f"[UPDATE] Source {filename} has no URL and its archive type cannot be "
            f"derived; put the new archive next to {spec_path.name} yourself.")
        return False
    spec_text = spec_path.read_text(encoding="utf-8", errors="replace")
    candidates = archive_url_candidates(spec_text, version)
    if not candidates:
        log(f"[UPDATE] Source {filename} has no URL and no known project URL in "
            f"{spec_path.name} to derive it from; add the archive yourself or a URL to the Source line.")
        return False
    tmp = dest.with_name(f".{dest.name}.download.tar.gz")
    for url in candidates:
        tmp.unlink(missing_ok=True)
        log(f"[UPDATE] Source {filename} has no URL, trying {url}")
        if not download(url, tmp) or not tmp.exists():
            continue
        if not tarfile.is_tarfile(tmp):
            log(f"[UPDATE] {url} is not a tar archive, ignored.")
            continue
        try:
            if mode:
                _recompress(tmp, dest, mode)
            else:
                tmp.replace(dest)
        except (OSError, tarfile.TarError) as e:
            log(f"[UPDATE] Could not store {filename}: {e}")
            dest.unlink(missing_ok=True)
            continue
        finally:
            tmp.unlink(missing_ok=True)
        log(f"[UPDATE] Stored {url} as {filename}.")
        return True
    log(f"[UPDATE] Could not download {filename}: none of the derived URLs worked. "
        f"Add the archive yourself or a URL to the Source line.")
    return False
