"""
LM Studio client — OpenAI-compatible local LLM interface.
"""
import os, json, re, logging
import httpx
from app.database import get_db, PlatformConfig

logger = logging.getLogger(__name__)

DEFAULT_URL   = "http://localhost:1234/v1"
DEFAULT_MODEL = "local-model"
TIMEOUT       = 180.0  # seconds — local models can be slow

def get_lm_config() -> tuple[str, str]:
    """Get LM Studio URL and model from DB or env."""
    try:
        with get_db() as db:
            cfg = db.query(PlatformConfig).filter(
                PlatformConfig.platform.like('lmstudio%'),
                PlatformConfig.is_active == True
            ).first()
            if cfg:
                url = cfg.api_url or DEFAULT_URL
                model = cfg.extra_field_1 or DEFAULT_MODEL
                return url.rstrip('/'), model
    except:
        pass
    return os.getenv('LM_STUDIO_URL', DEFAULT_URL).rstrip('/'), os.getenv('LM_STUDIO_MODEL', DEFAULT_MODEL)

def call_lm_studio(prompt: str, system: str = None, max_tokens: int = 2048,
                    temperature: float = 0.3) -> str:
    """
    Call the local LM Studio inference server.
    Returns the raw text response.
    """
    url, model = get_lm_config()
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False
    }

    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.post(f"{url}/chat/completions", json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error(f"[LMStudio] Error: {e}")
        raise

def parse_json(text: str) -> list | dict | None:
    """
    Extract JSON from LLM response — handles markdown code blocks and raw JSON.
    """
    if not text:
        return None
    
    # Try code blocks first
    for pattern in [r'```(?:json)?\s*([\s\S]*?)```', r'`([\s\S]*?)`']:
        m = re.search(pattern, text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1).strip())
            except:
                pass
    
    # Try raw JSON — find first [ or { and match to end
    for start_char, end_char in [('[', ']'), ('{', '}')]:
        idx = text.find(start_char)
        if idx >= 0:
            # Find matching end bracket
            depth = 0
            in_str = False
            escape = False
            for i, ch in enumerate(text[idx:], idx):
                if escape:
                    escape = False
                    continue
                if ch == '\\' and in_str:
                    escape = True
                    continue
                if ch == '"' and not escape:
                    in_str = not in_str
                if not in_str:
                    if ch in ('{', '['):
                        depth += 1
                    elif ch in ('}', ']'):
                        depth -= 1
                        if depth == 0:
                            try:
                                return json.loads(text[idx:i+1])
                            except:
                                break
    return None
