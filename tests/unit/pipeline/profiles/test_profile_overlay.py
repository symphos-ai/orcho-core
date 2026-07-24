"""Workspace shared and personal config overlays for built-in profile JSON.

Operators override per-profile, per-phase fields via the ``profiles_v2``
block in any layered ``config.local.json`` without touching the shipped
``pipeline_profiles_v2.json``. The overlay path is loader-side
(see :func:`pipeline.profiles.loader._apply_profile_overlays`) and runs
before ``parse_profile`` so the same invariants apply to overridden
profiles as to built-in ones.

These tests pin:

* the layered read in :func:`core.infra.config.load_profile_overlays`
  (precedence + ``ORCHO_DISABLE_LOCAL_CONFIG`` opt-out);
* the loader-side deep-merge and error semantics
  (missing profile / phase / ambiguous match).

Filesystem isolation: ``ORCHO_WORKSPACE`` + the user-config home dir
are redirected to ``tmp_path`` for every test so a real
``~/.orcho/config.local.json`` or workspace ``config.json`` on the developer's
machine cannot leak
into the assertion.
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from core.infra import config as cfg_module
from pipeline.profiles.loader import (
    ProfileLoadError,
    _apply_profile_overlays,
)
from pipeline.runtime import LoopStep, PhaseHandoffType, PhaseStep

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def isolated_config_layers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[dict[str, Path]]:
    """Redirect every overlay layer this test cares about into
    ``tmp_path`` so the test cannot read or write a real
    config file on disk.

    Yields a dict with ``package``, ``user``, ``workspace_shared``, and
    ``workspace_personal`` paths the
    test populates as needed. The package layer is faked by
    monkeypatching ``_CONFIG_DIR`` inside ``core.infra.config``; the
    other two layers are redirected through ``paths.user_config_dir``
    and ``paths.workspace_config_dir``.
    """
    package_dir = tmp_path / "_config"
    user_dir = tmp_path / "user"
    workspace_dir = tmp_path / "workspace"
    package_dir.mkdir()
    user_dir.mkdir()
    workspace_dir.mkdir()

    # ``core.infra.config`` did ``from core.infra.paths import
    # user_config_dir, workspace_config_dir`` at import time, so those
    # names are bound on the config module itself — patching them on
    # ``paths_module`` would change nothing. Patch the imported
    # references directly. ``_CONFIG_DIR`` was renamed on import so
    # patching it on ``cfg_module`` is the right surface.
    monkeypatch.setattr(cfg_module, "_CONFIG_DIR", package_dir)
    monkeypatch.setattr(
        cfg_module, "user_config_dir", lambda: user_dir,
    )
    monkeypatch.setattr(
        cfg_module, "workspace_config_dir", lambda: workspace_dir,
    )
    monkeypatch.delenv("ORCHO_DISABLE_LOCAL_CONFIG", raising=False)

    yield {
        "package": package_dir / "config.local.json",
        "user": user_dir / "config.local.json",
        "workspace_shared": workspace_dir / "config.json",
        "workspace_personal": workspace_dir / "config.local.json",
    }


def _write_layer(path: Path, body: dict) -> None:
    path.write_text(json.dumps(body), encoding="utf-8")


# ── load_profile_overlays ────────────────────────────────────────────────────


class TestLoadProfileOverlays:
    """:func:`core.infra.config.load_profile_overlays` reads
    ``profiles_v2`` from package, user, workspace shared, and workspace
    personal config layers."""

    def test_returns_empty_when_no_layers_exist(
        self, isolated_config_layers: dict[str, Path],
    ) -> None:
        assert cfg_module.load_profile_overlays() == {}

    def test_single_layer_passes_through(
        self, isolated_config_layers: dict[str, Path],
    ) -> None:
        _write_layer(isolated_config_layers["user"], {
            "profiles_v2": {
                "advanced": {
                    "validate_plan": {
                        "handoff": {"type": "human_feedback_always"},
                    },
                },
            },
        })
        overlays = cfg_module.load_profile_overlays()
        assert overlays == {
            "advanced": {
                "validate_plan": {
                    "handoff": {"type": "human_feedback_always"},
                },
            },
        }

    def test_workspace_shared_layer_overrides_user_layer(
        self, isolated_config_layers: dict[str, Path],
    ) -> None:
        """Workspace shared > user > package precedence, mirroring the
        existing phase-overlay precedence."""
        _write_layer(isolated_config_layers["user"], {
            "profiles_v2": {
                "advanced": {
                    "validate_plan": {
                        "handoff": {"type": "human_feedback_always"},
                    },
                },
            },
        })
        _write_layer(isolated_config_layers["workspace_shared"], {
            "profiles_v2": {
                "advanced": {
                    "validate_plan": {
                        "handoff": {"type": "human_bypass"},
                    },
                },
            },
        })
        overlays = cfg_module.load_profile_overlays()
        assert overlays["advanced"]["validate_plan"]["handoff"] == {
            "type": "human_bypass",
        }

    def test_workspace_personal_layer_overrides_shared_layer(
        self, isolated_config_layers: dict[str, Path],
    ) -> None:
        _write_layer(isolated_config_layers["workspace_shared"], {
            "profiles_v2": {
                "advanced": {"implement": {"effort": "medium"}},
            },
        })
        _write_layer(isolated_config_layers["workspace_personal"], {
            "profiles_v2": {
                "advanced": {"implement": {"effort": "high"}},
            },
        })

        overlays = cfg_module.load_profile_overlays()

        assert overlays["advanced"]["implement"] == {"effort": "high"}

    def test_layers_union_disjoint_keys(
        self, isolated_config_layers: dict[str, Path],
    ) -> None:
        """Different (profile, phase) keys across layers union — neither
        clobbers the other."""
        _write_layer(isolated_config_layers["workspace_shared"], {
            "profiles_v2": {
                "advanced": {
                    "validate_plan": {
                        "handoff": {"type": "human_feedback_always"},
                    },
                },
            },
        })
        _write_layer(isolated_config_layers["workspace_personal"], {
            "profiles_v2": {
                "lite": {
                    "implement": {"effort": "low"},
                },
            },
        })
        overlays = cfg_module.load_profile_overlays()
        assert overlays == {
            "advanced": {
                "validate_plan": {
                    "handoff": {"type": "human_feedback_always"},
                },
            },
            "lite": {
                "implement": {"effort": "low"},
            },
        }

    def test_profile_level_patch_passes_through(
        self, isolated_config_layers: dict[str, Path],
    ) -> None:
        _write_layer(isolated_config_layers["workspace_personal"], {
            "profiles_v2": {
                "review": {
                    "_profile": {"worktree_isolation": "off"},
                },
            },
        })

        overlays = cfg_module.load_profile_overlays()

        assert overlays == {
            "review": {
                "_profile": {"worktree_isolation": "off"},
            },
        }

    def test_env_var_disables_overlays(
        self, isolated_config_layers: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``ORCHO_DISABLE_LOCAL_CONFIG=1`` returns ``{}`` regardless of
        layer contents — the engine must run with strict built-in
        profiles in CI / harness paths that ask for it."""
        _write_layer(isolated_config_layers["user"], {
            "profiles_v2": {
                "advanced": {
                    "validate_plan": {
                        "handoff": {"type": "human_feedback_always"},
                    },
                },
            },
        })
        monkeypatch.setenv("ORCHO_DISABLE_LOCAL_CONFIG", "1")
        assert cfg_module.load_profile_overlays() == {}

    def test_malformed_layer_is_skipped(
        self, isolated_config_layers: dict[str, Path],
    ) -> None:
        """A broken local layer must not crash overlay discovery;
        the layer is silently dropped (mirrors phase-overlay behavior)
        and well-formed layers still apply."""
        isolated_config_layers["user"].write_text(
            "{not valid json", encoding="utf-8",
        )
        _write_layer(isolated_config_layers["workspace_personal"], {
            "profiles_v2": {
                "advanced": {
                    "validate_plan": {
                        "handoff": {"type": "human_feedback_always"},
                    },
                },
            },
        })
        overlays = cfg_module.load_profile_overlays()
        assert overlays == {
            "advanced": {
                "validate_plan": {
                    "handoff": {"type": "human_feedback_always"},
                },
            },
        }

    def test_non_dict_patches_are_skipped(
        self, isolated_config_layers: dict[str, Path],
    ) -> None:
        """Defensive: an operator writing a string instead of a dict
        for a phase patch must be ignored, not crash the engine."""
        _write_layer(isolated_config_layers["user"], {
            "profiles_v2": {
                "advanced": {
                    "validate_plan": "human_feedback_always",
                    "implement": {"effort": "low"},
                },
            },
        })
        overlays = cfg_module.load_profile_overlays()
        assert overlays == {
            "advanced": {"implement": {"effort": "low"}},
        }


# ── _apply_profile_overlays ──────────────────────────────────────────────────


def _shipped_raw_advanced() -> dict:
    """Minimal built-in-shape JSON: one profile with a plan loop +
    a top-level implement step. Mirrors the shape of the real
    ``pipeline_profiles_v2.json`` for ``advanced``."""
    return {
        "advanced": {
            "kind": "full_cycle",
            "variant": "advanced",
            "description": "test fixture",
            "steps": [
                {"loop": {
                    "max_rounds": 2,
                    "round_extras_key": "plan_round",
                    "until": "validate_plan.approved",
                    "steps": [
                        {"phase": "plan"},
                        {
                            "phase": "validate_plan",
                            "handoff": {"type": "human_feedback_on_reject"},
                        },
                    ],
                }},
                {"phase": "implement"},
            ],
        },
    }


class TestApplyProfileOverlays:
    def test_no_overlay_is_no_op(self) -> None:
        raw = _shipped_raw_advanced()
        snapshot = json.loads(json.dumps(raw))
        _apply_profile_overlays(raw, {})
        assert raw == snapshot

    def test_overlay_patches_nested_phase_step(self) -> None:
        """The patch targets ``validate_plan`` which lives inside a loop;
        the overlay must descend into ``loop.steps`` to find it."""
        raw = _shipped_raw_advanced()
        _apply_profile_overlays(raw, {
            "advanced": {
                "validate_plan": {
                    "handoff": {"type": "human_feedback_always"},
                },
            },
        })
        loop = raw["advanced"]["steps"][0]["loop"]
        validate_plan = loop["steps"][1]
        assert validate_plan["handoff"] == {
            "type": "human_feedback_always",
        }

    def test_overlay_deep_merges_preserving_siblings(self) -> None:
        """Only the patched leaf is replaced; other fields on the same
        step stay untouched. This is the load-bearing invariant — an
        overlay that touches ``handoff`` must not wipe ``execution``
        or ``prompt`` that the operator never mentioned."""
        raw = _shipped_raw_advanced()
        loop = raw["advanced"]["steps"][0]["loop"]
        # Seed extra fields on the validate_plan step that the overlay
        # does not mention.
        loop["steps"][1]["execution"] = {"mode": "linear"}
        loop["steps"][1]["prompt"] = {"role": "plan_reviewer"}

        _apply_profile_overlays(raw, {
            "advanced": {
                "validate_plan": {
                    "handoff": {"type": "human_feedback_always"},
                },
            },
        })

        validate_plan = loop["steps"][1]
        assert validate_plan["execution"] == {"mode": "linear"}
        assert validate_plan["prompt"] == {"role": "plan_reviewer"}
        assert validate_plan["handoff"] == {
            "type": "human_feedback_always",
        }

    def test_overlay_can_patch_top_level_phase_step(self) -> None:
        """``implement`` lives directly under ``steps`` (no loop wrapper);
        the overlay walker still finds it."""
        raw = _shipped_raw_advanced()
        _apply_profile_overlays(raw, {
            "advanced": {
                "implement": {"effort": "high"},
            },
        })
        implement = raw["advanced"]["steps"][1]
        assert implement["effort"] == "high"

    def test_profile_overlay_patches_top_level_profile_fields(self) -> None:
        raw = _shipped_raw_advanced()
        _apply_profile_overlays(raw, {
            "advanced": {
                "_profile": {"worktree_isolation": "off"},
            },
        })

        assert raw["advanced"]["worktree_isolation"] == "off"

    def test_overlay_unknown_profile_raises(self) -> None:
        raw = _shipped_raw_advanced()
        with pytest.raises(ProfileLoadError, match="profile 'ghost'"):
            _apply_profile_overlays(raw, {
                "ghost": {"validate_plan": {"handoff": {"type": "human_bypass"}}},
            })

    def test_overlay_unknown_phase_raises(self) -> None:
        raw = _shipped_raw_advanced()
        with pytest.raises(
            ProfileLoadError, match="phase='final_acceptance'",
        ):
            _apply_profile_overlays(raw, {
                "advanced": {
                    "final_acceptance": {"effort": "high"},
                },
            })

    def test_overlay_duplicate_phase_raises(self) -> None:
        """Custom profile where ``plan`` appears twice (top-level +
        inside the loop) — the overlay walker counts both, refuses to
        guess which one to patch, raises with a clear message."""
        raw = {
            "weird": {
                "kind": "custom",
                "steps": [
                    {"phase": "plan"},
                    {"loop": {
                        "max_rounds": 1,
                        "until": "validate_plan.approved",
                        "steps": [
                            {"phase": "plan"},
                            {"phase": "validate_plan"},
                        ],
                    }},
                ],
            },
        }
        with pytest.raises(ProfileLoadError, match="2 PhaseSteps"):
            _apply_profile_overlays(raw, {
                "weird": {"plan": {"effort": "low"}},
            })


# ── End-to-end: load_profiles_v2 picks up the overlay ────────────────────────


class TestLoadProfilesV2WithOverlay:
    """The full path: a layered ``config.local.json`` declares an
    overlay, ``load_profiles_v2`` picks it up, the parsed ``Profile``
    surfaces the override.

    This is the contract the operator actually relies on — overlay on
    disk → typed dataclass behavior changes at run time.
    """

    def test_overlay_changes_built_in_advanced_handoff(
        self, isolated_config_layers: dict[str, Path],
        tmp_path: Path,
    ) -> None:
        from pipeline.profiles.loader import load_profiles_v2

        profile_json = tmp_path / "profiles.json"
        profile_json.write_text(
            json.dumps(_shipped_raw_advanced()), encoding="utf-8",
        )
        _write_layer(isolated_config_layers["user"], {
            "profiles_v2": {
                "advanced": {
                    "validate_plan": {
                        "handoff": {"type": "human_feedback_always"},
                    },
                },
            },
        })

        profiles = load_profiles_v2(profile_json)
        loop_step = profiles["advanced"].steps[0]
        assert isinstance(loop_step, LoopStep)
        validate_plan = next(
            s for s in loop_step.steps
            if isinstance(s, PhaseStep) and s.phase == "validate_plan"
        )
        assert validate_plan.handoff is not None
        assert (
            validate_plan.handoff.type
            is PhaseHandoffType.HUMAN_FEEDBACK_ALWAYS
        )

    def test_profile_overlay_changes_worktree_isolation(
        self, isolated_config_layers: dict[str, Path],
        tmp_path: Path,
    ) -> None:
        from pipeline.profiles.loader import load_profiles_v2

        profile_json = tmp_path / "profiles.json"
        profile_json.write_text(
            json.dumps(_shipped_raw_advanced()), encoding="utf-8",
        )
        _write_layer(isolated_config_layers["workspace_personal"], {
            "profiles_v2": {
                "advanced": {
                    "_profile": {"worktree_isolation": "off"},
                },
            },
        })

        profiles = load_profiles_v2(profile_json)

        assert profiles["advanced"].worktree_isolation == "off"

    def test_scoped_profile_level_overlays_do_not_hide_correction_profile(
        self, isolated_config_layers: dict[str, Path],
        tmp_path: Path,
    ) -> None:
        """Regression for correction follow-up after scoped defaults landed.

        The workspace may carry ``profiles_v2.<profile>._profile`` overlays for
        scoped profiles. The loader must treat ``_profile`` as a top-level patch,
        not as a phase name, and still load the internal ``correction`` profile.
        """
        from core.infra.paths import CONFIG_DIR
        from pipeline.profiles.loader import load_profiles_v2

        profile_json = tmp_path / "profiles.json"
        profile_json.write_text(
            (CONFIG_DIR / "pipeline_profiles_v2.json").read_text(
                encoding="utf-8",
            ),
            encoding="utf-8",
        )
        _write_layer(isolated_config_layers["workspace_personal"], {
            "profiles_v2": {
                name: {"_profile": {"worktree_isolation": "off"}}
                for name in ("small_task", "planning", "delivery_audit", "task")
            },
        })

        profiles = load_profiles_v2(profile_json)

        assert profiles["correction"].internal is True
        for name in ("small_task", "planning", "delivery_audit", "task"):
            assert profiles[name].worktree_isolation == "off"

    def test_disabled_overlay_leaves_built_in_intact(
        self, isolated_config_layers: dict[str, Path],
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``ORCHO_DISABLE_LOCAL_CONFIG=1`` short-circuits both the
        phase-overlay path and the new profile-overlay path; the JSON
        on disk wins. Lets test harnesses pin built-in semantics even
        when the developer has a ``config.local.json`` lying around."""
        from pipeline.profiles.loader import load_profiles_v2

        profile_json = tmp_path / "profiles.json"
        profile_json.write_text(
            json.dumps(_shipped_raw_advanced()), encoding="utf-8",
        )
        _write_layer(isolated_config_layers["user"], {
            "profiles_v2": {
                "advanced": {
                    "validate_plan": {
                        "handoff": {"type": "human_feedback_always"},
                    },
                },
            },
        })
        monkeypatch.setenv("ORCHO_DISABLE_LOCAL_CONFIG", "1")

        profiles = load_profiles_v2(profile_json)
        loop_step = profiles["advanced"].steps[0]
        assert isinstance(loop_step, LoopStep)
        validate_plan = next(
            s for s in loop_step.steps
            if isinstance(s, PhaseStep) and s.phase == "validate_plan"
        )
        assert validate_plan.handoff is not None
        assert (
            validate_plan.handoff.type
            is PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT
        )
