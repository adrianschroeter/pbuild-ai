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

"""Sync the Python dependencies of a spec with the pyproject.toml of its source.

After a version update the Python BuildRequires (``%{python_module foo}``)
and the Requires (``python-foo``) of the main package follow the new
pyproject.toml:

- minimum versions are taken over,
- Requires: lists only runtime dependencies ([project] dependencies /
  non-optional poetry dependencies); missing ones are added,
- new build-system requirements are added as BuildRequires, and new runtime
  and test dependencies too when the spec already lists those (test section),
- dependencies upstream dropped since the old pyproject.toml are removed.
"""

import re
import tarfile
import zipfile
from pathlib import Path

try:
    import tomllib
except ImportError:  # Python < 3.11
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None

_ARCHIVE_SUFFIXES = ('.tar.gz', '.tgz', '.tar.xz', '.txz', '.tar.bz2', '.tbz2', '.tar.zst', '.tar', '.zip')
_REQ_RE = re.compile(r'^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*\(?([^;()]*)\)?\s*(?:;\s*(.*))?$')
_PY_MODULE_RE = re.compile(r'%\{python_module\s+([A-Za-z0-9][A-Za-z0-9._-]*)(\s*[<>=!~]=?\s*[^\s}]+)?\s*\}')
_REQUIRES_RE = re.compile(r'^Requires:(\s*)python-([A-Za-z0-9][A-Za-z0-9._-]*)(\s*[<>=]=?\s*\S+)?\s*$')


def normalize(name):
    """PEP 503 name: 'Py_YAML' and 'py.yaml' are both 'py-yaml'."""
    return re.sub(r'[-_.]+', '-', name).lower()


def lower_bound(specifier):
    """'>= 1.2' style lower bound of a PEP 440 specifier ('>=1.2,<2'), or ''."""
    for part in (specifier or '').split(','):
        m = re.match(r'\s*(>=|~=|==|>)\s*([0-9][\w.]*?)(?:\.\*)?\s*$', part)
        if m:
            return f"{'>' if m.group(1) == '>' else '>='} {m.group(2)}"
    return ''


def _poetry_bound(value):
    if isinstance(value, dict):
        value = value.get('version', '')
    if not isinstance(value, str):
        return ''
    m = re.match(r'\s*[\^~]\s*([0-9][\w.]*)', value)
    return f'>= {m.group(1)}' if m else lower_bound(value)


def parse_pyproject(text):
    """{'runtime': {name: (display name, bound, marker)}, 'build': {...}, 'test': {...}, 'other': {...}}

    'runtime' are the install requirements, 'build' the build-system
    requirements, 'test' the test extras/groups, 'other' further optional
    and dev dependencies."""
    data = tomllib.loads(text)
    result = {'runtime': {}, 'build': {}, 'test': {}, 'other': {}}

    def add(kind, req):
        m = _REQ_RE.match(req) if isinstance(req, str) else None
        if m:
            result[kind].setdefault(normalize(m.group(1)),
                                    (m.group(1), lower_bound(m.group(2)), (m.group(3) or '').strip()))

    project = data.get('project', {})
    for req in project.get('dependencies', []):
        add('runtime', req)
    for req in data.get('build-system', {}).get('requires', []):
        add('build', req)
    for group, reqs in list(project.get('optional-dependencies', {}).items()) + \
            list(data.get('dependency-groups', {}).items()):
        for req in reqs:
            add(_group_kind(group), req)
    poetry = data.get('tool', {}).get('poetry', {})
    for name, value in poetry.get('dependencies', {}).items():
        if name.lower() == 'python':
            continue
        kind = 'other' if isinstance(value, dict) and value.get('optional') else 'runtime'
        result[kind].setdefault(normalize(name), (name, _poetry_bound(value), ''))
    groups = [(n, g.get('dependencies', {})) for n, g in poetry.get('group', {}).items()]
    for group, deps in groups + [('dev', poetry.get('dev-dependencies', {}))]:
        for name, value in deps.items():
            result[_group_kind(group)].setdefault(normalize(name), (name, _poetry_bound(value), ''))
    return result


def _group_kind(group):
    return 'test' if re.match(r'(?i)tests?$|testing$', str(group)) else 'other'


def read_pyproject(spec_dir, version):
    """pyproject.toml text of the source archive of *version* next to the spec, or None.

    The pristine copies of an osc checkout (.osc/sources, .osc) are searched
    too: the old archive is usually gone after the update."""
    spec_dir = Path(spec_dir)
    archives = []
    for d in (spec_dir, spec_dir / '.osc' / 'sources', spec_dir / '.osc'):
        if d.is_dir():
            archives += sorted(d.iterdir())
    for archive in archives:
        if not (archive.is_file() and version in archive.name and archive.name.endswith(_ARCHIVE_SUFFIXES)):
            continue
        try:
            if archive.name.endswith('.zip'):
                with zipfile.ZipFile(archive) as z:
                    names = [n for n in z.namelist() if n.rstrip('/').endswith('pyproject.toml')]
                    if names:
                        return z.read(min(names, key=lambda n: n.count('/'))).decode('utf-8', 'replace')
            else:
                with tarfile.open(archive, 'r:*') as t:
                    members = [m for m in t.getmembers() if m.isfile() and m.name.endswith('pyproject.toml')]
                    if members:
                        best = min(members, key=lambda m: m.name.count('/'))
                        return t.extractfile(best).read().decode('utf-8', 'replace')
        except (OSError, tarfile.TarError, zipfile.BadZipFile, EOFError):
            continue
    return None


def _preamble_end(lines):
    """Index of the first line after the main package preamble."""
    for i, line in enumerate(lines):
        if re.match(r'^%(package|description|prep)\b', line):
            return i
    return len(lines)


def _tag_line(tag, value, like=None):
    """'Tag:    value' aligned like *like* (another tag line) or to column 16."""
    m = re.match(r'^\w+:(\s*)', like or '')
    width = len(m.group(0)) if m else 16
    return f"{tag}:".ljust(max(width, len(tag) + 2)) + value


# Needed by %pyproject_wheel even when pyproject.toml no longer names them.
_KEEP = {'pip', 'wheel'}


def _merged(deps):
    return {**deps['other'], **deps['test'], **deps['build'], **deps['runtime']} if deps else {}


def sync_spec(spec_text, deps, log=print, old_deps=None):
    """*spec_text* with its Python dependencies synced to *deps* (see
    parse_pyproject). *old_deps*, the parsed pyproject.toml of the old
    version, tells which dependencies upstream dropped."""
    known = _merged(deps)
    runtime = deps['runtime']
    old_runtime = old_deps['runtime'] if old_deps else {}
    # Gone from every section of pyproject.toml since the old version.
    dropped = set(_merged(old_deps)) - set(known) - _KEEP
    lines = spec_text.split('\n')
    end = _preamble_end(lines)

    def bound_of(name):
        return known[normalize(name)][1] if normalize(name) in known else ''

    def fix_module(m):
        name, old = m.group(1), (m.group(2) or '').strip()
        if normalize(name) in dropped:
            log(f"[PYTHON] Removed BuildRequires {name}: dropped from pyproject.toml.")
            return ''
        bound = bound_of(name)
        if not bound or ' '.join(old.split()) == bound or 'with' in m.group(0):
            return m.group(0)
        log(f"[PYTHON] {name}: {old or '(no version)'} -> {bound}")
        return f"%{{python_module {name} {bound}}}"

    out = []
    br_present, req_present = set(), set()
    # Insert positions (index in out) per kind of BuildRequires.
    anchor = {}
    for i, line in enumerate(lines):
        if re.match(r'^BuildRequires:', line):
            keys = [normalize(m.group(1)) for m in _PY_MODULE_RE.finditer(line)]
            line = _PY_MODULE_RE.sub(fix_module, line)
            if keys and not line.split(':', 1)[1].strip():
                continue
            if keys:
                # close the gap a removed macro left between two others
                line = re.sub(r'\}\s{2,}%\{', '} %{', line.rstrip())
            if i < end:
                anchor['any'] = len(out)
                if keys:
                    anchor['python'] = len(out)
                for key in keys:
                    br_present.add(key)
                    for kind in ('build', 'runtime', 'test'):
                        if key in deps[kind] or (old_deps and key in old_deps[kind]):
                            anchor[kind] = len(out)
        elif i < end and (m := _REQUIRES_RE.match(line)):
            name, old = m.group(2), (m.group(3) or '').strip()
            key = normalize(name)
            if key in dropped or key in old_runtime and key not in runtime:
                log(f"[PYTHON] Removed 'Requires: python-{name}': dropped from the runtime dependencies.")
                continue
            if key in known and key not in runtime:
                log(f"[PYTHON] Removed 'Requires: python-{name}': not a runtime dependency in pyproject.toml.")
                continue
            req_present.add(key)
            bound = bound_of(name)
            if bound and ' '.join(old.split()) != bound:
                log(f"[PYTHON] python-{name}: {old or '(no version)'} -> {bound}")
                line = f"Requires:{m.group(1)}python-{name} {bound}"
            anchor['requires'] = len(out)
        out.append(line)

    def missing(kind, present):
        return [(n, b) for key, (n, b, marker) in deps[kind].items()
                if key not in present and key not in _KEEP and not marker]

    inserts = {}

    def module(name, bound):
        return f"%{{python_module {name} {bound}}}" if bound else f"%{{python_module {name}}}"

    def insert(at, tag, values, what):
        if at is None:
            log(f"[PYTHON] No place found for the {what}: " + ", ".join(values))
            return
        inserts.setdefault(at, []).extend(_tag_line(tag, v, out[at]) for v in values)
        for v in values:
            log(f"[PYTHON] Added '{tag}: {v}' ({what}).")

    new_build = missing('build', br_present)
    if new_build:
        insert(anchor.get('build', anchor.get('python', anchor.get('any'))), 'BuildRequires',
               [module(n, b) for n, b in new_build],
               'build requirement in pyproject.toml')
    # Runtime and test dependencies are BuildRequires only when the spec runs
    # the tests, i.e. lists such dependencies already.
    for kind, what in (('runtime', 'runtime dependency for the tests'), ('test', 'test dependency')):
        if kind in anchor:
            new = [(n, b) for n, b in missing(kind, br_present)
                   if not any(normalize(n) == normalize(x) for x, _ in new_build)]
            br_present |= {normalize(n) for n, _ in new}
            if new:
                insert(anchor[kind], 'BuildRequires',
                       [module(n, b) for n, b in new], what)
    new_req = missing('runtime', req_present)
    if new_req:
        insert(anchor.get('requires', anchor.get('any')), 'Requires',
               [f"python-{n} {b}".rstrip() for n, b in new_req], 'runtime dependency in pyproject.toml')
    for at in sorted(inserts, reverse=True):
        out[at + 1:at + 1] = inserts[at]
    return '\n'.join(out)


def snapshot_pyproject(spec_path, spec_text):
    """pyproject.toml text of the current (old) source archive, taken before
    the update replaces it; None when there is none."""
    m = re.search(r'^Version:\s*(\S+)', spec_text or '', re.M)
    return read_pyproject(Path(spec_path).parent, m.group(1)) if m and '%' not in m.group(1) else None


def update_spec_from_pyproject(spec_path, spec_text, new_version, log=print,
                               old_version=None, old_pyproject=None):
    """sync_spec with the pyproject.toml of the new source archive; *spec_text*
    unchanged when there is none. The old pyproject.toml (*old_pyproject* or
    the archive of *old_version*) lets dropped dependencies be removed."""
    if tomllib is None:
        log("[PYTHON] tomllib is not available; Python dependencies not synced with pyproject.toml.")
        return spec_text
    text = read_pyproject(Path(spec_path).parent, new_version)
    if text is None:
        return spec_text
    if old_pyproject is None and old_version and old_version != new_version:
        old_pyproject = read_pyproject(Path(spec_path).parent, old_version)
    try:
        deps = parse_pyproject(text)
        old_deps = parse_pyproject(old_pyproject) if old_pyproject else None
    except Exception as e:
        log(f"[PYTHON] Could not parse pyproject.toml: {e}")
        return spec_text
    log(f"[PYTHON] Syncing Python dependencies with pyproject.toml of {new_version}"
        + ("." if old_deps else " (old pyproject.toml not found, dropped dependencies are kept)."))
    return sync_spec(spec_text, deps, log, old_deps)


# Import name of a build backend -> its package.
_BACKENDS = {
    'hatchling': 'hatchling', 'setuptools': 'setuptools', 'poetry': 'poetry-core',
    'flit_core': 'flit-core', 'flit': 'flit', 'pdm': 'pdm-backend', 'mesonpy': 'meson-python',
    'scikit_build_core': 'scikit-build-core', 'maturin': 'maturin', 'sipbuild': 'sip',
    'whey': 'whey', 'uv_build': 'uv-build', 'enscons': 'enscons', 'jupyter_packaging': 'jupyter-packaging',
}
# Import name -> package name where they differ.
_IMPORT_NAMES = {
    'yaml': 'PyYAML', 'PIL': 'Pillow', 'dateutil': 'python-dateutil', 'attr': 'attrs',
    'bs4': 'beautifulsoup4', 'Crypto': 'pycryptodome', 'Cryptodome': 'pycryptodomex',
    'sklearn': 'scikit-learn', 'skimage': 'scikit-image', 'gi': 'gobject', 'jwt': 'PyJWT',
    'serial': 'pyserial', 'magic': 'python-magic', 'OpenSSL': 'pyOpenSSL', 'dns': 'dnspython',
    'git': 'GitPython', 'zmq': 'pyzmq', 'usb': 'pyusb', 'docx': 'python-docx', 'cv2': 'opencv',
    'google': 'protobuf', 'distutils': 'setuptools', 'pkg_resources': 'setuptools',
    'setuptools_scm': 'setuptools_scm', 'hatch_vcs': 'hatch-vcs', 'mpl_toolkits': 'matplotlib',
    'Levenshtein': 'Levenshtein', 'dbus': 'dbus-python', 'cairo': 'pycairo', 'xdist': 'pytest-xdist',
    'pytest_asyncio': 'pytest-asyncio', 'pytest_mock': 'pytest-mock', 'markdown': 'Markdown',
    'jinja2': 'Jinja2', 'markupsafe': 'MarkupSafe', 'lxml': 'lxml', 'nacl': 'PyNaCl',
    'socks': 'PySocks', 'Xlib': 'python-xlib', 'ruamel': 'ruamel.yaml', 'tomli_w': 'tomli-w',
}
_BACKEND_UNAVAILABLE_RE = re.compile(r"BackendUnavailable: Cannot import '([\w.]+)'")
_NO_MODULE_RE = re.compile(r"(?:ModuleNotFoundError|ImportError): No module named '([\w.]+)'")
_FAILED_SECTION_RE = re.compile(r'Bad exit status from \S+ \((%\w+)\)')


def _spec_module_keys(spec_text):
    return {normalize(m.group(1)) for line in spec_text.splitlines()
            if line.startswith('BuildRequires:') for m in _PY_MODULE_RE.finditer(line)}


def _insert_buildrequires(spec_text, values, in_tests):
    """Insert BuildRequires lines: build requirements after the last
    unconditional %{python_module} line, test dependencies after the last
    %{python_module} line of the preamble (the test section)."""
    lines = spec_text.split('\n')
    end = _preamble_end(lines)
    depth, at, at_any, at_br = 0, None, None, None
    for i, line in enumerate(lines[:end]):
        if re.match(r'^%if', line):
            depth += 1
        elif re.match(r'^%endif', line):
            depth = max(depth - 1, 0)
        elif line.startswith('BuildRequires:'):
            at_br = i
            if _PY_MODULE_RE.search(line):
                at_any = i
                if depth == 0:
                    at = i
    pos = at_any if in_tests else at
    if pos is None:
        pos = at if at is not None else at_any if at_any is not None else at_br
    if pos is None:
        pos = next((i for i, l in enumerate(lines) if l.startswith('Version:')), len(lines) - 1)
    like = lines[pos] if lines[pos].startswith('BuildRequires:') else None
    lines[pos + 1:pos + 1] = [_tag_line('BuildRequires', v, like) for v in values]
    return '\n'.join(lines)


def fix_missing_module(spec_path, spec_text, build_log, log=print):
    """*spec_text* with BuildRequires for the Python modules *build_log*
    misses (BackendUnavailable, ModuleNotFoundError), or None when there is
    nothing to add."""
    backend = _BACKEND_UNAVAILABLE_RE.search(build_log or '')
    missing = _NO_MODULE_RE.findall(build_log or '')
    if not backend and not missing:
        return None
    version = re.search(r'^Version:\s*(\S+)', spec_text, re.M)
    pyproject = read_pyproject(Path(spec_path).parent, version.group(1)) if version else None
    deps = None
    if pyproject and tomllib is not None:
        try:
            deps = parse_pyproject(pyproject)
        except Exception:
            deps = None
    present = _spec_module_keys(spec_text)
    name = re.search(r'^Name:\s*(\S+)', spec_text, re.M)
    own = normalize(re.sub(r'^python-', '', name.group(1))) if name else ''
    wanted = []  # (package, bound, reason)

    def want(pkg, bound, reason):
        key = normalize(pkg)
        if key not in present and key != own and all(normalize(p) != key for p, _b, _r in wanted):
            wanted.append((pkg, bound, reason))

    if backend:
        reason = f"build backend {backend.group(1)} cannot be imported"
        if deps and deps['build']:
            for pkg, bound, marker in deps['build'].values():
                if not marker:
                    want(pkg, bound, reason)
        else:
            top = backend.group(1).split('.')[0]
            want(_BACKENDS.get(top, top), '', reason)
    section = _FAILED_SECTION_RE.findall(build_log or '')
    in_tests = bool(section) and section[-1] == '%check'
    known = _merged(deps)
    for module in dict.fromkeys(missing):
        top = module.split('.')[0]
        key = normalize(top)
        if key in known:
            pkg, bound = known[key][0], known[key][1]
        else:
            pkg, bound = _IMPORT_NAMES.get(top, top), ''
        want(pkg, bound, f"No module named '{module}'")
    if not wanted:
        return None
    for pkg, bound, reason in wanted:
        log(f"[PYTHON] Adding BuildRequires %{{python_module {pkg}}}: {reason}.")
    values = [f"%{{python_module {p} {b}}}" if b else f"%{{python_module {p}}}" for p, b, _r in wanted]
    # Backend requirements are needed for %build, never only for the tests.
    return _insert_buildrequires(spec_text, values, in_tests and not backend)
