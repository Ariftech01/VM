"""Synthetic data generation and benchmark harness."""

from gitmem.eval.benchmarks import calculate_baseline_savings, run_benchmark
from gitmem.eval.loaders import load_local_subset
from gitmem.eval.synthetic import SyntheticDataset, generate_synthetic_dataset

__all__ = [
    "SyntheticDataset",
    "calculate_baseline_savings",
    "generate_synthetic_dataset",
    "load_local_subset",
    "run_benchmark",
]
