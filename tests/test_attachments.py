"""Tests for dashboard file attachments (§loop.py's _build_message_content):
a plain-text/document attachment is named in the message so `read`/`glob`
can reach it; an image attachment is *also* embedded as a real image_url
content part, for a model that has vision."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from grandice import loop as agent_loop
from grandice.config import Config
from grandice.permissions import Gate, always_deny
from grandice.session import build as build_session


@pytest.fixture
def session(tmp_path: Path):
    config = replace(
        Config.from_env(),
        workspace=tmp_path / "ws",
        sandbox="sandbox-exec",
        api_key=None,
        base_url=None,
    )
    return build_session(config, Gate(always_deny))


def test_no_attachments_leaves_content_a_plain_string(session):
    content = agent_loop._build_message_content(session, "hello", [])
    assert content == "hello"


def test_a_document_attachment_is_named_but_not_embedded(session):
    (session.sandbox.workspace / "report.txt").write_text("quarterly numbers")

    content = agent_loop._build_message_content(session, "look at this", ["report.txt"])

    assert isinstance(content, str)
    assert "report.txt" in content
    assert "look at this" in content


def test_an_image_attachment_is_embedded_as_image_url(session):
    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
        "3de9000000017352474200aece1ce90000000d49444154789c6360000002000155"
        "0002ceb37f4c0000000049454e44ae426082"
    )
    (session.sandbox.workspace / "photo.png").write_bytes(png_bytes)

    content = agent_loop._build_message_content(session, "what is this", ["photo.png"])

    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert "photo.png" in content[0]["text"]
    assert "what is this" in content[0]["text"]
    image_parts = [p for p in content if p["type"] == "image_url"]
    assert len(image_parts) == 1
    assert image_parts[0]["image_url"]["url"].startswith("data:image/png;base64,")


def test_multiple_attachments_mix_docs_and_images(session):
    (session.sandbox.workspace / "notes.txt").write_text("hi")
    (session.sandbox.workspace / "shot.jpg").write_bytes(b"\xff\xd8\xff\xe0not a real jpeg but fine for this test")

    content = agent_loop._build_message_content(session, "hi", ["notes.txt", "shot.jpg"])

    assert isinstance(content, list)
    assert "notes.txt" in content[0]["text"]
    assert "shot.jpg" in content[0]["text"]
    image_parts = [p for p in content if p["type"] == "image_url"]
    assert len(image_parts) == 1
    assert image_parts[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_an_oversized_image_is_named_but_not_embedded(session, monkeypatch):
    monkeypatch.setattr(agent_loop, "MAX_INLINE_IMAGE_BYTES", 10)
    (session.sandbox.workspace / "big.png").write_bytes(b"x" * 100)

    content = agent_loop._build_message_content(session, "check this", ["big.png"])

    assert isinstance(content, str)  # no image part at all — just the path mention
    assert "big.png" in content


def test_a_missing_attachment_file_still_gets_named_in_the_message(session):
    """A path that was never actually uploaded (or was since deleted)
    shouldn't crash the turn — best-effort, same as everywhere else this
    file reads from the sandbox."""
    content = agent_loop._build_message_content(session, "hi", ["ghost.png"])
    assert isinstance(content, str)
    assert "ghost.png" in content


async def test_run_turn_passes_attachments_into_the_stored_message(session):
    (session.sandbox.workspace / "notes.txt").write_text("hi")
    async for _ in agent_loop.run_turn(session, "see attached", attachments=["notes.txt"]):
        pass
    user_message = session.messages[0]
    assert user_message["role"] == "user"
    assert "notes.txt" in user_message["content"]


async def test_run_turn_model_override_reaches_the_router(session, monkeypatch):
    captured = {}
    original_complete = session.router.complete

    async def spy_complete(*args, **kwargs):
        captured["model"] = kwargs.get("model")
        async for item in original_complete(*args, **kwargs):
            yield item

    monkeypatch.setattr(session.router, "complete", spy_complete)
    async for _ in agent_loop.run_turn(session, "hi", model="a-specific-model-id"):
        pass
    assert captured["model"] == "a-specific-model-id"
