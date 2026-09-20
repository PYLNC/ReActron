"""Workspace path and Git worktree safety primitives.

The caller supplies a trusted workspace scope from storage.  Model arguments
never choose a root and are resolved before reaching a filesystem primitive.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import tempfile
import time
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import MappingProxyType
from typing import Mapping

from backend.config import BASE_DIR


MAX_PATH_CHARS = 1_024
MAX_LIST_ENTRIES = 256
MAX_TEXT_BYTES = 64 * 1024
MAX_GIT_OUTPUT_BYTES = 256 * 1024
GIT_TIMEOUT_SECONDS = 30
STATE_ROOT = BASE_DIR / "data" / "state"
_SCOPE_FIELDS = frozenset({"workspace_id", "kind", "effective_root", "mode"})
_KINDS = frozenset({"directory", "git"})
_MODES = frozenset({"read_only", "read_write"})
_TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_WINDOWS_RESERVED = frozenset({
    "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
})
_REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class WorkspaceError(ValueError):
    """A stable, fail-closed workspace rejection."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _is_reparse(info) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & _REPARSE_ATTRIBUTE
    )


def _checked_root(raw_root) -> Path:
    try:
        root = Path(raw_root)
        windows = PureWindowsPath(str(root))
        if (not root.is_absolute() or windows.anchor.startswith("\\\\")
                or not root.is_dir()):
            raise WorkspaceError("workspace_scope_invalid")
        absolute = Path(os.path.abspath(root))
        current = Path(absolute.anchor)
        if _is_reparse(os.lstat(current)):
            raise WorkspaceError("workspace_scope_invalid")
        for part in absolute.parts[1:]:
            current /= part
            if _is_reparse(os.lstat(current)):
                raise WorkspaceError("workspace_scope_invalid")
        resolved = root.resolve(strict=True)
        if os.path.normcase(os.path.abspath(root)) != os.path.normcase(str(resolved)):
            raise WorkspaceError("workspace_scope_invalid")
        return resolved
    except (OSError, TypeError, ValueError) as exc:
        if isinstance(exc, WorkspaceError):
            raise
        raise WorkspaceError("workspace_scope_invalid") from None


def initialize_workspace_state() -> None:
    """Create the two fixed host-owned state directories, never a supplied path."""
    try:
        _checked_root(STATE_ROOT.parent)
        STATE_ROOT.mkdir(exist_ok=True)
        state = _checked_root(STATE_ROOT)
        for name in ("worktrees", "artifacts"):
            child = state / name
            child.mkdir(exist_ok=True)
            _checked_root(child)
    except WorkspaceError:
        raise
    except OSError:
        raise WorkspaceError("workspace_scope_invalid") from None


def _relative_parts(raw_path: str) -> tuple[str, ...]:
    if not isinstance(raw_path, str) or not raw_path or len(raw_path) > MAX_PATH_CHARS:
        raise WorkspaceError("invalid_relative_path")
    if "\x00" in raw_path or PurePosixPath(raw_path).is_absolute():
        raise WorkspaceError("invalid_relative_path")
    windows = PureWindowsPath(raw_path)
    if windows.drive or windows.root or raw_path.startswith(("/", "\\")):
        raise WorkspaceError("invalid_relative_path")
    parts = tuple(re.split(r"[\\/]", raw_path))
    if any(not part or part in {".", ".."} for part in parts):
        raise WorkspaceError("invalid_relative_path")
    for part in parts:
        stem = part.split(".", 1)[0].upper()
        if (":" in part or part[-1] in {" ", "."} or stem in _WINDOWS_RESERVED
                or len(part) > 255):
            raise WorkspaceError("invalid_relative_path")
    return parts


def _inside(root: Path, candidate: Path) -> bool:
    try:
        root_key = os.path.normcase(str(root))
        candidate_key = os.path.normcase(str(candidate))
        return os.path.commonpath((root_key, candidate_key)) == root_key
    except (OSError, ValueError):
        return False


def resolve_workspace_path(root, raw_path: str, *, for_write: bool = False) -> Path:
    """Resolve one strict relative path without following reparse points."""
    trusted_root = _checked_root(root)
    parts = _relative_parts(raw_path)
    candidate = trusted_root.joinpath(*parts)
    current = trusted_root
    try:
        for index, part in enumerate(parts):
            current = current / part
            exists = os.path.lexists(current)
            if exists:
                if _is_reparse(os.lstat(current)):
                    raise WorkspaceError("path_not_granted")
            elif index != len(parts) - 1 or not for_write:
                raise WorkspaceError("path_not_found")
        resolved = candidate.resolve(strict=not for_write or os.path.lexists(candidate))
        if not _inside(trusted_root, resolved):
            raise WorkspaceError("path_not_granted")
        if for_write and not resolved.parent.is_dir():
            raise WorkspaceError("path_not_found")
        return resolved
    except (OSError, RuntimeError) as exc:
        if isinstance(exc, WorkspaceError):
            raise
        raise WorkspaceError("path_not_granted") from None


def _relative_name(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        raise WorkspaceError("path_not_granted") from None


def list_workspace_directory(root: Path, directory: Path) -> dict:
    root, directory = _checked_root(root), Path(directory)
    if resolve_workspace_path(root, _relative_name(root, directory)) != directory:
        raise WorkspaceError("path_not_granted")
    if not directory.is_dir():
        raise WorkspaceError("not_a_directory")
    entries = []
    try:
        with os.scandir(directory) as iterator:
            for entry in iterator:
                if len(entries) >= MAX_LIST_ENTRIES:
                    raise WorkspaceError("limit_exceeded")
                info = entry.stat(follow_symlinks=False)
                entry_type = ("blocked" if _is_reparse(info) else
                              "directory" if stat.S_ISDIR(info.st_mode) else
                              "file" if stat.S_ISREG(info.st_mode) else "other")
                entries.append({"name": entry.name, "type": entry_type})
    except OSError:
        raise WorkspaceError("workspace_io_failed") from None
    entries.sort(key=lambda item: (item["type"] != "directory", item["name"].casefold()))
    return {"path": _relative_name(root, directory), "entries": entries}


def read_workspace_text(root: Path, path: Path, *, max_bytes: int = MAX_TEXT_BYTES) -> dict:
    root, path = _checked_root(root), Path(path)
    if not isinstance(max_bytes, int) or not 1 <= max_bytes <= MAX_TEXT_BYTES:
        raise WorkspaceError("limit_exceeded")
    if resolve_workspace_path(root, _relative_name(root, path)) != path:
        raise WorkspaceError("path_not_granted")
    try:
        info = os.lstat(path)
        if _is_reparse(info) or not stat.S_ISREG(info.st_mode):
            raise WorkspaceError("not_a_file")
        with path.open("rb") as stream:
            data = stream.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise WorkspaceError("limit_exceeded")
        content = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise WorkspaceError("invalid_utf8") from None
    except OSError:
        raise WorkspaceError("workspace_io_failed") from None
    return {"path": _relative_name(root, path), "bytes": len(data), "content": content}


def write_workspace_text(root: Path, path: Path, content: str,
                         *, max_bytes: int = MAX_TEXT_BYTES) -> dict:
    root, path = _checked_root(root), Path(path)
    if not isinstance(content, str) or not isinstance(max_bytes, int) or not 1 <= max_bytes <= MAX_TEXT_BYTES:
        raise WorkspaceError("invalid_content")
    try:
        data = content.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise WorkspaceError("invalid_utf8") from None
    if len(data) > max_bytes:
        raise WorkspaceError("limit_exceeded")
    relative = _relative_name(root, path)
    if resolve_workspace_path(root, relative, for_write=True) != path:
        raise WorkspaceError("path_not_granted")
    if os.path.lexists(path):
        info = os.lstat(path)
        if _is_reparse(info) or not stat.S_ISREG(info.st_mode):
            raise WorkspaceError("not_a_file")
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=".reactron-", dir=path.parent)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError:
        raise WorkspaceError("workspace_io_failed") from None
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass
    return {"path": relative, "bytes": len(data)}


def _scope(scope: Mapping | None):
    if not isinstance(scope, Mapping) or set(scope) != _SCOPE_FIELDS:
        return None
    if (not isinstance(scope["workspace_id"], str) or not scope["workspace_id"]
            or scope["kind"] not in _KINDS or scope["mode"] not in _MODES):
        return None
    try:
        return _checked_root(scope["effective_root"]), scope["mode"]
    except WorkspaceError:
        return None


def workspace_tool_specs(scope: Mapping | None):
    """Return fixed tools only for a trusted, valid workspace scope."""
    validated = _scope(scope)
    if not validated:
        return MappingProxyType({})
    root, mode = validated
    from backend.tool_gateway import ToolSpec

    path_schema = {"type": "string", "minLength": 1, "maxLength": MAX_PATH_CHARS}

    def list_handler(path: str):
        return list_workspace_directory(root, resolve_workspace_path(root, path))

    def read_handler(path: str):
        return read_workspace_text(root, resolve_workspace_path(root, path))

    def write_handler(path: str, content: str):
        return write_workspace_text(root, resolve_workspace_path(root, path, for_write=True), content)

    specs = {
        "workspace_list": ToolSpec(
            name="workspace_list",
            schema={"type": "object", "properties": {"path": path_schema},
                    "required": ["path"], "additionalProperties": False},
            handler=list_handler, effects=frozenset({"filesystem.read"}),
            idempotent=True, approval="never", origin="builtin",
        ),
        "workspace_read_text": ToolSpec(
            name="workspace_read_text",
            schema={"type": "object", "properties": {"path": path_schema},
                    "required": ["path"], "additionalProperties": False},
            handler=read_handler, effects=frozenset({"filesystem.read"}),
            idempotent=True, approval="never", origin="builtin",
        ),
    }
    if mode == "read_write":
        specs["workspace_write_text"] = ToolSpec(
            name="workspace_write_text",
            schema={"type": "object", "properties": {
                "path": path_schema,
                "content": {"type": "string", "maxLength": MAX_TEXT_BYTES},
            }, "required": ["path", "content"], "additionalProperties": False},
            handler=write_handler, effects=frozenset({"filesystem.write"}),
            idempotent=False, approval="always", origin="builtin",
        )
    return MappingProxyType(specs)


def _git(root: Path, *arguments: str, max_bytes: int = MAX_GIT_OUTPUT_BYTES,
         allowed_codes=(0,)) -> tuple[int, str]:
    if not isinstance(max_bytes, int) or max_bytes < 1 or max_bytes > MAX_GIT_OUTPUT_BYTES:
        raise WorkspaceError("limit_exceeded")
    command = ["git", "-C", str(root), *arguments]
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith("GIT_")}
    environment.update({"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never",
                        "GIT_PAGER": "cat"})
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            process = subprocess.Popen(command, stdout=stdout, stderr=stderr, shell=False,
                                       env=environment)
            deadline = time.monotonic() + GIT_TIMEOUT_SECONDS
            while process.poll() is None:
                if (os.fstat(stdout.fileno()).st_size > max_bytes
                        or os.fstat(stderr.fileno()).st_size > MAX_GIT_OUTPUT_BYTES):
                    process.kill()
                    process.wait()
                    raise WorkspaceError("limit_exceeded")
                if time.monotonic() >= deadline:
                    process.kill()
                    process.wait()
                    raise WorkspaceError("git_timeout")
                time.sleep(0.01)
            if (os.fstat(stdout.fileno()).st_size > max_bytes
                    or os.fstat(stderr.fileno()).st_size > MAX_GIT_OUTPUT_BYTES):
                raise WorkspaceError("limit_exceeded")
            stdout.seek(0)
            stderr.seek(0)
            output = stdout.read(max_bytes + 1).decode("utf-8", errors="strict")
            error = stderr.read(MAX_GIT_OUTPUT_BYTES + 1).decode("utf-8", errors="replace")
        except FileNotFoundError:
            raise WorkspaceError("git_unavailable") from None
        except UnicodeDecodeError:
            raise WorkspaceError("invalid_utf8") from None
    if process.returncode not in allowed_codes:
        raise WorkspaceError("git_failed") from None
    return process.returncode, output.rstrip("\r\n")


def _registered_git_root(raw_root) -> Path:
    root = _checked_root(raw_root)
    _, reported = _git(root, "rev-parse", "--show-toplevel")
    try:
        actual = Path(reported).resolve(strict=True)
    except (OSError, ValueError):
        raise WorkspaceError("git_root_invalid") from None
    if os.path.normcase(str(actual)) != os.path.normcase(str(root)):
        raise WorkspaceError("git_root_invalid")
    return root


def normalize_workspace_root(raw_root, kind: str) -> Path:
    """Canonicalize a host-registered root; Git roots must match top-level exactly."""
    if kind not in _KINDS:
        raise WorkspaceError("workspace_kind_invalid")
    return _registered_git_root(raw_root) if kind == "git" else _checked_root(raw_root)


def _task_parts(state_root, task_id: str) -> tuple[Path, str, Path]:
    if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
        raise WorkspaceError("invalid_task_id")
    state = _checked_root(state_root)
    worktrees = state / "worktrees"
    if (not os.path.lexists(worktrees) or _is_reparse(os.lstat(worktrees))
            or not worktrees.is_dir()):
        raise WorkspaceError("workspace_scope_invalid")
    checkout = worktrees / task_id
    if not _inside(state, checkout):
        raise WorkspaceError("path_not_granted")
    return state, f"reactron/{task_id}", checkout


def _worktree_paths(git_root: Path) -> set[Path]:
    _, output = _git(git_root, "worktree", "list", "--porcelain")
    paths = set()
    for line in output.splitlines():
        if line.startswith("worktree "):
            paths.add(Path(line[9:]).resolve(strict=False))
    return paths


def create_git_worktree(git_root, state_root, task_id: str) -> dict:
    """Create one isolated checkout at current HEAD; never changes the main checkout."""
    root = _registered_git_root(git_root)
    _, branch, checkout = _task_parts(state_root, task_id)
    if os.path.lexists(checkout):
        raise WorkspaceError("worktree_exists")
    _git(root, "check-ref-format", "--branch", branch)
    branch_code, _ = _git(root, "show-ref", "--verify", "--quiet",
                          f"refs/heads/{branch}", allowed_codes=(0, 1))
    if branch_code == 0:
        raise WorkspaceError("branch_exists")
    _, head = _git(root, "rev-parse", "--verify", "HEAD")
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", head):
        raise WorkspaceError("git_failed")
    _git(root, "worktree", "add", "-b", branch, str(checkout), head)
    return {"branch": branch, "checkout_path": str(checkout), "head": head}


def inspect_git_worktree(git_root, state_root, task_id: str) -> dict:
    root = _registered_git_root(git_root)
    _, branch, checkout = _task_parts(state_root, task_id)
    if (not checkout.is_dir() or _is_reparse(os.lstat(checkout))
            or checkout.resolve(strict=True) not in _worktree_paths(root)):
        raise WorkspaceError("worktree_not_found")
    _, actual_branch = _git(checkout, "symbolic-ref", "--short", "HEAD")
    if actual_branch != branch:
        raise WorkspaceError("worktree_mismatch")
    _, head = _git(checkout, "rev-parse", "--verify", "HEAD")
    _, status = _git(checkout, "status", "--porcelain=v1", "--untracked-files=all")
    return {"branch": branch, "checkout_path": str(checkout), "head": head,
            "dirty": bool(status)}


def git_worktree_diff(git_root, state_root, task_id: str,
                      *, max_bytes: int = MAX_GIT_OUTPUT_BYTES) -> str:
    checkout = Path(inspect_git_worktree(git_root, state_root, task_id)["checkout_path"])
    return _git(checkout, "diff", "--no-ext-diff", "--no-textconv", "HEAD", "--",
                max_bytes=max_bytes)[1]


def remove_git_worktree(git_root, state_root, task_id: str,
                        *, has_active_runs: bool) -> dict:
    """Ordinary Git removal only: active or dirty checkouts remain untouched."""
    if type(has_active_runs) is not bool:
        raise WorkspaceError("active_run_state_required")
    if has_active_runs:
        raise WorkspaceError("workspace_in_use")
    root = _registered_git_root(git_root)
    details = inspect_git_worktree(root, state_root, task_id)
    if details["dirty"]:
        raise WorkspaceError("worktree_dirty")
    checkout = Path(details["checkout_path"])
    _git(root, "worktree", "remove", str(checkout))
    return {"branch": details["branch"], "removed": True}


def _task_checkout(database, task_id: str, allowed_states: frozenset[str]):
    if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
        raise WorkspaceError("invalid_task_id")
    task = database.get_task(task_id)
    if not isinstance(task, dict) or not isinstance(task.get("workspace_id"), str):
        raise WorkspaceError("task_workspace_not_found")
    workspace = database.get_workspace(task["workspace_id"])
    if (not isinstance(workspace, dict) or workspace.get("kind") != "git"
            or not isinstance(workspace.get("root_path"), str)):
        raise WorkspaceError("task_workspace_not_found")
    checkout = database.get_worktree_checkout(task_id)
    if (not isinstance(checkout, dict) or checkout.get("task_id") != task_id
            or checkout.get("workspace_id") != task["workspace_id"]
            or checkout.get("state") not in allowed_states):
        raise WorkspaceError("worktree_not_found")
    root = normalize_workspace_root(workspace["root_path"], "git")
    initialize_workspace_state()
    _, branch, expected = _task_parts(STATE_ROOT, task_id)
    raw_checkout = checkout.get("path")
    if (not isinstance(raw_checkout, str) or not Path(raw_checkout).is_absolute()
            or os.path.normcase(os.path.abspath(raw_checkout)) != os.path.normcase(str(expected))
            or checkout.get("branch") != branch):
        raise WorkspaceError("worktree_mismatch")
    return root, checkout


def get_task_diff(database, task_id: str,
                  max_bytes: int = MAX_GIT_OUTPUT_BYTES) -> str:
    """API-facing safe diff: storage supplies identity, never a client path."""
    root, _ = _task_checkout(database, task_id, frozenset({"active", "retained"}))
    return git_worktree_diff(root, STATE_ROOT, task_id, max_bytes=max_bytes)


def cleanup_task_workspace(database, task_id: str) -> dict:
    """API-facing cleanup; the removing transition must atomically reject active Runs."""
    root, _ = _task_checkout(
        database, task_id, frozenset({"active", "retained", "failed"})
    )
    if database.task_has_active_run(task_id):
        raise WorkspaceError("workspace_in_use")
    # Storage owns the race boundary: this transition repeats the active-Run check
    # in the same write transaction before changing state to removing.
    database.transition_worktree_checkout(task_id, "removing")
    try:
        result = remove_git_worktree(root, STATE_ROOT, task_id, has_active_runs=False)
    except WorkspaceError as exc:
        database.transition_worktree_checkout(task_id, "failed", error_code=exc.code)
        raise
    database.transition_worktree_checkout(task_id, "removed")
    return result
