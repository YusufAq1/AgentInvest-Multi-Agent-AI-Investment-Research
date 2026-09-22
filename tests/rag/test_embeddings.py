"""Tests for backend.rag.embeddings.

WHY the real BAAI/bge-m3 model is never loaded here: it's a ~2GB download
(see the module's docstring) — every test monkeypatches the module-level
`_model` singleton directly with a fake exposing the same `.encode()`/
`.tokenizer` surface `_get_model()`'s callers actually use.
"""

from unittest.mock import MagicMock

import backend.rag.embeddings as embeddings_module
import pytest
from backend.rag.embeddings import EMBEDDING_DIM, embed_texts, get_tokenizer


class _FakeVector:
    def __init__(self, values: list[float]) -> None:
        self._values = values

    def tolist(self) -> list[float]:
        return self._values


def _install_fake_model(
    monkeypatch: pytest.MonkeyPatch, encode_return: list[_FakeVector]
) -> MagicMock:
    fake_model = MagicMock()
    fake_model.encode.return_value = encode_return
    fake_model.tokenizer = "fake-tokenizer"
    monkeypatch.setattr(embeddings_module, "_model", fake_model)
    return fake_model


async def test_embed_texts_empty_input_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_model = _install_fake_model(monkeypatch, encode_return=[])

    result = await embed_texts([])

    assert result == []
    fake_model.encode.assert_not_called()


async def test_embed_texts_returns_vectors_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    vectors = [_FakeVector([0.1] * EMBEDDING_DIM), _FakeVector([0.2] * EMBEDDING_DIM)]
    fake_model = _install_fake_model(monkeypatch, encode_return=vectors)

    result = await embed_texts(["first chunk", "second chunk"])

    assert result == [[0.1] * EMBEDDING_DIM, [0.2] * EMBEDDING_DIM]
    args, kwargs = fake_model.encode.call_args
    assert args[0] == ["first chunk", "second chunk"]
    assert kwargs["normalize_embeddings"] is True


def test_get_tokenizer_returns_the_singleton_models_tokenizer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_model(monkeypatch, encode_return=[])

    # WHY the ignore: get_tokenizer() is statically typed as the real
    # transformers.PreTrainedTokenizerFast (see embeddings.py) — the fake
    # string stand-in here is deliberate (avoids depending on the real
    # class's shape in a test that never touches a real model).
    assert get_tokenizer() == "fake-tokenizer"  # type: ignore[comparison-overlap]
