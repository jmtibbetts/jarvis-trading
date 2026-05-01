"""
lib/lmstudio.py — Unified LLM client.
v6.9.4: Robust Qwen3 thinking-token fix.
         - Strip <think>...</think> and partial <think> blocks from LLM output BEFORE
           declaring content empty. When finish=length, the response may contain post-think
           content even if the content field looks truncated.
         - Retry now uses a capped max_tokens (2000) so the model has budget for actual output.
         - Added chat_template_kwargs: {enable_thinking: false} for newer LM Studio builds.
         - The /no_think prefix is kept as a belt-and-suspenders measure.
v6.9.3: Add enable_thinking/thinking payload fields — only reliable way to disable Qwen3 thinking.
         Prompt-level /no_think is ignored by LM Studio; API payload field works correctly.
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
TIMEOUT       = 120.0  # 2 min — enough for big prompts; reduced from 300s to prevent shutdown hangs

# Max tokens to request on retry with /no_think — must stay under LM Studio's hard server cap.
# Set conservatively so the model has budget for actual output after thinking tokens are stripped.
RETRY_MAX_TOKENS = 2000

# ── Shutdown flag — set on SIGINT/SIGTERM so blocking calls abort cleanly ─────
_shutdown_event = threading.Event()

# _shutdown_event is set externally by main.py lifespan on shutdown

# NOTE: Do NOT register signal handlers here — uvicorn owns SIGINT/SIGTERM.
# The _shutdown_event is set by main.py's lifespan shutdown hook instead.

# ── Typed sentinel for thinking-only empty responses ──────────────────────────
class _EmptyThinkingResponse(RuntimeError):
    """Raised when LM Studio returns 0 chars because the model only produced <think> tokens."""
    pass

# ── Concurrency limiter — LM Studio supports N parallel inference slots ─────
# BoundedSemaphore(4) allows 4 concurrent LLM calls, matching LM Studio's 4-slot config.
# Increase LLM_MAX_PARALLEL env var to match your LM Studio "Parallel Requests" setting.
_LLM_MAX_PARALLEL = int(os.getenv("LLM_MAX_PARALLEL", "4"))
_llm_lock = threading.BoundedSemaphore(_LLM_MAX_PARALLEL)

# ── Model auto-resolution cache ───────────────────────────────────────────────
# When the DB/env model is the generic placeholder, we query /v1/models and cache
# the first real loaded model ID so we don't hit LM Studio on every call.
_resolved_model_cache: dict = {}   # keyed by base_url → resolved model id
_model_cache_lock = threading.Lock()

# ── Provider detection ─────────────────────────────────────────────────────────
OPENAI_COMPAT_PLATFORMS = {'lmstudio', 'ollama', 'openai', 'groq', 'deepseek', 'other'}
ANTHROPIC_PLATFORMS     = {'anthropic'}

PLACEHOLDER_MODELS = {'local-model', 'default', '', None}


def _strip_thinking_tokens(text: str) -> str:
    """
    Remove Qwen3 <think>...</think> blocks from LLM output.
    Handles:
      - Complete blocks: <think>...</think>content
      - Partial blocks (truncated mid-think): <think>...EOF  → returns ''
      - Blocks with no closing tag but content after last </think>
    """
    if not text:
        return text

    # Remove complete <think>...</think> blocks (non-greedy, handles multiline)
    cleaned = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)

    # If a <think> tag opened but never closed (truncated), drop everything from it
    if '<think>' in cleaned:
        cleaned = cleaned[:cleaned.index('<think>')]

    return cleaned.strip()


def _resolve_model(cfg: dict) -> str:
    """
    If the configured model name is a generic placeholder, query the server's
    /v1/models endpoint and return the first loaded model ID.
    Result is cached per base URL so subsequent calls are free.
    Falls back gracefully to the placeholder if the server is unreachable.
    """
    model = cfg.get('model', '')
    if model not in PLACEHOLDER_MODELS:
        return model  # Already a real name — nothing to do

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

    # Can't reach server — return placeholder as-is and let the call fail with a clear error
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
                # Cap max_tokens at RETRY_MAX_TOKENS if the DB value exceeds LM Studio's hard limit
                db_max = int(cfg.extra_field_2 or 2000)
                return {
                    'url':        (cfg.api_url or DEFAULT_URL).rstrip('/'),
                    'model':      cfg.extra_field_1 or DEFAULT_MODEL,
                    'api_key':    cfg.api_key or '',
                    'max_tokens': db_max,
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
        'max_tokens': int(os.getenv('LM_STUDIO_MAX_TOKENS', 2000)),
        'platform':   'lmstudio',
        'provider':   'openai_compat',
    }


def check_health() -> dict:
    """Ping the LLM server and return status."""
    cfg = get_llm_config()
    # Invalidate cache so health check always probes live
    with _model_cache_lock:
        _resolved_model_cache.pop(cfg.get('url', DEFAULT_URL), None)
    resolved = _resolve_model(cfg)
    try:
        if cfg['provider'] == 'openai_compat':
            r = httpx.get(f"{cfg['url']}/models", timeout=5,
                          headers=({'Authorization': f"Bearer {cfg['api_key']}"} if cfg['api_key'] else {}))
            if r.status_code == 200:
                models = r.json().get('data', [])
                return {'ok': True, 'platform': cfg['platform'], 'model': resolved,
                        'url': cfg['url'], 'models': [m.get('id') for m in models[:5]]}
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

    thinking=True  → full chain-of-thought (signal gen, position mgmt, Tier 5 review)
    thinking=False → /no_think prefix for fast classification (news tagging, heartbeat)
    """
    if _shutdown_event.is_set():
        raise RuntimeError("LLM call aborted — shutdown in progress")

    cfg = get_llm_config()
    # Auto-resolve placeholder model names (e.g. "local-model") to the real loaded model ID
    cfg['model'] = _resolve_model(cfg)
    effective_max = max_tokens or cfg['max_tokens']

    # Qwen3 thinking toggle — only applies to local models (lmstudio / ollama)
    # /no_think prefix suppresses chain-of-thought for fast, low-stakes calls
    effective_system = system
    if not thinking and cfg.get('platform') in ('lmstudio', 'ollama'):
        prefix = '/no_think\n\n'
        effective_system = prefix + (system or '')

    with _llm_lock:
        if _shutdown_event.is_set():
            raise RuntimeError("LLM call aborted — shutdown in progress")
        logger.debug(f"[LLM] Acquired lock → {cfg['platform']} @ {cfg['url']} model={cfg['model']} max_tokens={effective_max} thinking={thinking}")
        try:
            if cfg['provider'] == 'anthropic':
                return _call_anthropic(prompt, effective_system, effective_max, temperature, cfg)
            else:
                return _call_openai_compat(prompt, effective_system, effective_max, temperature, cfg, thinking_mode=thinking)
        except _EmptyThinkingResponse:
            # Qwen3 produced only <think> tokens — retry immediately with /no_think
            # Use RETRY_MAX_TOKENS (not the full requested amount) so the model has room to output
            if thinking and cfg.get('platform') in ('lmstudio', 'ollama'):
                logger.warning(f"[LLM] Retrying with /no_think + {RETRY_MAX_TOKENS} token cap — model produced thinking-only output on first attempt")
                fallback_system = '/no_think\n\n' + (system or '')
                try:
                    if cfg['provider'] == 'anthropic':
                        return _call_anthropic(prompt, fallback_system, RETRY_MAX_TOKENS, temperature, cfg)
                    else:
                        return _call_openai_compat(prompt, fallback_system, RETRY_MAX_TOKENS, temperature, cfg, thinking_mode=False)
                except _EmptyThinkingResponse:
                    # Both attempts exhausted — LM Studio token cap is overriding max_tokens.
                    # Return empty JSON array so the track degrades gracefully instead of crashing.
                    logger.error("[LLM] Both thinking and no_think attempts returned empty content. "
                                 "Check LM Studio → Model Settings → Max Response Tokens → set to 0 (unlimited).")
                    return "[]"
            raise RuntimeError("LLM returned empty content (thinking-only) and no retry possible")


def _call_openai_compat(prompt: str, system: str, max_tokens: int,
                         temperature: float, cfg: dict, thinking_mode: bool = False) -> str:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    headers = {"Content-Type": "application/json"}
    if cfg['api_key']:
        headers["Authorization"] = f"Bearer {cfg['api_key']}"

    # Sanitize messages — strip non-BMP Unicode (emoji, rare CJK, etc.) that
    # some LM Studio builds reject with a 400 even on large-context models.
    def _sanitize(s: str) -> str:
        return s.encode('utf-8', errors='replace').decode('utf-8') if s else s
    messages = [{"role": m["role"], "content": _sanitize(m["content"])} for m in messages]

    payload = {
        "model":        cfg['model'],
        "messages":     messages,
        "max_tokens":   max_tokens,   # OpenAI-compat field
        "num_predict":  max_tokens,   # llama.cpp / LM Studio native field (same value)
        "temperature":  temperature,
    }

    # Qwen3 thinking control — belt-and-suspenders approach:
    # 1. enable_thinking / thinking: Qwen3 native API fields
    # 2. chat_template_kwargs: newer LM Studio builds honour this to disable reasoning mode
    # 3. /no_think prefix in system prompt: handled by caller via effective_system
    if cfg.get('platform') in ('lmstudio', 'ollama'):
        payload["enable_thinking"]       = thinking_mode
        payload["thinking"]              = thinking_mode
        payload["chat_template_kwargs"]  = {"enable_thinking": thinking_mode}

    url = f"{cfg['url']}/chat/completions"
    logger.info(f"[LLM] → POST {url} | model={cfg['model']} | ~{len(prompt)//4} tokens prompt")

    # Use a streaming-capable client with a shorter connect timeout
    # so we don't block forever if LM Studio is gone
    timeout = httpx.Timeout(connect=10.0, read=TIMEOUT, write=30.0, pool=10.0)

    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.post(url, json=payload, headers=headers)
        if r.status_code == 400:
            logger.error(f"[LLM] 400 Bad Request body: {r.text[:500]}")
        r.raise_for_status()
        data    = r.json()
        choice  = data['choices'][0]
        raw_content = choice['message']['content'] or ''
        tokens  = data.get('usage', {}).get('completion_tokens', '?')
        finish  = choice.get('finish_reason', '?')
        logger.info(f"[LLM] ← {tokens} completion tokens | {len(raw_content)} chars | finish={finish}")

        if finish == 'length':
            logger.warning(f"[LLM] finish_reason=length — hit token cap ({tokens} tokens). "
                           "In LM Studio: Model Settings → Max Response Tokens → set to 0 (unlimited).")

        # Strip Qwen3 <think>...</think> blocks — these consume tokens but aren't output.
        # Even when finish=length (truncated), there may be valid content AFTER the think block.
        content = _strip_thinking_tokens(raw_content)

        if content:
            if raw_content != content:
                logger.debug(f"[LLM] Stripped thinking tokens → {len(content)} chars of real content remain")
            return content

        # Truly empty after stripping — raise typed sentinel for retry handler
        logger.warning(f"[LLM] Empty content after stripping thinking tokens ({tokens} tokens used) — will retry with /no_think")
        raise _EmptyThinkingResponse(f"Empty content after stripping {tokens} tokens")

    except _EmptyThinkingResponse:
        raise  # pass through to retry handler — do NOT wrap
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
