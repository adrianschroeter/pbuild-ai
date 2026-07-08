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
import signal
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
        line = re.sub(r'^\d+\.\s+', '', line)
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
        from pbuild_ai.pbuild_ai import _cleanup_stale_build_processes
        if cmd and cmd[0] == "pbuild":
            _cleanup_stale_build_processes()
        if stream_output:
            proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', bufsize=1)
            lines = []
            for line in proc.stdout:
                lines.append(line)
                try:
                    print(line, end='', flush=True)
                except BlockingIOError:
                    pass
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

    def run_orphan_build(self, dist=None, preset=None, stream_output=False, force_clean=False):
        cmd = ["pbuild", "--orphan", "--release", "0"]
        if self.root_dir:
            cmd.extend(["--root", self.root_dir])
        if not (self.do_clean or force_clean):
            cmd.append("--no-clean")
        if preset:
            cmd.extend(["--preset", preset])
        elif dist:
            cmd.extend(["--dist", dist])
        else:
            cmd.extend(["--dist", "tumbleweed"])
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
        from pbuild_ai.pbuild_ai import _cleanup_stale_build_processes
        _cleanup_stale_build_processes()
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
        elif package_name:
            cmd.extend(["--dist", "tumbleweed"])
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

        _SHELL_PROMPT_RE = re.compile(r'[#\$>]\s*(\x1b\[[0-9;]*m)?\s*$')

        master_fd, slave_fd = pty.openpty()
        t0 = time.time()
        proc = subprocess.Popen(cmd, cwd=self.base_dir,
                               stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                               close_fds=True, start_new_session=True)
        os.close(slave_fd)  # Close parent's copy so PTY master sees EOF on child exit

        def pty_read(timeout=10, wait_prompt=False):
            nonlocal collected
            end = time.time() + timeout
            buf = ""
            while time.time() < end:
                r, _, _ = select.select([master_fd], [], [], 0.5)
                if r:
                    try:
                        data = os.read(master_fd, 4096).decode('utf-8', errors='replace')
                        if not data:
                            break
                        buf += data
                        collected += data
                        print(data, end='', flush=True)
                        if wait_prompt and _SHELL_PROMPT_RE.search(buf):
                            time.sleep(0.2)
                            r2, _, _ = select.select([master_fd], [], [], 0.2)
                            if not r2:
                                break
                            else:
                                try:
                                    extra = os.read(master_fd, 4096).decode('utf-8', errors='replace')
                                    buf += extra
                                    collected += extra
                                    print(extra, end='', flush=True)
                                except OSError:
                                    break
                                break
                    except OSError:
                        break
            return buf

        def pty_write(text):
            os.write(master_fd, (text + "\n").encode('utf-8'))

        def _wait_for_shell(timeout=600):
            """Read PTY output until a shell prompt appears, process exit, or timeout."""
            nonlocal collected
            print(f"[DEEP] Waiting for build to fail and shell to open (timeout {timeout}s)...")
            end = time.time() + timeout
            while time.time() < end:
                # Check if pbuild exited before select timeout
                if proc.poll() is not None:
                    collected_all = collected
                    _lower = collected_all.lower()
                    if "unresolvable" in _lower or "nothing provides" in _lower:
                        print(f"\n[DEEP] pbuild exited early (code {proc.returncode}) — unresolvable dependencies detected.")
                        for _line in collected_all.split("\n"):
                            if "unresolvable" in _line.lower() or "nothing provides" in _line.lower():
                                print(f"  [DEP] {_line.strip()}")
                        return "UNRESOLVABLE"
                    elif "no buildstatus set" in _lower or "failed to get" in _lower:
                        print(f"\n[DEEP] pbuild exited early (code {proc.returncode}) — build environment setup failure.")
                        print(f"[DEEP] VM/KVM build root stale — retry with --clean instead of --no-clean.")
                        return "ENV_FAILURE"
                    else:
                        print(f"\n[DEEP] pbuild exited early (code {proc.returncode}) — build setup failed before shell could open.")
                    print(f"[DEEP] Bailing out of shell wait. Use --fix to resolve build setup issues.")
                    return False
                r, _, _ = select.select([master_fd], [], [], 1.0)
                if r:
                    try:
                        data = os.read(master_fd, 4096).decode('utf-8', errors='replace')
                        if not data:
                            break
                        collected += data
                        print(data, end='', flush=True)
                        if _SHELL_PROMPT_RE.search(collected[-200:]):
                            time.sleep(0.3)
                            r2, _, _ = select.select([master_fd], [], [], 0.3)
                            if not r2:
                                print(f"\n[DEEP] Shell ready. Starting investigation.")
                                return True
                    except OSError:
                        break
            if proc.poll() is not None:
                print(f"\n[DEEP] pbuild exited (code {proc.returncode}) during shell wait. Bailing out.")
                return False
            print(f"\n[DEEP] Timed out waiting for shell prompt. Proceeding anyway.")
            return False

        def _send_and_wait(command, timeout=15):
            """Send a command to the PTY and wait for its output (prompt-based)."""
            print(f"[DEEP] Running: {command}")
            pty_write(command)
            return pty_read(timeout, wait_prompt=True)

        try:
            _shell_ready = _wait_for_shell()
            if _shell_ready == "ENV_FAILURE":
                print("[DEEP] Build environment cannot be created (stale build root). Skipping investigation.")
                return False, "BUILD_ENV_SETUP_FAILURE"
            if _shell_ready == "UNRESOLVABLE":
                print("[DEEP] Unresolvable dependencies — build environment cannot be created. Skipping investigation.")
                return True, "\n".join(collected.strip().split('\n')[-100:])
            if not _shell_ready:
                _collected_lower = collected.lower()
                if "unresolvable" in _collected_lower or "nothing provides" in _collected_lower:
                    print("[DEEP] Unresolvable dependencies — build environment cannot be created. Skipping investigation.")
                    return True, "\n".join(collected.strip().split('\n')[-100:])
                if "no buildstatus set" in _collected_lower or "failed to get" in _collected_lower:
                    print("[DEEP] Build environment setup failure detected. Skipping investigation.")
                    return False, "BUILD_ENV_SETUP_FAILURE"
            _send_and_wait("cd ~/rpmbuild/BUILD/*-build/ 2>/dev/null || cd ~/rpmbuild/BUILD/*/ 2>/dev/null || true")
            _send_and_wait("pwd && ls -la")

            # Auto-detect cd failure in %prep and inspect tarball
            if package_name:
                _, build_log_text = self.get_build_log(package_name)
                cd_failure = build_log_text and re.search(r'cd:\s*\S+\s*:\s*No such file or directory', build_log_text, re.I)
                prep_failure = build_log_text and '%prep' in build_log_text
                if cd_failure and prep_failure:
                    print(f"[DEEP] Detected cd failure in %prep. Inspecting tarball contents...")
                    _send_and_wait("echo '--- SOURCES ---' && ls -la ~/rpmbuild/SOURCES/")
                    _send_and_wait("for f in ~/rpmbuild/SOURCES/*.tar.gz ~/rpmbuild/SOURCES/*.tar.bz2 ~/rpmbuild/SOURCES/*.tar.xz ~/rpmbuild/SOURCES/*.tar; do echo \"=== $f ===\"; tar -tf \"$f\" 2>/dev/null | head -20; done")
                    _send_and_wait("echo '--- TOP-LEVEL DIRS ---' && for f in ~/rpmbuild/SOURCES/*.tar.gz ~/rpmbuild/SOURCES/*.tar.bz2 ~/rpmbuild/SOURCES/*.tar.xz ~/rpmbuild/SOURCES/*.tar; do echo \"=== $f ===\"; tar -tf \"$f\" 2>/dev/null | sed 's|/.*||' | sort -u; done")
                    print(f"[DEEP] Tarball inspection complete.")

            max_rounds = 48
            _last_command = None
            _last_output = None
            _diagnosis = None
            for round_i in range(max_rounds):
                if round_i == 0:
                    _cmd_section = "No commands have been run yet. Suggest the first command to investigate the failure."
                else:
                    _cmd_section = f"""Last command run: {_last_command}

Output:
{_last_output[-5000:] if _last_output else '(no output)'}

Everything gathered so far from the shell:
{collected[-15000:]}"""
                combined_prompt = f"""{deep_analyze_prompt + chr(10) if deep_analyze_prompt else ''}You have an interactive shell inside the failed RPM build environment at ~/rpmbuild/BUILD/. The build of {package_name} failed.

{_cmd_section}

Do you have enough information to diagnose and fix the {package_name} build failure?
- If YES: Start your response with "DONE:" then explain the root cause and the specific fix needed.
- If NO: Start your response with "NEXT:" then output a SINGLE raw shell command to run next. NO markdown, NO backticks, NO explanation — just the command after "NEXT:"."""
                print(f"\n[DEEP] Asking Ollama (round {round_i+1}/{max_rounds})...")
                if debug:
                    print(f"[DEEP PROMPT]\n{combined_prompt}\n[/DEEP PROMPT]")
                raw = ollama.analyze("You are investigating a failed RPM build interactively.", combined_prompt, full_context).strip()
                if not raw:
                    print(f"[DEEP] Empty response, skipping round.")
                    continue
                _upper = raw.upper()
                if _upper.startswith("DONE:"):
                    _diagnosis = raw[5:].strip()
                    model_name = ollama.model if ollama else "unknown"
                    print(f"[DEEP] AI({model_name}) has enough information. Proceeding to fix.")
                    break
                if _upper.startswith("NEXT:"):
                    raw = raw[5:].strip()
                command = _extract_shell_command(raw)
                if command is None:
                    print(f"[DEEP] No valid command found in response, skipping round.")
                    continue
                if command.lower().startswith("exit") or not command:
                    print(f"[DEEP] Ollama finished investigation.")
                    break
                output = _send_and_wait(command, timeout=15)
                if not output or not output.strip():
                    output = pty_read(10, wait_prompt=True)
                _last_command = command
                _last_output = output

            if _diagnosis:
                print(f"\n[DEEP] Final diagnosis:\n{_diagnosis}\n")
            else:
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
            # Kill entire process group (pbuild + all children like qemu)
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except:
                os.killpg(pgid, signal.SIGKILL)
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

    def run_full_project_build(self, stream_output=False, force_clean=False, dist=None):
        cmd = ["pbuild", "--abort-on-fail"]
        if self.root_dir:
            cmd.extend(["--root", self.root_dir])
        if not (self.do_clean or force_clean):
            cmd.append("--no-clean")
        if self.preset:
            cmd.extend(["--preset", self.preset])
        elif dist:
            cmd.extend(["--dist", dist])
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
