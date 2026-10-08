import io
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from pbuild_ai.pyproject_deps import (lower_bound, parse_pyproject, snapshot_pyproject, sync_spec,
                                     update_spec_from_pyproject)

PYPROJECT = """\
[build-system]
requires = ["setuptools>=77", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "demo"
dependencies = [
    "requests>=2.32.0,<3",
    "PyYAML ~= 6.0.2",
    "attrs",
    "tomli>=2; python_version < '3.11'",
    "click>=8.1",
]

[project.optional-dependencies]
test = ["pytest>=8", "pytest-cov"]
"""

SPEC = """\
Name:           python-demo
Version:        2.0
BuildRequires:  %{python_module setuptools >= 61}
BuildRequires:  %{python_module wheel}
BuildRequires:  python-rpm-macros
# SECTION test
BuildRequires:  %{python_module pytest >= 7}
BuildRequires:  %{python_module requests >= 2.28}
BuildRequires:  %{python_module PyYAML}
# /SECTION
Requires:       python-requests >= 2.28
Requires:       python-PyYAML
Requires:       python-attrs
Requires:       python-setuptools
Requires:       python-pytest
Requires:       python-distro
%python_subpackages

%description
Demo.

%package doc
Summary:        Docs
Requires:       python-setuptools

%prep
"""


def _log():
    msgs = []
    return msgs, msgs.append


class TestLowerBound(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(lower_bound(">=2.32.0,<3"), ">= 2.32.0")
        self.assertEqual(lower_bound("<3,>=1.0"), ">= 1.0")
        self.assertEqual(lower_bound("~= 6.0.2"), ">= 6.0.2")
        self.assertEqual(lower_bound("==1.2.*"), ">= 1.2")
        self.assertEqual(lower_bound(">1"), "> 1")
        self.assertEqual(lower_bound("<3"), "")
        self.assertEqual(lower_bound(""), "")


class TestParsePyproject(unittest.TestCase):
    def test_pep621(self):
        deps = parse_pyproject(PYPROJECT)
        self.assertEqual(deps['runtime']['requests'][1], ">= 2.32.0")
        self.assertEqual(deps['runtime']['pyyaml'][:2], ("PyYAML", ">= 6.0.2"))
        self.assertEqual(deps['runtime']['tomli'][2], "python_version < '3.11'")
        self.assertEqual(deps['build']['setuptools'][1], ">= 77")
        self.assertIn('pytest-cov', deps['test'])

    def test_poetry(self):
        deps = parse_pyproject("""\
[tool.poetry.dependencies]
python = "^3.9"
httpx = "^0.27"
rich = {version = ">=13", optional = true}
[tool.poetry.group.dev.dependencies]
pytest = "^8"
""")
        self.assertEqual(deps['runtime'], {'httpx': ('httpx', '>= 0.27', '')})
        self.assertIn('rich', deps['other'])
        self.assertIn('pytest', deps['other'])


class TestSyncSpec(unittest.TestCase):
    def setUp(self):
        self.msgs, log = _log()
        self.out = sync_spec(SPEC, parse_pyproject(PYPROJECT), log)

    def test_buildrequires_versions(self):
        self.assertIn("BuildRequires:  %{python_module setuptools >= 77}", self.out)
        self.assertIn("BuildRequires:  %{python_module wheel}", self.out)
        self.assertIn("BuildRequires:  %{python_module pytest >= 8}", self.out)
        self.assertIn("BuildRequires:  %{python_module requests >= 2.32.0}", self.out)
        self.assertIn("BuildRequires:  %{python_module PyYAML >= 6.0.2}", self.out)

    def test_requires_runtime_only(self):
        self.assertIn("Requires:       python-requests >= 2.32.0\n", self.out)
        self.assertIn("Requires:       python-PyYAML >= 6.0.2\n", self.out)
        self.assertIn("Requires:       python-attrs\n", self.out)
        self.assertNotIn("Requires:       python-pytest", self.out)
        # setuptools is a build requirement, not a runtime one
        self.assertNotIn("Requires:       python-setuptools\n%python", self.out)
        # unknown to pyproject.toml: kept
        self.assertIn("Requires:       python-distro", self.out)

    def test_missing_runtime_added_without_marker_deps(self):
        self.assertIn("Requires:       python-distro\nRequires:       python-click >= 8.1\n", self.out)
        self.assertNotIn("tomli", self.out)

    def test_test_section_gets_new_runtime_and_test_deps(self):
        self.assertIn("BuildRequires:  %{python_module pytest >= 8}\n"
                      "BuildRequires:  %{python_module pytest-cov}\n", self.out)
        self.assertIn("BuildRequires:  %{python_module PyYAML >= 6.0.2}\n"
                      "BuildRequires:  %{python_module attrs}\n"
                      "BuildRequires:  %{python_module click >= 8.1}\n# /SECTION", self.out)

    def test_nothing_dropped_without_old_pyproject(self):
        self.assertIn("python_module wheel", self.out)

    def test_subpackages_untouched(self):
        self.assertIn("Summary:        Docs\nRequires:       python-setuptools\n", self.out)

    def test_idempotent(self):
        msgs, log = _log()
        self.assertEqual(sync_spec(self.out, parse_pyproject(PYPROJECT), log), self.out)
        self.assertEqual(msgs, [])


OLD_PYPROJECT = """\
[build-system]
requires = ["setuptools>=61", "wheel", "setuptools-scm"]

[project]
dependencies = ["requests>=2.28", "PyYAML", "attrs", "distro", "six"]

[project.optional-dependencies]
test = ["pytest>=7", "mock"]
"""

NEW_PYPROJECT = """\
[build-system]
requires = ["hatchling>=1.25"]

[project]
dependencies = ["requests>=2.32", "PyYAML", "httpx>=0.27", "mock"]

[project.optional-dependencies]
test = ["pytest>=8"]
"""

OLD_SPEC = """\
Name:           python-demo
BuildRequires:  %{python_module setuptools >= 61}
BuildRequires:  %{python_module setuptools-scm}
BuildRequires:  %{python_module wheel}
BuildRequires:  %{python_module pip}
BuildRequires:  fdupes
%if %{with test}
BuildRequires:  %{python_module attrs}
BuildRequires:  %{python_module mock}
BuildRequires:  %{python_module pytest >= 7}
BuildRequires:  %{python_module requests >= 2.28} %{python_module six}  %{python_module PyYAML}
%endif
Requires:       python-PyYAML
Requires:       python-attrs
Requires:       python-distro
Requires:       python-requests >= 2.28
Requires:       python-six
Requires:       python-gobject
%python_subpackages

%description
Demo.
"""


class TestAddRemove(unittest.TestCase):
    """Dependencies upstream added or dropped between two pyproject.toml."""

    def setUp(self):
        self.msgs, log = _log()
        self.out = sync_spec(OLD_SPEC, parse_pyproject(NEW_PYPROJECT), log,
                             old_deps=parse_pyproject(OLD_PYPROJECT))

    def test_build_backend_switch(self):
        self.assertNotIn("setuptools", self.out)
        self.assertIn("BuildRequires:  %{python_module hatchling >= 1.25}\n", self.out)
        # needed by %pyproject_wheel, never removed
        self.assertIn("%{python_module wheel}", self.out)
        self.assertIn("%{python_module pip}", self.out)
        self.assertIn("BuildRequires:  fdupes", self.out)

    def test_test_section(self):
        self.assertNotIn("python_module attrs", self.out)
        self.assertIn("BuildRequires:  %{python_module requests >= 2.32} %{python_module PyYAML}\n"
                      "BuildRequires:  %{python_module httpx >= 0.27}\n%endif", self.out)
        self.assertIn("%{python_module mock}", self.out)
        self.assertIn("%{python_module pytest >= 8}", self.out)

    def test_requires(self):
        req = [l for l in self.out.splitlines() if l.startswith("Requires:")]
        self.assertEqual(req, ["Requires:       python-PyYAML",
                               "Requires:       python-requests >= 2.32",
                               "Requires:       python-gobject",
                               "Requires:       python-httpx >= 0.27",
                               "Requires:       python-mock"])

    def test_idempotent(self):
        msgs, log = _log()
        again = sync_spec(self.out, parse_pyproject(NEW_PYPROJECT), log, old_deps=parse_pyproject(OLD_PYPROJECT))
        self.assertEqual(again, self.out)
        self.assertEqual(msgs, [])


def _tar(path, files):
    with tarfile.open(path, "w:gz") as t:
        for name, text in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(text.encode())
            t.addfile(info, io.BytesIO(text.encode()))


class TestOldPyproject(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.d, True)
        _tar(self.d / "demo-2.0.tar.gz", {"demo-2.0/pyproject.toml": NEW_PYPROJECT})

    def test_snapshot_before_update(self):
        _tar(self.d / "demo-1.0.tar.gz", {"demo-1.0/pyproject.toml": OLD_PYPROJECT})
        snap = snapshot_pyproject(self.d / "python-demo.spec", "Version: 1.0\n")
        (self.d / "demo-1.0.tar.gz").unlink()
        msgs, log = _log()
        out = update_spec_from_pyproject(self.d / "python-demo.spec", OLD_SPEC, "2.0", log,
                                         old_version="1.0", old_pyproject=snap)
        self.assertNotIn("six", out)

    def test_osc_pristine_copy(self):
        (self.d / ".osc" / "sources").mkdir(parents=True)
        _tar(self.d / ".osc" / "sources" / "demo-1.0.tar.gz", {"demo-1.0/pyproject.toml": OLD_PYPROJECT})
        msgs, log = _log()
        out = update_spec_from_pyproject(self.d / "python-demo.spec", OLD_SPEC, "2.0", log, old_version="1.0")
        self.assertNotIn("six", out)

    def test_without_old_keeps_unknown(self):
        msgs, log = _log()
        out = update_spec_from_pyproject(self.d / "python-demo.spec", OLD_SPEC, "2.0", log, old_version="1.0")
        self.assertIn("python-six", out)
        self.assertIn("old pyproject.toml not found", msgs[0])


class TestUpdateFromArchive(unittest.TestCase):
    def test_reads_new_archive(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, d, True)
        with tarfile.open(d / "demo-2.0.tar.gz", "w:gz") as t:
            for name, text in {"demo-2.0/pyproject.toml": PYPROJECT,
                               "demo-2.0/vendor/x/pyproject.toml": "[project]\ndependencies = ['bad>=9']\n"}.items():
                info = tarfile.TarInfo(name)
                info.size = len(text.encode())
                t.addfile(info, io.BytesIO(text.encode()))
        (d / "python-demo.spec").write_text(SPEC)
        msgs, log = _log()
        out = update_spec_from_pyproject(d / "python-demo.spec", SPEC, "2.0", log)
        self.assertIn("python_module setuptools >= 77", out)
        self.assertNotIn("bad", out)
        self.assertEqual(update_spec_from_pyproject(d / "python-demo.spec", SPEC, "3.0", log), SPEC)


if __name__ == "__main__":
    unittest.main()
