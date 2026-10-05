#!/usr/bin/env python3
"""Pure branch-policy decisions. The CLI only reads JSON and emits a dry-run plan."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sys


EVENTS = {"push", "pull_request", "release"}
SHA = re.compile(r"[0-9a-f]{40}")
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def object_keys(value: object, required: set[str], optional: set[str] = frozenset()) -> None:
    require(isinstance(value, dict), "expected a JSON object")
    require(required <= value.keys() <= required | optional, "missing or unknown fields")


def text(value: object) -> bool:
    return isinstance(value, str) and bool(value) and len(value) <= 512


def valid_sha(value: object) -> bool:
    return isinstance(value, str) and SHA.fullmatch(value) is not None and value != "0" * 40


def valid_ref(value: object) -> bool:
    """Accept full branch/tag refs, excluding Git's forbidden ref syntax."""
    return (
        text(value)
        and value.startswith(("refs/heads/", "refs/tags/"))
        and not re.search(r"[\x00-\x20\x7f~^:?*\[\\]", value)
        and ".." not in value and "@{" not in value and not value.endswith(".")
        and all(part and not part.startswith(".") and not part.endswith(".lock")
                for part in value.split("/"))
    )


def render(template: str, groups: dict[str, str]) -> str:
    """Named captures only; no expressions, attribute access, or shell expansion."""
    def replace(match: re.Match) -> str:
        name = match.group(1)
        require(name in groups and groups[name] is not None, "missing template capture")
        return groups[name]

    require(text(template), "empty template")
    rendered = re.sub(r"\{([A-Za-z_][A-Za-z_0-9]*)\}", replace, template)
    require("{" not in rendered and "}" not in rendered, "unsupported template syntax")
    return rendered


def validate(policy: dict, event: dict, state: dict) -> None:
    object_keys(policy, {"schema_version", "rules"}, {"scopes"})
    object_keys(event, {"schema_version", "repository", "name", "action", "ref", "sha",
                        "expected_revision"}, {"base_ref", "base_sha", "fork", "merged"})
    object_keys(state, {"schema_version", "repository", "revision", "phase", "refs",
                        "applied_keys", "writer"})
    require(all(type(doc["schema_version"]) is int and doc["schema_version"] == 1
                for doc in (policy, event, state)), "unsupported schema_version")
    require(text(event["repository"]) and event["repository"].count("/") == 1,
            "invalid repository")
    require(text(event["name"]) and isinstance(event["action"], str), "invalid event")
    require(valid_ref(event["ref"]), "invalid event ref")
    require(valid_sha(event["sha"]), "invalid event sha")
    require(text(event["expected_revision"]) and text(state["revision"]) and text(state["phase"]),
            "invalid state revision or phase")
    require(isinstance(state["refs"], dict), "invalid refs snapshot")
    require(all(valid_ref(ref) and valid_sha(sha)
                for ref, sha in state["refs"].items()), "invalid refs snapshot")
    require(isinstance(state["applied_keys"], list)
            and all(text(key) for key in state["applied_keys"]), "invalid applied_keys")
    require(state["writer"] is None or text(state["writer"]), "invalid writer")
    if event["name"] == "pull_request":
        require(all(key in event for key in ("base_ref", "base_sha", "fork", "merged")),
                "incomplete pull_request context")
        require(event["ref"].startswith("refs/heads/") and valid_ref(event["base_ref"])
                and event["base_ref"].startswith("refs/heads/"), "invalid pull_request refs")
        require(valid_sha(event["base_sha"]),
                "invalid pull_request base_sha")
        require(type(event["fork"]) is bool and type(event["merged"]) is bool,
                "invalid pull_request trust or merged flag")
    else:
        require(not {"base_ref", "base_sha", "fork", "merged"} & event.keys(),
                "pull_request fields on another event")
    if event["name"] == "release":
        require(event["ref"].startswith("refs/tags/"), "release requires a tag ref")
    require(isinstance(policy["rules"], list), "rules must be a list")
    require(isinstance(policy.get("scopes", []), list), "scopes must be a list")
    for scope in policy.get("scopes", []):
        object_keys(scope, {"event", "actions", "ref_pattern"})
        require(scope["event"] in EVENTS and isinstance(scope["actions"], list)
                and bool(scope["actions"]) and all(isinstance(action, str) for action in scope["actions"])
                and text(scope["ref_pattern"]), "invalid scope")
        re.compile(scope["ref_pattern"])
    ids = set()
    for rule in policy["rules"]:
        object_keys(rule, {"id", "event", "actions", "from_state", "ref_pattern"},
                    {"base_ref", "merged", "create_branch", "bump", "to_state"})
        require(text(rule["id"]) and rule["id"] not in ids, "invalid or duplicate rule id")
        ids.add(rule["id"])
        require(rule["event"] in EVENTS and text(rule["from_state"]), "invalid rule event/state")
        require(isinstance(rule["actions"], list) and bool(rule["actions"])
                and all(isinstance(action, str) for action in rule["actions"]), "invalid actions")
        require(text(rule["ref_pattern"]), "invalid ref_pattern")
        re.compile(rule["ref_pattern"])
        if "base_ref" in rule or "merged" in rule:
            require(rule["event"] == "pull_request", "PR predicate on another event")
        if "base_ref" in rule:
            require(text(rule["base_ref"]), "invalid base_ref template")
        if "merged" in rule:
            require(type(rule["merged"]) is bool, "invalid merged predicate")
        if "create_branch" in rule:
            object_keys(rule["create_branch"], {"parent", "target"})
            require(all(text(value) for value in rule["create_branch"].values()),
                    "invalid branch templates")
            require(text(rule.get("to_state")), "transition requires to_state")
        else:
            require("to_state" not in rule and "bump" not in rule,
                    "check rules cannot transition or bump")
        if "bump" in rule:
            object_keys(rule["bump"], {"from", "to"})
            require(all(text(value) for value in rule["bump"].values()), "invalid bump templates")


def decide(policy: dict, event: dict, state: dict) -> dict:
    """Return a plan/noop/deny without changing inputs, files, refs, or state."""
    result = {"schema_version": 1, "dry_run": True, "decision": "deny", "reason": "invalid_input",
              "idempotency_key": None, "rule": None, "preconditions": {},
              "transition": None, "operations": []}

    def finish(decision: str, reason: str) -> dict:
        return {**result, "decision": decision, "reason": reason}

    try:
        validate(policy, event, state)
        # Deliberately independent of delivery ID, run attempt, policy, and snapshot revision.
        identity = {key: value for key, value in event.items() if key != "expected_revision"}
        key = "branch-policy-v1:" + hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        result["idempotency_key"] = key
        if event["repository"] != state["repository"]:
            return finish("deny", "repository_mismatch")
        if event["name"] not in EVENTS:
            return finish("noop", "unsupported_event")
        if event.get("fork"):
            return finish("deny", "fork_pull_request")
        if key in state["applied_keys"]:
            return finish("noop", "already_applied")
        if event["expected_revision"] != state["revision"]:
            return finish("deny", "stale_state")
        if state["writer"] not in (None, key):
            return finish("deny", "conflicting_writer")
        candidates = []
        for rule in policy["rules"]:
            if rule["event"] != event["name"] or event["action"] not in rule["actions"]:
                continue
            match = re.fullmatch(rule["ref_pattern"], event["ref"])
            if match and ("merged" not in rule or rule["merged"] == event.get("merged")):
                candidates.append((rule, match.groupdict()))
        if not candidates:
            if any(scope["event"] == event["name"] and event["action"] in scope["actions"]
                   and re.fullmatch(scope["ref_pattern"], event["ref"])
                   for scope in policy.get("scopes", [])):
                return finish("deny", "disallowed_ref")
            return finish("noop", "no_matching_rule")
        candidates = [(rule, groups) for rule, groups in candidates
                      if rule["from_state"] == state["phase"]]
        if not candidates:
            return finish("deny", "unexpected_state")
        candidates = [(rule, groups) for rule, groups in candidates if "base_ref" not in rule
                      or render(rule["base_ref"], groups) == event.get("base_ref")]
        if not candidates:
            return finish("deny", "wrong_base")
        if len(candidates) != 1:
            return finish("deny", "ambiguous_policy")
        rule, groups = candidates[0]
        result["rule"] = rule["id"]
        expected_refs = {event["ref"]: event["sha"]}
        if event["name"] == "pull_request":
            if event["ref"] == event["base_ref"]:
                return finish("deny", "same_pr_refs")
            expected_refs[event["base_ref"]] = event["base_sha"]
        if any(state["refs"].get(ref) != sha for ref, sha in expected_refs.items()):
            return finish("deny", "stale_event")
        result["preconditions"] = {"repository": state["repository"], "revision": state["revision"],
                                   "phase": state["phase"], "writer": state["writer"],
                                   "refs": expected_refs}
        if "create_branch" not in rule:
            return finish("noop", "policy_match")
        parent = render(rule["create_branch"]["parent"], groups)
        target = render(rule["create_branch"]["target"], groups)
        require(valid_ref(parent) and valid_ref(target) and target.startswith("refs/heads/"),
                "invalid proposed branch refs")
        if parent not in state["refs"]:
            return finish("deny", "missing_parent")
        if state["refs"][parent] != event["sha"]:
            return finish("deny", "parent_event_mismatch")
        if target in state["refs"]:
            return finish("deny", "target_exists")
        # An absent ref cannot be created below/above an existing branch ref.
        if any(ref.startswith(target + "/") or target.startswith(ref + "/")
               for ref in state["refs"]):
            return finish("deny", "ref_namespace_conflict")
        expected_refs[parent] = state["refs"][parent]
        expected_refs[target] = None
        operations = [{"type": "create_branch", "ref": target, "parent_ref": parent,
                       "sha": state["refs"][parent]}]
        if "bump" in rule:
            bump = {name: render(template, groups) for name, template in rule["bump"].items()}
            require(all(VERSION.fullmatch(value) for value in bump.values())
                    and bump["from"] != bump["to"], "invalid bump versions")
            operations.append({"type": "bump_version", "ref": target, "expected_sha": state["refs"][parent],
                               "inputs": {**bump, "dry-run": "true", "commit": "false"}})
        result["operations"] = operations
        result["transition"] = {"from": state["phase"], "to": rule["to_state"]}
        return finish("plan", "transition_proposed")
    except (ValueError, TypeError, KeyError, re.error) as exc:
        # A malformed envelope/policy must never turn into a permissive match.
        result["operations"] = []
        result["transition"] = None
        return {**finish("deny", "invalid_input"), "detail": str(exc)}


def main() -> int:
    if len(sys.argv) != 4:
        print("usage: branch_policy.py POLICY.json EVENT.json STATE.json", file=sys.stderr)
        return 2
    try:
        documents = [json.loads(Path(path).read_text(encoding="utf-8")) for path in sys.argv[1:]]
        decision = decide(*documents)
    except (OSError, ValueError) as exc:
        decision = {"schema_version": 1, "dry_run": True, "decision": "deny", "reason": "invalid_json",
                    "detail": str(exc), "operations": []}
    encoded = json.dumps(decision, sort_keys=True, separators=(",", ":"))
    print(encoded)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write(f"decision={decision['decision']}\nplan={encoded}\n")
    return 1 if decision["decision"] == "deny" else 0


if __name__ == "__main__":
    sys.exit(main())
