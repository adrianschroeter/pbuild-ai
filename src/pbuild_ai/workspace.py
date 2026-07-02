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

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from pbuild_ai.spinner import Spinner, YELLOW


def _extract_shell_command(text: str) -> str | None:
    """Strip markdown and natural language from Ollama output, return first shell command."""
    if not text:
        return None
    lines = text.split("\n")
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # Strip surrounding backticks
        line = line.strip("`").strip()
        if not line:
            continue
        # Strip markdown formatting
        line = re.sub(r'^\*{1,2}(.+?)\*{1,2}(?:\s|$)', r'\1 ', line, count=1)
        line = re.sub(r'^#+\s+', '', line)
        line = re.sub(r'^[-*+]\s+', '', line)
        line = line.strip()
        if not line:
            continue
        # Skip lines that are exclusively markdown separators
        if re.match(r'^[-*=_#]{3,}\s*$', line):
            continue
        # Skip natural language: starts with a capital letter followed by lowercase
        if re.match(r'^[A-Z]([a-z]|\s)', line):
            continue
        # Skip lines that end with English sentence punctuation (period after word,
        # not period as a shell argument like `find .`)
        if re.match(r'^[A-Za-z]', line) and re.search(r'\w[.:?]$', line.rstrip()):
            continue
        # Skip lines that are too long for a single shell command
        if len(line) > 500:
            continue
        # Validate it looks like a shell command
        if not re.match(r'^[a-z./$\'"#0-9%_]', line) and not re.match(r'^[A-Z_][A-Z_0-9]*=', line):
            continue
        return line
    return None


from pbuild_ai.diff_utils import show_diff


class RpmSourceManager:
    def __init__(self, base_dir, do_clean=False, vm_type=None, vm_memory=None, shell_after_build=False, preset=None, root_dir=None, build_log_path=None):
        self.base_dir = Path(base_dir).resolve()
        if not self.base_dir.is_dir():
            raise ValueError(f"Base directory {self.base_dir} does not exist.")
        self.root_dir = root_dir

        self.allowed_commands = {
            "fetch_sources": ["spectool", "-g", "-R", "{0}"],
            "lint": ["rpmlint", "{0}"]
        }
        self.deep_exploration = ""
        self.do_clean = do_clean
        self.vm_type = vm_type
        self.vm_memory = vm_memory
        self.shell_after_build = shell_after_build
        self.preset = preset
        self.pbuild_calls = 0
        self.pbuild_time = 0.0
        self.last_build_successful = True
        self.pbuild_attempt = 0
        self.build_log_path = Path(build_log_path).resolve() if build_log_path else None

    def _run_captured(self, cmd, cwd, stream_output=False):
        if stream_output:
            proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1)
            lines = []
            spinner = Spinner(prefix="[BUILD]", color=YELLOW)
            spinner.start()
            try:
                for raw in proc.stdout:
                    line = raw.decode('utf-8', errors='replace')
                    lines.append(line)
                    try:
                        print(line, end='', flush=True)
                    except BlockingIOError:
                        pass
            finally:
                spinner.stop()
            proc.wait()
            output = ''.join(lines)
            self._write_build_log(output)
            if proc.returncode != 0:
                raise subprocess.CalledProcessError(proc.returncode, cmd, output, '')
            return subprocess.CompletedProcess(cmd, 0, output, '')
        else:
            spinner = Spinner(prefix="[BUILD]", color=YELLOW)
            spinner.start()
            try:
                result = subprocess.run(cmd, cwd=cwd, capture_output=True)
            finally:
                spinner.stop()
            stdout = result.stdout.decode('utf-8', errors='replace')
            stderr = result.stderr.decode('utf-8', errors='replace')
            self._write_build_log(stdout + ('\nSTDERR:\n' + stderr if stderr else ''))
            if result.returncode != 0:
                raise subprocess.CalledProcessError(result.returncode, cmd, stdout, stderr)
            return subprocess.CompletedProcess(result.args, result.returncode, stdout, stderr)

    def _write_build_log(self, content):
        if not self.build_log_path:
            return
        try:
            path_str = str(self.build_log_path)
            if "_NUMBER_" in path_str:
                path_str = path_str.replace("_NUMBER_", str(self.pbuild_attempt))
            log_path = Path(path_str)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(content, encoding='utf-8')
            self._last_log_path = log_path
            print(f"[BUILD LOG] Wrote {len(content)} bytes to {log_path}")
        except Exception as e:
            print(f"[WARNING] Failed to write build log: {e}")

    def _is_safe_path(self, target_path) -> bool:
        try:
            return Path(target_path).resolve(strict=False).is_relative_to(self.base_dir)
        except ValueError:
            return False

    def find_spec_files(self):
        return [f for f in self.base_dir.rglob("*.spec") if self._is_safe_path(f)]

    def read_file_safe(self, file_path):
        if not self._is_safe_path(file_path):
            raise PermissionError("Access denied: Path is outside the sandbox.")
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read()

    def fix_file_content(self, file_path, fix_function):
        if not self._is_safe_path(file_path):
            raise PermissionError("Access denied: Path is outside the sandbox.")
        content = self.read_file_safe(file_path)
        new_content = fix_function(content)
        if content != new_content:
            show_diff(content, new_content, file_path, prefix="[SKILL]")
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(new_content)
            print(f"[OK] File {file_path.name} was updated by a skill.")

    def run_test_build(self, cmd_name, target_file, stream_output=False):
        if cmd_name not in self.allowed_commands:
            raise PermissionError(f"Command '{cmd_name}' is not allowed.")
        template = self.allowed_commands[cmd_name]
        cmd = [part.format(str(target_file)) for part in template]
        print(f"[EXEC] {' '.join(cmd)}")
        try:
            if stream_output:
                subprocess.run(cmd, cwd=self.base_dir, check=True)
                return True, ""
            else:
                result = subprocess.run(cmd, cwd=self.base_dir, check=True, capture_output=True)
                stdout = result.stdout.decode('utf-8', errors='replace')
                return True, "\n".join(stdout.strip().split('\n')[-50:])
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or b"").decode('utf-8', errors='replace').strip()
            stdout = (e.stdout or b"").decode('utf-8', errors='replace').strip()
            err = f"STDERR:\n{stderr}\n\nSTDOUT (last 100 lines):\n"
            err += "\n".join(stdout.split('\n')[-100:]) if stdout else "No output"
            return False, err

    def run_orphan_build(self, dist="tumbleweed", stream_output=False, force_clean=False):
        cmd = ["pbuild", "--orphan", "--release", "0"]
        if self.root_dir:
            cmd.extend(["--root", self.root_dir])
        if not (self.do_clean or force_clean):
            cmd.append("--no-clean")
        if dist:
            cmd.extend(["--dist", dist])
        if self.vm_type:
            cmd.extend(["--vm-type", self.vm_type])
        if self.vm_memory:
            cmd.extend(["--vm-memory", self.vm_memory])
        if self.shell_after_build:
            cmd.append("--shell-after-build")
        print(f"[EXEC] {' '.join(cmd)}")
        self.pbuild_attempt += 1
        t0 = time.time()
        try:
            result = self._run_captured(cmd, self.base_dir, stream_output=stream_output)
            elapsed = time.time() - t0
            self.pbuild_calls += 1
            self.pbuild_time += elapsed
            self.last_build_successful = True
            return True, "\n".join(result.stdout.strip().split('\n')[-50:])
        except subprocess.CalledProcessError as e:
            elapsed = time.time() - t0
            self.pbuild_calls += 1
            self.pbuild_time += elapsed
            self.last_build_successful = False
            stderr = (e.stderr or "").strip()
            stdout = (e.stdout or "").strip()
            err = f"STDERR:\n{stderr}\n\nSTDOUT (last 100 lines):\n"
            err += "\n".join(stdout.split('\n')[-100:]) if stdout else "No output"
            return False, err

    def check_build_result(self):
        cmd = ["pbuild", "--result"]
        print(f"[EXEC] {' '.join(cmd)}")
        try:
            result = subprocess.run(cmd, cwd=self.base_dir, check=True, capture_output=True)
            output = (result.stdout or b"").decode('utf-8', errors='replace').strip()
            return output, output
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or b"").decode('utf-8', errors='replace').strip()
            stdout = (e.stdout or b"").decode('utf-8', errors='replace').strip()
            err = f"STDERR:\n{stderr}\n\nSTDOUT (last 100 lines):\n"
            err += "\n".join(stdout.split('\n')[-100:]) if stdout else "No output"
            return "unknown", err

    def has_prior_failed_build(self):
        build_dirs = [d for d in self.base_dir.iterdir() if d.is_dir() and d.name.startswith("_build.")]
        if not build_dirs:
            return False
        result, _ = self.check_build_result()
        if "failed" in result.lower() or "unresolvable" in result.lower():
            return True
        return False

    def build_phase_reached(self, package_name=None):
        build_dirs = [d for d in self.base_dir.iterdir() if d.is_dir() and d.name.startswith("_build.")]
        if not build_dirs:
            return True
        build_dir = sorted(build_dirs, key=lambda d: d.stat().st_mtime, reverse=True)[0]

        def raw_log():
            if package_name:
                p = build_dir / package_name / "_log"
                if p.exists():
                    return p.read_text(encoding="utf-8", errors="replace")
            for name in ("_log", "build.log"):
                p = build_dir / name
                if p.exists():
                    return p.read_text(encoding="utf-8", errors="replace")
            for d in build_dir.iterdir():
                if d.is_dir():
                    for name in ("_log", "build.log"):
                        p = d / name
                        if p.exists():
                            return p.read_text(encoding="utf-8", errors="replace")
            return None

        content = raw_log()
        if content is None:
            return True
        return "----- building " in content

    def get_build_log(self, package_name=None):
        build_dirs = [d for d in self.base_dir.iterdir() if d.is_dir() and d.name.startswith("_build.")]
        if not build_dirs:
            return None, "No _build.* directory found."

        build_dir = sorted(build_dirs, key=lambda d: d.stat().st_mtime, reverse=True)[0]

        def read_log(path):
            return path.read_text(encoding="utf-8")

        def extract_error_context(text):
            lines = text.split("\n")
            for i, line in enumerate(lines):
                if re.search(r'\berror\s*:', line, re.I) or re.search(r'\bFAILED\b', line):
                    start = max(0, i - 10)
                    end = min(len(lines), i + 31)
                    excerpt = lines[start:end]
                    result = []
                    if start > 0:
                        result.append(f"... ({start} lines before first error)")
                    result.extend(f"{'>>>' if j == i else '   '} {l}" for j, l in enumerate(lines[start:end], start=start))
                    if end < len(lines):
                        result.append(f"... ({len(lines) - end} lines after)")
                    return "\n".join(result)
            return None

        if package_name:
            pkg_log = build_dir / package_name / "_log"
            if pkg_log.exists():
                content = read_log(pkg_log)
                excerpt = extract_error_context(content)
                if excerpt:
                    return True, excerpt
                return True, "... (no 'error:' found) " + "\n".join(content.split("\n")[-200:])

        log_file = build_dir / "build.log"
        if log_file.exists():
            content = read_log(log_file)
            excerpt = extract_error_context(content)
            if excerpt:
                return True, excerpt

        # Check for _log directly in build_dir
        log_file = build_dir / "_log"
        if log_file.exists():
            content = read_log(log_file)
            excerpt = extract_error_context(content)
            if excerpt:
                return True, excerpt
            return True, "... (no 'error:' found) " + "\n".join(content.split("\n")[-200:])

        candidates = sorted(
            (d / f) for d in build_dir.iterdir() if d.is_dir()
            for f in ("_log", "build.log")
            if (d / f).is_file()
        ) + sorted(build_dir.rglob("*.log"))

        if candidates:
            content = read_log(candidates[-1])
            excerpt = extract_error_context(content)
            if excerpt:
                return True, excerpt

        return None, f"No build log found in {build_dir}"

    def run_project_build(self, package_name, dist=None, preset=None, stream_output=False, force_clean=False):
        cmd = ["pbuild", "--single", package_name]
        if self.root_dir:
            cmd.extend(["--root", self.root_dir])
        if not (self.do_clean or force_clean):
            cmd.append("--no-clean")
        if preset:
            cmd.extend(["--preset", preset])
        elif dist:
            cmd.extend(["--dist", dist])
        if self.vm_type:
            cmd.extend(["--vm-type", self.vm_type])
        if self.vm_memory:
            cmd.extend(["--vm-memory", self.vm_memory])
        if self.shell_after_build:
            cmd.append("--shell-after-build")
        print(f"[EXEC] {' '.join(cmd)}")
        self.pbuild_attempt += 1
        t0 = time.time()
        try:
            result = self._run_captured(cmd, self.base_dir, stream_output=stream_output)
            elapsed = time.time() - t0
            self.pbuild_calls += 1
            self.pbuild_time += elapsed
            self.last_build_successful = True
            return True, "\n".join(result.stdout.strip().split('\n')[-50:])
        except subprocess.CalledProcessError as e:
            elapsed = time.time() - t0
            self.pbuild_calls += 1
            self.pbuild_time += elapsed
            self.last_build_successful = False
            stderr = (e.stderr or "").strip()
            stdout = (e.stdout or "").strip()
            err = f"STDERR:\n{stderr}\n\nSTDOUT (last 100 lines):\n"
            err += "\n".join(stdout.split('\n')[-100:]) if stdout else "No output"
            return False, err

    def run_deep_analyze_shell(self, package_name=None, ollama=None, full_context=None, project_mode=False, debug=False, deep_analyze_prompt=""):
        cmd = ["pbuild"]
        if self.root_dir:
            cmd.extend(["--root", self.root_dir])
        if package_name and project_mode:
            cmd.extend(["--single", package_name])
        else:
            cmd.append("--orphan")
        cmd.append("--shell-after-fail")
        if not self.do_clean:
            cmd.append("--no-clean")
        if self.preset and package_name:
            cmd.extend(["--preset", self.preset])
        if self.vm_type:
            cmd.extend(["--vm-type", self.vm_type])
        if self.vm_memory:
            cmd.extend(["--vm-memory", self.vm_memory])

        collected = ""
        self.deep_exploration = ""
        print(f"[DEEP] Ollama-driven investigation in build env for {package_name or 'package'}...")
        print(f"[EXEC] {' '.join(cmd)}")

        import pty
        import select

        master_fd, slave_fd = pty.openpty()
        t0 = time.time()
        proc = subprocess.Popen(cmd, cwd=self.base_dir,
                               stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                               close_fds=True)

        def pty_read(timeout=10):
            nonlocal collected
            end = time.time() + timeout
            buf = ""
            while time.time() < end:
                r, _, _ = select.select([master_fd], [], [], 0.5)
                if r:
                    try:
                        data = os.read(master_fd, 4096).decode('utf-8', errors='replace')
                        buf += data
                        collected += data
                        print(data, end='', flush=True)
                    except OSError:
                        break
                else:
                    if buf:
                        break
            return buf

        def pty_write(text):
            os.write(master_fd, (text + "\n").encode('utf-8'))

        try:
            time.sleep(3)
            pty_read(3)
            pty_write("cd ~/rpmbuild/BUILD/*-build/")
            time.sleep(1)
            pty_read(3)
            pty_write("pwd && ls -la")
            time.sleep(2)
            pty_read(5)

            # Auto-detect cd failure in %prep and inspect tarball
            if package_name:
                _, build_log_text = self.get_build_log(package_name)
                cd_failure = build_log_text and re.search(r'cd:\s*\S+\s*:\s*No such file or directory', build_log_text, re.I)
                prep_failure = build_log_text and '%prep' in build_log_text
                if cd_failure and prep_failure:
                    print(f"[DEEP] Detected cd failure in %prep. Inspecting tarball contents...")
                    pty_write("echo '--- SOURCES ---' && ls -la ~/rpmbuild/SOURCES/")
                    time.sleep(2)
                    pty_read(5)
                    pty_write("for f in ~/rpmbuild/SOURCES/*.tar.gz ~/rpmbuild/SOURCES/*.tar.bz2 ~/rpmbuild/SOURCES/*.tar.xz ~/rpmbuild/SOURCES/*.tar; do echo \"=== $f ===\"; tar -tf \"$f\" 2>/dev/null | head -20; done")
                    time.sleep(3)
                    pty_read(8)
                    pty_write("echo '--- TOP-LEVEL DIRS ---' && for f in ~/rpmbuild/SOURCES/*.tar.gz ~/rpmbuild/SOURCES/*.tar.bz2 ~/rpmbuild/SOURCES/*.tar.xz ~/rpmbuild/SOURCES/*.tar; do echo \"=== $f ===\"; tar -tf \"$f\" 2>/dev/null | sed 's|/.*||' | sort -u; done")
                    time.sleep(3)
                    pty_read(8)
                    print(f"[DEEP] Tarball inspection complete.")

            max_rounds = 48
            for round_i in range(max_rounds):
                inquiry_prompt = f"""{deep_analyze_prompt + chr(10) if deep_analyze_prompt else ''}You have an interactive shell inside the failed RPM build environment at ~/rpmbuild/BUILD/. The build of {package_name} failed.

Here is everything gathered so far from the shell:
{collected[-15000:]}

Suggest ONE shell command to run next to investigate the failure.

RULES:
- Output a SINGLE raw shell command only.
- NO markdown, NO backticks, NO explanation, NO analysis.
- NO **bold**, NO --- separators, NO headers.
- Just the command itself, nothing else."""
                print(f"\n[DEEP] Asking Ollama for command (round {round_i+1}/{max_rounds})...")
                if debug:
                    print(f"[DEEP PROMPT]\n{inquiry_prompt}\n[/DEEP PROMPT]")
                raw = ollama.analyze("You are investigating a failed RPM build interactively.", inquiry_prompt, full_context).strip()
                command = _extract_shell_command(raw)
                if command is None:
                    print(f"[DEEP] No valid command found in response, skipping round.")
                    continue
                if command.lower().startswith("exit") or not command:
                    print(f"[DEEP] Ollama finished investigation.")
                    break
                print(f"[DEEP] Running: {command}")
                pty_write(command)
                time.sleep(2)
                output = pty_read(8)
                if not output:
                    time.sleep(3)
                    output = pty_read(5)

                continue_prompt = f"""The command was: {command}

Output:
{output[-5000:] if output else '(no output)'}

Do you have enough information to diagnose and fix the {package_name} build failure? Reply with exactly one word: YES or NO."""
                if debug:
                    print(f"[DEEP PROMPT]\n{continue_prompt}\n[/DEEP PROMPT]")
                answer = ollama.analyze("You are investigating a failed RPM build.", continue_prompt, full_context).strip().upper()
                model_name = ollama.model if ollama else "unknown"
                print(f"[DEEP] AI({model_name}) says: {answer}")
                if answer.startswith("Y"):
                    print(f"[DEEP] Ollama has enough information. Proceeding to fix.")
                    break

            final_prompt = f"""The interactive investigation is complete. Here is everything collected from the build environment:

{collected[-20000:]}

Summarize the root cause of the {package_name} build failure and what fix is needed. Be specific."""
            if debug:
                print(f"\n[DEEP PROMPT]\n{final_prompt}\n[/DEEP PROMPT]")
            summary = ollama.analyze("You summarize build failure investigations.", final_prompt, full_context)
            print(f"\n[DEEP] Final diagnosis:\n{summary}\n")

        except Exception as e:
            print(f"[DEEP ERROR] {e}")
        finally:
            elapsed = time.time() - t0
            self.pbuild_calls += 1
            self.pbuild_time += elapsed
            self.deep_exploration = collected
            try:
                os.write(master_fd, b"exit\n")
                time.sleep(1)
            except:
                pass
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except:
                proc.kill()
            proc.wait()
            try:
                os.close(master_fd)
            except:
                pass
            try:
                os.close(slave_fd)
            except:
                pass

        return True, "\n".join(collected.strip().split('\n')[-100:])

    def run_full_project_build(self, stream_output=False, force_clean=False):
        cmd = ["pbuild", "--abort-on-fail"]
        if self.root_dir:
            cmd.extend(["--root", self.root_dir])
        if not (self.do_clean or force_clean):
            cmd.append("--no-clean")
        if self.preset:
            cmd.extend(["--preset", self.preset])
        if self.vm_type:
            cmd.extend(["--vm-type", self.vm_type])
        if self.vm_memory:
            cmd.extend(["--vm-memory", self.vm_memory])
        if self.shell_after_build:
            cmd.append("--shell-after-build")
        print(f"[EXEC] {' '.join(cmd)}")
        try:
            result = self._run_captured(cmd, self.base_dir, stream_output=stream_output)
            return True, "\n".join(result.stdout.strip().split('\n')[-100:])
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or "").strip()
            stdout = (e.stdout or "").strip()
            err = f"STDERR:\n{stderr}\n\nSTDOUT (last 100 lines):\n"
            err += "\n".join(stdout.split('\n')[-100:]) if stdout else "No output"
            return False, err

    def find_agents_md(self):
        current = self.base_dir
        while True:
            agents_file = current / "AGENTS.md"
            if agents_file.is_file():
                return agents_file
            parent = current.parent
            if parent == current:
                break
            current = parent
        return None

    def read_agents_md(self):
        agents_file = self.find_agents_md()
        if agents_file:
            return agents_file.read_text(encoding="utf-8")
        return None
