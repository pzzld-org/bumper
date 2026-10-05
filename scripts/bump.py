#!/usr/bin/env python3
"""bumper — format-aware version-string replacement for FL03/bumper@v1.

Reads configuration from environment variables set by the composite action:

  BUMPER_FROM          old version string (e.g. 1.2.3)
  BUMPER_TO            new version string (e.g. 1.2.4)
  BUMPER_INCLUDES      files/globs to process  (CSV, newline, or [list])
  BUMPER_EXCLUDES      paths/globs to skip      (CSV, newline, or [list])
  BUMPER_TOML_DEP_PREFIX  comma-separated name prefixes for TOML dep-pin bumping
  BUMPER_DRY_RUN       'true' to skip writes

Format-aware replacers
──────────────────────
  .toml   — exact-line `version = "OLD"` (workspace.package / [package])
             + dep-pin lines that match a crate-prefix
  .nix    — `version = "OLD";`  (Nix attribute syntax with trailing semicolon)
  .json   — `"version": "OLD"`
  .md     — shields.io badge params, **bold**, `code`, bare word-boundary refs
  *       — generic literal replacement fallback
"""

from __future__ import annotations

import fnmatch
import os
import re
import sys
from pathlib import Path


# ─── Utility ──────────────────────────────────────────────────────────────────


def parse_pattern_list(raw: str) -> list[str]:
    """Parse `[a, b, c]`, `"a,b,c"`, or `"a\nb\nc"` into a clean list."""
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    parts = re.split(r"[,\n]+", raw)
    return [p.strip() for p in parts if p.strip()]


def is_excluded(path: Path, exclude_patterns: list[str], root: Path) -> bool:
    """Return True if any exclude pattern matches a component or the full rel-path."""
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False
    rel_str = str(rel)
    for pat in exclude_patterns:
        # Match any single path component (e.g. 'target', 'node_modules')
        for part in rel.parts:
            if fnmatch.fnmatch(part, pat):
                return True
        # Match full relative path (e.g. '.artifacts/foo.md')
        if fnmatch.fnmatch(rel_str, pat):
            return True
        # Match filename only (e.g. '*.lock')
        if fnmatch.fnmatch(path.name, pat):
            return True
    return False


def expand_includes(patterns: list[str], root: Path) -> list[Path]:
    """Glob-expand patterns relative to root; return unique sorted file paths."""
    results: set[Path] = set()
    for pat in patterns:
        for match in root.glob(pat):
            if match.is_file():
                results.add(match.resolve())
    return sorted(results)


# ─── Format-aware replacers ───────────────────────────────────────────────────


def bump_toml(content: str, old: str, new: str, crate_prefixes: list[str]) -> str:
    """TOML: bump workspace/package version declarations and dep-pin lines.

    Pass 1 — exact-line: `^version = "OLD"$`
      Matches the canonical version field in [package] or [workspace.package].

    Pass 2 — prefix-scoped: for each crate prefix, find lines containing that
      prefix and replace `version = "OLD"` within that line only.
      This is intentionally line-scoped to avoid touching unrelated inline tables.
    """
    old_e = re.escape(old)

    # Pass 1: standalone version declaration (line-anchored)
    content = re.sub(
        r'^(version\s*=\s*)"' + old_e + r'"',
        r'\g<1>"' + new + '"',
        content,
        flags=re.MULTILINE,
    )

    # Pass 2: dep-pin lines for known crate prefixes
    for prefix in crate_prefixes:
        prefix_e = re.escape(prefix)

        def _replace_dep_pin(m: re.Match) -> str:
            return re.sub(
                r'(version\s*=\s*)"' + old_e + r'"',
                r'\g<1>"' + new + '"',
                m.group(0),
            )

        content = re.sub(
            r"^.*" + prefix_e + r".*$",
            _replace_dep_pin,
            content,
            flags=re.MULTILINE,
        )

    return content


def bump_nix(content: str, old: str, new: str) -> str:
    """Nix: `version = "OLD";` → `version = "NEW";` (trailing semicolon required)."""
    return re.sub(
        r'(version\s*=\s*)"' + re.escape(old) + r'";',
        r'\g<1>"' + new + '";',
        content,
    )


def bump_json(content: str, old: str, new: str) -> str:
    """JSON: `"version": "OLD"` → `"version": "NEW"`."""
    return re.sub(
        r'("version"\s*:\s*)"' + re.escape(old) + r'"',
        r'\g<1>"' + new + '"',
        content,
    )


def bump_markdown(content: str, old: str, new: str) -> str:
    """Markdown: shields.io badge params, **bold**, `inline-code`, word-boundary refs."""
    old_e = re.escape(old)
    # shields.io badge query param or path segment: ?v=OLD  /v{OLD}  /OLD-
    content = re.sub(r"(v=|/v)" + old_e, r"\g<1>" + new, content)
    # Bold: **OLD**
    content = re.sub(r"\*\*" + old_e + r"\*\*", "**" + new + "**", content)
    # Inline code: `OLD`
    content = re.sub(r"`" + old_e + r"`", "`" + new + "`", content)
    # Bare word-boundary reference (catches "Current version: **OLD**" fallthrough,
    # version numbers in prose, etc.)
    content = re.sub(r"\b" + old_e + r"\b", new, content)
    return content


def bump_generic(content: str, old: str, new: str) -> str:
    """Fallback: literal string replacement for any file type not handled above."""
    return content.replace(old, new)


# ─── File processor ───────────────────────────────────────────────────────────

_REPLACERS = {
    ".toml": None,   # handled separately (needs crate_prefixes)
    ".nix":  bump_nix,
    ".json": bump_json,
    ".md":   bump_markdown,
}


def process_file(
    path: Path,
    old: str,
    new: str,
    crate_prefixes: list[str],
    dry_run: bool,
) -> bool:
    """Apply version replacement to a single file. Returns True if changed."""
    try:
        original = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, PermissionError) as exc:
        print(f"::warning::Skipping {path}: {exc}")
        return False

    suffix = path.suffix.lower()

    if suffix == ".toml":
        updated = bump_toml(original, old, new, crate_prefixes)
    elif suffix in _REPLACERS:
        updated = _REPLACERS[suffix](original, old, new)  # type: ignore[operator]
    else:
        updated = bump_generic(original, old, new)

    if updated == original:
        return False

    if dry_run:
        print(f"[dry-run] would modify: {path}")
    else:
        path.write_text(updated, encoding="utf-8")
        print(f"  bumped: {path}")

    return True


# ─── Entry point ──────────────────────────────────────────────────────────────


def main() -> None:
    old = os.environ.get("BUMPER_FROM", "").strip()
    new = os.environ.get("BUMPER_TO", "").strip()
    includes_raw = os.environ.get("BUMPER_INCLUDES", "Cargo.toml,**/*.nix,README.md")
    excludes_raw = os.environ.get(
        "BUMPER_EXCLUDES",
        ".git,target,node_modules,.artifacts,*.lock,*-lock.*",
    )
    prefix_raw = os.environ.get("BUMPER_TOML_DEP_PREFIX", "")
    dry_run = os.environ.get("BUMPER_DRY_RUN", "false").lower() == "true"

    if not old or not new:
        print("::error::BUMPER_FROM and BUMPER_TO must both be non-empty.")
        sys.exit(1)

    if old == new:
        print(f"::warning::from ({old}) == to ({new}); nothing to do.")
        _write_output("", dry_run)
        return

    include_patterns = parse_pattern_list(includes_raw)
    exclude_patterns = parse_pattern_list(excludes_raw)
    crate_prefixes = [p.strip() for p in prefix_raw.split(",") if p.strip()]

    root = Path.cwd().resolve()
    candidates = expand_includes(include_patterns, root)
    files = [f for f in candidates if not is_excluded(f, exclude_patterns, root)]

    print(f"bumper: {old} → {new}")
    print(f"  includes : {', '.join(include_patterns)}")
    print(f"  excludes : {', '.join(exclude_patterns)}")
    if crate_prefixes:
        print(f"  toml-dep-prefix : {', '.join(crate_prefixes)}")
    print(f"  {len(files)} candidate file(s) after include/exclude filtering")
    if dry_run:
        print("  [dry-run mode — no files will be written]")
    print()

    changed: list[str] = []
    for f in files:
        if process_file(f, old, new, crate_prefixes, dry_run):
            changed.append(str(f.relative_to(root)))

    label = "[dry-run] " if dry_run else ""
    print(f"\n{label}Modified {len(changed)} file(s).")

    _write_output("\n".join(changed), dry_run)


def _write_output(changed_files: str, dry_run: bool) -> None:
    """Write the changed_files output to GITHUB_OUTPUT (no-op outside Actions)."""
    if dry_run:
        changed_files = ""
    github_output = os.environ.get("GITHUB_OUTPUT", "")
    if not github_output:
        return
    with open(github_output, "a", encoding="utf-8") as fh:
        # Multiline value uses the heredoc syntax required by GHA.
        fh.write(f"changed_files<<EOF\n{changed_files}\nEOF\n")


if __name__ == "__main__":
    main()
