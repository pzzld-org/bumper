# Branch policy dry run

This optional sub-action evaluates a trusted policy, a normalized event, and a current repository snapshot. It returns `plan`, `noop`, or `deny`. **It never executes proposals.** There is no activation flag, Git invocation, network request, version edit, branch/tag creation, merge, or release operation. The Python decision function does not mutate its inputs. Its CLI writes only stdout and, when supplied, `GITHUB_OUTPUT`.

Use the existing root bumper action for version substitution. The controller only proposes that action's `from` and `to` inputs, fixed to `dry-run: 'true'` and `commit: 'false'`.

## Run locally

Python 3.9+ is sufficient; there are no third-party packages.

```sh
python3 scripts/branch_policy.py \
  branch-controller/examples/historical.json \
  branch-controller/examples/event.json \
  branch-controller/examples/state.json
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

The example produces a plan to create `dev/v0.3.9-dev.0` at the current `v0.3.9` branch SHA and proposes a `0.3.9` → `0.3.9-dev.0` version edit. It does not create either. CLI exit status is 0 for plan/noop, 1 for deny, and 2 for incorrect arguments. The composite action takes three required file paths, `policy`, `event`, and `state`; outputs are `decision` and compact JSON `plan`. Denial fails the step.

Future callers can use `pzzld-org/bumper/branch-controller@<reviewed-full-commit-sha>` with these inputs. No workflow is installed by this prototype. Pin both the action and any checkout action to reviewed full commit SHAs. Keep policy and planner code on a trusted, pinned revision; never execute a PR's copy of them.

## Policy v1

All documents declare integer `schema_version: 1`; unknown fields fail closed. [historical.json](examples/historical.json) and [website.json](examples/website.json) are illustrative policies, not an activated convention for any repository. The historical example models `main` → `vX.Y.Z` → `dev/vX.Y.Z-dev.N`, plus an optional lane naming example. The website example accepts `patch/<slug>` directly against `main`. Adopt each pattern only after checking the destination repository's conventions. The sample version and initial dev iteration are explicit configuration; the planner does not infer or increment versions.

Each rule has these fields:

| Field | Meaning |
| --- | --- |
| `id` | Unique, nonempty rule name. |
| `event`, `actions` | `push`, `pull_request`, or `release`, plus exact allowed activity types. Push uses `[""]`. |
| `from_state` | Required snapshot phase. States are caller-defined strings. |
| `ref_pattern` | Python regex, always matched against the entire full ref. Named captures can supply templates. |
| `base_ref` | Optional exact rendered PR base ref. |
| `merged` | Optional boolean PR condition, useful for `closed` events. |
| `create_branch` | Optional `{ "parent": "refs/heads/...", "target": "refs/heads/..." }` templates. Targets must be branch refs. |
| `to_state` | Required with `create_branch`; proposed next phase. |
| `bump` | Optional `{ "from": "{version}", "to": "{version}-dev.0" }`, only alongside branch creation. |

Only `{named_capture}` substitution is accepted. No arithmetic, expressions, property access, commands, or variable expansion occurs. Parent refs may be branches or tags; snapshot tag SHAs must be peeled commit SHAs. The parent commit must equal the event commit. In the release example this requires the release branch and tag to resolve to the same commit; a moved release branch is denied as `parent_event_mismatch`. This prototype does not infer ancestry or merge a tag into a branch. The bumper bridge accepts version-shaped strings and deliberately does not decide release ordering or semantic-version increments.

Optional `scopes` contain `event`, `actions`, and `ref_pattern`. They identify managed namespaces. If no rule matches an event/activity/ref inside a scope, the outcome is `deny: disallowed_ref`. Outside all scopes and rules it is `noop: no_matching_rule`. This lets `patch/UPPERCASE` fail a website's slug policy while `topic/unrelated` remains out of scope. Git-invalid refs are denied before matching. Rule candidates with the wrong state or PR base are denied; multiple applicable candidates are denied as ambiguous. A matching check-only rule returns `noop: policy_match`.

## Normalized event and state

This prototype intentionally accepts normalized JSON, not arbitrary GitHub webhook payloads. A trusted caller must collect complete context and a fresh snapshot. Missing required context is denied; do not invent SHAs, fork status, or state. The provided example data is synthetic.

| Event field | Source/contract |
| --- | --- |
| `repository` | Base repository `owner/name`. Must equal the snapshot's repository. |
| `name`, `action` | Actual event name and activity type. Normalize a push's absent activity type to `""`. |
| `ref`, `sha` | Full source ref and its event commit SHA, lowercase 40-character hex. |
| `expected_revision` | Opaque snapshot revision that the caller observed before evaluating this event. Must equal current snapshot revision. This is caller state, not a GitHub event field. |
| `base_ref`, `base_sha` | Required for PRs: full base branch ref and event base commit SHA. |
| `fork`, `merged` | Required booleans for PRs. Fork PRs are always denied. Trusted callers must also classify other untrusted PR sources such as Dependabot appropriately. |

For push events use payload `ref`/`after`. Deleted refs and zero SHAs cannot produce a plan against a valid current snapshot. For PRs use the head and base repository/ref/SHA fields; **do not substitute `GITHUB_REF` or the merge-test `GITHUB_SHA`**. A missing or deleted PR head is denied when a rule requires it. For release events use `refs/tags/<tag_name>` and the tagged commit, not `target_commitish`; the current snapshot must independently resolve the tag. Subscribe to `published` only if publication is the intended transition; the examples' stable-tag regex deliberately rejects prerelease tags in its managed `v*` namespace. See GitHub's [event and ref semantics](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows) and [webhook payload reference](https://docs.github.com/en/webhooks/webhook-events-and-payloads).

The state object requires `repository`, `revision`, `phase`, `refs` (full ref → commit SHA), `applied_keys` (completed semantic event keys), and `writer` (null or the key of the current writer). The relevant ref namespace must be complete enough to detect both existing targets and prefix conflicts such as `dev` versus `dev/v1.2.3-dev.0`. The planner does not fetch or authenticate this snapshot.

## Decision and concurrency contract

Every evaluated decision carries `schema_version`, `dry_run: true`, `decision`, `reason`, `idempotency_key`, `rule`, `preconditions`, `transition`, and `operations`. Malformed JSON is a CLI denial with `reason: invalid_json`. Plans contain only `create_branch` and optional `bump_version` proposals. Noops and denials contain no operations.

The semantic event key hashes normalized event content except `expected_revision`. It is stable across workflow retries, delivery retries, state changes, and policy changes; run IDs and delivery IDs are not inputs. This conservatively treats repeated activity with identical content as the same event. Completed keys return `noop: already_applied`, even after the state has advanced. The planner never adds a completed key or advances state merely because a plan was generated. Existing target refs without a recorded completed key are denied for reconciliation, even when their SHA matches.

A plan binds the repository, revision, phase, current writer, event head/base SHAs, parent SHA, and absent target (`null`) in `preconditions`. The state revision rejects stale snapshots, event SHA comparisons reject stale events, and a different writer key returns `deny: conflicting_writer`. Missing parents, existing targets, ref namespace conflicts, and ambiguous policy are explicit denials.

These checks are **optimistic proposals, not a lock or an atomic write guarantee**. Two planners can read the same free snapshot. Any future executor must acquire exclusive ownership and atomically compare/revalidate the complete preconditions before writing, then durably advance revision/state and record the key only after success. Partial branch/bump failure needs explicit reconciliation; replay is not permission to overwrite refs. That executor and its activation need separate review and approval.

## Caller workflow boundary

The action owns deterministic evaluation. A caller workflow owns event subscriptions, trusted normalization, snapshot collection, permissions, and concurrency. Keep the prototype local until a final or release-critical hosted check is intentionally activated; this repository adds no hosted triggers and needs no secrets.

If activated for planning, use read-only `contents` permission for snapshot collection (the planner itself requires no token), no publish permission, and no persisted checkout credentials. Do not use `pull_request_target` to execute PR code or load PR-controlled policy/state. A fork denial is not a reason to grant a write token. A future transition job should serialize by repository, with `cancel-in-progress: false`, because cancelling a writer can leave partial state. Pure superseded PR validation may use a PR-specific group and cancellation. Workflow concurrency alone is not the compare-and-swap or durable replay ledger described above.

## Local evidence and limits

The test suite covers the JSON fixtures for push/PR/release, optional lanes, website patches, fork PRs, malformed Git refs and policy names, duplicate events, state/head/base staleness, unrelated branches, wrong PR bases, missing parents, occupied targets, namespace collisions, and competing writers. Additional tests cover ambiguous rules, malformed envelopes/templates, merged-only PR checks, deterministic keys, and the CLI contract.

The existing bumper is exercised with the proposed from/to values against TOML, JSON, Nix, Markdown, and an excluded lockfile. Dry-run preserves original bytes (including CRLF) and emits empty `changed-files`; normal bump behavior remains covered. The root action also requires explicit `dry-run: 'false'` before committing.

These fixtures prove local decisions and absence of source writes, not GitHub authentication, atomic remote locking, delivery completeness, runner startup cost, or hosted savings. The planner performs no Rust compilation or caching. Hosted budget has not been measured; adoption should reuse an already justified release job rather than add a branch-event job matrix.
