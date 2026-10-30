"""Runtime configuration, read from environment variables (or a local .env file)."""
import os

from dotenv import load_dotenv

load_dotenv()

# --- Qloo -------------------------------------------------------------------
QLOO_API_KEY = os.getenv("QLOO_API_KEY", "").strip()
QLOO_BASE_URL = os.getenv("QLOO_BASE_URL", "https://hackathon.api.qloo.com").rstrip("/")
# Demo mode: synthetic data, no Qloo key needed. Never use for the real submission.
QLOO_MOCK = os.getenv("QLOO_MOCK", "").lower() in {"1", "true", "yes"} or not QLOO_API_KEY

# --- LLM (any OpenAI-compatible provider; Gemini by default) ------------------
LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip()
LLM_BASE_URL = os.getenv(
    "LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/"
)
LLM_MODEL = os.getenv("LLM_MODEL", "gemini-3.1-flash-lite")
LLM_ENABLED = bool(LLM_API_KEY)
LLM_TIMEOUT_S = float(os.getenv("LLM_TIMEOUT_S", "25"))       # per model call
AGENT_BUDGET_S = float(os.getenv("AGENT_BUDGET_S", "75"))    # whole agent run
# "low" makes thinking models (Gemini 3.x) answer much faster; "" to disable.
LLM_REASONING_EFFORT = os.getenv("LLM_REASONING_EFFORT", "low")

# --- App --------------------------------------------------------------------
CACHE_DIR = os.getenv("CACHE_DIR", ".cache")  # never commit this folder
MAX_AGENT_STEPS = int(os.getenv("MAX_AGENT_STEPS", "14"))

COUNTRIES = {
    "United States": "US",
    "India": "IN",
    "United Kingdom": "GB",
    "Canada": "CA",
    "Australia": "AU",
    "Germany": "DE",
}

# Age buckets accepted by signal.demographics.age ("" = all ages)
AGE_BUCKETS = {
    "": "All ages",
    "24_and_younger": "24 and younger",
    "25_to_29": "25 to 29",
    "30_to_34": "30 to 34",
    "35_to_44": "35 to 44",
    "45_to_54": "45 to 54",
    "55_and_older": "55 and older",
}
