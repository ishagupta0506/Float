from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
import os

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent

ENV_FILES = (BACKEND_ROOT / ".env", PROJECT_ROOT / ".env")


def load_env_files(paths=ENV_FILES) -> None:
    # .env must beat values a terminal inherited earlier; later files win, so the root .env has the last word.
    for path in paths:
        load_dotenv(path, override=True)


load_env_files()


def _optional(name: str) -> str:
    return os.getenv(name, "").strip()


@dataclass(frozen=True)
class Settings:
    cafe_client_id: str
    cafe_client_secret: str
    cafe_merchant_email: str
    supplier_client_id: str
    supplier_client_secret: str
    client_payer_email: str
    llm_api_key: str
    llm_base_url: str
    llm_model: str
    starting_cash_cents: int
    paypal_base_url: str
    ai_mode_setting: str = "auto"

    @property
    def cafe_ready(self) -> bool:
        return bool(self.cafe_client_id and self.cafe_client_secret and self.client_payer_email)

    @property
    def supplier_ready(self) -> bool:
        return bool(self.supplier_client_id and self.supplier_client_secret)

    @property
    def paypal_ready(self) -> bool:
        return self.cafe_ready and self.supplier_ready

    @property
    def llm_ready(self) -> bool:
        return bool(self.llm_api_key)

    @property
    def ai_mode(self) -> str:
        if self.ai_mode_setting == "mock":
            return "mock"
        return "llm" if self.llm_ready else "mock"

    @property
    def ai_mode_warning(self) -> str | None:
        if self.ai_mode_setting == "llm" and not self.llm_ready:
            return "AI_MODE is llm but LLM_API_KEY is empty, so the mock reader is in use."
        return None


def load_settings() -> Settings:
    raw_cash = _optional("STARTING_CASH_CENTS") or "2000"
    try:
        starting_cash = int(raw_cash)
    except ValueError:
        starting_cash = 2000
    return Settings(
        cafe_client_id=_optional("CAFE_PAYPAL_CLIENT_ID"),
        cafe_client_secret=_optional("CAFE_PAYPAL_CLIENT_SECRET"),
        cafe_merchant_email=_optional("CAFE_MERCHANT_EMAIL"),
        supplier_client_id=_optional("SUPPLIER_PAYPAL_CLIENT_ID"),
        supplier_client_secret=_optional("SUPPLIER_PAYPAL_CLIENT_SECRET"),
        client_payer_email=_optional("CLIENT_PAYER_EMAIL"),
        llm_api_key=_optional("LLM_API_KEY"),
        llm_base_url=_optional("LLM_BASE_URL") or "https://api.groq.com/openai/v1",
        llm_model=_optional("LLM_MODEL") or "llama-3.3-70b-versatile",
        starting_cash_cents=starting_cash,
        paypal_base_url=_optional("PAYPAL_BASE_URL") or "https://api-m.sandbox.paypal.com",
        ai_mode_setting=(_optional("AI_MODE").lower() or "auto")
        if _optional("AI_MODE").lower() in {"auto", "mock", "llm"}
        else "auto",
    )
