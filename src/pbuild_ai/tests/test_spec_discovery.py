"""Tests for spec file discovery: build recipes are never picked up from
vendored source trees, only from the workspace top level (orphan mode) or the
_manifest package directories (project mode)."""

import os
import sys
import tempfile
import types
import unittest

def _safe_load(stream):
    text = stream.read() if hasattr(stream, 'read') else stream
    data = {}
    current = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        if not line.startswith((' ', '\t')):
            key, _, rest = line.partition(':')
            key, rest = key.strip(), rest.strip()
            if rest:
                data[key] = rest
                current = None
            else:
                data[key] = []
                current = key
            continue
        if current is not None and stripped.startswith('- '):
            data[current].append(stripped[2:].strip())
    return data


_yaml_stub = types.ModuleType('yaml')
_yaml_stub.YAMLError = Exception
_yaml_stub.safe_load = _safe_load

_installed_yaml_stub = 'yaml' not in sys.modules
if _installed_yaml_stub:
    sys.modules['yaml'] = _yaml_stub

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from pbuild_ai import manifest as _manifest
from pbuild_ai.manifest import find_spec_files

if _installed_yaml_stub:
    sys.modules.pop('yaml', None)

_ORIGINAL_MANIFEST_YAML = _manifest.yaml


def setUpModule():
    _manifest.yaml = _yaml_stub


def tearDownModule():
    _manifest.yaml = _ORIGINAL_MANIFEST_YAML
    if _installed_yaml_stub:
        sys.modules.pop('yaml', None)


def _touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("Name: dummy\nVersion: 1.0\n")


def _rel(base, paths):
    return sorted(os.path.relpath(str(p), base) for p in paths)


class TestOrphanMode(unittest.TestCase):

    def test_only_toplevel_specs(self):
        with tempfile.TemporaryDirectory() as td:
            _touch(os.path.join(td, "ollama.spec"))
            _touch(os.path.join(td, "llama.cpp", ".devops", "llama-cpp.srpm.spec"))
            _touch(os.path.join(td, "llama.cpp", ".devops", "llama-cpp-cuda.srpm.spec"))
            _touch(os.path.join(td, ".git", "hidden.spec"))
            _touch(os.path.join(td, "_build.tumbleweed.x86_64", "build.spec"))
            self.assertEqual(_rel(td, find_spec_files(td)), ["ollama.spec"])

    def test_no_specs(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(find_spec_files(td), [])

    def test_missing_directory(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(find_spec_files(os.path.join(td, "nope")), [])

    def test_subdir_spec_ignored_even_without_vendoring(self):
        with tempfile.TemporaryDirectory() as td:
            _touch(os.path.join(td, "top.spec"))
            _touch(os.path.join(td, "subdir", "nested.spec"))
            self.assertEqual(_rel(td, find_spec_files(td)), ["top.spec"])


class TestProjectMode(unittest.TestCase):

    def test_manifest_subdirectories(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "_manifest"), "w") as f:
                f.write("subdirectories:\n  - .\n")
            _touch(os.path.join(td, "pkg-a", "pkg-a.spec"))
            _touch(os.path.join(td, "pkg-b", "pkg-b.spec"))
            _touch(os.path.join(td, "pkg-b", "vendor", "vendored.spec"))
            _touch(os.path.join(td, "pkg-b", "src", ".devops", "upstream.srpm.spec"))
            self.assertEqual(_rel(td, find_spec_files(td, project_mode=True)),
                             ["pkg-a/pkg-a.spec", "pkg-b/pkg-b.spec"])

    def test_manifest_nested_group(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "_manifest"), "w") as f:
                f.write("subdirectories:\n  - grouping\n")
            _touch(os.path.join(td, "grouping", "pkg-a", "pkg-a.spec"))
            _touch(os.path.join(td, "grouping", "pkg-a", "sub", "deep.spec"))
            self.assertEqual(_rel(td, find_spec_files(td, project_mode=True)),
                             ["grouping/pkg-a/pkg-a.spec"])

    def test_manifest_packages_entries(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "_manifest"), "w") as f:
                f.write("packages:\n  - alpha\n  - beta\n")
            _touch(os.path.join(td, "alpha", "alpha.spec"))
            _touch(os.path.join(td, "beta", "beta.spec"))
            _touch(os.path.join(td, "beta", "bundled", "bundled.spec"))
            self.assertEqual(_rel(td, find_spec_files(td, project_mode=True)),
                             ["alpha/alpha.spec", "beta/beta.spec"])

    def test_project_mode_ignores_loose_toplevel_spec(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "_manifest"), "w") as f:
                f.write("subdirectories:\n  - .\n")
            _touch(os.path.join(td, "stray.spec"))
            _touch(os.path.join(td, "pkg-a", "pkg-a.spec"))
            self.assertEqual(_rel(td, find_spec_files(td, project_mode=True)),
                             ["pkg-a/pkg-a.spec"])

    def test_orphan_mode_still_finds_toplevel(self):
        with tempfile.TemporaryDirectory() as td:
            _touch(os.path.join(td, "ollama.spec"))
            _touch(os.path.join(td, "llama.cpp", ".devops", "llama-cpp.srpm.spec"))
            self.assertEqual(_rel(td, find_spec_files(td, project_mode=False)),
                             ["ollama.spec"])


if __name__ == "__main__":
    unittest.main()
