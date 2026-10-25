"""`Edit`: exact string replacement with opencode's fallback matchers (trimmed lines, block anchors,
whitespace, indentation, escapes, ...). Keeps the file's BOM and line endings. Follows opencode's edit tool.

The matching approaches are sourced, as in opencode, from
https://github.com/cline/cline/blob/main/evals/diff-edits/diff-apply/diff-06-23-25.ts
https://github.com/google-gemini/gemini-cli/blob/main/packages/core/src/utils/editCorrector.ts
https://github.com/cline/cline/blob/main/evals/diff-edits/diff-apply/diff-06-26-25.ts
"""

from __future__ import annotations

import difflib
import os
import re
import stat
import threading
import weakref
from collections.abc import Callable, Iterator
from pathlib import Path

from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.messages import ToolReturn

from ..core.deps import HarnessDeps
from ..core.permissions import PermissionDenied
from .base import tool
from .read import path_subject

DESCRIPTION = """Performs exact string replacements in files.

Usage:
- You must use your `Read` tool at least once in the conversation before editing. This tool will error if you attempt an edit without reading the file.
- When editing text from Read tool output, ensure you preserve the exact indentation (tabs/spaces) as it appears AFTER the line number prefix. The line number prefix format is: line number + colon + space (e.g., `1: `). Everything after that space is the actual file content to match. Never include any part of the line number prefix in the oldString or newString.
- ALWAYS prefer editing existing files in the codebase. NEVER write new files unless explicitly required.
- Only use emojis if the user explicitly requests it. Avoid adding emojis to files unless asked.
- The edit will FAIL if `oldString` is not found in the file with an error "oldString not found in content".
- The edit will FAIL if `oldString` is found multiple times in the file with an error "Found multiple matches for oldString. Provide more surrounding lines in oldString to identify the correct match." Either provide a larger string with more surrounding context to make it unique or use `replaceAll` to change every instance of `oldString`.
- Use `replaceAll` for replacing and renaming strings across the file. This parameter is useful if you want to rename a variable for instance."""

BOM = "\ufeff"
EMPTY_OLD_STRING = (
    "oldString cannot be empty when editing an existing file. Provide the exact text to replace, "
    "or use write for an intentional full-file replacement."
)


@tool(subject=path_subject, truncates=True, description=DESCRIPTION)
def Edit(
    ctx: RunContext[HarnessDeps], filePath: str, oldString: str, newString: str, replaceAll: bool | None = None
) -> str | ToolReturn:
    """Replace text in a file.

    Args:
        filePath: The absolute path to the file to modify
        oldString: The text to replace
        newString: The text to replace it with (must be different from oldString)
        replaceAll: Replace all occurrences of oldString (default false)
    """
    if not filePath:
        raise ModelRetry("filePath is required")
    if oldString == newString:
        raise ModelRetry("No changes to apply: oldString and newString are identical.")
    path = ctx.deps.resolve_path(filePath)
    try:  # a symlink can lead somewhere the rules deny
        ctx.deps.permissions.check("Edit", str(path))
    except PermissionDenied as exc:
        return str(exc)

    with file_lock(path):
        info = stat_path(path)
        if oldString == "":
            if info is not None:
                raise ModelRetry(EMPTY_OLD_STRING)
            content_old = ""
            bom, content_new = split_bom(newString)
            write_text(path, content_new, bom)
        else:
            if info is None:
                raise ModelRetry(f"File {path} not found")
            if stat.S_ISDIR(info.st_mode):
                raise ModelRetry(f"Path is a directory, not a file: {path}")
            source_bom, content_old = read_text(path)
            ending = detect_line_ending(content_old)
            old = convert_to_line_ending(normalize_line_endings(oldString), ending)
            replacement = convert_to_line_ending(normalize_line_endings(newString), ending)
            try:
                replaced = replace(content_old, old, replacement, bool(replaceAll))
            except ValueError as exc:
                raise ModelRetry(str(exc)) from exc
            next_bom, content_new = split_bom(replaced)
            write_text(path, content_new, source_bom or next_bom)

    old_lines = normalize_line_endings(content_old)
    new_lines = normalize_line_endings(content_new)
    diff = unified_diff(str(path), old_lines, new_lines)
    additions, deletions = count_changes(diff)
    diff = trim_diff(diff)
    return ToolReturn(
        "Edit applied successfully.",
        metadata={"filepath": str(path), "diff": diff, "additions": additions, "deletions": deletions},
    )


# ---------------------------------------------------------------- file helpers shared with Write

_locks: weakref.WeakValueDictionary[str, threading.Lock] = weakref.WeakValueDictionary()
_locks_guard = threading.Lock()


def file_lock(path: Path) -> threading.Lock:
    """One lock per file, so concurrent edits of the same file don't lose each other's changes.
    The caller's reference keeps the lock alive while it is used; unused locks are dropped."""
    with _locks_guard:
        lock = _locks.get(str(path))
        if lock is None:
            lock = _locks[str(path)] = threading.Lock()
        return lock


def stat_path(path: Path) -> os.stat_result | None:
    """`None` if there is no such file. `Path.exists()` raises on other errors, e.g. an unsearchable parent."""
    try:
        return path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        raise ModelRetry(f"Cannot access {path}: {exc.strerror or exc}") from exc


def split_bom(text: str) -> tuple[bool, str]:
    return (True, text[1:]) if text.startswith(BOM) else (False, text)


def read_text(path: Path) -> tuple[bool, str]:
    """`(has BOM, text without it)`. Refuses files that aren't UTF-8, which a rewrite would corrupt."""
    try:
        return split_bom(path.read_bytes().decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ModelRetry(f"Cannot edit {path}: it is not a UTF-8 text file") from exc
    except OSError as exc:
        raise ModelRetry(f"Cannot read {path}: {exc.strerror or exc}") from exc


def write_text(path: Path, text: str, bom: bool) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(((BOM if bom else "") + split_bom(text)[1]).encode("utf-8"))
    except OSError as exc:  # e.g. a parent path is a file, or the file is read-only
        raise ModelRetry(f"Cannot write {path}: {exc.strerror or exc}") from exc


def normalize_line_endings(text: str) -> str:
    return text.replace("\r\n", "\n")


def detect_line_ending(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def convert_to_line_ending(text: str, ending: str) -> str:
    return text if ending == "\n" else text.replace("\n", "\r\n")


def _lines(text: str) -> list[str]:
    """Lines with their `\\n`, split on `\\n` only like jsdiff (`str.splitlines` also splits on `\\f`, `\\r`, ...)."""
    return re.findall(r"[^\n]*\n|[^\n]+$", text)


def unified_diff(path: str, old: str, new: str) -> str:
    """A patch in the format of jsdiff's `createTwoFilesPatch`, which opencode puts in tool metadata."""
    out = [f"Index: {path}\n", "=" * 67 + "\n"]
    lines = difflib.unified_diff(_lines(old), _lines(new), path, path)
    for line in lines:
        out.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    if len(out) == 2:
        out += [f"--- {path}\n", f"+++ {path}\n"]
    return "".join(out)


def count_changes(diff: str) -> tuple[int, int]:
    """`(lines added, lines removed)` in a `unified_diff`, so the counts always agree with the patch.
    (`difflib.ndiff` or a non-autojunk matcher take minutes on long runs of similar or repeated lines.)"""
    additions = deletions = 0
    in_hunks = False
    for line in diff.split("\n"):
        if line.startswith("@@"):
            in_hunks = True
        elif in_hunks and line.startswith("+"):
            additions += 1
        elif in_hunks and line.startswith("-"):
            deletions += 1
    return additions, deletions


def _is_hunk_line(line: str) -> bool:
    return line[:1] in ("+", "-", " ") and not line.startswith(("---", "+++"))


def trim_diff(diff: str) -> str:
    """Remove the indentation every changed or context line shares, so deeply nested edits read well."""
    lines = diff.split("\n")
    content_lines = [line for line in lines if _is_hunk_line(line)]
    if not content_lines:
        return diff
    indents = [len(c) - len(c.lstrip()) for c in (line[1:] for line in content_lines) if c.strip()]
    least = min(indents, default=0)
    if least == 0:
        return diff
    return "\n".join(line[0] + line[1:][least:] if _is_hunk_line(line) else line for line in lines)


# ---------------------------------------------------------------- replacers

Replacer = Callable[[str, str], Iterator[str]]

SINGLE_CANDIDATE_SIMILARITY_THRESHOLD = 0.65
MULTIPLE_CANDIDATES_SIMILARITY_THRESHOLD = 0.65
MAX_SCORED_LINE_LENGTH = 200
"""Longer lines (minified code) are compared for equality only: `levenshtein` is quadratic in the line length."""


def levenshtein(a: str, b: str) -> int:
    if a == "" or b == "":
        return max(len(a), len(b))
    previous = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        current = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
        previous = current
    return previous[len(b)]


def _span(lines: list[str], start: int, end: int) -> tuple[int, int]:
    """Character offsets of lines `start..end` (inclusive) in the text the lines were split from."""
    begin = sum(len(line) + 1 for line in lines[:start])
    finish = begin + sum(len(line) for line in lines[start : end + 1]) + (end - start)
    return begin, finish


def simple_replacer(content: str, find: str) -> Iterator[str]:
    yield find


def line_trimmed_replacer(content: str, find: str) -> Iterator[str]:
    original = content.split("\n")
    search = find.split("\n")
    if search[-1] == "":
        search.pop()
    for i in range(len(original) - len(search) + 1):
        if all(original[i + j].strip() == search[j].strip() for j in range(len(search))):
            begin, finish = _span(original, i, i + len(search) - 1)
            yield content[begin:finish]


def _middle_similarity(original: list[str], start: int, size: int, search: list[str], early_exit: bool) -> float:
    lines_to_check = min(len(search) - 2, size - 2)
    if lines_to_check <= 0:
        return 1.0  # no middle lines to compare: accept on the anchors
    similarity = 0.0
    for j in range(1, min(len(search) - 1, size - 1)):
        original_line = original[start + j].strip()
        search_line = search[j].strip()
        longest = max(len(original_line), len(search_line))
        if longest == 0:
            continue
        if original_line == search_line:
            score = 1.0
        elif longest > MAX_SCORED_LINE_LENGTH:
            score = 0.0
        else:
            score = 1 - levenshtein(original_line, search_line) / longest
        if early_exit:
            similarity += score / lines_to_check
            if similarity >= SINGLE_CANDIDATE_SIMILARITY_THRESHOLD:
                break
        else:
            similarity += score
    return similarity if early_exit else similarity / lines_to_check


def block_anchor_replacer(content: str, find: str) -> Iterator[str]:
    original = content.split("\n")
    search = find.split("\n")
    if len(search) < 3:
        return
    if search[-1] == "":
        search.pop()
    first, last = search[0].strip(), search[-1].strip()
    max_delta = max(1, len(search) // 4)

    candidates: list[tuple[int, int]] = []
    for i, line in enumerate(original):
        if line.strip() != first:
            continue
        for j in range(i + 2, len(original)):
            if original[j].strip() == last:
                if abs((j - i + 1) - len(search)) <= max_delta:
                    candidates.append((i, j))
                break  # only the first occurrence of the last line
    if not candidates:
        return

    if len(candidates) == 1:
        start, end = candidates[0]
        if _middle_similarity(original, start, end - start + 1, search, True) >= SINGLE_CANDIDATE_SIMILARITY_THRESHOLD:
            begin, finish = _span(original, start, end)
            yield content[begin:finish]
        return

    best: tuple[int, int] | None = None
    best_similarity = -1.0
    for start, end in candidates:
        similarity = _middle_similarity(original, start, end - start + 1, search, False)
        if similarity > best_similarity:
            best_similarity, best = similarity, (start, end)
    if best is not None and best_similarity >= MULTIPLE_CANDIDATES_SIMILARITY_THRESHOLD:
        begin, finish = _span(original, *best)
        yield content[begin:finish]


def whitespace_normalized_replacer(content: str, find: str) -> Iterator[str]:
    def normalize(text: str) -> str:
        return re.sub(r"\s+", " ", text).strip()

    normalized_find = normalize(find)
    lines = content.split("\n")
    for line in lines:
        normalized_line = normalize(line)
        if normalized_line == normalized_find:
            yield line
        elif normalized_find in normalized_line:
            words = find.strip().split()
            if words:
                match = re.search(r"\s+".join(re.escape(word) for word in words), line)
                if match:
                    yield match.group(0)

    find_lines = find.split("\n")
    if len(find_lines) > 1:
        for i in range(len(lines) - len(find_lines) + 1):
            block = "\n".join(lines[i : i + len(find_lines)])
            if normalize(block) == normalized_find:
                yield block


def indentation_flexible_replacer(content: str, find: str) -> Iterator[str]:
    def remove_indentation(text: str) -> str:
        lines = text.split("\n")
        non_empty = [line for line in lines if line.strip()]
        if not non_empty:
            return text
        least = min(len(line) - len(line.lstrip()) for line in non_empty)
        return "\n".join(line if not line.strip() else line[least:] for line in lines)

    normalized_find = remove_indentation(find)
    content_lines = content.split("\n")
    find_lines = find.split("\n")
    for i in range(len(content_lines) - len(find_lines) + 1):
        block = "\n".join(content_lines[i : i + len(find_lines)])
        if remove_indentation(block) == normalized_find:
            yield block


_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "'": "'", '"': '"', "`": "`", "\\": "\\", "\n": "\n", "$": "$"}
_ESCAPE = re.compile(r"\\(n|t|r|'|\"|`|\\|\n|\$)")


def _unescape(text: str) -> str:
    return _ESCAPE.sub(lambda m: _ESCAPES[m.group(1)], text)


def escape_normalized_replacer(content: str, find: str) -> Iterator[str]:
    unescaped_find = _unescape(find)
    if unescaped_find in content:
        yield unescaped_find
    lines = content.split("\n")
    find_lines = unescaped_find.split("\n")
    for i in range(len(lines) - len(find_lines) + 1):
        block = "\n".join(lines[i : i + len(find_lines)])
        if _unescape(block) == unescaped_find:
            yield block


def multi_occurrence_replacer(content: str, find: str) -> Iterator[str]:
    """Every exact match, so `replace` can decide between unique and `replaceAll`."""
    start = 0
    while (index := content.find(find, start)) != -1:
        yield find
        start = index + len(find)


def trimmed_boundary_replacer(content: str, find: str) -> Iterator[str]:
    trimmed = find.strip()
    if trimmed == find:
        return
    if trimmed in content:
        yield trimmed
    lines = content.split("\n")
    find_lines = find.split("\n")
    for i in range(len(lines) - len(find_lines) + 1):
        block = "\n".join(lines[i : i + len(find_lines)])
        if block.strip() == trimmed:
            yield block


def context_aware_replacer(content: str, find: str) -> Iterator[str]:
    find_lines = find.split("\n")
    if len(find_lines) < 3:
        return  # needs at least 3 lines to have meaningful context
    if find_lines[-1] == "":
        find_lines.pop()
    content_lines = content.split("\n")
    first, last = find_lines[0].strip(), find_lines[-1].strip()
    for i, line in enumerate(content_lines):
        if line.strip() != first:
            continue
        for j in range(i + 2, len(content_lines)):
            if content_lines[j].strip() != last:
                continue
            block_lines = content_lines[i : j + 1]
            if len(block_lines) == len(find_lines):
                # at least half of the non-empty middle lines must match when trimmed
                matching = total = 0
                for k in range(1, len(block_lines) - 1):
                    block_line, find_line = block_lines[k].strip(), find_lines[k].strip()
                    if block_line or find_line:
                        total += 1
                        matching += block_line == find_line
                if total == 0 or matching / total >= 0.5:
                    yield "\n".join(block_lines)
            break


REPLACERS: tuple[Replacer, ...] = (
    simple_replacer,
    line_trimmed_replacer,
    block_anchor_replacer,
    whitespace_normalized_replacer,
    indentation_flexible_replacer,
    escape_normalized_replacer,
    trimmed_boundary_replacer,
    context_aware_replacer,
    multi_occurrence_replacer,
)


def replace(content: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    """Replace `old_string`, trying each replacer in turn until one finds a unique match (or any, with
    `replace_all`). Raises `ValueError` with the message for the model when nothing fits."""
    if old_string == new_string:
        raise ValueError("No changes to apply: oldString and newString are identical.")
    if old_string == "":
        raise ValueError(EMPTY_OLD_STRING)

    not_found = True
    for replacer in REPLACERS:
        for search in replacer(content, old_string):
            index = content.find(search)
            if index == -1:
                continue
            not_found = False
            if _is_disproportionate_match(search, old_string):
                raise ValueError(
                    "Refusing replacement because the matched span is much larger than oldString. "
                    "Re-read the file and provide the full exact oldString for the intended replacement."
                )
            if replace_all:
                return content.replace(search, new_string)
            if index != content.rfind(search):
                continue
            return content[:index] + new_string + content[index + len(search) :]

    if not_found:
        raise ValueError(
            "Could not find oldString in the file. It must match exactly, including whitespace, indentation, "
            "and line endings."
        )
    raise ValueError("Found multiple matches for oldString. Provide more surrounding context to make the match unique.")


def _is_disproportionate_match(search: str, old_string: str) -> bool:
    old_lines = old_string.count("\n") + 1
    search_lines = search.count("\n") + 1
    if search_lines >= max(old_lines + 3, old_lines * 2):
        return True
    if old_lines == 1:
        return False
    return len(search.strip()) > max(len(old_string.strip()) + 500, len(old_string.strip()) * 4)
