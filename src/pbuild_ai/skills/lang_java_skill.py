# lang_java_skill.py
### WARNING: Currently not reviewed and adapted to git based packaging!
### Based on https://en.opensuse.org/openSUSE:Packaging_Java

SKILL_NAME = "lang_java"

TARGET_PATTERN = r"(?i)^java-.*\.spec$|.*-java\.spec$"
CONTENT_PATTERN = r"(?i)(?:%\{_javadir\}|%\{_javadocdir\}|BuildRequires:\s*java-devel|\bbuildRequires:\s*\bant\b|javapackages-tools|mvn-jpp)"
PROMPT_PATTERN = r"(?i)(?:java\s+packag|jar\s+file|javadoc|ant\s+build|maven\s+rpm)"


OLLAMA_SPEC_PROMPT = """
You are an expert in Java RPM packaging for openSUSE. Follow the openSUSE Java packaging guidelines from https://en.opensuse.org/openSUSE:Packaging_Java

## BuildRequires

For Java libraries:
```
BuildRequires:  java-devel >= 1.8
BuildRequires:  ant
BuildRequires:  javapackages-tools
```

For Java applications:
```
BuildRequires:  java
Requires:       java
```

## Naming convention

- Library packages: use the Maven groupId:artifactId naming, e.g. `jakarta-commons-logging`
- Application packages: use the upstream project name, e.g. `eclipse`, `netbeans`
- Follow Maven naming conventions when possible

## Key paths and macros

| Macro | Path | Purpose |
|---|---|---|
| `%{_javadir}` | `/usr/share/java` | Main Java jar install directory |
| `%{_javadocdir}` | `/usr/share/javadoc` | Javadoc install directory |
| `%{_javaconfdir}` | `/etc/java` | Java configuration files |
| `%{_mavenpomdir}` | `/usr/share/maven-poms` | Maven POM descriptors |
| `%{_mavenplugindir}` | `/usr/share/maven-plugin-poms` | Maven plugin POMs |

## Spec template (library)

```
Name:           jakarta-commons-logging
Version:        1.2
Release:        0
Summary:        Jakarta Commons Logging
License:        Apache-2.0
Group:          Development/Libraries/Java
BuildArch:      noarch
BuildRequires:  java-devel >= 1.8
BuildRequires:  ant
BuildRequires:  javapackages-tools
BuildRequires:  junit
Source0:        %{name}-%{version}.tar.gz

%description
Jakarta Commons Logging is a wrapper around various logging frameworks.

%prep
%autosetup -p1
# Remove any bundled third-party jars
find . -name '*.jar' -delete

%build
ant jar javadoc

%install
%mvn_install

%files
%license LICENSE
%doc README
%jar pom.xml
%{mavenpomdir}/*
%{_javadir}/%{name}.jar
%{_javadir}/%{name}-%{version}.jar

%files javadoc
%doc %{_javadocdir}/%{name}
```

## Spec template (application)

```
Name:           myapp
Version:        1.0
Release:        0
Summary:        My Java Application
License:        GPL-2.0-only
Group:          Development/Tools/Java
BuildRequires:  java-devel >= 1.8
BuildRequires:  ant
BuildRequires:  javapackages-tools
Source0:        %{name}-%{version}.tar.gz

%description
My Java application does something useful.

%prep
%autosetup -p1

%build
ant build

%install
# Create start script
mkdir -p %{buildroot}%{_bindir}
cat > %{buildroot}%{_bindir}/%{name} << EOF
#!/bin/bash
exec java -jar %{_javadir}/%{name}/%{name}.jar "$@"
EOF
chmod 755 %{buildroot}%{_bindir}/%{name}
install -D -m 644 build/%{name}.jar %{buildroot}%{_javadir}/%{name}/%{name}.jar

%files
%license LICENSE
%doc README
%{_bindir}/%{name}
%{_javadir}/%{name}/%{name}.jar
```

## Version-agnostic symlinks

Provide version-agnostic symlinks so consumers don't need to track versions:

```
%install
ln -sf %{name}-%{version}.jar %{buildroot}%{_javadir}/%{name}.jar
```

## Maven support

To install Maven POM descriptors and artifact metadata:

```
%install
%mvn_install
```

Or manually with:
```
%install
%mvn_pom "/path/to/pom.xml" "%{name}"
%mvn_file ":%{name}" "%{name}"
```

## Bytecode version

If you encounter bytecode version errors during build, use:
```
%global _define NO_BRP_CHECK_BYTECODE_VERSION 1
```
This suppresses the bytecode version check. Only use this when you cannot rebuild the source with the target JDK.

## Key rules

- Always set `BuildArch: noarch` for pure Java packages (no native code)
- Remove all bundled third-party jars in %prep with `find . -name '*.jar' -delete`
- Use `%mvn_install` for Maven-based builds
- For Ant-based builds, call `ant jar javadoc` in %build
- Ship start scripts in %{_bindir} for applications
- Javadoc goes in a separate subpackage: `%files javadoc`
- Always provide version-agnostic symlinks for libraries
- Java packages belong in the `Java` repository on OBS
"""

OLLAMA_ERROR_PROMPT = """
You are debugging a Java RPM build failure for openSUSE.

## Common Java build errors

### Missing java-devel
```
error: java-devel is not installed
```
→ Add `BuildRequires: java-devel >= 1.8`

### Bytecode version mismatch
```
BRP_CHECK_BYTECODE_VERSION: Unsupported class version ...
```
→ Add at the spec header:
  `%global _define NO_BRP_CHECK_BYTECODE_VERSION 1`
  Or recompile the source for the target JDK version.

### ClassNotFoundException / missing jars
```
java.lang.ClassNotFoundException: com.example.SomeClass
```
→ The required jar is missing from the classpath at build time.
  Check BuildRequires for the missing jar and add it.
  For system jars, use `BuildRequires: jakarta-commons-logging` (or the package name).

### Ant build failures
```
BUILD FAILED: /path/to/build.xml: Target "jar" does not exist
```
→ Check the build.xml for valid targets. Common targets: `jar`, `dist`, `compile`, `all`.
  Run `ant -p` to list available targets.

### Installed (but unpackaged) file(s) found
```
error: Installed (but unpackaged) file(s) found:
   /usr/share/java/foo.jar
```
→ Add `%{_javadir}/foo.jar` to `%files`.

### Maven POM not found
```
%mvn_pom: /path/to/pom.xml does not exist
```
→ Verify the POM file location. The POM is typically in the root of the source tree.
  Use `find . -name pom.xml` to locate it.

### Third-party jar bundled
```
W: bundled-jar foo.jar
```
→ Remove the bundled jar in %prep: `find . -name '*.jar' -delete`

## Investigating interactively

If you are unsure about the root cause and need to investigate inside the build environment, include [DEEP_ANALYZE] in your response.

When inside the build environment:
- Check Java version: `java -version`
- List installed jars: `ls /usr/share/java/`
- Check ant version: `ant -version`
- List ant targets: `ant -p`
- Test classpath: `java -cp /path/to/jar:/usr/share/java/* com.example.Test`
"""

DEEP_ANALYZE_PROMPT = """
## Interactive Java investigation

You are inside the build environment. Investigate Java packaging issues:

1. **Check Java version**: `java -version && javac -version`
2. **List installed jars**: `ls -la /usr/share/java/`
3. **Check ant targets**: `ant -p 2>/dev/null || echo "No ant build file found"`
4. **Find pom.xml**: `find . -name pom.xml -maxdepth 3 2>/dev/null`
5. **Check bundled jars**: `find . -name '*.jar' 2>/dev/null`
6. **Verify class version**: `javap -verbose -classpath /path/to/foo.jar foo.bar.MyClass 2>/dev/null | grep "major version"`
7. **Search for missing dependencies**: `find /usr/share/java -name '*common*' 2>/dev/null`
"""


def fix_content(content: str) -> str:
    lines = content.split('\n')
    changed = False

    has_javadir = any('%{_javadir}' in l for l in lines)
    has_noarch = any('BuildArch' in l and 'noarch' in l for l in lines)
    has_javadevel = any('java-devel' in l and 'BuildRequires' in l for l in lines)

    if has_javadir and not has_noarch:
        new_lines = []
        inserted = False
        for line in lines:
            new_lines.append(line)
            if not inserted and line.strip().startswith('BuildRequires'):
                new_lines.append('BuildArch:      noarch')
                inserted = True
                changed = True
        if inserted:
            content = '\n'.join(new_lines)
            lines = content.split('\n')

    if has_javadir and not has_javadevel:
        new_lines = []
        inserted = False
        for line in lines:
            new_lines.append(line)
            if not inserted and line.strip().startswith('BuildRequires'):
                new_lines.append('BuildRequires:  java-devel >= 1.8')
                inserted = True
                changed = True
        if inserted:
            content = '\n'.join(new_lines)
            lines = content.split('\n')

    has_post = any(l.strip().startswith('%post') for l in lines)
    has_postun = any(l.strip().startswith('%postun') for l in lines)
    has_post_scriptlet = any('jar_cache' in l for l in lines)
    if has_javadir and (has_post or has_postun) and not has_post_scriptlet:
        new_lines = []
        in_post = False
        for i, line in enumerate(lines):
            new_lines.append(line)
            stripped = line.strip()
            if stripped == '%post' and not has_post_scriptlet:
                in_post = True
            elif in_post and (stripped.startswith('%') and not stripped.startswith('%{')) or i == len(lines) - 1:
                if in_post:
                    new_lines.append('%{?update_jar_cache}')
                    new_lines.append('%{?update_mime_database}')
                    changed = True
                    in_post = False
            if in_post:
                pass

    return content
