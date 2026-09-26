"""Revision-pinned local Chroma EF. Import and call register() before restoration.

Construction needs only the standard library. Inference lazily requires
sentence-transformers and its torch dependencies, with a predownloaded snapshot.
The snapshot's provenance belongs to the sealed artifact manifest; this module
never resolves model IDs remotely or downloads missing files.
"""
from collections.abc import Mapping
import math
from numbers import Real
from pathlib import Path
import re
from threading import Lock
from types import MappingProxyType
from typing import Any


_FIELDS = frozenset({
    'schema', 'model_id', 'revision', 'local_path', 'device', 'dtype',
    'query_prompt', 'document_prompt', 'max_chars', 'max_tokens',
    'normalize', 'trust_remote_code', 'dimension',
})

# Chroma reconstructs EFs per query. Retain only the most recent full config;
# live EF instances can still own their previously loaded model independently.
_MODEL_CACHE: dict[tuple[tuple[str, Any], ...], Any] = {}
_MODEL_LOCK = Lock()


class LocalEmbeddingFunction:
    """Explicit document/query roles with immutable, JSON-serializable config."""

    def __init__(self, config: dict[str, Any]):
        self.validate_config(config)
        self._config = MappingProxyType(dict(config))
        self._model = None

    @staticmethod
    def validate_config(config: dict[str, Any]) -> None:
        if not isinstance(config, dict) or frozenset(config) != _FIELDS:
            raise ValueError('Config must contain exactly the sb-embedding-1 fields')
        if config['schema'] != 'sb-embedding-1':
            raise ValueError('Unsupported embedding config schema')
        for key in ('model_id', 'revision', 'local_path', 'device', 'dtype'):
            if not isinstance(config[key], str) or not config[key].strip():
                raise ValueError(f'{key} must be a nonempty string')
        if re.fullmatch(r'[0-9a-f]{40}', config['revision']) is None:
            raise ValueError('revision must be a full lowercase commit SHA')
        if not Path(config['local_path']).is_absolute():
            raise ValueError('local_path must be an absolute pinned snapshot directory')
        if config['dtype'] not in ('float32', 'float16', 'bfloat16'):
            raise ValueError('dtype must be float32, float16 or bfloat16')
        for key in ('query_prompt', 'document_prompt'):
            if not isinstance(config[key], str):
                raise ValueError(f'{key} must be an explicit string, including empty')
        for key in ('max_chars', 'max_tokens', 'dimension'):
            if type(config[key]) is not int or config[key] < 1:
                raise ValueError(f'{key} must be a positive integer')
        for key in ('normalize', 'trust_remote_code'):
            if type(config[key]) is not bool:
                raise ValueError(f'{key} must be a boolean')

    def _load(self) -> Any:
        if self._model is None:
            config = self._config
            key = tuple(sorted(config.items()))
            # Serialize cache misses too: functools.lru_cache alone permits
            # duplicate constructor calls while the first load is in flight.
            with _MODEL_LOCK:
                if key not in _MODEL_CACHE:
                    _MODEL_CACHE.clear()
                    if not Path(config['local_path']).is_dir():
                        raise FileNotFoundError(config['local_path'])
                    from sentence_transformers import SentenceTransformer

                    model = SentenceTransformer(
                        model_name_or_path=config['local_path'], revision=config['revision'],
                        device=config['device'], trust_remote_code=config['trust_remote_code'],
                        local_files_only=True,
                        model_kwargs={'torch_dtype': config['dtype'], 'local_files_only': True},
                        config_kwargs={'local_files_only': True},
                        processor_kwargs={'local_files_only': True},
                    )
                    model.max_seq_length = config['max_tokens']
                    _MODEL_CACHE[key] = model
                self._model = _MODEL_CACHE[key]
        return self._model

    def _embed(self, input: list[str], prompt: str) -> list[list[float]]:
        if not isinstance(input, (list, tuple)) or any(not isinstance(text, str) for text in input):
            raise ValueError('input must be a batch of strings')
        if not input:
            return []
        texts = [text[:self._config['max_chars']] for text in input]
        # ST applies this prompt and computes its pooling length. An explicit
        # empty prompt disables the model default; manual prepending would not.
        vectors = self._load().encode(
            texts, prompt=prompt, normalize_embeddings=False,
            convert_to_numpy=True, show_progress_bar=False,
        )
        result: list[list[float]] = []
        try:
            if isinstance(vectors, (str, bytes, Mapping)):
                raise ValueError('Embeddings must be a dense matrix')
            for vector in vectors:
                if isinstance(vector, (str, bytes, Mapping)):
                    raise ValueError('Embedding must be a dense vector')
                values = list(vector)
                if len(values) != self._config['dimension']:
                    raise ValueError('Embedding dimension does not match config')
                if any(isinstance(value, bool) or not isinstance(value, Real) for value in values):
                    raise ValueError('Embedding components must be real numbers')
                row = [float(value) for value in values]
                if not all(math.isfinite(value) for value in row):
                    raise ValueError('Embedding components must be finite')
                norm = math.hypot(*row)
                if not math.isfinite(norm) or norm == 0:
                    raise ValueError('Embedding norm must be finite and nonzero')
                result.append([value / norm for value in row] if self._config['normalize'] else row)
        except (TypeError, OverflowError) as exc:
            raise ValueError('Encoder returned invalid dense embeddings') from exc
        if len(result) != len(input):
            raise ValueError('Embedding count does not match input count')
        return result

    def __call__(self, input: list[str]) -> list[list[float]]:
        return self._embed(input, self._config['document_prompt'])

    def embed_query(self, input: list[str]) -> list[list[float]]:
        return self._embed(input, self._config['query_prompt'])

    @staticmethod
    def name() -> str:
        return 'secondbrain_local'

    def get_config(self) -> dict[str, Any]:
        return dict(self._config)

    @staticmethod
    def build_from_config(config: dict[str, Any]) -> 'LocalEmbeddingFunction':
        return LocalEmbeddingFunction(config)

    def validate_config_update(self, old_config: dict[str, Any], new_config: dict[str, Any]) -> None:
        self.validate_config(old_config)
        self.validate_config(new_config)
        if old_config != new_config:
            raise ValueError('Embedding configuration cannot be changed in place')

    def default_space(self) -> str:
        return 'cosine'

    def supported_spaces(self) -> list[str]:
        return ['cosine']

    def is_legacy(self) -> bool:
        return False


def make_local_ef(config: dict[str, Any]) -> LocalEmbeddingFunction:
    return LocalEmbeddingFunction(config)


def register() -> bool:
    """Register for Chroma restoration; False means Chroma/API is unavailable.

    Broken installations and registration failures propagate to the caller.
    Registration never imports sentence-transformers or loads weights.
    """
    try:
        from chromadb.utils import embedding_functions
    except ModuleNotFoundError as exc:
        if exc.name == 'chromadb':
            return False
        raise
    registrar = getattr(embedding_functions, 'register_embedding_function', None)
    if registrar is None:
        return False
    registrar(LocalEmbeddingFunction)
    return True
