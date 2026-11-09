"""Lazy-loaded singleton embedding model for vector generation.

Two backends, chosen by `settings.embedding_backend`, behind one `get_model()`:

- **"onnx"** (default) — fastembed's ONNX Runtime path. The same MiniLM
  weights, the same 384-dim normalized vectors, and PyTorch never loads:
  `import sentence_transformers` pulls torch (~300 MB RSS) into the process
  unconditionally, because its classes extend `nn.Module` — so ST's own
  `backend="onnx"` would not have helped. Measured baseline 2026-11-02:
  the torch block was ~305 MB of the daemon's resident set.
- **"sentence-transformers"** — the original path, kept as the fallback and
  for the parity test (tests/test_embeddings_parity.py: the two backends
  agree at cosine >= 0.999 on a sample corpus, far above every distance
  threshold in the codebase, so stored embeddings never need re-embedding).

Model names: a settings value without a namespace is read as a
sentence-transformers model by both backends (fastembed resolves it under
`sentence-transformers/`). A name fastembed does not know raises with a hint
to switch the backend rather than failing obscurely.

Fallback to sentence-transformers happens ONLY when onnx genuinely cannot
serve the configured model: fastembed is not installed, or it does not list
the model. Any other failure - a broken download, an onnxruntime error, a
corrupt cache - is a real fault and propagates with its own message; silently
loading torch (~300 MB) over it hid the cause and cost the memory the onnx
backend exists to save.

The model cache lives under the data dir (`models/fastembed`), not fastembed's
default `$TMPDIR/fastembed_cache`, which macOS clears - a cleared cache meant a
re-download on the next daemon start. `FASTEMBED_CACHE_PATH`, fastembed's own
variable, still wins when set.
"""

import logging
import os
import threading
from pathlib import Path

import numpy as np

from memory_mcp.config import settings

log = logging.getLogger(__name__)

_model = None
_model_backend: str | None = None
_lock = threading.Lock()

#: fastembed indexes models by their full HF repo id; this project's settings
#: use the bare sentence-transformers names ("all-MiniLM-L6-v2").
FASTEMBED_NAMESPACE = "sentence-transformers/"

BACKEND_ONNX = "onnx"
BACKEND_ST = "sentence-transformers"

#: fastembed's own override for its cache directory.
FASTEMBED_CACHE_ENV = "FASTEMBED_CACHE_PATH"
MODEL_CACHE_SUBDIR = Path("models") / "fastembed"


class OnnxBackendUnavailable(RuntimeError):
    """onnx cannot serve this model at all - the one case that falls back."""


def model_cache_dir() -> Path:
    """Where fastembed keeps downloaded models: persistent, under the data dir."""
    override = os.environ.get(FASTEMBED_CACHE_ENV)
    if override:
        return Path(override)
    return settings.data_dir / MODEL_CACHE_SUBDIR


def _fastembed_supports(TextEmbedding, model_name: str) -> bool:
    supported = TextEmbedding.list_supported_models()
    return any(entry.get("model") == model_name for entry in supported)


def _fastembed_model_name(name: str) -> str:
    if "/" in name:
        return name
    return FASTEMBED_NAMESPACE + name


class _OnnxModel:
    """fastembed handle behind the encode() shape the rest of the code uses."""

    def __init__(self, name: str):
        try:
            from fastembed import TextEmbedding
        except ImportError as e:
            raise OnnxBackendUnavailable(
                "embedding_backend is 'onnx' but fastembed is not installed; "
                "add the dependency or set MEMORY_MCP_EMBEDDING_BACKEND="
                "sentence-transformers"
            ) from e
        model_name = _fastembed_model_name(name)
        if not _fastembed_supports(TextEmbedding, model_name):
            raise OnnxBackendUnavailable(
                f"fastembed does not know model {name!r}; check the preset or "
                "set MEMORY_MCP_EMBEDDING_BACKEND=sentence-transformers"
            )
        cache = model_cache_dir()
        cache.mkdir(parents=True, exist_ok=True)
        # Deliberately not wrapped: a download or onnxruntime failure here is a
        # real fault and must surface as itself, not as "unknown model".
        self._m = TextEmbedding(model_name, cache_dir=str(cache))
        # onnxruntime's session is thread-safe, but fastembed's generator
        # pipeline is not documented as such and tool workers embed
        # concurrently. One encode at a time costs nothing at these sizes.
        self._encode_lock = threading.Lock()

    def encode(self, texts, normalize: bool = True, batch_size: int = 32):
        # fastembed's tokenizer rejects the empty string outright (a
        # whitespace-only string is fine); a blank memory text would otherwise
        # turn every store of one into a 500.
        cleaned = [t if (t and t.strip()) else " " for t in texts]
        with self._encode_lock:
            vectors = np.array(list(self._m.embed(cleaned, batch_size=batch_size)))
        if not normalize:
            return vectors
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        return vectors / np.maximum(norms, 1e-12)


class _STModel:
    """The original sentence-transformers handle, unchanged behaviour."""

    def __init__(self, name: str):
        from sentence_transformers import SentenceTransformer

        self._m = SentenceTransformer(name)

    def encode(self, texts, normalize: bool = True, batch_size: int = 32):
        return self._m.encode(
            texts, normalize_embeddings=normalize, batch_size=batch_size
        )


def get_model():
    """Get or lazily load the embedding model (thread-safe singleton)."""
    global _model, _model_backend
    if _model is not None:
        return _model
    with _lock:
        if _model is not None:
            return _model
        backend = settings.embedding_backend
        try:
            model = (
                _OnnxModel(settings.embedding_model)
                if backend == BACKEND_ONNX
                else _STModel(settings.embedding_model)
            )
        except OnnxBackendUnavailable as e:
            # onnx cannot serve this model at all (no fastembed, or a model it
            # does not list); the original path stays available underneath.
            # Every other onnx failure propagates - see the module docstring.
            log.warning(
                "onnx embedding backend unavailable for %r: %s; falling back "
                "to sentence-transformers",
                settings.embedding_model, e,
            )
            model = _STModel(settings.embedding_model)
            backend = BACKEND_ST
        _model = model
        _model_backend = backend
        return _model


def current_backend() -> str:
    """Which backend is loaded (or configured, before the first load)."""
    return _model_backend or settings.embedding_backend


def embed_text(text: str) -> list[float]:
    """Generate embedding for a single text. Returns normalized 384-dim vector."""
    model = get_model()
    embedding = model.encode([text], normalize=True)
    return embedding[0].tolist()


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Generate embeddings for multiple texts. Returns list of normalized vectors."""
    if not texts:
        return []
    model = get_model()
    embeddings = model.encode(texts, normalize=True, batch_size=32)
    return [e.tolist() for e in embeddings]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two normalized vectors."""
    arr_a = np.array(a)
    arr_b = np.array(b)
    return float(np.dot(arr_a, arr_b))
