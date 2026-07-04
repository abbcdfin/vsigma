"""Scope ladder: namespaces, visibility, and the recall retrieve path."""

from vsigma import (
    AGENT,
    GLOBAL,
    InMemoryStore,
    Scope,
    State,
    episode,
    persona,
    recall,
    session,
    visible_scopes,
)


def test_namespace_format() -> None:
    assert GLOBAL.namespace("knowledge") == "global/knowledge"
    assert AGENT.namespace("context") == "agent/context"
    assert persona("alice").namespace("knowledge") == "persona:alice/knowledge"
    assert session("s1").namespace("context") == "session:s1/context"
    assert episode("r1").namespace("context") == "episode:r1/context"


def test_visible_scopes_serving_persona_only() -> None:
    state: State = {"user_id": "alice", "session_id": "s1", "run_id": "r1"}
    scopes = visible_scopes(state)
    assert persona("alice") in scopes
    assert persona("bob") not in scopes
    assert GLOBAL in scopes and AGENT in scopes
    assert session("s1") in scopes and episode("r1") in scopes


def test_visible_scopes_without_ids() -> None:
    # One-shot run: no persona, no session — only the shared scopes + episode.
    scopes = visible_scopes({"run_id": "r1"})
    assert scopes == [GLOBAL, AGENT, episode("r1")]


def test_recall_never_serves_another_personas_memory() -> None:
    store = InMemoryStore()
    store.write(persona("alice").namespace("knowledge"), "pref", "likes tea")
    store.write(persona("bob").namespace("knowledge"), "pref", "likes coffee")
    store.write(session("s1").namespace("context"), "turn:1", "shared history")

    # Group session s1, currently serving alice.
    serving_alice: State = {"user_id": "alice", "session_id": "s1"}
    triples = recall(store, serving_alice)
    values = [value for _, _, value in triples]

    assert "likes tea" in values
    assert "shared history" in values  # session-shared is visible to participants
    assert "likes coffee" not in values  # bob's private memory never leaks


def test_recall_filters_by_kind() -> None:
    store = InMemoryStore()
    store.write(GLOBAL.namespace("knowledge"), "fact", "the sky is blue")
    store.write(episode("r1").namespace("context"), "act:0", "step update")
    state: State = {"run_id": "r1"}

    knowledge = recall(store, state, kind="knowledge")
    assert [v for _, _, v in knowledge] == ["the sky is blue"]

    context = recall(store, state, kind="context")
    assert [v for _, _, v in context] == ["step update"]


def test_scope_is_hashable_value_object() -> None:
    assert Scope("persona", "alice") == persona("alice")
    assert len({persona("a"), persona("a"), persona("b")}) == 2
