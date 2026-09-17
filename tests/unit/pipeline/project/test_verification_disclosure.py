"""The "no verification contract declared" fact: one owner, one writer.

``pipeline.project.verification_disclosure`` owns the fact and every piece of
wording built on it; the session-init chain is the only writer. These tests
pin both halves: the value semantics of the typed fact, and the real producer
path — ``init_run_session`` → ``init_session_with_atexit`` → ``meta.json``
before the run's first ``save_session``.
"""

from __future__ import annotations

import dataclasses
import json
import types
from pathlib import Path

import pytest

from agents.protocols import SessionMode
from pipeline.plugins import PluginConfig
from pipeline.project import bootstrap
from pipeline.project.run_setup import init_run_session
from pipeline.project.state_setup import hydrate_state_extras_from_session
from pipeline.project.verification_disclosure import (
    HEADER_VALUE,
    META_KEY,
    VerificationContractPresence,
    delivery_gate_line,
    readiness_block,
    stamp_contract_presence,
    status_line,
    tail_line,
)

_DECLARED_FALSE = VerificationContractPresence(declared=False)


def _init_run_session(tmp_path: Path, **overrides) -> dict:
    kwargs = dict(
        task="do a thing",
        project_path=tmp_path,
        plugin=PluginConfig(),
        model="m",
        max_rounds=1,
        profile_name="feature",
        session_mode=SessionMode.AUTO,
        change_handoff="uncommitted",
        output_dir=tmp_path,
        plan_source="local",
        projected_profile=None,
        resume_mode=None,
        followup_parent_run_id=None,
        followup_parent_run_dir=None,
        followup_parent_status=None,
        followup_base_task=None,
        plan_source_run_id=None,
    )
    kwargs.update(overrides)
    session = init_run_session(**kwargs)
    session["status"] = "done"  # keep the test atexit-safe
    return session


def _persisted(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "meta.json").read_text(encoding="utf-8"))


# ── the fact itself ──────────────────────────────────────────────────────


class TestVerificationContractPresence:
    def test_from_contract_none_is_not_declared(self) -> None:
        assert VerificationContractPresence.from_contract(None).declared is False

    def test_from_contract_object_is_declared(self) -> None:
        contract = object()
        assert VerificationContractPresence.from_contract(contract).declared is True

    def test_to_meta_shape(self) -> None:
        assert _DECLARED_FALSE.to_meta() == {"declared": False}
        assert VerificationContractPresence(declared=True).to_meta() == {
            "declared": True,
        }

    @pytest.mark.parametrize("declared", [False, True])
    def test_meta_round_trip(self, declared: bool) -> None:
        presence = VerificationContractPresence(declared=declared)
        source = {META_KEY: presence.to_meta()}
        assert VerificationContractPresence.from_mapping(source) == presence

    def test_from_mapping_without_block_is_none(self) -> None:
        """A run written before the block existed reads as "never recorded"."""
        assert VerificationContractPresence.from_mapping({}) is None

    @pytest.mark.parametrize(
        "source",
        [
            None,
            "not-a-mapping",
            {META_KEY: None},
            {META_KEY: "declared"},
            {META_KEY: []},
            {META_KEY: {}},
            {META_KEY: {"declared": "false"}},
            {META_KEY: {"declared": 0}},
        ],
    )
    def test_from_mapping_rejects_malformed_input(self, source) -> None:
        assert VerificationContractPresence.from_mapping(source) is None

    def test_is_frozen(self) -> None:
        with pytest.raises(dataclasses.FrozenInstanceError):
            _DECLARED_FALSE.declared = True  # type: ignore[misc]


class TestStampContractPresence:
    def test_stamp_writes_the_block(self) -> None:
        session: dict = {}
        stamp_contract_presence(session, _DECLARED_FALSE)
        assert session == {META_KEY: {"declared": False}}

    def test_stamp_without_presence_is_a_noop(self) -> None:
        session: dict = {}
        stamp_contract_presence(session, None)
        assert session == {}

    def test_stamp_is_idempotent_on_resume(self) -> None:
        session: dict = {META_KEY: {"declared": True}}
        stamp_contract_presence(session, _DECLARED_FALSE)
        assert session[META_KEY] == {"declared": False}


# ── the producer: the real session-init chain ────────────────────────────


class TestProducerChain:
    def test_init_run_session_persists_the_block(self, tmp_path: Path) -> None:
        session = _init_run_session(
            tmp_path, verification_contract_presence=_DECLARED_FALSE,
        )
        assert session[META_KEY] == {"declared": False}
        assert _persisted(tmp_path)[META_KEY] == {"declared": False}

    def test_init_run_session_persists_a_declared_contract_too(
        self, tmp_path: Path,
    ) -> None:
        """The block records the fact, not just its negative case."""
        _init_run_session(
            tmp_path,
            verification_contract_presence=VerificationContractPresence(
                declared=True,
            ),
        )
        assert _persisted(tmp_path)[META_KEY] == {"declared": True}

    def test_init_run_session_without_presence_writes_no_block(
        self, tmp_path: Path,
    ) -> None:
        session = _init_run_session(tmp_path)
        assert META_KEY not in session
        assert META_KEY not in _persisted(tmp_path)

    def test_block_lands_in_the_first_save(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Not a re-persist after the fact: the very first write carries it.

        A SIGKILL between the first ``save_session`` and any later one must
        still leave a ``meta.json`` that states the fact.
        """
        seen: list[dict] = []
        real_save = bootstrap.save_session

        def _recording_save(output_dir, session):
            seen.append(json.loads(json.dumps(session, default=str)))
            return real_save(output_dir, session)

        monkeypatch.setattr(bootstrap, "save_session", _recording_save)
        _init_run_session(
            tmp_path, verification_contract_presence=_DECLARED_FALSE,
        )
        assert seen, "session init must persist meta.json"
        assert seen[0][META_KEY] == {"declared": False}

    def test_atexit_hook_captures_the_stamped_dict(
        self, tmp_path: Path,
    ) -> None:
        """The hook holds the same dict, so the block survives abnormal exit."""
        session = bootstrap.init_session_with_atexit(
            task="t", project_path=tmp_path, plugin=PluginConfig(), model="m",
            max_rounds=1,
            profile_name="feature", session_mode=SessionMode.AUTO,
            change_handoff="", output_dir=tmp_path,
            verification_contract_presence=_DECLARED_FALSE,
        )
        session["status"] = "interrupted"
        bootstrap.save_session(tmp_path, session)
        session["status"] = "done"  # keep the test atexit-safe
        assert _persisted(tmp_path)[META_KEY] == {"declared": False}

    def test_init_session_with_atexit_without_presence_writes_no_block(
        self, tmp_path: Path,
    ) -> None:
        session = bootstrap.init_session_with_atexit(
            task="t", project_path=tmp_path, plugin=PluginConfig(), model="m",
            max_rounds=1,
            profile_name="feature", session_mode=SessionMode.AUTO,
            change_handoff="", output_dir=tmp_path,
        )
        session["status"] = "done"  # keep the test atexit-safe
        assert META_KEY not in session
        assert META_KEY not in _persisted(tmp_path)


# ── the in-process channel: state.extras ─────────────────────────────────


class TestHydrateStateExtras:
    def test_block_is_lifted_into_extras(self) -> None:
        state = types.SimpleNamespace(extras={})
        hydrate_state_extras_from_session(
            state, {META_KEY: {"declared": False}},
        )
        assert state.extras[META_KEY] == {"declared": False}

    def test_lifted_copy_is_detached_from_the_session(self) -> None:
        session = {META_KEY: {"declared": False}}
        state = types.SimpleNamespace(extras={})
        hydrate_state_extras_from_session(state, session)
        state.extras[META_KEY]["declared"] = True
        assert session[META_KEY] == {"declared": False}

    def test_live_copy_is_not_overwritten(self) -> None:
        state = types.SimpleNamespace(extras={META_KEY: {"declared": True}})
        hydrate_state_extras_from_session(
            state, {META_KEY: {"declared": False}},
        )
        assert state.extras[META_KEY] == {"declared": True}

    def test_absent_block_leaves_extras_untouched(self) -> None:
        state = types.SimpleNamespace(extras={})
        hydrate_state_extras_from_session(state, {})
        assert state.extras == {}


# ── the wording ──────────────────────────────────────────────────────────


_WORDINGS = (
    HEADER_VALUE,
    tail_line(),
    readiness_block(),
    status_line(),
    delivery_gate_line(),
)


class TestWording:
    @pytest.mark.parametrize("text", _WORDINGS)
    def test_no_box_drawing_rules(self, text: str) -> None:
        assert "─" * 8 not in text

    @pytest.mark.parametrize("text", _WORDINGS)
    def test_provider_neutral(self, text: str) -> None:
        """No plugin file name and no provider-specific vocabulary."""
        lowered = text.lower()
        for term in ("orcho.py", "plugin", "claude", "codex", "gemini"):
            assert term not in lowered

    def test_tail_line_is_one_line_and_points_at_the_doc(self) -> None:
        line = tail_line()
        assert "\n" not in line
        assert "docs/architecture/verification_contract.md" in line

    def test_readiness_block_states_zero_receipts(self) -> None:
        block = readiness_block()
        assert block.startswith("Verification readiness — final_acceptance:")
        assert (
            "No verification contract declared; 0 receipts — the engine ran no "
            "gates; verification came from the agents only." in block
        )

    def test_header_value_is_the_no_contract_fact(self) -> None:
        assert HEADER_VALUE == (
            "contract: none — engine runs no gates; verification comes from "
            "the agents only"
        )
