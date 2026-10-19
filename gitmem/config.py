"""Load and validate GitMem's editable YAML configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = Path(__file__).parent / "config" / "default.yaml"


@dataclass(frozen=True, slots=True)
class ScoringWeights:
    """Weights for the explainable memory score."""

    importance: float
    relevance: float
    redundancy: float
    recency: float
    pin_boost: float


@dataclass(frozen=True, slots=True)
class ScoringThresholds:
    """Score and budget thresholds that control tier decisions."""

    keep_verbatim: float
    summarize: float
    hot_token_budget: int


@dataclass(frozen=True, slots=True)
class GitMemConfig:
    """Validated application configuration needed by the core engine."""

    scoring_weights: ScoringWeights
    scoring_thresholds: ScoringThresholds
    cost_per_1k_input_tokens: float
    default_token_budget: int
    embedding_model: str
    database_path: str
    raw: dict[str, Any]


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> GitMemConfig:
    """Read YAML configuration and reject missing or unsafe core values."""
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError(
            "GitMem configuration requires PyYAML. Install the project dependencies first."
        ) from exc

    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as config_file:
        raw = yaml.safe_load(config_file)

    if not isinstance(raw, dict):
        raise ValueError(f"Configuration at {config_path} must contain a YAML mapping")

    scoring = _mapping(raw, "scoring")
    weights_raw = _mapping(scoring, "weights")
    thresholds_raw = _mapping(scoring, "thresholds")
    retrieval = _mapping(raw, "retrieval")
    embedding = _mapping(raw, "embeddings")
    dashboard = _mapping(raw, "dashboard")
    storage = _mapping(raw, "storage")

    weights = ScoringWeights(
        importance=_bounded_float(weights_raw, "importance"),
        relevance=_bounded_float(weights_raw, "relevance"),
        redundancy=_bounded_float(weights_raw, "redundancy"),
        recency=_bounded_float(weights_raw, "recency"),
        pin_boost=_bounded_float(weights_raw, "pin_boost"),
    )
    if abs(
        weights.importance
        + weights.relevance
        + weights.redundancy
        + weights.recency
        + weights.pin_boost
        - 1.0
    ) > 1e-6:
        raise ValueError("scoring.weights must sum to 1.0")

    thresholds = ScoringThresholds(
        keep_verbatim=_bounded_float(thresholds_raw, "keep_verbatim"),
        summarize=_bounded_float(thresholds_raw, "summarize"),
        hot_token_budget=_positive_int(thresholds_raw, "hot_token_budget"),
    )
    if thresholds.summarize > thresholds.keep_verbatim:
        raise ValueError("scoring.thresholds.summarize cannot exceed keep_verbatim")

    cost = _nonnegative_float(dashboard, "cost_per_1k_input_tokens")
    return GitMemConfig(
        scoring_weights=weights,
        scoring_thresholds=thresholds,
        cost_per_1k_input_tokens=cost,
        default_token_budget=_positive_int(retrieval, "default_token_budget"),
        embedding_model=_nonempty_string(embedding, "model"),
        database_path=_nonempty_string(storage, "database_path"),
        raw=raw,
    )


def _mapping(parent: dict[str, Any], key: str) -> dict[str, Any]:
    value = parent.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"Configuration key '{key}' must be a mapping")
    return value


def _bounded_float(parent: dict[str, Any], key: str) -> float:
    value = parent.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"Configuration key '{key}' must be a number")
    result = float(value)
    if not 0.0 <= result <= 1.0:
        raise ValueError(f"Configuration key '{key}' must be between 0 and 1")
    return result


def _positive_int(parent: dict[str, Any], key: str) -> int:
    value = parent.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"Configuration key '{key}' must be a positive integer")
    return value


def _nonnegative_float(parent: dict[str, Any], key: str) -> float:
    value = parent.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        raise ValueError(f"Configuration key '{key}' must be a non-negative number")
    return float(value)


def _nonempty_string(parent: dict[str, Any], key: str) -> str:
    value = parent.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Configuration key '{key}' must be a non-empty string")
    return value.strip()
