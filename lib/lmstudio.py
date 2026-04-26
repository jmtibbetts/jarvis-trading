"""
lib/lmstudio.py — Unified LLM client.
v6.1: Global threading lock — only one LLM call at a time (local model can't parallelize).
      Supports LM Studio, Ollama, OpenAI, Anthropic, Groq, DeepSeek.
"""
import os, json, re, logging, threading
import httpx
from app.database import get_db, PlatformConfig

logger = logging.getLogger(__name__)

DEFAULT_URL   = "http://localhost:1234/v1"
DEFAULT_MODEL = "local-model"
TIMEOUT       = 300.0  # 5 min — local models can be slow on big prompts

# ── Global serialization lock — local LLMs can't handle concurrent requests ───
_llm_lock = threading.Lock()

# ── Provider detection ─────────────────────────────────────────────────────────
OPENAI_COMPAT_PLATFORMS = {'lmstudio', 'ollama', 'openai', 'groq', 'deepseek', 'other'}
ANTHROPIC_PLATFORMS     = {'anthropic'}

def get_llm_config() -> dict:
    """Read active LLM config from DB. Falls back to env vars / defaults."""
    try:
        with get_db() as db:
            all_cfgs = db.query(PlatformConfig).filter(
                PlatformConfig.is_active == True
            ).all()
            llm_platforms = {'lmstudio', 'ollama', 'openai', 'anthropic', 'groq', 'deepseek'}
            llm_cfgs = [c for c in all_cfgs if c.platform and c.platform.lower() in llm_platforms]
            cfg = next((c for c in llm_cfgs if c.is_default), None) or \
                  (llm_cfgs[0] if llm_cfgs else None)
            if cfg:
                platform = (cfg.platform or 'lmstudio').lower()
                return {
                    'url':        (cfg.api_url or DEFAULT_URL).rstrip('/'),
                    'model':      cfg.extra_field_1 or DEFAULT_MODEL,
                    'api_key':    cfg.api_key or '',
                    'max_tokens': int(cfg.extra_field_2 or 4096),
                    'platform':   platform,
                    'provider':   'anthropic' if platform == 'anthropic' else 'openai_compat',
                }
    except Exception as e:
        logger.debug(f"[LLM] DB config lookup failed: {e}")

    # Fallback to env
    return {
        'url':        os.getenv('LM_STUDIO_URL', DEFAULT_URL).rstrip('/'),
        'model':      os.getenv('LM_STUDIO_MODEL', DEFAULT_MODEL),
        'api_key':    os.getenv('OPENAI_API_KEY', ''),
        'max_tokens': int(os.getenv('LM_STUDIO_MAX_TOKENS', 4096)),
        'platform':   'lmstudio',
        'provider':   'openai_compat',
    }


def check_health() -> dict:
    """Ping the LLM server and return status."""
    cfg = get_llm_config()
    try:
        if cfg['provider'] == 'openai_compat':
            r = httpx.get(f"{cfg['url']}/models", timeout=5,
                          headers=({'Authorization': f"Bearer {cfg['api_key']}"} if cfg['api_key'] else {}))
            if r.status_code == 200:
                models = r.json().get('data', [])
                return {'ok': True, 'platform': cfg['platform'], 'model': cfg['model'],
                        'url': cfg['url'], 'models': [m.get('id') for m in models[:5]]}
            return {'ok': False, 'platform': cfg['platform'], 'url': cfg['url'],
                    'status_code': r.status_code}
        elif cfg['provider'] == 'anthropic':
            return {'ok': True, 'platform': 'anthropic', 'model': cfg['model']}
    except Exception as e:
        return {'ok': False, 'platform': cfg['platform'], 'url': cfg['url'], 'error': str(e)}
    return {'ok': False, 'error': 'Unknown provider'}


def call_lm_studio(prompt: str, system: str = None, max_tokens: int = None,
                   temperature: float = 0.15) -> str:
    """
    Unified LLM call — serialized via global lock so local models aren't overwhelmed.
    """
    cfg = get_llm_config()
    effective_max = max_tokens or cfg['max_tokens']

    with _llm_lock:
        logger.debug(f"[LLM] Acquired lock → {cfg['platform']} @ {cfg['url']} model={cfg['model']} max_tokens={effective_max}")
        if cfg['provider'] == 'anthropic':
            return _call_anthropic(prompt, system, effective_max, temperature, cfg)
        else:
            return _call_openai_compat(prompt, system, effective_max, temperature, cfg)


def _call_openai_compat(prompt: str, system: str, max_tokens: int,
                         temperature: float, cfg: dict) -> str:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    headers = {"Content-Type": "application/json"}
    if cfg['api_key']:
        headers["Authorization"] = f"Bearer {cfg['api_key']}"

    payload = {
        "model":       cfg['model'],
        "messages":    messages,
        "max_tokens":  max_tokens,
        "temperature": temperature,
    }

    url = f"{cfg['url']}/chat/completions"
    logger.info(f"[LLM] → POST {url} | model={cfg['model']} | ~{len(prompt)//4} tokens prompt")

    try:
        r = httpx.post(url, json=payload, headers=headers, timeout=TIMEOUT)
        r.raise_for_status()
        data    = r.json()
        content = data['choices'][0]['message']['content']
        tokens  = data.get('usage', {}).get('completion_tokens', '?')
        logger.info(f"[LLM] ← {tokens} completion tokens | {len(content)} chars")
        return content
    except httpx.TimeoutException:
        raise RuntimeError(f"LLM timeout after {TIMEOUT}s — is {cfg['platform']} running at {cfg['url']}?")
    except Exception as e:
        raise RuntimeError(f"LLM call failed ({cfg['platform']} @ {cfg['url']}): {e}")


def _call_anthropic(prompt: str, system: str, max_tokens: int,
                    temperature: float, cfg: dict) -> str:
    headers = {
        "x-api-key":         cfg['api_key'],
        "anthropic-version": "2023-06-01",
        "Content-Type":      "application/json",
    }
    payload = {
        "model":       cfg['model'],
        "max_tokens":  max_tokens,
        "temperature": temperature,
        "messages":    [{"role": "user", "content": prompt}],
    }
    if system:
        payload["system"] = system

    url = "https://api.anthropic.com/v1/messages"
    try:
        r = httpx.post(url, json=payload, headers=headers, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        return data['content'][0]['text']
    except Exception as e:
        raise RuntimeError(f"Anthropic API error: {e}")


def parse_json(text: str):
    """Extract JSON from LLM response, handling markdown fences and leading text."""
    if not text:
        return None

    text = re.sub(r'```(?:json)?\s*', '', text)
    text = re.sub(r'```\s*$', '', text, flags=re.MULTILINE)
    text = text.strip()

    try:
        return json.loads(text)
    except:
        pass

    arr_match = re.search(r'\[\s*\{.*\}\s*\]', text, re.DOTALL)
    if arr_match:
        try:
            return json.loads(arr_match.group())
        except:
            pass

    obj_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text, re.DOTALL)
    if obj_match:
        try:
            return json.loads(obj_match.group())
        except:
            pass

    start = text.find('[')
    end   = text.rfind(']')
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end+1])
        except:
            pass

    logger.warning(f"[LLM] Could not parse JSON (len={len(text)}): {text[:300]}")
    return None
