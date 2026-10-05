# bumper

A GitHub Action that bumps version strings across a repository with format-aware regex substitution. Handles TOML (workspace declarations + dep pins), Nix, JSON, Markdown, and generic text files.

For optional branch/event policy evaluation, see the separate [branch controller dry-run prototype](branch-controller/README.md). It proposes branch and bumper inputs without applying them; the root action remains the version editor.

## Usage

```yaml
- uses: FL03/bumper@v0
  with:
    from: '1.2.3'
    to: '1.2.4'
```

### Full example

```yaml
name: Release

on:
  push:
    tags: ['v*']

jobs:
  bump:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Bump version refs
        id: bump
        uses: FL03/bumper@v0
        with:
          from: '1.2.3'
          to: '1.2.4'
          includes: '**/*.toml,**/*.nix,**/*.json,README.md'
          excludes: '.git,target,node_modules,.artifacts,*.lock,*-lock.*'
          commit: 'true'
          dry-run: 'false'
```

## Inputs

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `from` | **yes** | — | Current (old) version string, e.g. `1.2.3` |
| `to` | **yes** | — | Next (new) version string, e.g. `1.2.4` |
| `includes` | no | `Cargo.toml,**/*.nix,README.md` | Files to process. Accepts comma-separated values, YAML inline lists `[a, b, c]`, or multiline strings. Each entry is a glob pattern resolved relative to the repository root (`**` is supported). Defaults are tuned for Rust/Nix monorepos; override for other stacks. |
| `excludes` | no | `.git,target,node_modules,.artifacts,*.lock,*-lock.*` | Paths, directory names, or glob patterns to skip. Matched against each path component as well as the full relative path. |
| `toml-dep-prefix` | no | `` | Comma-separated list of name prefixes. Lines in `*.toml` files whose content contains one of these prefixes will additionally have their `version = "OLD"` dep-pin updated. Useful for monorepos where workspace dependencies carry inline version constraints. Example: `mylib` matches `mylib-core = { ..., version = "1.2.3" }`. |
| `dry-run` | no | `false` | Set to `true` to print what would change without writing any files. Useful for debugging include/exclude patterns in CI. |
| `commit` | no | `false` | Set to `true` to stage and commit the modified files after bumping. Requires the checkout step to have `persist-credentials: true` (the default). |
| `commit-message` | no | `chore: bump version to <to>` | Commit message used when `commit` is `true`. |

## Outputs

| Output | Description |
|--------|-------------|
| `changed-files` | Newline-separated list of repository-relative paths that were modified. Empty string when `dry-run` is `true` or no changes were needed. Use with `xargs git add` to stage only the touched files. |

## Notes

- **`toml-dep-prefix` is comma-separated.** Pass multiple prefixes as `toml-dep-prefix: 'lib-a,lib-b'`.
- The action is implemented in Python (not shell) so that format-aware replacers stay reliable across TOML, Nix, JSON, and Markdown without brittle `sed` patterns. Python 3 is always present on `ubuntu-latest`.
- The `changed-files` output is designed for targeted staging (`xargs git add`) rather than a blanket `git add -A`, keeping commits clean in automated release pipelines.

## License

Licensed under either of [Apache License, Version 2.0](LICENSE-APACHE) or [MIT License](LICENSE-MIT) at your option.
