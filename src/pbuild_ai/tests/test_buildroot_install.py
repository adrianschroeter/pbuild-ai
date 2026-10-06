import unittest

from pbuild_ai.parsing import parse_buildroot_install_failures, spec_binary_packages

LOG = """\
[   96s] librocblas5-7.2.0-9.1                 ########################################
[   96s] error: unpacking of archive failed on file /usr/lib64/rocblas/library/x.co;6ac4971a: cpio: write failed - No data available
[   96s] error: librocblas5-7.2.0-9.1.x86_64: install failed
[  108s] error: unpacking of archive failed on file /usr/lib64/librocsolver.so.0.7;6ac4971a: cpio: write failed - No such file or directory
[  108s] error: librocsolver0-7.2.0-7.1.x86_64: install failed
[  114s] error: libhipblaslt1-7.2.0-7.1.x86_64: install failed
[  114s] rpm-build-4.20.1-11.1                 ########################################
"""


class TestInstallFailures(unittest.TestCase):
    def test_names_and_lines(self):
        names, lines = parse_buildroot_install_failures(LOG)
        self.assertEqual(names, ["librocblas5", "librocsolver0", "libhipblaslt1"])
        self.assertEqual(len(lines), 5)
        self.assertTrue(lines[0].startswith("error: unpacking of archive failed"))

    def test_clean_log(self):
        self.assertEqual(parse_buildroot_install_failures("all fine\nerror: foo.c:1: bad"), ([], []))
        self.assertEqual(parse_buildroot_install_failures(None), ([], []))


class TestSpecBinaryPackages(unittest.TestCase):
    def test_main_and_subpackages(self):
        spec = ("Name:           rocblas\n%define sover 5\nVersion: 7.2.0\n"
                "%package -n librocblas%{sover}\n%package devel\n")
        self.assertEqual(spec_binary_packages(spec), {"rocblas", "librocblas5", "rocblas-devel"})

    def test_no_name(self):
        self.assertEqual(spec_binary_packages("%package devel\n"), set())


if __name__ == "__main__":
    unittest.main()
