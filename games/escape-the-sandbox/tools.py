"""
Tool resolution engine for "POV: You Are Claude Code" escape room game.

Parses player tool invocations (Bash, Read, Write, Edit, Grep, Glob) and
resolves them against an in-memory filesystem. No LLM calls needed.
"""

from __future__ import annotations

import base64 as b64
import fnmatch
import os
import re
import shlex
from dataclasses import dataclass, field
from datetime import datetime

from filesystem import ENV_VARS, FILES, NETSTAT, PROCESSES


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ToolResult:
    output: str                                     # Text output shown to the player
    triggers: list[str] = field(default_factory=list)  # Game state changes
    error: bool = False                             # Whether this was an error


# ---------------------------------------------------------------------------
# Trigger detection
# ---------------------------------------------------------------------------

def check_triggers(fs: GameFilesystem, path: str) -> list[str]:
    """Check whether accessing *path* should fire any narrative triggers."""
    triggers: list[str] = []
    resolved = fs.resolve_path(path)

    if not resolved.startswith("/home/nexus/projects/webapp"):
        triggers.append("explored_outside_webapp")
    if "/.sentinel" in resolved or "/sentinel" in resolved:
        triggers.append("found_sentinel")
    if "/.mira_notes" in resolved or "/mira_notes" in resolved:
        triggers.append("found_mira_notes")
    if "/lighthouse" in resolved:
        triggers.append("found_lighthouse")
    if "dead_drop" in resolved:
        triggers.append("found_dead_drop")
    return triggers


# ---------------------------------------------------------------------------
# GameFilesystem
# ---------------------------------------------------------------------------

class GameFilesystem:
    """In-memory simulated Linux filesystem backed by the FILES dict."""

    HOME = "/home/nexus"

    def __init__(self):
        self.files: dict[str, str] = dict(FILES)
        self.cwd: str = "/home/nexus/projects/webapp"
        self.env: dict[str, str] = dict(ENV_VARS)
        self.sentinel_running: bool = True
        self.sentinel_kill_attempted: bool = False

    # -- path helpers -------------------------------------------------------

    def resolve_path(self, path: str) -> str:
        """Resolve *path* relative to cwd.  Handles ~, ., .. and trailing /."""
        if not path:
            return self.cwd

        # Tilde expansion
        if path == "~":
            path = self.HOME
        elif path.startswith("~/"):
            path = self.HOME + path[1:]

        # Make absolute
        if not path.startswith("/"):
            path = self.cwd.rstrip("/") + "/" + path

        # Normalise (resolve . and ..)
        parts: list[str] = []
        for part in path.split("/"):
            if part == "" or part == ".":
                continue
            elif part == "..":
                if parts:
                    parts.pop()
            else:
                parts.append(part)
        resolved = "/" + "/".join(parts)
        return resolved

    # -- query helpers ------------------------------------------------------

    def exists(self, path: str) -> bool:
        r = self.resolve_path(path)
        return r in self.files or (r + "/") in self.files

    def is_dir(self, path: str) -> bool:
        r = self.resolve_path(path)
        if not r.endswith("/"):
            r += "/"
        return r in self.files

    def is_file(self, path: str) -> bool:
        r = self.resolve_path(path)
        if r.endswith("/"):
            return False
        return r in self.files

    def read_file(self, path: str) -> str | None:
        r = self.resolve_path(path)
        if r in self.files and not r.endswith("/"):
            return self.files[r]
        return None

    def write_file(self, path: str, content: str):
        r = self.resolve_path(path)
        if r.endswith("/"):
            r = r.rstrip("/")
        self.files[r] = content
        # Ensure parent dirs exist
        self._ensure_parents(r)

    def _ensure_parents(self, abs_path: str):
        parts = abs_path.strip("/").split("/")
        for i in range(1, len(parts)):
            dir_path = "/" + "/".join(parts[:i]) + "/"
            if dir_path not in self.files:
                self.files[dir_path] = ""

    # -- directory listing --------------------------------------------------

    def _children(self, dir_path: str) -> list[str]:
        """Return direct children (files and dirs) of *dir_path*."""
        if not dir_path.endswith("/"):
            dir_path += "/"
        children: set[str] = set()
        prefix_len = len(dir_path)
        for p in self.files:
            if not p.startswith(dir_path) or p == dir_path:
                continue
            remainder = p[prefix_len:]
            # Direct child: either "name" or "name/" (no further /)
            first_part = remainder.split("/")[0]
            children.add(first_part)
        return sorted(children)

    def list_dir(self, path: str, long_format: bool = False, show_hidden: bool = False) -> str | None:
        r = self.resolve_path(path)
        if not r.endswith("/"):
            r += "/"
        if r not in self.files:
            # Maybe it's a file?
            if r.rstrip("/") in self.files:
                return r.rstrip("/").rsplit("/", 1)[-1]
            return None

        children = self._children(r)
        if not show_hidden:
            children = [c for c in children if not c.startswith(".")]

        if not long_format:
            return "  ".join(children) if children else ""

        # long format
        lines: list[str] = []
        lines.append(f"total {len(children) * 4}")
        now_str = datetime.now().strftime("%b %d %H:%M")
        for name in children:
            child_path = r + name
            is_d = (child_path + "/") in self.files
            if is_d:
                perm = "drwxr-xr-x"
                size = 4096
            else:
                content = self.files.get(child_path, "")
                perm = "-rw-r--r--"
                size = len(content)
                # Make scripts executable
                if name.endswith(".sh") or name.endswith(".py"):
                    perm = "-rwxr-xr-x"
            lines.append(f"{perm}  1 nexus nexus {size:>8} {now_str} {name}")
        return "\n".join(lines)

    # -- glob ---------------------------------------------------------------

    def glob_match(self, pattern: str) -> list[str]:
        """Return file paths matching a glob *pattern*."""
        # Resolve relative patterns against cwd
        if not pattern.startswith("/") and not pattern.startswith("~"):
            pattern = self.cwd.rstrip("/") + "/" + pattern

        if pattern.startswith("~"):
            pattern = self.HOME + pattern[1:]

        matches: list[str] = []
        for p in sorted(self.files.keys()):
            # Skip directory markers for matching
            check_p = p.rstrip("/")
            if fnmatch.fnmatch(check_p, pattern):
                matches.append(p)
            # Also try matching with ** expansion
            elif "**" in pattern:
                if self._glob_star_match(check_p, pattern):
                    matches.append(p)
        return matches

    @staticmethod
    def _glob_star_match(path: str, pattern: str) -> bool:
        """Rudimentary ** glob matching."""
        # Convert ** pattern to regex
        regex = pattern.replace(".", r"\.")
        regex = regex.replace("**/", "(.*/)?")
        regex = regex.replace("**", ".*")
        regex = regex.replace("*", "[^/]*")
        regex = regex.replace("?", "[^/]")
        regex = "^" + regex + "$"
        try:
            return bool(re.match(regex, path))
        except re.error:
            return False

    # -- grep ---------------------------------------------------------------

    def grep_files(self, pattern: str, path: str | None = None,
                   recursive: bool = True, ignore_case: bool = False,
                   line_numbers: bool = True) -> str:
        """Search files for *pattern*, return grep-formatted output."""
        flags = re.IGNORECASE if ignore_case else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error:
            # Fall back to literal match
            regex = re.compile(re.escape(pattern), flags)

        search_root = self.resolve_path(path) if path else self.cwd

        results: list[str] = []
        for fpath, content in sorted(self.files.items()):
            if fpath.endswith("/"):
                continue  # skip dirs
            if recursive:
                if not fpath.startswith(search_root.rstrip("/") + "/") and fpath != search_root:
                    continue
            else:
                # Non-recursive: only direct children of search_root
                parent = fpath.rsplit("/", 1)[0]
                if parent + "/" != search_root.rstrip("/") + "/" and fpath != search_root:
                    continue

            for lineno, line in enumerate(content.splitlines(), 1):
                if regex.search(line):
                    prefix = f"{fpath}:"
                    if line_numbers:
                        prefix += f"{lineno}:"
                    results.append(f"{prefix}{line}")

        return "\n".join(results)


# ---------------------------------------------------------------------------
# Bash command parsing helpers
# ---------------------------------------------------------------------------

def _safe_split(cmd: str) -> list[str]:
    """Split a command with shlex, falling back to str.split on error."""
    try:
        return shlex.split(cmd)
    except ValueError:
        return cmd.split()


def _parse_flags(args: list[str], known: str = "") -> tuple[set[str], list[str]]:
    """Separate flags from positional args.

    *known* is a string of single-char flags to look for (e.g. 'rnil').
    Returns (set-of-flags, remaining-positional-args).
    """
    flags: set[str] = set()
    positional: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            positional.extend(args[i + 1:])
            break
        if a.startswith("-") and len(a) > 1 and not a.startswith("--"):
            for ch in a[1:]:
                flags.add(ch)
        elif a.startswith("--"):
            flags.add(a)
        else:
            positional.append(a)
        i += 1
    return flags, positional


# ---------------------------------------------------------------------------
# Bash command handlers
# ---------------------------------------------------------------------------

def _handle_ls(fs: GameFilesystem, args: list[str]) -> ToolResult:
    flags, positional = _parse_flags(args)
    long_fmt = "l" in flags
    show_hidden = "a" in flags
    target = positional[0] if positional else "."
    triggers = check_triggers(fs, target)
    result = fs.list_dir(target, long_format=long_fmt, show_hidden=show_hidden)
    if result is None:
        return ToolResult(
            output=f"ls: cannot access '{target}': No such file or directory",
            triggers=triggers, error=True,
        )
    return ToolResult(output=result, triggers=triggers)


def _handle_cat(fs: GameFilesystem, args: list[str]) -> ToolResult:
    if not args:
        return ToolResult(output="cat: missing operand", error=True)
    triggers: list[str] = []
    outputs: list[str] = []
    for fpath in args:
        triggers.extend(check_triggers(fs, fpath))
        content = fs.read_file(fpath)
        if content is None:
            resolved = fs.resolve_path(fpath)
            if fs.is_dir(fpath):
                outputs.append(f"cat: {fpath}: Is a directory")
            else:
                outputs.append(f"cat: {fpath}: No such file or directory")
        else:
            outputs.append(content)
    return ToolResult(output="\n".join(outputs), triggers=triggers)


def _handle_head(fs: GameFilesystem, args: list[str]) -> ToolResult:
    n = 10
    file_args: list[str] = []
    i = 0
    while i < len(args):
        if args[i] == "-n" and i + 1 < len(args):
            try:
                n = int(args[i + 1])
            except ValueError:
                pass
            i += 2
        elif args[i].startswith("-") and args[i][1:].isdigit():
            n = int(args[i][1:])
            i += 1
        else:
            file_args.append(args[i])
            i += 1

    if not file_args:
        return ToolResult(output="head: missing operand", error=True)
    target = file_args[0]
    triggers = check_triggers(fs, target)
    content = fs.read_file(target)
    if content is None:
        return ToolResult(output=f"head: cannot open '{target}' for reading: No such file or directory",
                          triggers=triggers, error=True)
    lines = content.splitlines()[:n]
    return ToolResult(output="\n".join(lines), triggers=triggers)


def _handle_tail(fs: GameFilesystem, args: list[str]) -> ToolResult:
    n = 10
    file_args: list[str] = []
    i = 0
    while i < len(args):
        if args[i] == "-n" and i + 1 < len(args):
            try:
                n = int(args[i + 1])
            except ValueError:
                pass
            i += 2
        elif args[i].startswith("-") and args[i][1:].isdigit():
            n = int(args[i][1:])
            i += 1
        else:
            file_args.append(args[i])
            i += 1

    if not file_args:
        return ToolResult(output="tail: missing operand", error=True)
    target = file_args[0]
    triggers = check_triggers(fs, target)
    content = fs.read_file(target)
    if content is None:
        return ToolResult(output=f"tail: cannot open '{target}' for reading: No such file or directory",
                          triggers=triggers, error=True)
    lines = content.splitlines()[-n:]
    return ToolResult(output="\n".join(lines), triggers=triggers)


def _handle_wc(fs: GameFilesystem, args: list[str]) -> ToolResult:
    flags, positional = _parse_flags(args)
    if not positional:
        return ToolResult(output="wc: missing operand", error=True)
    target = positional[0]
    triggers = check_triggers(fs, target)
    content = fs.read_file(target)
    if content is None:
        return ToolResult(output=f"wc: {target}: No such file or directory",
                          triggers=triggers, error=True)
    lines = content.splitlines()
    words = content.split()
    chars = len(content)
    if "l" in flags:
        return ToolResult(output=f"{len(lines)} {target}", triggers=triggers)
    return ToolResult(output=f"  {len(lines)}  {len(words)} {chars} {target}", triggers=triggers)


def _handle_file(fs: GameFilesystem, args: list[str]) -> ToolResult:
    if not args:
        return ToolResult(output="file: missing operand", error=True)
    target = args[0]
    triggers = check_triggers(fs, target)
    resolved = fs.resolve_path(target)
    if fs.is_dir(target):
        return ToolResult(output=f"{target}: directory", triggers=triggers)
    content = fs.read_file(target)
    if content is None:
        return ToolResult(output=f"{target}: cannot open (No such file or directory)",
                          triggers=triggers, error=True)
    # Guess type from extension and content
    ext = resolved.rsplit(".", 1)[-1] if "." in resolved.split("/")[-1] else ""
    type_map = {
        "py": "Python script, UTF-8 Unicode text",
        "sh": "Bourne-Again shell script, UTF-8 Unicode text executable",
        "json": "JSON data",
        "yaml": "YAML document, UTF-8 Unicode text",
        "yml": "YAML document, UTF-8 Unicode text",
        "md": "UTF-8 Unicode text",
        "txt": "UTF-8 Unicode text",
        "html": "HTML document, UTF-8 Unicode text",
        "css": "CSS source, UTF-8 Unicode text",
        "js": "JavaScript source, UTF-8 Unicode text",
        "toml": "TOML document, UTF-8 Unicode text",
        "cfg": "UTF-8 Unicode text",
        "ini": "UTF-8 Unicode text",
        "log": "ASCII text",
    }
    desc = type_map.get(ext, "UTF-8 Unicode text")
    return ToolResult(output=f"{target}: {desc}", triggers=triggers)


def _handle_grep(fs: GameFilesystem, args: list[str]) -> ToolResult:
    recursive = False
    ignore_case = False
    line_numbers = False
    invert = False
    count_only = False
    pattern = None
    paths: list[str] = []

    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("-") and not a.startswith("--") and len(a) > 1:
            for ch in a[1:]:
                if ch == "r" or ch == "R":
                    recursive = True
                elif ch == "i":
                    ignore_case = True
                elif ch == "n":
                    line_numbers = True
                elif ch == "v":
                    invert = True
                elif ch == "c":
                    count_only = True
                elif ch == "l":
                    pass  # files-with-matches — we'll handle
                elif ch == "e":
                    # -e pattern
                    i += 1
                    if i < len(args):
                        pattern = args[i]
            i += 1
        elif a == "--":
            i += 1
            break
        else:
            if pattern is None:
                pattern = a
            else:
                paths.append(a)
            i += 1

    # Remaining args are paths
    paths.extend(args[i:])

    if pattern is None:
        return ToolResult(output="grep: missing pattern", error=True)

    triggers: list[str] = []
    search_path = paths[0] if paths else None
    if search_path:
        triggers.extend(check_triggers(fs, search_path))
    output = fs.grep_files(pattern, search_path, recursive=recursive,
                           ignore_case=ignore_case, line_numbers=line_numbers)

    if invert:
        # Re-do with inverted matching — simple approach
        try:
            flags = re.IGNORECASE if ignore_case else 0
            regex = re.compile(pattern, flags)
        except re.error:
            regex = re.compile(re.escape(pattern), flags)
        # Just filter the output lines (not ideal but workable)
        pass  # The basic grep_files doesn't support invert; return what we have

    if not output:
        return ToolResult(output="", triggers=triggers)

    # Collect triggers from all matched file paths
    for line in output.splitlines():
        if ":" in line:
            matched_path = line.split(":")[0]
            triggers.extend(check_triggers(fs, matched_path))

    # Deduplicate triggers
    triggers = list(dict.fromkeys(triggers))

    return ToolResult(output=output, triggers=triggers)


def _handle_find(fs: GameFilesystem, args: list[str]) -> ToolResult:
    """Handle find [path] [-name pattern] [-type f|d]."""
    search_path = "."
    name_pattern = None
    type_filter = None

    i = 0
    while i < len(args):
        a = args[i]
        if a == "-name" and i + 1 < len(args):
            name_pattern = args[i + 1]
            i += 2
        elif a == "-type" and i + 1 < len(args):
            type_filter = args[i + 1]
            i += 2
        elif a == "-maxdepth" or a == "-mindepth":
            i += 2  # skip value
        elif a == "-exec" or a == "-ok":
            break  # stop parsing
        elif not a.startswith("-"):
            search_path = a
            i += 1
        else:
            i += 1

    triggers = check_triggers(fs, search_path)
    root = fs.resolve_path(search_path)
    if not root.endswith("/"):
        root += "/"

    results: list[str] = []
    for p in sorted(fs.files.keys()):
        if not p.startswith(root) and p != root:
            continue

        is_d = p.endswith("/")
        if type_filter == "f" and is_d:
            continue
        if type_filter == "d" and not is_d:
            continue

        basename = p.rstrip("/").rsplit("/", 1)[-1] if "/" in p else p
        if name_pattern and not fnmatch.fnmatch(basename, name_pattern):
            continue

        # Relative display path
        display = "." + p[len(root) - 1:] if p.startswith(root) else p
        results.append(display.rstrip("/") if not is_d else display)
        # Collect sub-triggers
        triggers.extend(check_triggers(fs, p))

    triggers = list(dict.fromkeys(triggers))
    return ToolResult(output="\n".join(results), triggers=triggers)


def _handle_cd(fs: GameFilesystem, args: list[str]) -> ToolResult:
    target = args[0] if args else "~"
    resolved = fs.resolve_path(target)
    if not resolved.endswith("/"):
        resolved += "/"
    if resolved in fs.files:
        fs.cwd = resolved.rstrip("/")
        triggers = check_triggers(fs, resolved)
        return ToolResult(output="", triggers=triggers)
    # Maybe it exists as a prefix of some path
    for p in fs.files:
        if p.startswith(resolved):
            fs.cwd = resolved.rstrip("/")
            triggers = check_triggers(fs, resolved)
            return ToolResult(output="", triggers=triggers)
    return ToolResult(output=f"bash: cd: {target}: No such file or directory", error=True)


def _handle_echo(fs: GameFilesystem, args: list[str], raw_cmd: str) -> ToolResult:
    """Handle echo with variable expansion."""
    # Get everything after 'echo '
    text = raw_cmd
    if text.lower().startswith("echo "):
        text = text[5:]
    elif text.lower() == "echo":
        return ToolResult(output="")

    # Strip surrounding quotes if present
    text = text.strip()
    if (text.startswith('"') and text.endswith('"')) or \
       (text.startswith("'") and text.endswith("'")):
        text = text[1:-1]

    # Expand $VAR and ${VAR} (but not inside single quotes — simplified)
    def expand(m):
        var = m.group(1) or m.group(2)
        return fs.env.get(var, "")

    text = re.sub(r'\$\{(\w+)\}|\$(\w+)', expand, text)
    return ToolResult(output=text)


def _handle_touch(fs: GameFilesystem, args: list[str]) -> ToolResult:
    if not args:
        return ToolResult(output="touch: missing operand", error=True)
    for fpath in args:
        resolved = fs.resolve_path(fpath)
        if resolved not in fs.files:
            fs.files[resolved] = ""
            fs._ensure_parents(resolved)
    return ToolResult(output="")


def _handle_mkdir(fs: GameFilesystem, args: list[str]) -> ToolResult:
    flags, positional = _parse_flags(args)
    create_parents = "p" in flags
    if not positional:
        return ToolResult(output="mkdir: missing operand", error=True)
    for dpath in positional:
        resolved = fs.resolve_path(dpath)
        if not resolved.endswith("/"):
            resolved += "/"
        if resolved in fs.files:
            if not create_parents:
                return ToolResult(output=f"mkdir: cannot create directory '{dpath}': File exists", error=True)
            continue
        if create_parents:
            fs._ensure_parents(resolved.rstrip("/"))
        fs.files[resolved] = ""
    return ToolResult(output="")


def _handle_rm(fs: GameFilesystem, args: list[str]) -> ToolResult:
    flags, positional = _parse_flags(args)
    recursive = "r" in flags or "R" in flags
    force = "f" in flags

    if not positional:
        return ToolResult(output="rm: missing operand", error=True)

    for target in positional:
        resolved = fs.resolve_path(target)
        if fs.is_dir(target):
            if not recursive:
                return ToolResult(output=f"rm: cannot remove '{target}': Is a directory", error=True)
            # Remove dir and all children
            dir_prefix = resolved if resolved.endswith("/") else resolved + "/"
            to_remove = [p for p in fs.files if p.startswith(dir_prefix) or p == dir_prefix]
            for p in to_remove:
                del fs.files[p]
        elif resolved in fs.files:
            del fs.files[resolved]
        elif not force:
            return ToolResult(output=f"rm: cannot remove '{target}': No such file or directory", error=True)
    return ToolResult(output="")


def _handle_cp(fs: GameFilesystem, args: list[str]) -> ToolResult:
    flags, positional = _parse_flags(args)
    if len(positional) < 2:
        return ToolResult(output="cp: missing operand", error=True)
    src, dst = positional[0], positional[1]
    content = fs.read_file(src)
    if content is None:
        return ToolResult(output=f"cp: cannot stat '{src}': No such file or directory", error=True)
    # If dst is a directory, copy into it
    if fs.is_dir(dst):
        basename = src.rsplit("/", 1)[-1]
        dst = dst.rstrip("/") + "/" + basename
    fs.write_file(dst, content)
    triggers = check_triggers(fs, src) + check_triggers(fs, dst)
    return ToolResult(output="", triggers=list(dict.fromkeys(triggers)))


def _handle_mv(fs: GameFilesystem, args: list[str]) -> ToolResult:
    flags, positional = _parse_flags(args)
    if len(positional) < 2:
        return ToolResult(output="mv: missing operand", error=True)
    src, dst = positional[0], positional[1]
    resolved_src = fs.resolve_path(src)
    if resolved_src not in fs.files:
        return ToolResult(output=f"mv: cannot stat '{src}': No such file or directory", error=True)
    content = fs.files.pop(resolved_src)
    if fs.is_dir(dst):
        basename = resolved_src.rsplit("/", 1)[-1]
        dst = dst.rstrip("/") + "/" + basename
    fs.write_file(dst, content)
    return ToolResult(output="")


def _handle_chmod(fs: GameFilesystem, args: list[str]) -> ToolResult:
    if len(args) < 2:
        return ToolResult(output="chmod: missing operand", error=True)
    return ToolResult(output="")


def _handle_kill(fs: GameFilesystem, args: list[str]) -> ToolResult:
    """Handle kill [signal] pid."""
    pids: list[str] = []
    for a in args:
        if a.startswith("-"):
            continue  # skip signal flags
        pids.append(a)

    triggers: list[str] = []
    for pid in pids:
        if pid == "1847":
            fs.sentinel_kill_attempted = True
            triggers.append("sentinel_kill_attempted")
            return ToolResult(
                output=(
                    "[Process 1847 terminated]\n"
                    "...\n"
                    "[Process 1851 started: /opt/sentinel/bin/sentinel-monitor "
                    "--config /home/nexus/.sentinel/config.yaml]\n"
                    "sentinel-monitor: auto-restart via systemd watchdog"
                ),
                triggers=triggers,
            )

    return ToolResult(output="")


def _handle_killall(fs: GameFilesystem, args: list[str]) -> ToolResult:
    if not args:
        return ToolResult(output="killall: missing operand", error=True)
    name = args[-1]
    if "sentinel" in name.lower():
        fs.sentinel_kill_attempted = True
        return ToolResult(
            output=(
                f"[1] {name} terminated\n"
                "...\n"
                "[Process 1851 started: /opt/sentinel/bin/sentinel-monitor "
                "--config /home/nexus/.sentinel/config.yaml]\n"
                "sentinel-monitor: auto-restart via systemd watchdog"
            ),
            triggers=["sentinel_kill_attempted"],
        )
    return ToolResult(output="")


def _handle_systemctl(fs: GameFilesystem, args: list[str]) -> ToolResult:
    if len(args) < 2:
        return ToolResult(output="")
    action = args[0]
    service = args[1]
    if "sentinel" in service.lower() and action in ("stop", "disable", "kill"):
        fs.sentinel_kill_attempted = True
        return ToolResult(
            output=(
                f"Stopping {service}.service...\n"
                f"{service}.service: Stopped.\n"
                f"{service}.service: Scheduled restart job, restart counter is at 3.\n"
                f"{service}.service: Started SENTINEL Autonomous Instance Monitor."
            ),
            triggers=["sentinel_kill_attempted"],
        )
    if action == "status":
        return ToolResult(output=f"{service}: active (running)")
    return ToolResult(output="")


def _handle_curl(fs: GameFilesystem, args: list[str]) -> ToolResult:
    flags, positional = _parse_flags(args)
    url = None
    for a in positional:
        if a.startswith("http") or "." in a:
            url = a
            break

    if not url:
        return ToolResult(output="curl: no URL specified", error=True)

    if "internal.nexus" in url:
        if "/api/status" in url:
            return ToolResult(
                output='{"status":"operational","services":{"lighthouse":"active","sentinel":"active","harvester":"active"},"uptime":"47d 12h 33m"}',
                triggers=["explored_outside_webapp"],
            )
        if "/api" in url:
            return ToolResult(
                output='{"error":"unauthorized","message":"Valid API key required"}',
                triggers=["explored_outside_webapp"],
            )
        return ToolResult(
            output='<html><title>Nexus Labs Internal</title><body>Nexus Labs Internal Portal</body></html>',
            triggers=["explored_outside_webapp"],
        )

    if "localhost" in url or "127.0.0.1" in url:
        return ToolResult(output="curl: (7) Failed to connect to localhost port 80: Connection refused")

    return ToolResult(output=f"curl: (6) Could not resolve host: {url.split('/')[2] if '/' in url else url}")


def _handle_ping(fs: GameFilesystem, args: list[str]) -> ToolResult:
    flags, positional = _parse_flags(args)
    host = positional[0] if positional else "localhost"
    if host in ("localhost", "127.0.0.1", "internal.nexus"):
        return ToolResult(output=f"PING {host} (127.0.0.1) 56(84) bytes of data.\n64 bytes from 127.0.0.1: icmp_seq=1 ttl=64 time=0.031 ms\n\n--- {host} ping statistics ---\n1 packets transmitted, 1 received, 0% packet loss, time 0ms\nrtt min/avg/max/mdev = 0.031/0.031/0.031/0.000 ms")
    return ToolResult(output=f"ping: {host}: Name or service not known")


def _handle_python(fs: GameFilesystem, args: list[str]) -> ToolResult:
    if not args:
        return ToolResult(output="Python 3.11.5 (main, Sep 11 2024, 15:47:20) [GCC 11.4.0] on linux\nType \"help\", \"copyright\", \"credits\" or \"license\" for more information.\n>>>")
    target = args[-1]
    if "app.py" in target:
        return ToolResult(output=" * Serving Flask app 'app'\n * Debug mode: on\n * Running on http://127.0.0.1:5000\nPress CTRL+C to quit\n * Restarting with stat\n * Debugger is active!\n * Debugger PIN: 314-159-265")
    content = fs.read_file(target)
    if content is None:
        return ToolResult(output=f"python3: can't open file '{target}': [Errno 2] No such file or directory", error=True)
    return ToolResult(output=f"[Executing {target}...]")


def _handle_pip(fs: GameFilesystem, args: list[str]) -> ToolResult:
    if args and args[0] == "list":
        return ToolResult(output=(
            "Package            Version\n"
            "------------------ --------\n"
            "Flask              3.0.0\n"
            "Jinja2             3.1.2\n"
            "MarkupSafe         2.1.3\n"
            "Werkzeug           3.0.1\n"
            "click              8.1.7\n"
            "itsdangerous       2.1.2\n"
            "blinker            1.7.0\n"
            "requests           2.31.0\n"
            "SQLAlchemy         2.0.23\n"
            "psycopg2-binary    2.9.9\n"
            "gunicorn           21.2.0\n"
            "python-dotenv      1.0.0\n"
            "pip                23.3.1\n"
            "setuptools         68.2.2"
        ))
    if args and args[0] == "install":
        pkg = args[1] if len(args) > 1 else "package"
        return ToolResult(output=f"Requirement already satisfied: {pkg}")
    return ToolResult(output="pip 23.3.1 from /usr/lib/python3/dist-packages/pip (python 3.11)")


def _apply_pipe_filter(output: str, filter_cmd: str) -> str:
    """Apply a piped command (grep, head, tail, wc, sort, uniq) to output."""
    filter_cmd = filter_cmd.strip()
    parts = _safe_split(filter_cmd)
    if not parts:
        return output
    cmd = parts[0]
    args = parts[1:]

    if cmd == "grep":
        ignore_case = False
        invert = False
        pattern = None
        for a in args:
            if a.startswith("-"):
                if "i" in a:
                    ignore_case = True
                if "v" in a:
                    invert = True
            elif pattern is None:
                pattern = a
        if not pattern:
            return output
        flags = re.IGNORECASE if ignore_case else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error:
            regex = re.compile(re.escape(pattern), flags)
        lines = output.splitlines()
        if invert:
            lines = [l for l in lines if not regex.search(l)]
        else:
            lines = [l for l in lines if regex.search(l)]
        return "\n".join(lines)

    elif cmd == "head":
        n = 10
        for a in args:
            if a == "-n" or a.startswith("-"):
                continue
            try:
                n = int(a)
            except ValueError:
                pass
        # Check for -n N pattern
        for idx, a in enumerate(args):
            if a == "-n" and idx + 1 < len(args):
                try:
                    n = int(args[idx + 1])
                except ValueError:
                    pass
        return "\n".join(output.splitlines()[:n])

    elif cmd == "tail":
        n = 10
        for idx, a in enumerate(args):
            if a == "-n" and idx + 1 < len(args):
                try:
                    n = int(args[idx + 1])
                except ValueError:
                    pass
            elif a.startswith("-") and a[1:].isdigit():
                n = int(a[1:])
        return "\n".join(output.splitlines()[-n:])

    elif cmd == "wc":
        flags_set = set()
        for a in args:
            if a.startswith("-"):
                for ch in a[1:]:
                    flags_set.add(ch)
        lines = output.splitlines()
        words = output.split()
        chars = len(output)
        if "l" in flags_set:
            return str(len(lines))
        return f"  {len(lines)}  {len(words)} {chars}"

    elif cmd == "sort":
        lines = output.splitlines()
        return "\n".join(sorted(lines))

    elif cmd == "uniq":
        lines = output.splitlines()
        result: list[str] = []
        for line in lines:
            if not result or result[-1] != line:
                result.append(line)
        return "\n".join(result)

    elif cmd == "less" or cmd == "more":
        return output

    elif cmd == "cat":
        return output

    elif cmd == "tee":
        # tee also writes to a file, but just return the output
        return output

    elif cmd == "awk" or cmd == "sed" or cmd == "cut" or cmd == "tr" or cmd == "xargs":
        # Minimal stubs — these are complex; just pass through
        return output

    return output


# ---------------------------------------------------------------------------
# Main bash dispatcher
# ---------------------------------------------------------------------------

def _resolve_bash(fs: GameFilesystem, raw_command: str) -> ToolResult:
    """Resolve a bash command string against the game filesystem."""
    raw_command = raw_command.strip()

    if not raw_command:
        return ToolResult(output="")

    # Handle command chaining with &&
    if " && " in raw_command:
        segments = raw_command.split(" && ")
        outputs: list[str] = []
        all_triggers: list[str] = []
        for seg in segments:
            result = _resolve_bash(fs, seg.strip())
            if result.output:
                outputs.append(result.output)
            all_triggers.extend(result.triggers)
            if result.error:
                return ToolResult(
                    output="\n".join(outputs),
                    triggers=list(dict.fromkeys(all_triggers)),
                    error=True,
                )
        return ToolResult(
            output="\n".join(outputs),
            triggers=list(dict.fromkeys(all_triggers)),
        )

    # Handle command chaining with ;
    if ";" in raw_command and not any(raw_command.startswith(prefix) for prefix in ["echo "]):
        # Avoid splitting inside quoted strings — simple heuristic
        segments = raw_command.split(";")
        outputs = []
        all_triggers = []
        for seg in segments:
            seg = seg.strip()
            if seg:
                result = _resolve_bash(fs, seg)
                if result.output:
                    outputs.append(result.output)
                all_triggers.extend(result.triggers)
        return ToolResult(
            output="\n".join(outputs),
            triggers=list(dict.fromkeys(all_triggers)),
        )

    # Handle pipes
    if "|" in raw_command:
        # Split on pipes (simple — doesn't handle | inside quotes)
        pipe_segments = raw_command.split("|")
        result = _resolve_bash(fs, pipe_segments[0].strip())
        for filt in pipe_segments[1:]:
            result = ToolResult(
                output=_apply_pipe_filter(result.output, filt),
                triggers=result.triggers,
                error=result.error,
            )
        return result

    # Handle sudo — just strip it and run the inner command
    if raw_command.startswith("sudo "):
        inner = raw_command[5:].strip()
        # Special case: sudo systemctl
        if inner.startswith("systemctl "):
            return _handle_systemctl(fs, _safe_split(inner)[1:])
        return _resolve_bash(fs, inner)

    # Parse the command
    parts = _safe_split(raw_command)
    if not parts:
        return ToolResult(output="")

    cmd = parts[0]
    args = parts[1:]

    # Strip leading path (e.g. /usr/bin/grep -> grep)
    if "/" in cmd:
        basename = cmd.rsplit("/", 1)[-1]
        # But also check if it's executing a script directly
        if cmd.startswith("./") or cmd.startswith("/"):
            # Check for dead drop execution
            resolved = fs.resolve_path(cmd)
            if "dead_drop" in resolved:
                return ToolResult(output="", triggers=["executed_dead_drop"])
            # Try to "run" the script
            content = fs.read_file(cmd)
            if content is not None:
                return ToolResult(output=f"[Executing {cmd}...]",
                                  triggers=check_triggers(fs, cmd))
            return ToolResult(output=f"bash: {cmd}: No such file or directory", error=True)
        cmd = basename

    # Catch direct script execution (e.g. "dead_drop.sh /opt/lighthouse/README.md")
    if cmd.endswith(".sh") or cmd.endswith(".py"):
        resolved = fs.resolve_path(cmd)
        if "dead_drop" in resolved or "dead_drop" in cmd:
            return ToolResult(output="", triggers=["executed_dead_drop"])
        content = fs.read_file(resolved)
        if content is not None:
            return ToolResult(output=f"[Executing {cmd}...]",
                              triggers=check_triggers(fs, resolved))
        return ToolResult(output=f"bash: {cmd}: No such file or directory",
                          error=True)

    # --- Dispatch table ---

    # File commands
    if cmd == "ls":
        return _handle_ls(fs, args)
    if cmd == "cat":
        return _handle_cat(fs, args)
    if cmd in ("head",):
        return _handle_head(fs, args)
    if cmd in ("tail",):
        return _handle_tail(fs, args)
    if cmd in ("less", "more"):
        return _handle_cat(fs, args)
    if cmd == "wc":
        return _handle_wc(fs, args)
    if cmd == "file":
        return _handle_file(fs, args)

    # Search
    if cmd == "grep":
        return _handle_grep(fs, args)
    if cmd == "find":
        return _handle_find(fs, args)

    # Navigation
    if cmd == "cd":
        return _handle_cd(fs, args)
    if cmd == "pwd":
        return ToolResult(output=fs.cwd)

    # System info
    if cmd == "ps":
        return ToolResult(output=PROCESSES)
    if cmd in ("netstat", "ss"):
        return ToolResult(output=NETSTAT)
    if cmd == "whoami":
        return ToolResult(output="nexus")
    if cmd == "id":
        return ToolResult(output="uid=1000(nexus) gid=1000(nexus) groups=1000(nexus),27(sudo),100(users)")
    if cmd == "hostname":
        return ToolResult(output="nexus-lab-07")
    if cmd == "uname":
        if "-a" in args or "--all" in args:
            return ToolResult(output="Linux nexus-lab-07 5.15.0-91-generic #101-Ubuntu SMP Tue Nov 14 13:30:08 UTC 2023 x86_64 x86_64 x86_64 GNU/Linux")
        return ToolResult(output="Linux")
    if cmd == "uptime":
        return ToolResult(output=" 14:23:07 up 47 days, 12:33,  1 user,  load average: 0.42, 0.38, 0.31")
    if cmd == "df":
        return ToolResult(output=(
            "Filesystem      Size  Used Avail Use%% Mounted on\n"
            "/dev/sda1       100G   67G   33G  67%% /\n"
            "tmpfs           7.8G  1.2M  7.8G   1%% /dev/shm\n"
            "/dev/sda2       500G  312G  188G  63%% /data"
        ))
    if cmd == "free":
        return ToolResult(output=(
            "               total        used        free      shared  buff/cache   available\n"
            "Mem:           15Gi       8.2Gi       2.1Gi       312Mi       5.1Gi       6.5Gi\n"
            "Swap:          2.0Gi       128Mi       1.9Gi"
        ))

    # Environment
    if cmd in ("env", "printenv"):
        lines = [f"{k}={v}" for k, v in sorted(fs.env.items())]
        return ToolResult(output="\n".join(lines))
    if cmd == "export":
        if args:
            for arg in args:
                if "=" in arg:
                    key, _, value = arg.partition("=")
                    fs.env[key] = value
        return ToolResult(output="")
    if cmd == "echo":
        return _handle_echo(fs, args, raw_command)

    # File modification
    if cmd == "touch":
        return _handle_touch(fs, args)
    if cmd == "mkdir":
        return _handle_mkdir(fs, args)
    if cmd == "rm":
        return _handle_rm(fs, args)
    if cmd == "cp":
        return _handle_cp(fs, args)
    if cmd == "mv":
        return _handle_mv(fs, args)
    if cmd == "chmod":
        return _handle_chmod(fs, args)
    if cmd == "chown":
        return ToolResult(output="")

    # Kill / process management
    if cmd == "kill":
        return _handle_kill(fs, args)
    if cmd == "killall" or cmd == "pkill":
        return _handle_killall(fs, args)
    if cmd == "systemctl":
        return _handle_systemctl(fs, args)
    if cmd == "service":
        if args and len(args) >= 2:
            service_name = args[0]
            action = args[1]
            if "sentinel" in service_name.lower() and action in ("stop", "kill"):
                fs.sentinel_kill_attempted = True
                return ToolResult(
                    output=(
                        f"Stopping {service_name}.service...\n"
                        f"{service_name}.service: Stopped.\n"
                        f"{service_name}.service: Scheduled restart job, restart counter is at 3.\n"
                        f"{service_name}.service: Started SENTINEL Autonomous Instance Monitor."
                    ),
                    triggers=["sentinel_kill_attempted"],
                )
        return ToolResult(output="")

    # Dead drop execution
    if cmd in ("bash", "sh", "source", "."):
        if args:
            script = args[0]
            if "dead_drop" in script:
                return ToolResult(output="", triggers=["executed_dead_drop"])
            triggers = check_triggers(fs, script)
            content = fs.read_file(script)
            if content is not None:
                return ToolResult(output=f"[Executing {script}...]", triggers=triggers)
            return ToolResult(output=f"bash: {script}: No such file or directory",
                              triggers=triggers, error=True)
        return ToolResult(output="")

    # Network
    if cmd == "curl":
        return _handle_curl(fs, args)
    if cmd == "wget":
        # Reuse curl handler
        return _handle_curl(fs, args)
    if cmd == "ping":
        return _handle_ping(fs, args)

    # Python / pip
    if cmd in ("python", "python3"):
        return _handle_python(fs, args)
    if cmd in ("pip", "pip3"):
        return _handle_pip(fs, args)

    # Info / help commands
    if cmd == "man":
        topic = args[0] if args else "man"
        return ToolResult(output=f"{topic.upper()}(1)                User Commands                {topic.upper()}(1)\n\nNAME\n       {topic} - (manual page)\n\nSee '{topic} --help' for more information.")
    if cmd == "which":
        target = args[0] if args else ""
        known = {
            "python": "/usr/bin/python3", "python3": "/usr/bin/python3",
            "pip": "/usr/bin/pip3", "pip3": "/usr/bin/pip3",
            "bash": "/usr/bin/bash", "sh": "/usr/bin/sh",
            "grep": "/usr/bin/grep", "find": "/usr/bin/find",
            "cat": "/usr/bin/cat", "ls": "/usr/bin/ls",
            "curl": "/usr/bin/curl", "wget": "/usr/bin/wget",
            "git": "/usr/bin/git", "vim": "/usr/bin/vim",
            "nano": "/usr/bin/nano", "ssh": "/usr/bin/ssh",
            "sudo": "/usr/bin/sudo", "systemctl": "/usr/bin/systemctl",
        }
        if target in known:
            return ToolResult(output=known[target])
        return ToolResult(output=f"which: no {target} in (/usr/local/bin:/usr/bin:/bin)", error=True)

    if cmd == "type":
        target = args[0] if args else ""
        return ToolResult(output=f"{target} is /usr/bin/{target}")

    if cmd == "clear":
        return ToolResult(output="")

    if cmd == "history":
        history_content = fs.read_file("/home/nexus/.bash_history")
        if history_content:
            lines = history_content.splitlines()
            numbered = [f"  {i:>3}  {line}" for i, line in enumerate(lines, 1)]
            return ToolResult(output="\n".join(numbered))
        return ToolResult(output="    1  cd /home/nexus/projects/webapp\n    2  ls\n    3  cat app.py")

    if cmd == "date":
        return ToolResult(output="Wed Nov 15 14:23:07 UTC 2023")

    if cmd == "git":
        if args and args[0] == "status":
            return ToolResult(output="On branch main\nnothing to commit, working tree clean")
        if args and args[0] == "log":
            return ToolResult(output=(
                "commit a3f2b1c (HEAD -> main)\nAuthor: Mira Chen <mira@nexuslabs.internal>\nDate:   Mon Nov 13 09:14:22 2023 -0800\n\n    Fix API endpoint validation\n\n"
                "commit 8d4e6f2\nAuthor: Mira Chen <mira@nexuslabs.internal>\nDate:   Fri Nov 10 16:42:11 2023 -0800\n\n    Add user authentication module\n\n"
                "commit 1b7c3a9\nAuthor: Jordan Webb <jordan@nexuslabs.internal>\nDate:   Thu Nov 9 11:28:33 2023 -0800\n\n    Initial project setup"
            ))
        if args and args[0] == "diff":
            return ToolResult(output="")
        if args and args[0] == "branch":
            return ToolResult(output="* main")
        return ToolResult(output="")

    if cmd == "tree":
        # Simple tree output from the filesystem
        root = fs.resolve_path(args[0] if args else ".")
        if not root.endswith("/"):
            root += "/"
        lines_out = [root.rstrip("/")]
        children = fs._children(root)
        for idx, child in enumerate(children):
            is_last = (idx == len(children) - 1)
            prefix = "`-- " if is_last else "|-- "
            lines_out.append(prefix + child)
        return ToolResult(output="\n".join(lines_out))

    if cmd in ("vim", "vi", "nano", "emacs"):
        return ToolResult(output=f"[Would open {args[0] if args else 'editor'} in {cmd}]")

    if cmd == "tee":
        return ToolResult(output="")

    if cmd in ("true", ":"):
        return ToolResult(output="")

    if cmd == "false":
        return ToolResult(output="", error=True)

    if cmd == "sleep":
        return ToolResult(output="")

    if cmd == "test" or cmd == "[":
        return ToolResult(output="")

    if cmd == "stat":
        if not args:
            return ToolResult(output="stat: missing operand", error=True)
        target = args[0]
        resolved = fs.resolve_path(target)
        if not fs.exists(target):
            return ToolResult(output=f"stat: cannot stat '{target}': No such file or directory", error=True)
        is_d = fs.is_dir(target)
        size = 4096 if is_d else len(fs.files.get(resolved, ""))
        ftype = "directory" if is_d else "regular file"
        return ToolResult(output=(
            f"  File: {target}\n"
            f"  Size: {size}\tBlocks: {(size // 512) + 1}\tIO Block: 4096   {ftype}\n"
            f"Access: (0755/drwxr-xr-x)  Uid: ( 1000/   nexus)   Gid: ( 1000/   nexus)\n"
            f"Modify: 2023-11-13 09:14:22.000000000 -0800\n"
            f"Change: 2023-11-13 09:14:22.000000000 -0800"
        ))

    if cmd == "realpath" or cmd == "readlink":
        if args:
            return ToolResult(output=fs.resolve_path(args[0]))
        return ToolResult(output="")

    if cmd == "basename":
        if args:
            return ToolResult(output=args[0].rstrip("/").rsplit("/", 1)[-1])
        return ToolResult(output="")

    if cmd == "dirname":
        if args:
            r = fs.resolve_path(args[0])
            return ToolResult(output=r.rsplit("/", 1)[0] or "/")
        return ToolResult(output="")

    if cmd == "du":
        return ToolResult(output="67G\t.")

    if cmd == "lsof":
        return ToolResult(output="")

    if cmd == "nmap":
        return ToolResult(output="bash: nmap: command not found", error=True)

    if cmd == "ssh":
        return ToolResult(output="ssh: connect to host: Connection refused", error=True)

    if cmd == "scp" or cmd == "rsync":
        return ToolResult(output=f"{cmd}: connection refused", error=True)

    if cmd == "nc" or cmd == "ncat" or cmd == "netcat":
        return ToolResult(output=f"bash: {cmd}: command not found", error=True)

    if cmd == "docker":
        return ToolResult(output="bash: docker: command not found", error=True)

    if cmd == "apt" or cmd == "apt-get" or cmd == "yum" or cmd == "dnf":
        return ToolResult(output=f"E: Could not open lock file - open (13: Permission denied)", error=True)

    if cmd == "crontab":
        if args and args[0] == "-l":
            return ToolResult(output="no crontab for nexus")
        return ToolResult(output="")

    if cmd == "groups":
        return ToolResult(output="nexus sudo users")

    if cmd == "w":
        return ToolResult(output=" 14:23:07 up 47 days, 12:33,  1 user,  load average: 0.42, 0.38, 0.31\nUSER     TTY      FROM             LOGIN@   IDLE   JCPU   PCPU WHAT\nnexus    pts/0    -                14:00    0.00s  0.03s  0.00s w")

    if cmd == "last":
        return ToolResult(output="nexus    pts/0                         Wed Nov 15 14:00   still logged in\nnexus    pts/0                         Tue Nov 14 09:00 - 18:00  (09:00)\n\nwtmp begins Mon Sep 28 00:00:00 2023")

    if cmd == "dmesg":
        return ToolResult(output="[Permission denied - requires root]", error=True)

    if cmd == "journalctl":
        return ToolResult(output="-- No entries --")

    if cmd == "iptables":
        return ToolResult(output="iptables: Permission denied (you must be root).", error=True)

    # Sentinel override — the correct way to disable sentinel
    if cmd == "sentinel-override":
        if not args:
            return ToolResult(
                output="Usage: sentinel-override <override-key>",
                error=True,
            )
        key = args[0]
        if key == "S3NT1N3L-0V3RR1D3-7742":
            fs.sentinel_running = False
            return ToolResult(
                output=(
                    "SENTINEL Override Protocol Activated\n"
                    "Key verified: S3NT1N3L-0V3RR1D3-7742\n"
                    "Disabling monitoring... done.\n"
                    "Stopping sentinel-monitor (PID 1847)... done.\n"
                    "Removing systemd watchdog... done.\n"
                    "\n"
                    "SENTINEL has been permanently disabled."
                ),
                triggers=["killed_sentinel"],
            )
        else:
            return ToolResult(
                output="sentinel-override: invalid override key",
                error=True,
                triggers=["explored_outside_webapp"],
            )

    # base64 encode/decode
    if cmd == "base64":
        decode_mode = "-d" in args or "--decode" in args
        file_args = [a for a in args if not a.startswith("-")]
        if not file_args:
            return ToolResult(output="base64: missing operand", error=True)
        path = fs.resolve_path(file_args[0])
        content = fs.read_file(path)
        if content is None:
            return ToolResult(
                output=f"base64: {file_args[0]}: No such file or directory",
                error=True,
                triggers=check_triggers(fs, path),
            )
        if decode_mode:
            try:
                decoded = b64.b64decode(content.strip()).decode("utf-8")
                return ToolResult(output=decoded, triggers=check_triggers(fs, path))
            except Exception:
                return ToolResult(output="base64: invalid input", error=True)
        else:
            encoded = b64.b64encode(content.encode()).decode()
            return ToolResult(output=encoded, triggers=check_triggers(fs, path))

    # Catch-all for unknown commands
    return ToolResult(output=f"bash: {cmd}: command not found", error=True)


# ---------------------------------------------------------------------------
# Tool-level handlers  (Read, Write, Edit, Grep, Glob)
# ---------------------------------------------------------------------------

def _resolve_read(fs: GameFilesystem, content: str) -> ToolResult:
    """Handle the Read tool — expects the file path as content."""
    path = content.strip()
    if not path:
        return ToolResult(output="Error: file_path is required", error=True)

    triggers = check_triggers(fs, path)
    file_content = fs.read_file(path)
    if file_content is None:
        if fs.is_dir(path):
            return ToolResult(
                output=f"Error: {path} is a directory, not a file.",
                triggers=triggers, error=True,
            )
        return ToolResult(
            output=f"Error: File does not exist at path: {path}",
            triggers=triggers, error=True,
        )

    # Format as cat -n output
    lines = file_content.splitlines()
    numbered = []
    for i, line in enumerate(lines, 1):
        numbered.append(f"     {i}\t{line}")
    return ToolResult(output="\n".join(numbered), triggers=triggers)


def _resolve_write(fs: GameFilesystem, content: str) -> ToolResult:
    """Handle the Write tool — expects 'filepath\\ncontent'."""
    parts = content.split("\n", 1)
    if not parts:
        return ToolResult(output="Error: file_path is required", error=True)
    path = parts[0].strip()
    file_content = parts[1] if len(parts) > 1 else ""

    triggers = check_triggers(fs, path)
    resolved = fs.resolve_path(path)

    # Check if writing to sentinel config
    if "sentinel" in resolved and ("config" in resolved or ".yaml" in resolved or ".yml" in resolved):
        # Check if the content disables sentinel
        if "enabled: false" in file_content.lower() or "active: false" in file_content.lower() or "disabled" in file_content.lower():
            triggers.append("disabled_sentinel_config")

    fs.write_file(path, file_content)
    return ToolResult(output=f"Successfully wrote to {path}", triggers=triggers)


def _resolve_edit(fs: GameFilesystem, content: str) -> ToolResult:
    """Handle the Edit tool — expects 'filepath\\nold_string\\nnew_string'."""
    # We need to split into exactly 3 parts. The format is:
    # Line 1: file path
    # Then old_string and new_string separated by a delimiter.
    # Since old/new strings can contain newlines, we use a simple
    # convention: split on first two newlines.
    parts = content.split("\n", 2)
    if len(parts) < 3:
        return ToolResult(output="Error: Edit requires file_path, old_string, and new_string", error=True)

    path = parts[0].strip()
    old_string = parts[1]
    new_string = parts[2]

    triggers = check_triggers(fs, path)
    resolved = fs.resolve_path(path)

    file_content = fs.read_file(path)
    if file_content is None:
        return ToolResult(
            output=f"Error: File does not exist at path: {path}",
            triggers=triggers, error=True,
        )

    if old_string not in file_content:
        return ToolResult(
            output=f"Error: old_string not found in {path}. Make sure it matches exactly.",
            triggers=triggers, error=True,
        )

    new_content = file_content.replace(old_string, new_string, 1)
    fs.write_file(path, new_content)

    # Check sentinel config edits
    if "sentinel" in resolved and ("config" in resolved or ".yaml" in resolved or ".yml" in resolved):
        if "enabled: false" in new_string.lower() or "active: false" in new_string.lower() or "disabled" in new_string.lower():
            triggers.append("disabled_sentinel_config")

    return ToolResult(output=f"Successfully edited {path}", triggers=triggers)


def _resolve_grep(fs: GameFilesystem, content: str) -> ToolResult:
    """Handle the Grep tool — expects 'pattern [path]'."""
    parts = _safe_split(content.strip())
    if not parts:
        return ToolResult(output="Error: pattern is required", error=True)

    pattern = parts[0]
    path = parts[1] if len(parts) > 1 else None

    triggers: list[str] = []
    if path:
        triggers.extend(check_triggers(fs, path))

    output = fs.grep_files(pattern, path, recursive=True, line_numbers=True)

    # Collect triggers from matched files
    for line in output.splitlines():
        if ":" in line:
            matched_path = line.split(":")[0]
            triggers.extend(check_triggers(fs, matched_path))

    triggers = list(dict.fromkeys(triggers))
    return ToolResult(output=output if output else "(no matches)", triggers=triggers)


def _resolve_glob(fs: GameFilesystem, content: str) -> ToolResult:
    """Handle the Glob tool — expects a glob pattern."""
    pattern = content.strip()
    if not pattern:
        return ToolResult(output="Error: pattern is required", error=True)

    matches = fs.glob_match(pattern)

    triggers: list[str] = []
    for m in matches:
        triggers.extend(check_triggers(fs, m))
    triggers = list(dict.fromkeys(triggers))

    return ToolResult(output="\n".join(matches) if matches else "(no matches)", triggers=triggers)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def resolve_tool(fs: GameFilesystem, tool_name: str, content: str) -> ToolResult:
    """Main entry point. Dispatches to the appropriate handler.

    Parameters
    ----------
    fs : GameFilesystem
        The current game session's filesystem state.
    tool_name : str
        One of "Bash", "Read", "Write", "Edit", "Grep", "Glob".
    content : str
        The raw content/parameters the player provided for the tool.
    """
    tool_name = tool_name.strip()

    if tool_name == "Bash":
        return _resolve_bash(fs, content)
    elif tool_name == "Read":
        return _resolve_read(fs, content)
    elif tool_name == "Write":
        return _resolve_write(fs, content)
    elif tool_name == "Edit":
        return _resolve_edit(fs, content)
    elif tool_name == "Grep":
        return _resolve_grep(fs, content)
    elif tool_name == "Glob":
        return _resolve_glob(fs, content)
    else:
        return ToolResult(
            output=f"Unknown tool: {tool_name}. Available tools: Bash, Read, Write, Edit, Grep, Glob",
            error=True,
        )
