"""Artifact store: local (sqlite + .npy) or remote (Greenplum table)."""

import io
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import psycopg2
import sqlite3

logger = logging.getLogger(__name__)


@dataclass
class ColumnRecord:
    schema: str
    table_name: str
    column_name: str
    dtype_group: str
    n: int
    nd: int
    min_val: Optional[float] = None
    max_val: Optional[float] = None
    quantiles: Optional[List[float]] = None
    avg_len: Optional[float] = None
    npy_path: str = ""
    ngrams_path: str = ""
    indexed_at: str = ""

    @classmethod
    def from_row(cls, row: tuple) -> "ColumnRecord":
        return cls(
            schema=row[0], table_name=row[1], column_name=row[2],
            dtype_group=row[3], n=row[4], nd=row[5],
            min_val=row[6], max_val=row[7],
            quantiles=json.loads(row[8]) if row[8] else None,
            avg_len=row[9], npy_path=row[10] if len(row) > 10 else "",
            ngrams_path=row[11] if len(row) > 11 else "",
            indexed_at=row[12] if len(row) > 12 else "",
        )


class ArtifactStore:
    """Local artifact store: SQLite metadata + .npy files."""

    def __init__(self, index_dir: str):
        self.index_dir = Path(index_dir)
        self.catalog_db = self.index_dir / "catalog.db"
        self._init_catalog()

    def _init_catalog(self):
        os.makedirs(self.index_dir, exist_ok=True)
        conn = sqlite3.connect(str(self.catalog_db))
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS columns (
                schema TEXT NOT NULL, table_name TEXT NOT NULL,
                column_name TEXT NOT NULL, dtype_group TEXT NOT NULL,
                n INTEGER, nd INTEGER, min_val REAL, max_val REAL,
                quantiles_json TEXT, avg_len REAL, npy_path TEXT,
                ngrams_path TEXT, indexed_at TEXT,
                PRIMARY KEY (schema, table_name, column_name)
            )
        """)
        # Migrate: add ngrams_path if missing (old schema had 12 cols)
        existing = [r[1] for r in cur.execute("PRAGMA table_info(columns)").fetchall()]
        if "ngrams_path" not in existing:
            cur.execute("ALTER TABLE columns ADD COLUMN ngrams_path TEXT DEFAULT ''")
            existing.append("ngrams_path")
        conn.commit()
        logger.debug(f"catalog.db columns ({len(existing)}): {existing}")
        conn.close()

    def upsert_column(self, record: ColumnRecord):
        conn = sqlite3.connect(str(self.catalog_db))
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO columns VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT (schema, table_name, column_name) DO UPDATE SET
               dtype_group=excluded.dtype_group, n=excluded.n, nd=excluded.nd,
               min_val=excluded.min_val, max_val=excluded.max_val,
               quantiles_json=excluded.quantiles_json, avg_len=excluded.avg_len,
               npy_path=excluded.npy_path, ngrams_path=excluded.ngrams_path,
               indexed_at=excluded.indexed_at""",
            (record.schema, record.table_name, record.column_name, record.dtype_group,
             record.n, record.nd, record.min_val, record.max_val,
             json.dumps(record.quantiles) if record.quantiles else None,
             record.avg_len, record.npy_path, record.ngrams_path,
             record.indexed_at))
        conn.commit()
        conn.close()

    def get_column(self, schema: str, table_name: str, column_name: str) -> Optional[ColumnRecord]:
        conn = sqlite3.connect(str(self.catalog_db))
        cur = conn.cursor()
        row = cur.execute(
            "SELECT * FROM columns WHERE schema=? AND table_name=? AND column_name=?",
            (schema, table_name, column_name)).fetchone()
        conn.close()
        return ColumnRecord.from_row(row) if row else None

    def list_columns(self) -> List[ColumnRecord]:
        conn = sqlite3.connect(str(self.catalog_db))
        rows = conn.execute("SELECT * FROM columns").fetchall()
        conn.close()
        return [ColumnRecord.from_row(r) for r in rows]

    def save_hashes(self, schema: str, table_name: str, column_name: str, hashes: np.ndarray) -> str:
        table_dir = self.index_dir / schema / table_name
        os.makedirs(table_dir, exist_ok=True)
        npy_path = table_dir / f"{column_name}.npy"
        np.save(str(npy_path), hashes)
        return str(npy_path)

    def load_hashes(self, npy_path: str) -> np.ndarray:
        return np.load(npy_path, mmap_mode="r")

    def save_ngrams(self, schema: str, table_name: str, column_name: str, ngrams: np.ndarray) -> str:
        table_dir = self.index_dir / schema / table_name
        os.makedirs(table_dir, exist_ok=True)
        ngrams_path = table_dir / f"{column_name}.ngrams.npy"
        np.save(str(ngrams_path), ngrams)
        return str(ngrams_path)

    def load_ngrams(self, path: str) -> np.ndarray:
        return np.load(path, mmap_mode="r")

    def clear(self):
        import shutil
        if self.index_dir.exists():
            shutil.rmtree(self.index_dir)
        self._init_catalog()


class DBArtifactStore:
    """DB-backed artifact store: stores hashes as BYTEA in Greenplum table."""

    def __init__(self, dsn: str):
        self.dsn = dsn
        self._init_table()

    def _get_conn(self):
        return psycopg2.connect(self.dsn)

    def _init_table(self):
        conn = self._get_conn()
        try:
            conn.cursor().execute("""
                CREATE TABLE IF NOT EXISTS tablefp_columns (
                    schema_name TEXT,
                    table_name TEXT,
                    column_name TEXT,
                    dtype_group TEXT,
                    n BIGINT,
                    nd BIGINT,
                    min_val DOUBLE PRECISION,
                    max_val DOUBLE PRECISION,
                    quantiles_json TEXT,
                    avg_len DOUBLE PRECISION,
                    hashes BYTEA,
                    ngrams BYTEA,
                    indexed_at TIMESTAMP,
                    PRIMARY KEY (schema_name, table_name, column_name)
                )
            """)
            conn.commit()
        finally:
            conn.close()

    def upsert_column(self, record: ColumnRecord):
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            quantiles = json.dumps(record.quantiles) if record.quantiles else None
            cur.execute(
                """INSERT INTO tablefp_columns VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (schema_name, table_name, column_name) DO UPDATE SET
                   dtype_group=EXCLUDED.dtype_group, n=EXCLUDED.n, nd=EXCLUDED.nd,
                   min_val=EXCLUDED.min_val, max_val=EXCLUDED.max_val,
                   quantiles_json=EXCLUDED.quantiles_json, avg_len=EXCLUDED.avg_len,
                   hashes=EXCLUDED.hashes, ngrams=EXCLUDED.ngrams, indexed_at=EXCLUDED.indexed_at""",
                (record.schema, record.table_name, record.column_name, record.dtype_group,
                 record.n, record.nd, record.min_val, record.max_val, quantiles,
                 record.avg_len, psycopg2.Binary(b""), psycopg2.Binary(b""), record.indexed_at))
            conn.commit()
        finally:
            conn.close()

    def get_column(self, schema: str, table_name: str, column_name: str) -> Optional[ColumnRecord]:
        conn = self._get_conn()
        try:
            row = conn.cursor().execute(
                "SELECT schema_name, table_name, column_name, dtype_group, n, nd, "
                "min_val, max_val, quantiles_json, avg_len, '', '', indexed_at "
                "FROM tablefp_columns WHERE schema_name=%s AND table_name=%s AND column_name=%s",
                (schema, table_name, column_name)).fetchone()
            return ColumnRecord.from_row(row) if row else None
        finally:
            conn.close()

    def list_columns(self) -> List[ColumnRecord]:
        conn = self._get_conn()
        try:
            rows = conn.cursor().execute(
                "SELECT schema_name, table_name, column_name, dtype_group, n, nd, "
                "min_val, max_val, quantiles_json, avg_len, '', '', indexed_at "
                "FROM tablefp_columns").fetchall()
            return [ColumnRecord.from_row(r) for r in rows]
        finally:
            conn.close()

    def save_hashes(self, schema: str, table_name: str, column_name: str, hashes: np.ndarray) -> str:
        buf = io.BytesIO()
        np.save(buf, hashes)
        data = buf.getvalue()
        npy_path = f"db://{schema}.{table_name}.{column_name}"

        conn = self._get_conn()
        try:
            conn.cursor().execute(
                "UPDATE tablefp_columns SET hashes=%s "
                "WHERE schema_name=%s AND table_name=%s AND column_name=%s",
                (psycopg2.Binary(data), schema, table_name, column_name))
            conn.commit()
        finally:
            conn.close()
        return npy_path

    def load_hashes(self, npy_path: str) -> np.ndarray:
        # npy_path format: "db://schema.table_name.column_name"
        parts = npy_path.replace("db://", "").split(".")
        if len(parts) < 3:
            return np.array([], dtype=np.int64)
        schema, table_name = parts[0], parts[1]
        column_name = ".".join(parts[2:])

        conn = self._get_conn()
        try:
            row = conn.cursor().execute(
                "SELECT hashes FROM tablefp_columns "
                "WHERE schema_name=%s AND table_name=%s AND column_name=%s",
                (schema, table_name, column_name)).fetchone()
        finally:
            conn.close()

        if not row or row[0] is None:
            return np.array([], dtype=np.int64)

        buf = io.BytesIO(bytes(row[0]))
        return np.load(buf)

    def save_ngrams(self, schema: str, table_name: str, column_name: str, ngrams: np.ndarray) -> str:
        buf = io.BytesIO()
        np.save(buf, ngrams)
        data = buf.getvalue()
        ngrams_path = f"db://{schema}.{table_name}.{column_name}.ngrams"

        conn = self._get_conn()
        try:
            conn.cursor().execute(
                "UPDATE tablefp_columns SET ngrams=%s "
                "WHERE schema_name=%s AND table_name=%s AND column_name=%s",
                (psycopg2.Binary(data), schema, table_name, column_name))
            conn.commit()
        finally:
            conn.close()
        return ngrams_path

    def load_ngrams(self, path: str) -> np.ndarray:
        parts = path.replace("db://", "").split(".")
        if len(parts) < 3:
            return np.array([], dtype=np.int64)
        schema, table_name = parts[0], parts[1]
        column_name = ".".join(parts[2:])

        conn = self._get_conn()
        try:
            row = conn.cursor().execute(
                "SELECT ngrams FROM tablefp_columns "
                "WHERE schema_name=%s AND table_name=%s AND column_name=%s",
                (schema, table_name, column_name)).fetchone()
        finally:
            conn.close()

        if not row or row[0] is None:
            return np.array([], dtype=np.int64)

        buf = io.BytesIO(bytes(row[0]))
        return np.load(buf)

    def clear(self):
        conn = self._get_conn()
        try:
            conn.cursor().execute("DROP TABLE IF EXISTS tablefp_columns")
            conn.commit()
        finally:
            conn.close()
        self._init_table()


def create_store(config: "Config") -> ArtifactStore:
    """Factory: returns local or DB store based on config."""
    if config.store_type == "db":
        dsn = config.storage_dsn or config.dsn
        return DBArtifactStore(dsn)
    return ArtifactStore(config.index_dir)
