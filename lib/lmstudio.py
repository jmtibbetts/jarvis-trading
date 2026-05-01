"""
lib/lmstudio.py — Unified LLM client.
v7.4: Default max_tokens changed from 32768 → 2000 (matches LM Studio server cap); no more per-file token juggling
v7.3: Add max_output_tokens to payload (LM Studio native v1 field — overrides UI "Limit Response Length" cap)
v7.2: Default thinking=False — LM Studio's 150-token server cap causes all thinking
      calls to fail immediately. Thinking mode is now opt-in via THINKING_ENABLED env
      var or DB config extra_field_3 = 'thinking_on'. Eliminates wasted 3-4s retry
      overhead on every signal evaluation.
v7.1: Send maxTokens (camelCase) in payload to override LM Studio server default
v7.0: DeepSeek-R1 support — strips <think>...</think> blocks before JSON parsing,
      removes Qwen3-specific /no_think toggle, auto-detects R1 models.
v6.9.1: Auto-resolve model ID from /v1/models when configured name is placeholder.
v6.9: 4-slot parallel semaphore (LLM_MAX_PARALLEL=4), 120k context / 32768 output tokens default.
v6.2: Global threading lock — only one LLM call at a time (local model can't parallelize).
      Supports LM Studio, Ollama, OpenAI, Anthropic, Groq, DeepSeek.
      Graceful shutdown: _shutdown_event breaks blocking LLM calls on SIGINT/SIGTERM.
"""
import os, json, re, logging, threading
import httpx
from app.database import get_db, PlatformConfig

logger = logging.getLogger(__name__)

DEFAULT_URL   = "http://localhost:1234/v1"
DEFAULT_MODEL = "local-model"
TIMEOUT       = 180.0  # 3 min — R1 reasoning can take longer than Qwen3

# ── Thinking mode control ──────────────────────────────────────────────────────
# Set THINKING_ENABLED=true in env OR extra_field_3='thinking_on' in DB config
# to re-enable thinking mode. Default is OFF because LM Studio's server-side
# 150-token cap causes all thinking calls to fail with empty output.
_THINKING_ENABLED_ENV = os.getenv("THINKING_ENABLED", "false").lower() in ("true", "1", "yes")

def _is_thinking_enabled(cfg: dict) -> bool:
    """
    Returns True only if thinking mode is explicitly enabled — either via:
      - THINKING_ENABLED=true environment variable, OR
      - DB config extra_field_3 = 'thinking_on'
    Default is False to avoid wasted retry overhead from LM Studio's token cap.
    """
    if _THINKING_ENABLED_ENV:
        return True
    db_flag = (cfg.get('thinking_flag') or '').lower()
    return db_flag == 'thinking_on'

# ── Shutdown flag — set on SIGINT/SIGTERM so blocking calls abort cleanly ─────
_shutdown_event = threading.Event()

# _shutdown_event is set externally by main.py lifespan on shutdown

# NOTE: Do NOT register signal handlers here — uvicorn owns SIGINT/SIGTERM.
# The _shutdown_event is set by main.py's lifespan shutdown hook instead.

# ── Typed sentinel for thinking-only empty responses (Qwen3 legacy) ───────────
class _EmptyThinkingResponse(RuntimeError):
    """Raised when model returns 0 chars (Qwen3 thinking-only token exhaustion)."""
    pass

# ── Concurrency limiter — LM Studio supports N parallel inference slots ─────
_LLM_MAX_PARALLEL = int(os.getenv("LLM_MAX_PARALLEL", "4"))
_llm_lock = threading.BoundedSemaphore(_LLM_MAX_PARALLEL)

# ── Model auto-resolution cache ───────────────────────────────────────────────
_resolved_model_cache: dict = {}
_model_cache_lock = threading.Lock()

# ── Provider detection ─────────────────────────────────────────────────────────
OPENAI_COMPAT_PLATFORMS = {'lmstudio', 'ollama', 'openai', 'groq', 'deepseek', 'other'}
ANTHROPIC_PLATFORMS     = {'anthropic'}

PLACEHOLDER_MODELS = {'local-model', 'default', '', None}

# R1 model name fragments — used to detect R1-family models for think-tag stripping
R1_MODEL_FRAGMENTS = ('deepseek-r1', 'r1-distill', 'r1_distill')


def _is_r1_model(model_id: str) -> bool:
    """Returns True if the resolved model ID looks like a DeepSeek-R1 family model."""
    if not model_id:
        return False
    lower = model_id.lower()
    return any(frag in lower for frag in R1_MODEL_FRAGMENTS)


def _strip_think_tags(text: str) -> tuple[str, int]:
    """
    Remove <think>...</think> blocks from R1 model output.
    Returns (cleaned_text, think_token_estimate).
    """
    think_content = ''
    think_match = re.search(r'<think>(.*?)</think>', text, re.DOTALL)
    if think_match:
        think_content = think_match.group(1)
        text = text[:think_match.start()] + text[think_match.end():]
    text = text.strip()
    think_tokens = len(think_content) // 4
    return text, think_tokens


def _resolve_model(cfg: dict) -> str:
    """
    If the configured model name is a generic placeholder, query the server's
    /v1/models endpoint and return the first loaded model ID.
    """
    model = cfg.get('model', '')
    if model not in PLACEHOLDER_MODELS:
        return model

    base_url = cfg.get('url', DEFAULT_URL)
    with _model_cache_lock:
        if base_url in _resolved_model_cache:
            return _resolved_model_cache[base_url]

    try:
        headers = {}
        if cfg.get('api_key'):
            headers['Authorization'] = f"Bearer {cfg['api_key']}"
        r = httpx.get(f"{base_url}/models", headers=headers, timeout=5.0)
        if r.status_code == 200:
            models = r.json().get('data', [])
            if models:
                real_model = models[0].get('id', model)
                logger.info(f"[LLM] Auto-resolved model '{model}' → '{real_model}' from {base_url}/models")
                with _model_cache_lock:
                    _resolved_model_cache[base_url] = real_model
                return real_model
    except Exception as e:
        logger.warning(f"[LLM] Could not auto-resolve model from {base_url}/models: {e}")

    return model


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
                    'url':           (cfg.api_url or DEFAULT_URL).rstrip('/'),
                    'model':         cfg.extra_field_1 or DEFAULT_MODEL,
                    'api_key':       cfg.api_key or '',
                    'max_tokens':    int(cfg.extra_field_2 or 2000),   # LM Studio server cap default
                    'thinking_flag': cfg.extra_field_3 or '',
                    'platform':      platform,
                    'provider':      'anthropic' if platform == 'anthropic' else 'openai_compat',
                }
    except Exception as e:
        logger.debug(f"[LLM] DB config lookup failed: {e}")

    return {
        'url':           os.getenv('LM_STUDIO_URL', DEFAULT_URL).rstrip('/'),
        'model':         os.getenv('LM_STUDIO_MODEL', DEFAULT_MODEL),
        'api_key':       os.getenv('OPENAI_API_KEY', ''),
        'max_tokens':    int(os.getenv('LM_STUDIO_MAX_TOKENS', 2000)),   # LM Studio server cap default
        'thinking_flag': os.getenv('THINKING_ENABLED', ''),
        'platform':      'lmstudio',
        'provider':      'openai_compat',
    }


def check_health() -> dict:
    """Ping the LLM server and return status."""
    cfg = get_llm_config()
    with _model_cache_lock:
        _resolved_model_cache.pop(cfg.get('url', DEFAULT_URL), None)
    resolved = _resolve_model(cfg)
    try:
        if cfg['provider'] == 'openai_compat':
            r = httpx.get(f"{cfg['url']}/models", timeout=5,
                          headers=({'Authorization': f"Bearer {cfg['api_key']}"} if cfg['api_key'] else {}))
            if r.status_code == 200:
                models = r.json().get('data', [])
                thinking_on = _is_thinking_enabled(cfg)
                return {'ok': True, 'platform': cfg['platform'], 'model': resolved,
                        'url': cfg['url'], 'models': [m.get('id') for m in models[:5]],
                        'r1_mode': _is_r1_model(resolved),
                        'thinking_enabled': thinking_on}
            return {'ok': False, 'platform': cfg['platform'], 'url': cfg['url'],
                    'status_code': r.status_code}
        elif cfg['provider'] == 'anthropic':
            return {'ok': True, 'platform': 'anthropic', 'model': cfg['model']}
    except Exception as e:
        return {'ok': False, 'platform': cfg['platform'], 'url': cfg['url'], 'error': str(e)}
    return {'ok': False, 'error': 'Unknown provider'}


def call_lm_studio(prompt: str, system: str = None, max_tokens: int = None,
                   temperature: float = 0.15, thinking: bool = True) -> str:
    """
    Unified LLM call — serialized via global lock so local models aren't overwhelmed.
    Aborts immediately if shutdown has been signalled.

    THINKING MODE (v7.2):
    The 'thinking' parameter is now gated by _is_thinking_enabled(cfg).
    If thinking is not enabled (default), all calls use /no_think mode regardless
    of what callers pass. This eliminates wasted retry overhead from LM Studio's
    server-side token cap causing all thinking calls to return empty output.

    To re-enable thinking: set THINKING_ENABLED=true in .env, or set
    extra_field_3 = 'thinking_on' in the LLM's DB config row.

    For R1 models: thinking parameter is always ignored — R1 uses <think> tags natively.
    """
    if _shutdown_event.is_set():
        raise RuntimeError("LLM call aborted — shutdown in progress")

    cfg = get_llm_config()
    cfg['model'] = _resolve_model(cfg)
    effective_max = max_tokens or cfg['max_tokens']
    is_r1 = _is_r1_model(cfg['model'])

    # ── Gate thinking mode ─────────────────────────────────────────────────────
    # If LM Studio has a token cap making thinking calls fail, skip thinking entirely
    effective_thinking = thinking and _is_thinking_enabled(cfg) and not is_r1

    # ── System prompt modification ─────────────────────────────────────────────
    effective_system = system
    if not is_r1 and not effective_thinking and cfg.get('platform') in ('lmstudio', 'ollama'):
        effective_system = '/no_think\n\n' + (system or '')
        prompt = '/no_think\n\n' + prompt

    with _llm_lock:
        if _shutdown_event.is_set():
            raise RuntimeError("LLM call aborted — shutdown in progress")
        logger.debug(
            f"[LLM] Acquired lock → {cfg['platform']} @ {cfg['url']} "
            f"model={cfg['model']} max_tokens={effective_max} "
            f"{'[R1-mode]' if is_r1 else f'thinking={effective_thinking}'}"
        )
        try:
            if cfg['provider'] == 'anthropic':
                return _call_anthropic(prompt, effective_system, effective_max, temperature, cfg)
            else:
                return _call_openai_compat(prompt, effective_system, effective_max, temperature, cfg, is_r1=is_r1)
        except _EmptyThinkingResponse:
            # Qwen3 burned all tokens on <think> tags — LM Studio has a hard server cap
            # overriding our max_tokens. Strategy: retry with /no_think prefix and
            # progressively SMALLER max_tokens so the model is forced to emit JSON
            # before hitting the cap. Works even when cap is as low as 150 tokens.
            if not is_r1:
                retry_system = '/no_think\n\n' + (system or '').replace('/no_think\n\n', '')
                retry_prompt  = '/no_think\n\n' + prompt.replace('/no_think\n\n', '')
                # Try descending token budgets: 120 → 80 → 60
                for retry_max in (120, 80, 60):
                    logger.warning(
                        f"[LLM] Empty content (thinking-only) — retrying with max_tokens={retry_max} + /no_think. "
                        "Permanent fix: LM Studio → Model Settings → Max Response Tokens → 0 (unlimited)."
                    )
                    try:
                        return _call_openai_compat(retry_prompt, retry_system, retry_max, temperature, cfg, is_r1=False)
                    except _EmptyThinkingResponse:
                        continue  # try smaller budget
                    except Exception as retry_err:
                        logger.error(f"[LLM] Retry (max={retry_max}) failed: {retry_err}")
                        break
                logger.error("[LLM] All retries exhausted — LM Studio cap too tight. Returning empty string.")
                return ""
            raise RuntimeError("LLM returned empty content (thinking-only) and no retry possible")


def _call_openai_compat(prompt: str, system: str, max_tokens: int,
                         temperature: float, cfg: dict, is_r1: bool = False) -> str:
    headers = {"Content-Type": "application/json"}
    if cfg['api_key']:
        headers["Authorization"] = f"Bearer {cfg['api_key']}"

    def _sanitize(s: str) -> str:
        return s.encode('utf-8', errors='replace').decode('utf-8') if s else s

    messages = []
    if system:
        messages.append({"role": "system", "content": _sanitize(system)})
    messages.append({"role": "user", "content": _sanitize(prompt)})

    payload = {
        "model":             cfg['model'],
        "messages":          messages,
        "max_tokens":        max_tokens,   # OpenAI-compat (snake_case)
        "maxTokens":         max_tokens,   # LM Studio legacy camelCase
        "max_output_tokens": max_tokens,   # LM Studio native v1 API field (overrides UI cap)
        "num_predict":       max_tokens,   # llama.cpp / Ollama
        "temperature":       temperature,
    }

    url = f"{cfg['url']}/chat/completions"
    no_think_active = system and '/no_think' in system
    mode_tag = 'R1-mode' if is_r1 else ('thinking=True' if not no_think_active else 'thinking=False')
    logger.info(f"[LLM] -> POST {url} | model={cfg['model']} | ~{len(prompt)//4} tokens prompt | {mode_tag}")

    timeout = httpx.Timeout(connect=10.0, read=TIMEOUT, write=30.0, pool=10.0)

    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.post(url, json=payload, headers=headers)
        if r.status_code == 400:
            logger.error(f"[LLM] 400 Bad Request body: {r.text[:500]}")
        r.raise_for_status()
        data    = r.json()
        choice  = data['choices'][0]
        content = choice['message']['content'] or ''
        tokens  = data.get('usage', {}).get('completion_tokens', '?')
        finish  = choice.get('finish_reason', '?')

        # ── R1: strip <think> tags ─────────────────────────────────────────────
        if is_r1 and content:
            content, think_tokens = _strip_think_tags(content)
            if think_tokens > 0:
                logger.info(f"[LLM] ← R1 reasoning: ~{think_tokens} think tokens | {len(content)} chars output | finish={finish}")
            else:
                logger.info(f"[LLM] ← {tokens} completion tokens | {len(content)} chars | finish={finish}")
        else:
            logger.info(f"[LLM] ← {tokens} completion tokens | {len(content)} chars | finish={finish}")

        if finish == 'length':
            logger.warning(
                f"[LLM] finish_reason=length — hit token cap ({tokens} tokens). "
                "In LM Studio: Model Settings → Max Response Tokens → set to 0 (unlimited)."
            )

        # Guard: empty content (Qwen3 thinking-only exhaustion)
        if not content or not content.strip():
            logger.warning(f"[LLM] Empty content ({tokens} thinking-only tokens) — will retry with /no_think")
            raise _EmptyThinkingResponse(f"Empty content after {tokens} tokens")

        return content

    except _EmptyThinkingResponse:
        raise
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
    timeout = httpx.Timeout(connect=10.0, read=TIMEOUT, write=30.0, pool=10.0)
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.post(url, json=payload, headers=headers)
        r.raise_for_status()
        data = r.json()
        return data['content'][0]['text']
    except Exception as e:
        raise RuntimeError(f"Anthropic API error: {e}")


def parse_json(text: str):
    """Extract JSON from LLM response, handling markdown fences, leading text, and R1 think tags."""
    if not text:
        return None

    # Strip any residual R1 think tags that slipped through
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()

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

