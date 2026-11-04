import os
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from pydantic import BaseModel, SecretStr

from .errors import ReviewerError

ENVIRONMENT = {
    "openai_endpoint": "AZURE_OPENAI_ENDPOINT",
    "openai_key": "AZURE_OPENAI_API_KEY",
    "deployment": "AZURE_OPENAI_DEPLOYMENT",
    "search_endpoint": "AZURE_SEARCH_ENDPOINT",
    "search_key": "AZURE_SEARCH_ADMIN_KEY",
    "di_endpoint": "AZURE_DI_ENDPOINT",
    "di_key": "AZURE_DI_API_KEY",
}
ALIASES = {"openai_key": "Faundry_key"}
ENDPOINTS = ("openai_endpoint", "search_endpoint", "di_endpoint")


class Settings(BaseModel):
    openai_endpoint: str
    openai_key: SecretStr
    deployment: str
    search_endpoint: str
    search_key: SecretStr
    search_index: str = "project-docs"
    di_endpoint: str
    di_key: SecretStr
    structured_outputs: bool = True
    timeout: float = 60

    @classmethod
    def load(cls, env_file: Path | None = None) -> "Settings":
        if env_file:
            load_dotenv(env_file, override=False)
        values = {field: read(name, ALIASES.get(field)) for field, name in ENVIRONMENT.items()}
        missing = [ENVIRONMENT[field] for field, value in values.items() if not value]
        if missing:
            raise ReviewerError("Missing configuration: " + ", ".join(missing))
        for field in ENDPOINTS:
            check_endpoint(ENVIRONMENT[field], values[field])
        if not values["openai_endpoint"].rstrip("/").endswith("/openai/v1"):
            raise ReviewerError("AZURE_OPENAI_ENDPOINT must end with /openai/v1")
        structured = read("AZURE_OPENAI_STRUCTURED_OUTPUTS").lower() or "true"
        if structured not in ("true", "false"):
            raise ReviewerError("AZURE_OPENAI_STRUCTURED_OUTPUTS must be true or false")
        return cls(**values, structured_outputs=structured == "true",
                   search_index=read("AZURE_SEARCH_INDEX") or "project-docs")


def read(name: str, alias: str | None = None) -> str:
    return os.getenv(name, "").strip() or (os.getenv(alias, "").strip() if alias else "")


def check_endpoint(name: str, value: str) -> None:
    url = urlsplit(value)
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ReviewerError(f"{name} must be an HTTPS URL without credentials or query")
