"""Settings loaded from environment variables or a local .env file (no extra deps)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    sec_user_agent: str = os.getenv("SEC_USER_AGENT", "")
    sec_max_rps: float = float(os.getenv("SEC_MAX_RPS", "8"))
    ollama_url: str = os.getenv("OLLAMA_URL", "http://localhost:11434")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
    data_dir: Path = Path(os.getenv("DATA_DIR", str(ROOT / "data")))
    neo4j_uri: str = os.getenv("NEO4J_URI", "")
    neo4j_user: str = os.getenv("NEO4J_USER", "neo4j")
    neo4j_password: str = os.getenv("NEO4J_PASSWORD", "")

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "terminal.db"


settings = Settings()


def load_industry(name: str) -> dict:
    """Load config/industries/<name>.yaml."""
    path = ROOT / "config" / "industries" / f"{name.lower()}.yaml"
    if not path.exists():
        available = [p.stem for p in (ROOT / "config" / "industries").glob("*.yaml")]
        raise SystemExit(f"No industry config '{name}'. Available: {', '.join(available)}")
    data = yaml.safe_load(path.read_text())
    data["slug"] = path.stem
    return data
