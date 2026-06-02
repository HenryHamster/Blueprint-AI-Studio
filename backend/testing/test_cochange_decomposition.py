"""Tests for cochange_decomposition.py clustering and auto-decomposition logic.

Covers:
  (a) cluster_files_by_cochange — affinity-based union-find grouping
  (b) cluster_files_by_toplevel_dir — fallback top-level-dir grouping
  (c) cluster_files_by_subdir — third-tier subdir grouping
  (d) split_oversized_clusters_by_subdir — second-pass split for oversized clusters
  (e) auto_decompose_findings_by_cochange — full decomposition including preamble
      content (SCOPE_EMPTY guidance, cluster numbering, scope calibration)
"""

from __future__ import annotations

from app.cochange_decomposition import (
    auto_decompose_findings_by_cochange,
    cluster_files_by_cochange,
    cluster_files_by_subdir,
    cluster_files_by_toplevel_dir,
    split_oversized_clusters_by_subdir,
)


# ---------------------------------------------------------------------------
# cluster_files_by_cochange
# ---------------------------------------------------------------------------


class TestClusterFilesByCochange:
    def test_returns_none_for_single_file(self):
        assert cluster_files_by_cochange(["a.py"], {"a.py": {"b.py": 1.0}}) is None

    def test_returns_none_when_no_cochange_index(self):
        assert cluster_files_by_cochange(["a.py", "b.py"], {}) is None

    def test_returns_none_when_files_have_no_affinity(self):
        # Files are present but have no matching affinities
        result = cluster_files_by_cochange(["a.py", "b.py"], {"c.py": {"d.py": 1.0}})
        assert result is None

    def test_two_files_with_affinity_form_one_cluster(self):
        result = cluster_files_by_cochange(["a.py", "b.py"], {"a.py": {"b.py": 1.0}})
        assert result is not None
        assert len(result) == 1
        assert sorted(result[0]) == ["a.py", "b.py"]

    def test_files_below_threshold_skip_decomposition(self):
        # Below-threshold affinity merges nothing, so every file would be its own
        # singleton — that is the over-spawn bug (one task per file), not a valid
        # split. The function returns None (no real co-change structure) so the
        # finding stays a single coherent task.
        result = cluster_files_by_cochange(
            ["a.py", "b.py"], {"a.py": {"b.py": 0.5}}, threshold=1.0
        )
        assert result is None

    def test_three_files_two_clusters(self):
        cochange = {
            "a.py": {"b.py": 1.0},
            "b.py": {"a.py": 1.0},
        }
        result = cluster_files_by_cochange(["a.py", "b.py", "c.py"], cochange)
        assert result is not None
        assert len(result) == 2
        cluster_sets = [frozenset(c) for c in result]
        assert frozenset(["a.py", "b.py"]) in cluster_sets
        assert frozenset(["c.py"]) in cluster_sets

    def test_reverse_direction_affinity_merges_clusters(self):
        # b.py -> a.py affinity (reverse direction) should still union them
        cochange = {"b.py": {"a.py": 1.0}}
        result = cluster_files_by_cochange(["a.py", "b.py", "c.py"], cochange)
        assert result is not None
        cluster_sets = [frozenset(c) for c in result]
        assert frozenset(["a.py", "b.py"]) in cluster_sets


# ---------------------------------------------------------------------------
# cluster_files_by_toplevel_dir
# ---------------------------------------------------------------------------


class TestClusterFilesByToplevelDir:
    def test_returns_none_for_fewer_than_3_files(self):
        assert cluster_files_by_toplevel_dir(["backend/a.py", "agent/b.ts"]) is None

    def test_returns_none_when_fewer_than_3_dirs(self):
        files = ["backend/a.py", "backend/b.py", "backend/c.py"]
        assert cluster_files_by_toplevel_dir(files) is None

    def test_root_level_files_excluded_from_span(self):
        # root-level files don't establish a dir span
        files = ["root.py", "backend/a.py", "agent/b.ts"]
        assert cluster_files_by_toplevel_dir(files) is None

    def test_three_dirs_produces_three_clusters(self):
        files = [
            "backend/a.py",
            "agent/b.ts",
            "admin-ui/c.tsx",
        ]
        result = cluster_files_by_toplevel_dir(files)
        assert result is not None
        assert len(result) == 3

    def test_preserves_file_order_within_clusters(self):
        files = [
            "backend/a.py",
            "backend/b.py",
            "agent/c.ts",
            "agent/d.ts",
            "admin-ui/e.tsx",
        ]
        result = cluster_files_by_toplevel_dir(files)
        assert result is not None
        assert result[0] == ["backend/a.py", "backend/b.py"]
        assert result[1] == ["agent/c.ts", "agent/d.ts"]
        assert result[2] == ["admin-ui/e.tsx"]


# ---------------------------------------------------------------------------
# cluster_files_by_subdir
# ---------------------------------------------------------------------------


class TestClusterFilesBySubdir:
    def test_returns_none_for_fewer_than_3_files(self):
        assert (
            cluster_files_by_subdir(["backend/app/a.py", "backend/testing/b.py"])
            is None
        )

    def test_returns_none_for_multiple_top_level_dirs(self):
        files = [
            "backend/app/a.py",
            "agent/src/b.ts",
            "backend/testing/c.py",
        ]
        assert cluster_files_by_subdir(files) is None

    def test_returns_none_when_only_one_subdir(self):
        files = [
            "backend/app/a.py",
            "backend/app/b.py",
            "backend/app/c.py",
        ]
        assert cluster_files_by_subdir(files) is None

    def test_two_subdirs_produces_two_clusters(self):
        files = [
            "backend/app/a.py",
            "backend/app/b.py",
            "backend/testing/c.py",
        ]
        result = cluster_files_by_subdir(files)
        assert result is not None
        assert len(result) == 2
        assert result[0] == ["backend/app/a.py", "backend/app/b.py"]
        assert result[1] == ["backend/testing/c.py"]


# ---------------------------------------------------------------------------
# split_oversized_clusters_by_subdir
# ---------------------------------------------------------------------------


class TestSplitOversizedClustersBySubdir:
    def test_returns_none_when_no_oversized_clusters(self):
        clusters = [["a.py", "b.py"], ["c.py"]]
        assert split_oversized_clusters_by_subdir(clusters, small_budget=6) is None

    def test_returns_none_when_root_level_file_in_oversized_cluster(self):
        # A file with no slash prevents splitting
        oversized = [f"root_file_{i}.py" for i in range(8)]
        result = split_oversized_clusters_by_subdir([oversized], small_budget=6)
        assert result is None

    def test_splits_oversized_cluster_by_subdir(self):
        oversized = [
            "backend/app/a.py",
            "backend/app/b.py",
            "backend/app/c.py",
            "backend/app/d.py",
            "backend/testing/e.py",
            "backend/testing/f.py",
            "backend/testing/g.py",
        ]
        result = split_oversized_clusters_by_subdir([oversized], small_budget=6)
        assert result is not None
        assert len(result) == 2

    def test_returns_none_when_split_produces_no_fit(self):
        # All sub-clusters still exceed budget
        oversized = [f"backend/app/f{i}.py" for i in range(8)]
        # All in same subdir → only one group → no split
        result = split_oversized_clusters_by_subdir([oversized], small_budget=6)
        assert result is None


# ---------------------------------------------------------------------------
# auto_decompose_findings_by_cochange — preamble and decomposition logic
# ---------------------------------------------------------------------------


def _make_hash_fn():
    import hashlib

    def _hash(title: str, description: str) -> str:
        return hashlib.sha1(f"{title}:{description}".encode()).hexdigest()[:12]

    return _hash


class TestAutoDecomposeFindingsByCochange:
    def _decompose(self, finding, cochange_index=None, task_context=None):
        fhash = "abc123"
        results = auto_decompose_findings_by_cochange(
            [(0, finding, fhash)],
            cochange_index or {},
            task_context,
            _make_hash_fn(),
        )
        return results

    def test_small_scope_finding_not_decomposed(self):
        finding = {
            "title": "Fix X",
            "scope": "small",
            "related_files": ["a.py", "b.py"],
            "prompt": "Do the thing.",
        }
        results = self._decompose(finding)
        assert len(results) == 1
        assert results[0][1]["scope"] == "small"

    def test_medium_with_single_cluster_not_decomposed(self):
        cochange = {"a.py": {"b.py": 1.0, "c.py": 1.0}}
        finding = {
            "title": "Fix X",
            "scope": "medium",
            "related_files": ["a.py", "b.py", "c.py"],
            "prompt": "Do the thing.",
        }
        results = self._decompose(finding, cochange)
        # All files co-change together — stays as one finding
        assert len(results) == 1

    def test_medium_splits_into_two_clusters(self):
        cochange = {"a.py": {"b.py": 1.0}}
        finding = {
            "title": "Fix X",
            "scope": "medium",
            "related_files": ["a.py", "b.py", "c.py"],
            "prompt": "Do the thing.",
        }
        results = self._decompose(finding, cochange)
        assert len(results) == 2
        scopes = {r[1]["scope"] for r in results}
        assert scopes == {"small"}

    def test_cluster_preamble_contains_scope_empty_guidance(self):
        """SCOPE_EMPTY guidance must appear in every auto-decomposed cluster preamble."""
        cochange = {"a.py": {"b.py": 1.0}}
        finding = {
            "title": "My finding",
            "scope": "medium",
            "related_files": ["a.py", "b.py", "c.py"],
            "prompt": "Original prompt.",
        }
        results = self._decompose(finding, cochange)
        assert len(results) == 2
        for _, sub, _ in results:
            prompt = sub.get("prompt", "")
            assert "SCOPE_EMPTY" in prompt, (
                f"Cluster preamble missing SCOPE_EMPTY guidance; got: {prompt[:200]}"
            )

    def test_cluster_preamble_instructs_exit_cleanly(self):
        """The preamble should explicitly instruct exit cleanly via SCOPE_EMPTY."""
        cochange = {"a.py": {"b.py": 1.0}}
        finding = {
            "title": "My finding",
            "scope": "medium",
            "related_files": ["a.py", "b.py", "c.py"],
            "prompt": "Do X.",
        }
        results = self._decompose(finding, cochange)
        for _, sub, _ in results:
            prompt = sub.get("prompt", "")
            assert "exit cleanly via SCOPE_EMPTY" in prompt

    def test_cluster_preamble_mentions_sibling_cluster(self):
        """The preamble should mention 'sibling cluster' to name the source of pre-emption."""
        cochange = {"a.py": {"b.py": 1.0}}
        finding = {
            "title": "My finding",
            "scope": "medium",
            "related_files": ["a.py", "b.py", "c.py"],
            "prompt": "Do X.",
        }
        results = self._decompose(finding, cochange)
        for _, sub, _ in results:
            prompt = sub.get("prompt", "")
            assert "sibling cluster" in prompt

    def test_cluster_titles_include_n_of_total(self):
        cochange = {"a.py": {"b.py": 1.0}}
        finding = {
            "title": "Feat Y",
            "scope": "medium",
            "related_files": ["a.py", "b.py", "c.py"],
            "prompt": "Do Y.",
        }
        results = self._decompose(finding, cochange)
        titles = [r[1]["title"] for r in results]
        assert any("cluster 1/2" in t for t in titles)
        assert any("cluster 2/2" in t for t in titles)

    def test_auto_decomposed_flag_set(self):
        cochange = {"a.py": {"b.py": 1.0}}
        finding = {
            "title": "Feat Z",
            "scope": "medium",
            "related_files": ["a.py", "b.py", "c.py"],
            "prompt": "Do Z.",
        }
        results = self._decompose(finding, cochange)
        assert all(r[1].get("auto_decomposed") is True for r in results)

    def test_scope_calibrated_from_set_for_small_clusters(self):
        cochange = {"a.py": {"b.py": 1.0}}
        finding = {
            "title": "Feat Q",
            "scope": "medium",
            "related_files": ["a.py", "b.py", "c.py"],
            "prompt": "Do Q.",
        }
        results = self._decompose(finding, cochange)
        for _, sub, _ in results:
            if sub["scope"] == "small":
                assert sub.get("scope_calibrated_from") == "medium"

    def test_already_calibrated_finding_not_decomposed(self):
        """scope_calibrated_from set means this was already split — skip re-decomposition."""
        cochange = {"a.py": {"b.py": 1.0}}
        finding = {
            "title": "Feat R",
            "scope": "medium",
            "related_files": ["a.py", "b.py", "c.py"],
            "prompt": "Do R.",
            "scope_calibrated_from": "small",
        }
        results = self._decompose(finding, cochange)
        assert len(results) == 1

    def test_preamble_prepended_to_existing_prompt(self):
        cochange = {"a.py": {"b.py": 1.0}}
        finding = {
            "title": "Feat W",
            "scope": "medium",
            "related_files": ["a.py", "b.py", "c.py"],
            "prompt": "Original body.",
        }
        results = self._decompose(finding, cochange)
        for _, sub, _ in results:
            prompt = sub.get("prompt", "")
            # Preamble comes first, then original body
            assert prompt.startswith("## Scope boundary")
            assert "Original body." in prompt

    def test_toplevel_dir_fallback_when_no_cochange_index(self):
        """Falls back to top-level-dir clustering when cochange_index is empty."""
        finding = {
            "title": "Multi-dir finding",
            "scope": "medium",
            "related_files": [
                "backend/app/a.py",
                "agent/src/b.ts",
                "admin-ui/src/c.tsx",
            ],
            "prompt": "Do it.",
        }
        results = self._decompose(finding, cochange_index={})
        assert len(results) == 3
