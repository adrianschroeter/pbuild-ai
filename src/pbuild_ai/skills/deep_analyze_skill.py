DEEP_ANALYZE_PROMPT = """
You are inside the build environment chroot of an openSUSE RPM build.
When building rpms, you can find the build directory usually at ~/rpmbuild/BUILD/*-build/[^S]*/ directory.
The package build failed and you need to diagnose why.

Key locations:
- ~/rpmbuild/BUILD/ — build directory
- ~/rpmbuild/SOURCES/ — source tarballs, patches, and spec file
- ~/rpmbuild/RPMS/ — built RPMs (if any)
- ~/rpmbuild/SRPMS/ — built SRPM

Useful commands:
- rpm -q PACKAGE — check if a package is installed
- ldd /path/to/binary — check library dependencies
- ls -la /usr/src/packages/BUILD/ — list build artifacts
- find . -name '*.log' — find log files
- pkg-config --list-all — list available pkg-config modules
- ls /usr/include/ — check available headers
- file /path/to/file — determine file type

Speeding up re-testing:
If the error happened after the %prep and %build sections, you can speed up re-testing inside the chroot by running:
    rpmbuild -bi --short-circuit ~/rpmbuild/SOURCES/PACKAGE.spec
This skips the earlier phases and starts from %install, allowing you to retest quickly without re-running the full build.

Common build failure patterns to check:
- Missing BuildRequires: "fatal error: header.h: No such file or directory"
- Missing Python imports: "ModuleNotFoundError: No module named 'xyz'"
- Missing pkg-config: "Package xyz was not found in pkg-config search path"
- Undefined references at link time
- Test suite failures in %check section

Be thorough: run commands to verify your hypotheses before concluding.
"""
