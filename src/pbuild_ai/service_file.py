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

"""Keep a package's ``_service`` file in step with the spec Version.

Packages that fetch their sources with tar_scm/obs_scm pin the upstream tag in
``<param name="revision">`` (sometimes ``<param name="version">``). A version
update must move that pin too and re-run the services, otherwise the build
misses the new source archive.
"""

import re
import shutil
import subprocess
from pathlib import Path

# Params that pin the upstream release. versionformat/versionrewrite-* are
# patterns, not versions, and are never touched.
_PINNED_PARAMS = ("revision", "version")
_PARAM_RE = re.compile(
    r'(<param\s+name\s*=\s*["\'](%s)["\']\s*>)([^<]*)(</param>)' % "|".join(_PINNED_PARAMS))
_SOURCE_RE = re.compile(r'^Source\d*\s*:\s*(\S+)', re.M | re.I)
_DEFINE_RE = re.compile(r'^%(?:define|global)\s+(\w+)\s+(\S+)\s*$', re.M)


def _version_re(version):
    # Match the version as a whole token, also inside tags like 'v1.2.3'
    # or 'release-1.2.3', but never '1.2' inside '1.2.3' or '11.2'.
    return re.compile(r'(?<![\d.])' + re.escape(version) + r'(?![\d.]*\d)')


def bump_service_version(service_text, old_version, new_version):
    """Return ``(text, changed)`` with *old_version* replaced by
    *new_version* in the revision/version params; ``changed`` lists the
    ``(param, old_value, new_value)`` edits."""
    if not service_text or not old_version or not new_version or old_version == new_version:
        return service_text, []
    pattern = _version_re(old_version)
    changed = []

    def _sub(m):
        value = m.group(3)
        new_value = pattern.sub(new_version, value)
        if new_value != value:
            changed.append((m.group(2), value.strip(), new_value.strip()))
        return m.group(1) + new_value + m.group(4)

    return _PARAM_RE.sub(_sub, service_text), changed


def local_source_files(spec_text):
    """Names of Source files the spec expects next to it (no URLs), with
    %name, %version and simple %define/%global macros expanded."""
    macros = {k.lower(): v for k, v in re.findall(r'^(Name|Version)\s*:\s*(\S+)', spec_text or '', re.M | re.I)}
    for k, v in _DEFINE_RE.findall(spec_text or ''):
        macros.setdefault(k, v)
    files = []
    for src in _SOURCE_RE.findall(spec_text or ''):
        if '://' in src:
            continue
        for _ in range(3):
            src = re.sub(r'%\{\??(\w+)\}|%(\w+)',
                         lambda m: macros.get(m.group(1) or m.group(2), m.group(0)), src)
        if '%' not in src:
            files.append(Path(src).name)
    return files


def sync_service_version(spec_path, old_version, new_version,
                         run=subprocess.run, which=shutil.which, log=print):
    """Move the ``_service`` pin from *old_version* to *new_version* and run
    the services when a Source archive of the spec is missing.

    Returns True when the sources are (now) complete, False when a Source
    is still missing, None when the package has no ``_service`` file."""
    spec_path = Path(spec_path)
    service = spec_path.parent / "_service"
    if not service.is_file():
        return None
    text = service.read_text(encoding="utf-8", errors="replace")
    new_text, changed = bump_service_version(text, old_version, new_version)
    if changed:
        service.write_text(new_text, encoding="utf-8")
        for param, before, after in changed:
            log(f"[SERVICE] _service: {param} {before} -> {after}")

    spec_text = spec_path.read_text(encoding="utf-8", errors="replace")
    missing = [f for f in local_source_files(spec_text) if not (spec_path.parent / f).exists()]
    if not missing:
        return True
    if not which("osc"):
        log(f"[SERVICE] Missing source {', '.join(missing)}; osc is not installed, "
            f"run 'osc service manualrun' in {spec_path.parent} to fetch it.")
        return False
    log(f"[SERVICE] Missing source {', '.join(missing)} — running 'osc service manualrun'...")
    try:
        r = run(["osc", "service", "manualrun"], cwd=str(spec_path.parent),
                capture_output=True, text=True, timeout=1800)
        ok = r.returncode == 0
        output = (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.SubprocessError) as e:
        ok, output = False, str(e)
    if not ok:
        tail = "\n".join(output.strip().splitlines()[-15:])
        log(f"[SERVICE] 'osc service manualrun' failed:\n{tail}")
        return False
    spec_text = spec_path.read_text(encoding="utf-8", errors="replace")
    missing = [f for f in local_source_files(spec_text) if not (spec_path.parent / f).exists()]
    if missing:
        log(f"[SERVICE] Services ran, but {', '.join(missing)} is still missing.")
        return False
    log("[SERVICE] Services ran, sources are complete.")
    return True
