from copy import deepcopy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "branch-controller/examples"


def load_module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"scripts/{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


branch_policy = load_module("branch_policy")
bump = load_module("bump")


def read_json(path):
    return json.loads(path.read_text())


class BranchPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = read_json(EXAMPLES / "historical.json")
        self.event = read_json(EXAMPLES / "event.json")
        self.state = read_json(EXAMPLES / "state.json")

    def test_event_fixtures(self):
        for fixture in read_json(ROOT / "tests/fixtures/branch-events.json"):
            with self.subTest(fixture["name"]):
                policy = read_json(EXAMPLES / (fixture.get("policy", "historical") + ".json"))
                event = {**deepcopy(self.event), **fixture["event"]}
                state = {**deepcopy(self.state), **fixture["state"]}
                for ref in fixture.get("remove_refs", []):
                    del state["refs"][ref]
                state["refs"].update(fixture.get("add_refs", {}))
                key = branch_policy.decide(policy, event, self.state)["idempotency_key"]
                if state["writer"] == "$event_key":
                    state["writer"] = key
                state["applied_keys"] = [key if item == "$event_key" else item
                                         for item in state["applied_keys"]]
                before = deepcopy((policy, event, state))
                decision = branch_policy.decide(policy, event, state)
                self.assertEqual(decision["decision"], fixture["decision"], decision)
                self.assertEqual(decision["reason"], fixture["reason"], decision)
                self.assertEqual([op["type"] for op in decision["operations"]], fixture.get("operations", []))
                self.assertTrue(decision["dry_run"])
                self.assertEqual((policy, event, state), before, "planner mutated its inputs")

    def test_plan_preconditions_and_bumper_bridge(self):
        plan = branch_policy.decide(self.policy, self.event, self.state)
        self.assertEqual(plan["preconditions"], {
            "repository": "example/project", "revision": "snapshot-7", "phase": "release-development",
            "writer": None, "refs": {"refs/tags/v0.3.9": "a" * 40, "refs/heads/v0.3.9": "a" * 40,
                                      "refs/heads/dev/v0.3.9-dev.0": None}})
        self.assertEqual(plan["transition"], {"from": "release-development", "to": "development"})
        self.assertEqual(plan["operations"][1], {
            "type": "bump_version", "ref": "refs/heads/dev/v0.3.9-dev.0", "expected_sha": "a" * 40,
            "inputs": {"from": "0.3.9", "to": "0.3.9-dev.0", "dry-run": "true", "commit": "false"}})

    def test_idempotency_survives_state_transition_and_policy_edit(self):
        first = branch_policy.decide(self.policy, self.event, self.state)
        self.state["revision"] = "snapshot-8"
        self.state["phase"] = "development"
        self.state["applied_keys"].append(first["idempotency_key"])
        self.policy["rules"] = []
        replay = branch_policy.decide(self.policy, self.event, self.state)
        self.assertEqual(first["idempotency_key"], replay["idempotency_key"])
        self.assertEqual(replay["reason"], "already_applied")

    def test_snapshot_cas_conflict_between_distinct_events(self):
        first = branch_policy.decide(self.policy, self.event, self.state)
        self.state["writer"] = first["idempotency_key"]
        second_event = {**self.event, "sha": "c" * 40}
        self.state["refs"][second_event["ref"]] = second_event["sha"]
        second = branch_policy.decide(self.policy, second_event, self.state)
        self.assertNotEqual(first["idempotency_key"], second["idempotency_key"])
        self.assertEqual(second["reason"], "conflicting_writer")
        self.assertEqual(second["operations"], [])

    def test_ambiguous_rule_denied(self):
        duplicate = {**deepcopy(self.policy["rules"][-1]), "id": "overlap"}
        self.policy["rules"].append(duplicate)
        self.assertEqual(branch_policy.decide(self.policy, self.event, self.state)["reason"], "ambiguous_policy")

    def test_invalid_inputs_never_propose_operations(self):
        mutations = [
            lambda p, e, s: e.update(sha="short"),
            lambda p, e, s: e.update(sha="0" * 40),
            lambda p, e, s: s["refs"].update({"refs/heads/main": "0" * 40}),
            lambda p, e, s: e.update(schema_version=True),
            lambda p, e, s: e.update(activate=True),
            lambda p, e, s: s.update(refs=[]),
            lambda p, e, s: p["rules"][-1].update(ref_pattern="["),
            lambda p, e, s: p["rules"][-1].update(unknown="ignored?"),
            lambda p, e, s: p["rules"][-1]["create_branch"].update(target="refs/heads/{missing}"),
            lambda p, e, s: p["rules"][-1]["create_branch"].update(target="refs/heads/{version.__class__}"),
            lambda p, e, s: p["rules"][-1]["create_branch"].update(target="refs/tags/v1.0.0"),
            lambda p, e, s: p["rules"][-1]["bump"].update(to="1.0.0\ncommit=true"),
        ]
        for mutation in mutations:
            policy, event, state = deepcopy((self.policy, self.event, self.state))
            mutation(policy, event, state)
            decision = branch_policy.decide(policy, event, state)
            with self.subTest(mutation=mutation):
                self.assertEqual(decision["decision"], "deny", decision)
                self.assertEqual(decision["operations"], [])
                self.assertIsNone(decision["transition"])
        for ref in ("refs/heads/bad name", "refs/heads/x.lock", "refs/heads/.hidden", "refs/heads/x//y",
                    "refs/heads/x@{y}", "refs/heads/x\\y", "refs/heads/x?", "refs/heads/x.", "main", "refs/pull/1/merge"):
            with self.subTest(ref=ref):
                self.assertFalse(branch_policy.valid_ref(ref))

    def test_merged_predicate_and_missing_pr_context(self):
        self.policy["rules"] = [{
            "id": "merged-only", "event": "pull_request", "actions": ["closed"],
            "from_state": "release-development", "ref_pattern": "refs/heads/dev/v0.3.9-dev.6",
            "base_ref": "refs/heads/v0.3.9", "merged": True}]
        self.event.update(name="pull_request", action="closed", ref="refs/heads/dev/v0.3.9-dev.6", sha="b" * 40,
                          base_ref="refs/heads/v0.3.9", base_sha="a" * 40, fork=False, merged=False)
        self.assertEqual(branch_policy.decide(self.policy, self.event, self.state)["reason"], "no_matching_rule")
        self.event["merged"] = True
        self.assertEqual(branch_policy.decide(self.policy, self.event, self.state)["reason"], "policy_match")
        del self.event["fork"]
        self.assertEqual(branch_policy.decide(self.policy, self.event, self.state)["reason"], "invalid_input")

    def test_cli_outputs_json_and_preserves_files(self):
        before = {path: path.read_bytes() for path in EXAMPLES.glob("*.json")}
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "github-output"
            result = subprocess.run([sys.executable, str(ROOT / "scripts/branch_policy.py"),
                                     str(EXAMPLES / "historical.json"), str(EXAMPLES / "event.json"),
                                     str(EXAMPLES / "state.json")], capture_output=True, text=True,
                                    env={**os.environ, "GITHUB_OUTPUT": str(output)}, check=True)
            plan = json.loads(result.stdout)
            self.assertEqual(plan["decision"], "plan")
            self.assertEqual(output.read_text().splitlines(), ["decision=plan", "plan=" + result.stdout.strip()])
            self.assertEqual(list(Path(tmp).iterdir()), [output])
        self.assertEqual(before, {path: path.read_bytes() for path in EXAMPLES.glob("*.json")})

    def test_cli_deny_is_nonzero_and_invalid_json_is_structured(self):
        with tempfile.TemporaryDirectory() as tmp:
            invalid = Path(tmp) / "invalid.json"
            invalid.write_text("{")
            result = subprocess.run([sys.executable, str(ROOT / "scripts/branch_policy.py"),
                                     str(invalid), str(EXAMPLES / "event.json"), str(EXAMPLES / "state.json")],
                                    env={key: value for key, value in os.environ.items() if key != "GITHUB_OUTPUT"},
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(json.loads(result.stdout)["reason"], "invalid_json")


class ExistingBumperDryRunTests(unittest.TestCase):
    def test_proposed_bump_preserves_bytes_and_emits_no_changed_files(self):
        plan = branch_policy.decide(read_json(EXAMPLES / "historical.json"), read_json(EXAMPLES / "event.json"),
                                    read_json(EXAMPLES / "state.json"))
        inputs = plan["operations"][1]["inputs"]
        files = {"Cargo.toml": b'[package]\r\nname = "example"\r\nversion = "0.3.9"\r\n',
                 "README.md": b"Current: **0.3.9**\n", "package.json": b'{"version": "0.3.9"}\n',
                 "example.nix": b'{ version = "0.3.9"; }\n', "Cargo.lock": b'version = "0.3.9"\n'}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, content in files.items():
                (root / name).write_bytes(content)
            output = root / "github-output"
            with patch.dict(os.environ, {"BUMPER_FROM": inputs["from"], "BUMPER_TO": inputs["to"],
                                         "BUMPER_DRY_RUN": inputs["dry-run"], "BUMPER_INCLUDES": "*",
                                         "BUMPER_EXCLUDES": "*.lock", "GITHUB_OUTPUT": str(output)}), \
                    patch.object(Path, "cwd", return_value=root), redirect_stdout(io.StringIO()) as log:
                bump.main()
            self.assertIn("[dry-run] would modify:", log.getvalue())
            self.assertEqual(output.read_text(), "changed_files<<EOF\n\nEOF\n")
            for name, content in files.items():
                self.assertEqual((root / name).read_bytes(), content)

    def test_normal_bump_still_writes_and_reports_changed_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cargo = root / "Cargo.toml"
            cargo.write_text('[package]\nversion = "0.3.9"\n')
            output = root / "github-output"
            with patch.dict(os.environ, {"BUMPER_FROM": "0.3.9", "BUMPER_TO": "0.3.10",
                                         "BUMPER_DRY_RUN": "false", "BUMPER_INCLUDES": "Cargo.toml",
                                         "GITHUB_OUTPUT": str(output)}), patch.object(Path, "cwd", return_value=root), \
                    redirect_stdout(io.StringIO()):
                bump.main()
            self.assertIn('version = "0.3.10"', cargo.read_text())
            self.assertEqual(output.read_text(), "changed_files<<EOF\nCargo.toml\nEOF\n")

    def test_root_action_commit_requires_explicit_non_dry_run(self):
        action = (ROOT / "action.yml").read_text()
        self.assertIn("if: inputs.commit == 'true' && inputs.dry-run == 'false' && steps.bump.outputs.changed_files != ''", action)


if __name__ == "__main__":
    unittest.main()
