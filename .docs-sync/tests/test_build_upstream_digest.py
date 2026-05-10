"""Tests for .docs-sync/digest/build_upstream_digest.py (krkn-hub variant).

Each krkn-hub scenario is a top-level directory with:
  - env.sh             — bash defaults including SCENARIO_TYPE
  - krknctl-input.json — rich param schema (name/variable/type/default/required/description)
  - run.sh, Dockerfile.template, etc.

Output: .docs-sync-digest/llms.txt (index) + llms-full.txt (per-scenario detail) +
         digest.sha (sha256 of relevant source files for cache invalidation).
"""
import json
from pathlib import Path
from textwrap import dedent

import pytest

from digest.build_upstream_digest import (
    is_scenario_dir,
    parse_scenario_type,
    parse_krknctl_input,
    extract_scenario_metadata,
    discover_scenarios,
    render_llms_txt,
    render_llms_full_txt,
    compute_digest_sha,
    build_upstream_digest,
)


# ─────────────────────────────────────────────────────────────────────────────
# is_scenario_dir
# ─────────────────────────────────────────────────────────────────────────────

class TestIsScenarioDir:
    def test_dir_with_env_and_input_json_is_scenario(self, tmp_path: Path):
        d = tmp_path / "pod-scenarios"
        d.mkdir()
        (d / "env.sh").write_text("export SCENARIO_TYPE=foo\n")
        (d / "krknctl-input.json").write_text("[]")
        (d / "run.sh").write_text("#!/bin/bash\n")
        assert is_scenario_dir(d) is True

    def test_dir_without_env_sh_is_not_scenario(self, tmp_path: Path):
        d = tmp_path / "docs"
        d.mkdir()
        (d / "krknctl-input.json").write_text("[]")
        assert is_scenario_dir(d) is False

    def test_dir_without_krknctl_input_is_not_scenario(self, tmp_path: Path):
        # Some dirs might have env.sh for other reasons; require both
        d = tmp_path / "rollback"
        d.mkdir()
        (d / "env.sh").write_text("foo")
        assert is_scenario_dir(d) is False

    def test_hidden_dir_excluded(self, tmp_path: Path):
        d = tmp_path / ".github"
        d.mkdir()
        (d / "env.sh").write_text("foo")
        (d / "krknctl-input.json").write_text("[]")
        # Even with both files, a hidden dir is not a scenario
        assert is_scenario_dir(d) is False


# ─────────────────────────────────────────────────────────────────────────────
# parse_scenario_type — read SCENARIO_TYPE from env.sh
# ─────────────────────────────────────────────────────────────────────────────

class TestParseScenarioType:
    def test_extracts_default_from_export(self, tmp_path: Path):
        f = tmp_path / "env.sh"
        f.write_text(dedent("""\
            #!/bin/bash
            export NAMESPACE=${NAMESPACE:="openshift-.*"}
            export SCENARIO_TYPE=${SCENARIO_TYPE:=pod_disruption_scenarios}
            """))
        assert parse_scenario_type(f) == "pod_disruption_scenarios"

    def test_returns_none_when_no_scenario_type(self, tmp_path: Path):
        f = tmp_path / "env.sh"
        f.write_text("export NAMESPACE=foo\n")
        assert parse_scenario_type(f) is None

    def test_handles_quoted_default(self, tmp_path: Path):
        f = tmp_path / "env.sh"
        f.write_text('export SCENARIO_TYPE=${SCENARIO_TYPE:="cluster_shut_down_scenarios"}\n')
        assert parse_scenario_type(f) == "cluster_shut_down_scenarios"

    def test_handles_single_quoted_default(self, tmp_path: Path):
        f = tmp_path / "env.sh"
        f.write_text("export SCENARIO_TYPE=${SCENARIO_TYPE:='pod_scenarios'}\n")
        assert parse_scenario_type(f) == "pod_scenarios"

    # === Slice 0b inspection finding U1 — direct assignment form ===

    def test_handles_direct_assignment_double_quoted(self, tmp_path: Path):
        # Some env.sh files use direct assignment instead of bash-default form.
        # Earned from real-corpus inspection: node-scenarios-bm and
        # service-hijacking use this form, were marked (unknown) in run 1.
        f = tmp_path / "env.sh"
        f.write_text('export SCENARIO_TYPE="node_scenarios"\n')
        assert parse_scenario_type(f) == "node_scenarios"

    def test_handles_direct_assignment_single_quoted(self, tmp_path: Path):
        f = tmp_path / "env.sh"
        f.write_text("export SCENARIO_TYPE='service_hijacking_scenarios'\n")
        assert parse_scenario_type(f) == "service_hijacking_scenarios"

    def test_handles_direct_assignment_unquoted(self, tmp_path: Path):
        f = tmp_path / "env.sh"
        f.write_text("export SCENARIO_TYPE=container_scenarios\n")
        assert parse_scenario_type(f) == "container_scenarios"


# ─────────────────────────────────────────────────────────────────────────────
# parse_krknctl_input
# ─────────────────────────────────────────────────────────────────────────────

class TestParseKrknctlInput:
    def test_extracts_param_list(self, tmp_path: Path):
        f = tmp_path / "krknctl-input.json"
        f.write_text(json.dumps([
            {"name": "namespace", "variable": "NAMESPACE", "type": "string",
             "default": "openshift-*", "required": "false",
             "description": "Targeted namespace"},
            {"name": "kill-count", "variable": "KILL_COUNT", "type": "number",
             "default": "1", "required": "false",
             "description": "Number to kill"},
        ]))
        params = parse_krknctl_input(f)
        assert len(params) == 2
        assert params[0]["name"] == "namespace"
        assert params[0]["variable"] == "NAMESPACE"
        assert params[1]["default"] == "1"

    def test_required_normalized_to_bool(self, tmp_path: Path):
        # Source uses string "true"/"false"; normalize to actual bools so
        # downstream consumers don't have to remember.
        f = tmp_path / "krknctl-input.json"
        f.write_text(json.dumps([
            {"name": "x", "variable": "X", "type": "string",
             "default": "", "required": "true", "description": "."},
            {"name": "y", "variable": "Y", "type": "string",
             "default": "", "required": "false", "description": "."},
        ]))
        params = parse_krknctl_input(f)
        assert params[0]["required"] is True
        assert params[1]["required"] is False

    def test_handles_missing_optional_fields(self, tmp_path: Path):
        # Real-world JSON might omit some keys. Don't crash.
        f = tmp_path / "krknctl-input.json"
        f.write_text(json.dumps([
            {"name": "x", "variable": "X", "type": "string"},  # no default/required/description
        ]))
        params = parse_krknctl_input(f)
        assert params[0]["name"] == "x"
        # Missing fields get sensible empty defaults
        assert params[0].get("default", "") == ""
        assert params[0].get("description", "") == ""

    def test_returns_empty_for_empty_array(self, tmp_path: Path):
        f = tmp_path / "krknctl-input.json"
        f.write_text("[]")
        assert parse_krknctl_input(f) == []

    def test_returns_empty_on_malformed_json(self, tmp_path: Path):
        # Be defensive — don't fail the whole digest because one scenario has
        # broken JSON. Log and skip.
        f = tmp_path / "krknctl-input.json"
        f.write_text("{not json")
        assert parse_krknctl_input(f) == []


# ─────────────────────────────────────────────────────────────────────────────
# extract_scenario_metadata — combines env.sh + krknctl-input.json
# ─────────────────────────────────────────────────────────────────────────────

class TestExtractScenarioMetadata:
    def _make(self, root: Path, name: str, scenario_type: str, params: list):
        d = root / name
        d.mkdir(parents=True)
        (d / "env.sh").write_text(
            f"#!/bin/bash\nexport SCENARIO_TYPE=${{SCENARIO_TYPE:={scenario_type}}}\n"
        )
        (d / "krknctl-input.json").write_text(json.dumps(params))
        (d / "run.sh").write_text("#!/bin/bash\n")
        return d

    def test_returns_dict_with_name_type_params(self, tmp_path: Path):
        d = self._make(tmp_path, "pod-scenarios", "pod_disruption_scenarios", [
            {"name": "ns", "variable": "NS", "type": "string",
             "default": "default", "required": "false", "description": "."},
        ])
        meta = extract_scenario_metadata(d)
        assert meta["name"] == "pod-scenarios"
        assert meta["scenario_type"] == "pod_disruption_scenarios"
        assert len(meta["parameters"]) == 1


# ─────────────────────────────────────────────────────────────────────────────
# discover_scenarios — walk the repo
# ─────────────────────────────────────────────────────────────────────────────

class TestDiscoverScenarios:
    def test_returns_only_scenario_dirs_sorted(self, tmp_path: Path):
        # Real scenario
        (tmp_path / "pod-scenarios").mkdir()
        (tmp_path / "pod-scenarios/env.sh").write_text("export SCENARIO_TYPE=pod\n")
        (tmp_path / "pod-scenarios/krknctl-input.json").write_text("[]")
        # Real scenario, alphabetically first
        (tmp_path / "container-scenarios").mkdir()
        (tmp_path / "container-scenarios/env.sh").write_text("export SCENARIO_TYPE=ct\n")
        (tmp_path / "container-scenarios/krknctl-input.json").write_text("[]")
        # NOT a scenario (just docs)
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs/index.md").write_text("# Docs")
        # NOT a scenario (only env.sh, no JSON)
        (tmp_path / "rollback").mkdir()
        (tmp_path / "rollback/env.sh").write_text("foo")
        # Hidden — skip
        (tmp_path / ".github").mkdir()
        (tmp_path / ".github/env.sh").write_text("foo")
        (tmp_path / ".github/krknctl-input.json").write_text("[]")

        result = discover_scenarios(tmp_path)
        names = [m["name"] for m in result]
        assert names == ["container-scenarios", "pod-scenarios"]


# ─────────────────────────────────────────────────────────────────────────────
# render_llms_txt — index format
# ─────────────────────────────────────────────────────────────────────────────

class TestRenderLlmsTxt:
    def test_includes_repo_name_and_per_scenario_line(self):
        scenarios = [
            {"name": "pod-scenarios", "scenario_type": "pod_disruption_scenarios",
             "parameters": [{"name": "x"}]},
        ]
        out = render_llms_txt(scenarios, repo_name="krkn-hub")
        assert "# krkn-hub" in out
        assert "pod-scenarios" in out
        assert "pod_disruption_scenarios" in out

    def test_scenarios_sorted(self):
        scenarios = [
            {"name": "z-thing", "scenario_type": "z", "parameters": []},
            {"name": "a-thing", "scenario_type": "a", "parameters": []},
        ]
        out = render_llms_txt(scenarios, repo_name="x")
        a_pos = out.find("a-thing")
        z_pos = out.find("z-thing")
        assert a_pos < z_pos


# ─────────────────────────────────────────────────────────────────────────────
# render_llms_full_txt — full structured per-scenario
# ─────────────────────────────────────────────────────────────────────────────

class TestRenderLlmsFullTxt:
    def test_includes_parameter_table_per_scenario(self):
        scenarios = [
            {
                "name": "pod-scenarios",
                "scenario_type": "pod_disruption_scenarios",
                "parameters": [
                    {"name": "namespace", "variable": "NAMESPACE",
                     "type": "string", "default": "openshift-*",
                     "required": False, "description": "Target ns"},
                ],
            },
        ]
        out = render_llms_full_txt(scenarios, repo_name="krkn-hub")
        assert "## scenario: pod-scenarios" in out
        assert "scenario_type: pod_disruption_scenarios" in out
        assert "namespace" in out
        assert "NAMESPACE" in out
        assert "Target ns" in out

    def test_handles_scenario_with_no_parameters(self):
        scenarios = [{"name": "x", "scenario_type": "x_s", "parameters": []}]
        out = render_llms_full_txt(scenarios, repo_name="x")
        # Doesn't crash; section header still present
        assert "## scenario: x" in out


# ─────────────────────────────────────────────────────────────────────────────
# compute_digest_sha — for cache invalidation
# ─────────────────────────────────────────────────────────────────────────────

class TestComputeDigestSha:
    def test_deterministic(self, tmp_path: Path):
        (tmp_path / "x").mkdir()
        (tmp_path / "x/env.sh").write_text("foo")
        (tmp_path / "x/krknctl-input.json").write_text("[]")
        sha1 = compute_digest_sha([tmp_path / "x"])
        sha2 = compute_digest_sha([tmp_path / "x"])
        assert sha1 == sha2

    def test_changes_when_content_changes(self, tmp_path: Path):
        (tmp_path / "x").mkdir()
        (tmp_path / "x/env.sh").write_text("foo")
        (tmp_path / "x/krknctl-input.json").write_text("[]")
        sha1 = compute_digest_sha([tmp_path / "x"])

        (tmp_path / "x/env.sh").write_text("foo modified")
        sha2 = compute_digest_sha([tmp_path / "x"])
        assert sha1 != sha2

    def test_independent_of_directory_iteration_order(self, tmp_path: Path):
        # Same set of files, different order in input list → same sha.
        (tmp_path / "a").mkdir()
        (tmp_path / "a/env.sh").write_text("a")
        (tmp_path / "a/krknctl-input.json").write_text("[]")
        (tmp_path / "b").mkdir()
        (tmp_path / "b/env.sh").write_text("b")
        (tmp_path / "b/krknctl-input.json").write_text("[]")
        sha_ab = compute_digest_sha([tmp_path / "a", tmp_path / "b"])
        sha_ba = compute_digest_sha([tmp_path / "b", tmp_path / "a"])
        assert sha_ab == sha_ba


# ─────────────────────────────────────────────────────────────────────────────
# build_upstream_digest — full integration
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildUpstreamDigest:
    def _make_scenario(self, root: Path, name: str, scenario_type: str, params: list):
        d = root / name
        d.mkdir(parents=True)
        (d / "env.sh").write_text(
            f"export SCENARIO_TYPE=${{SCENARIO_TYPE:={scenario_type}}}\n"
        )
        (d / "krknctl-input.json").write_text(json.dumps(params))
        (d / "run.sh").write_text("#!/bin/bash\n")

    def test_writes_three_files(self, tmp_path: Path):
        self._make_scenario(tmp_path, "pod-scenarios", "pod_disruption_scenarios", [
            {"name": "ns", "variable": "NS", "type": "string",
             "default": "default", "required": "false", "description": "."},
        ])
        out = tmp_path / ".docs-sync-digest"

        build_upstream_digest(repo_root=tmp_path, output_dir=out, repo_name="krkn-hub")

        assert (out / "llms.txt").exists()
        assert (out / "llms-full.txt").exists()
        assert (out / "digest.sha").exists()

    def test_idempotent(self, tmp_path: Path):
        self._make_scenario(tmp_path, "pod-scenarios", "pod_d_s", [])
        out = tmp_path / ".docs-sync-digest"

        build_upstream_digest(tmp_path, out, "krkn-hub")
        first = (out / "llms-full.txt").read_text()

        build_upstream_digest(tmp_path, out, "krkn-hub")
        second = (out / "llms-full.txt").read_text()

        assert first == second
