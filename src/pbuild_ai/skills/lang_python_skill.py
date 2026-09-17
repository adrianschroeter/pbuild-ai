# lang_python_skill.py

SKILL_NAME = "lang_python"

VERSION_API = {
    "url": "https://pypi.org/pypi/{name}/json",
    "version_key": ["info", "version"],
    "name_regex": r"^python[-_\d]*",
}

# REGEX: This skill triggers for all files starting with "python-" and ending with ".spec".
TARGET_PATTERN = r"^python-.*\.spec$"
# Also trigger for any spec file with python_module BuildRequires or Python build macros
CONTENT_PATTERN = r"(BuildRequires:\s*%\{python_module\b|%py3_build\b|%python_build\b|%pyproject_wheel\b)"
# Trigger on build log errors about Python version mismatch
PROMPT_PATTERN = r"(?i)(requires a different Python|BackendUnavailable|Cannot import 'setuptools.build_meta')"

# Specific instruction to the LLM for Python packages before the build
AI_SPEC_PROMPT = """
You are an expert in Python RPM packaging for openSUSE.
Check the following Spec-file. Pay special attention to:
1. Do NOT use  %py3_build, %py3_install, %pyproject_buildrequires, %pyproject_files, or %pyproject_save_files macros — avoid them entirely.
2. Use %pyproject_wheel and %pyproject_install instead when the source uses a pyproject.toml.
3. Generate new spec files by using "py2pack generate MODULE VERSION" command
4. Write every Python BuildRequires as `BuildRequires: %{python_module MODULE_NAME}`. The %{python_module } macro is provided by python-rpm-macros, a default build-time requirement in openSUSE builds, and expands to the correct interpreter-specific package. Never write a literal python3x-... package name and never remove the %{python_module } wrapper.
5. Are there any obvious missing BuildRequires like for devel or for pip? or python-rpm-macros?
Summarize your analysis in a maximum of 3 sentences.
"""

# Specific instruction for build errors
AI_ERROR_PROMPT = """
You are a Python developer. The RPM build for this Python package failed.
Check the log for typical errors such as:
- Missing Python modules (ModuleNotFoundError)
- Errors in setup.py or pyproject.toml
- You may need to convert to %pyproject_wheel and %pyproject_install macros when the project switched to pyproject.toml.
Explain the cause and suggest the missing RPM package name for BuildRequires.

Include the exact BuildRequires line in your analysis, for example:
BuildRequires: %{python_module MODULENAME}

The build system will automatically pick up any line starting with
BuildRequires: from your analysis and insert it into the spec file.
Do NOT use run_tool_script — the parser handles this through your
analysis text alone.

When recommending another python3xx-MODULE package first, build instead python-MODULE source.

If you are unsure about the root cause and need to investigate interactively inside the build environment, include [DEEP_ANALYZE] in your response.

### 5. Python version mismatch
If the build log says "Package 'NAME' requires a different Python: X.Y.Z not in ...",
the package's Python version constraint excludes the system Python version.
Skip the unsupported version by adding at the top of the spec (below the
copyright header):

    %define skip_pythonXYZ 1

Where XYZ is the major.minor version without dots (e.g., for Python 3.14.4 use
%define skip_python314 1). For Python 2, use %define skip_python2 1.
Multiple skip lines can be added for different versions.

A missing python macro like %python_subpackages point to a missing
 
   BuildRequires: python-rpm-macros

### 6. pip cannot import build backend
If the build log shows:
    Cannot import 'setuptools.build_meta'
    BackendUnavailable
    pip._vendor.pyproject_hooks._impl.BackendUnavailable

This means the Python build backend (setuptools) is not installed in the build environment.
Add:
    BuildRequires: %{python_module setuptools}

If the backend is hatchling, flit, or poetry-core, the package name differs:
  - hatchling → BuildRequires: %{python_module hatchling}
  - flit_core  → BuildRequires: %{python_module flit-core}
  - poetry_core → BuildRequires: %{python_module poetry-core}

### 7. Unresolvable Python BuildRequires ("nothing provides pythonXX-MODULE")

If the build log shows `nothing provides python3XX-NAME` (an unresolvable
BuildRequires), the %{python_module } macro has correctly expanded to the
per-flavor package — the macro itself works. The MODULE NAME is wrong: it is
misspelled or does not exist. Fix it with a minimal rename that keeps the
macro, for example:

    BuildRequires: %{python_module pipper}  ->  BuildRequires: %{python_module pip}

Do NOT delete the BuildRequires line, do NOT replace %{python_module NAME}
with a literal python3x-NAME package name, and do NOT remove a whole
dependency block because one entry is wrong — the neighbouring
%{python_module ...} deps may resolve fine.

Only unwrap or remove a macro when the MACRO ITSELF is unresolvable, which is
detectable by the literal unexpanded macro name appearing in the error.
"""

def fix_content(content: str) -> str:
    """
    This function is executed to patch the Spec-file before the build.
    """
    # Example fix: Ensure outdated Python macros are replaced (highly simplified)
    if "%{__python3} setup.py install" in content:
        content = content.replace(
            "%{__python3} setup.py install", 
            "# WARNING: Outdated setup found. Replaced with a more modern variant (please check manually)\n%py3_install"
        )
    
    return content
