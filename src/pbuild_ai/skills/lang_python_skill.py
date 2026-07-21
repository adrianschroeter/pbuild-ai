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
OLLAMA_SPEC_PROMPT = """
You are an expert in Python RPM packaging for openSUSE.
Check the following Spec-file. Pay special attention to:
1. Do NOT use  %py3_build, %py3_install, %pyproject_buildrequires, %pyproject_files, or %pyproject_save_files macros — avoid them entirely.
2. Use %pyproject_wheel and %pyproject_install instead when the source uses a pyproject.toml.
3. Generate new spec files by using "py2pack generate MODULE VERSION" command
4. Use the BuildRequires macros %{python_module MODLE_NAME}
5. Are there any obvious missing BuildRequires like for devel or for pip? or python-rpm-macros?
Summarize your analysis in a maximum of 3 sentences.
"""

# Specific instruction for build errors
OLLAMA_ERROR_PROMPT = """
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
