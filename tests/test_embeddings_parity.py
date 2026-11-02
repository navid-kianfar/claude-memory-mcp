"""The two embedding backends must agree, because stored vectors never re-embed.

The onnx backend (fastembed) is the default and the sentence-transformers
backend is the fallback; embeddings written by either must rank identically
under the other. "Identical" is measured, not asserted: pairwise cosine over a
sample corpus, threshold 0.999 — orders of magnitude above every distance
threshold in the codebase (DUPLICATE_DISTANCE 0.10, OVERLAP_DISTANCE 0.50).
"""

import numpy as np
import pytest

from memory_mcp import embeddings
from memory_mcp.config import settings

pytestmark = pytest.mark.filterwarnings("ignore::UserWarning")

SAMPLE_TEXTS = [
    "Always use pnpm, never npm, for JavaScript projects.",
    "We chose PostgreSQL because of the vector column type.",
    "Deploy only from the main branch, never from a feature branch.",
    "The outbox drains every sixty seconds; a nudge during a flush "
    "marks the project dirty instead of starting a second flush.",
    "Combinatorial: 200ms at the 99th percentile, measured at the load balancer.",
    "Preparar el informe antes del viernes",
    "x" * 200,
    "   ",
]


@pytest.fixture(scope="module")
def both_backends():
    """Vectors from both backends over the same corpus. Skips when either
    model is unavailable (a fresh runner downloads; the suite already does
    this for the ST model today)."""
    try:
        onnx = embeddings._OnnxModel(settings.embedding_model)
        st = embeddings._STModel(settings.embedding_model)
    except Exception as e:  # noqa: BLE001 - a missing model download is a skip
        pytest.skip(f"embedding models unavailable: {e}")
    return (
        np.array(onnx.encode(SAMPLE_TEXTS, normalize=True)),
        np.array(st.encode(SAMPLE_TEXTS, normalize=True)),
    )


class TestBackendParity:
    def test_same_shape(self, both_backends):
        onnx_vecs, st_vecs = both_backends
        assert onnx_vecs.shape == st_vecs.shape
        assert onnx_vecs.shape[1] == settings.embedding_dim
        assert onnx_vecs.shape[0] == len(SAMPLE_TEXTS)

    def test_all_normalized(self, both_backends):
        onnx_vecs, st_vecs = both_backends
        assert np.allclose(np.linalg.norm(onnx_vecs, axis=1), 1.0, atol=1e-4)
        assert np.allclose(np.linalg.norm(st_vecs, axis=1), 1.0, atol=1e-4)

    def test_cosine_at_least_0_999(self, both_backends):
        onnx_vecs, st_vecs = both_backends
        cosines = (onnx_vecs * st_vecs).sum(axis=1)
        assert cosines.min() >= 0.999, f"worst cosine {cosines.min():.6f}"

    def test_ranking_preserved(self, both_backends):
        """The corpus ranked by similarity to a query must come back in the
        same order under both backends — that is what search actually does."""
        onnx_vecs, st_vecs = both_backends
        query = np.array(
            embeddings._OnnxModel(settings.embedding_model).encode(
                ["which database did we pick"], normalize=True
            )
        )[0]
        assert (
            np.argsort(-(st_vecs @ query)).tolist()
            == np.argsort(-(onnx_vecs @ query)).tolist()
        )


class TestEmptyTextGuard:
    def test_empty_string_embeds_like_a_blank(self):
        """fastembed's tokenizer rejects '' outright; the wrapper must not let
        a blank memory text turn a store into a 500."""
        model = embeddings._OnnxModel(settings.embedding_model)
        vecs = np.array(model.encode(["", " "], normalize=True))
        assert vecs.shape == (2, settings.embedding_dim)
        assert np.allclose(np.linalg.norm(vecs, axis=1), 1.0, atol=1e-4)

    def test_unknown_model_names_the_backend_switch(self):
        with pytest.raises(RuntimeError, match="EMBEDDING_BACKEND"):
            embeddings._OnnxModel("no-such-model-anywhere-xyz")


class TestBackendSetting:
    def test_unknown_backend_refused(self):
        from memory_mcp.config import Settings

        with pytest.raises(ValueError, match="embedding_backend"):
            Settings(embedding_backend="ggml")

    def test_current_backend_reports_config_before_first_load(self):
        # Module state may hold a model from an earlier test in this session;
        # the contract is only that the value is one of the two names.
        assert embeddings.current_backend() in ("onnx", "sentence-transformers")
