"""Load configuration from YAML."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml


@dataclass
class Config:
    """Configuration for tablefp."""

    dsn: str
    tables: List[str] = field(default_factory=list)
    exclude_columns: List[str] = field(default_factory=list)
    exclude_column_patterns: List[str] = field(default_factory=list)
    index_dir: str = "./fp_index"
    max_workers: int = 4
    dtype_groups: Optional[List[str]] = None
    skip_text_avg_len: int = 500
    min_containment: float = 0.3
    candidate_min_containment: float = 0.3
    min_template_distinct: int = 5
    store_type: str = "local"
    storage_dsn: str = ""
    fuzzy: dict = field(default_factory=lambda: {
        "enabled": False,
        "ngram_size": 3,
        "max_nd": 2_000_000,
        "columns": [],
        "alpha": 0.8,
        "verify_sim_threshold": 0.4,
        "metric": "jaccard",
    })

    @classmethod
    def from_yaml(cls, path: str) -> "Config":
        """Load config from YAML file."""
        try:
            with open(path, "r") as f:
                data = yaml.safe_load(f)
        except FileNotFoundError:
            raise FileNotFoundError(f"Config file not found: {path}")
        except yaml.YAMLError as e:
            raise ValueError(f"Invalid YAML in config file: {e}")

        if "dsn" not in data:
            raise ValueError("Missing 'dsn' in config file")

        return cls(
            dsn=data["dsn"],
            tables=data.get("tables", []),
            exclude_columns=data.get("exclude_columns", []),
            exclude_column_patterns=data.get("exclude_column_patterns", []),
            index_dir=data.get("index_dir", "./fp_index"),
            max_workers=data.get("max_workers", 4),
            dtype_groups=data.get("dtype_groups"),
            skip_text_avg_len=data.get("skip_text_avg_len", 500),
            min_containment=data.get("min_containment", 0.3),
            candidate_min_containment=data.get(
                "candidate_min_containment", data.get("min_containment", 0.3)
            ),
            min_template_distinct=data.get("min_template_distinct", 5),
            store_type=data.get("store_type", "local"),
            storage_dsn=data.get("storage_dsn", ""),
            fuzzy=data.get("fuzzy", {
                "enabled": False,
                "ngram_size": 3,
                "max_nd": 2_000_000,
                "columns": [],
                "alpha": 0.8,
                "verify_sim_threshold": 0.4,
                "metric": "jaccard",
            }),
        )


def parse_dsn(dsn: str) -> dict:
    """Parse postgres:// URL into connection params dict.

    Format: postgres://user:password@host:port/dbname
    """
    from urllib.parse import urlparse

    parsed = urlparse(dsn)
    params = {"dbname": parsed.path.lstrip("/")}

    if parsed.username:
        params["user"] = parsed.username
    if parsed.password:
        params["password"] = parsed.password
    if parsed.hostname:
        params["host"] = parsed.hostname
    if parsed.port:
        params["port"] = parsed.port

    return params