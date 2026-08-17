"""
lib/lmstudio.py — Unified LLM client.
v6.9.6: Fix enable_thinking placement — must be top-level in LM Studio 0.4.x payload.
         chat_template_kwargs is NOT needed; LM Studio reads enable_thinking directly.
         Removed conflicting "thinking" key which is not a valid LM Studio field.
v6.9.5: Restore max_tokens to 16384 — LM Studio "Limit Response Length" was never checked.
         The 2000/1024 API-level cap WAS the problem all along. Model has 130k context, unlimited output.
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
import os, json, re, logging, threading, time
from collections import namedtuple
from contextlib import contextmanager
from urllib.parse import urlsplit
import httpx
from app.database import get_db, PlatformConfig

logger = logging.getLogger(__name__)

DEFAULT_URL   = "http://localhost:1234/v1"
DEFAULT_MODEL = "local-model"


def _wsl_windows_host() -> str | None:
    """The Windows host's address as seen from inside WSL2, or None.

    WHY THIS EXISTS. LM Studio runs on Windows; JARVIS now runs in WSL2.
    Under WSL2's default NAT networking `localhost` inside the VM is the
    VM, not the host — so the documented default of
    `http://localhost:1234/v1` reaches nothing, and every LLM call fails
    with a connection error that looks like LM Studio being down.

    The host is reachable at the default gateway, but that address is
    ASSIGNED PER BOOT and changes whenever WSL restarts. Hard-coding
    whatever it happens to be today produces a config that works until the
    next reboot and then fails in a way nobody connects to the reboot — so
    it is resolved at runtime instead.

    Returns None when not under WSL and when the gateway cannot be read.
    It does NOT promise the address it returns is LM Studio — under
    mirrored networking the default route is the LAN router, not the
    Windows host. Every caller probes before believing it.
    """
    if not os.getenv("WSL_DISTRO_NAME"):
        try:
            with open("/proc/version", encoding="utf-8", errors="replace") as fh:
                if "microsoft" not in fh.read().lower():
                    return None
        except OSError:
            return None
    try:
        with open("/proc/net/route", encoding="utf-8") as fh:
            for line in fh.readlines()[1:]:
                parts = line.split()
                # Destination 00000000 is the default route; Gateway is
                # little-endian hex.
                if len(parts) > 2 and parts[1] == "00000000":
                    gw = int(parts[2], 16)
                    # A zero gateway is a route with no next hop. Formatting
                    # it yields "0.0.0.0", which is not an error anywhere it
                    # is passed and fails much later as a connect error.
                    if gw == 0:
                        continue
                    return ".".join(str((gw >> (8 * i)) & 0xFF) for i in range(4))
    except (OSError, ValueError):
        return None
    return None


# ── LM Studio endpoint resolution ─────────────────────────────────────────────
# WHY AN ADDRESS IS NOT CONFIGURATION HERE. LM Studio runs on the Windows
# host; JARVIS runs in WSL2. Both addresses the host answers on are assigned,
# not chosen: the LAN address by DHCP, the WSL-side gateway per boot. One of
# them had been written into PlatformConfig, where it outranked every runtime
# lookup — so the config worked until the next lease or reboot and then failed
# in a way nobody connected to either event.
#
# A DISCOVERED address is process-runtime state. It is cached for the life of
# the process and re-derived by the next one. Only an address the operator
# typed on purpose may persist, and it has to say so.

LM_STUDIO_DEFAULT_PORT = 1234

PROV_OPERATOR  = "OPERATOR_OVERRIDE"   # api_url in the DB, explicitly marked
PROV_ENV       = "ENV_OVERRIDE"        # LM_STUDIO_URL
PROV_LOCALHOST = "LOCALHOST"           # loopback, an automatic candidate
PROV_WSL       = "WSL_DISCOVERED"      # WSL default gateway, automatic
PROV_NONE      = "UNAVAILABLE"         # no endpoint selected

# An override is a statement of intent. An automatic candidate is a guess the
# resolver is allowed to abandon. Only the second kind may be walked past.
EXPLICIT_PROVENANCE = frozenset({PROV_ENV, PROV_OPERATOR})

# The whole point of the provenance mark: everything else is runtime state.
PERSISTABLE_PROVENANCE = frozenset({PROV_OPERATOR})

# ── What a probe found ────────────────────────────────────────────────────────
# "Down" and "up with nothing loaded" are different problems with different
# fixes, and collapsing them sends the operator to check the network when the
# answer is to load a model. Kept distinct all the way out to /api/llm/health.
ST_AVAILABLE   = "AVAILABLE"
ST_NO_MODELS   = "API_REACHABLE_NO_MODELS"
ST_INVALID     = "API_INVALID_RESPONSE"
ST_UNREACHABLE = "NETWORK_UNREACHABLE"

# How far a candidate got. Used to report the most informative failure rather
# than the last one tried.
_STATUS_RANK = {ST_UNREACHABLE: 0, ST_INVALID: 1, ST_NO_MODELS: 2, ST_AVAILABLE: 3}

ProbeResult = namedtuple("ProbeResult", "status models detail")
Candidate   = namedtuple("Candidate", "url provenance status models detail")
Resolution  = namedtuple("Resolution", "url provenance status candidates")

# A non-AVAILABLE resolution is re-probed after this long. Successes hold for
# the life of the process — that is the "rediscover after restart" rule, not a
# cache tuning knob.
ENDPOINT_RETRY_S = 30.0

_endpoint_cache: dict = {}   # (env_url, db_url, db_provenance) → (Resolution, at)
_endpoint_lock = threading.Lock()


def _port_of(value: str) -> int | None:
    """The port in a URL, or a bare port number, or None."""
    value = (value or "").strip()
    if not value:
        return None
    if value.isdigit():
        port = int(value)
        return port if 0 < port < 65536 else None
    try:
        return urlsplit(value).port
    except ValueError:      # malformed authority — not a usable hint
        return None


def _probe_endpoint(base_url: str, api_key: str = "", timeout: float = 3.0) -> ProbeResult:
    """How far `base_url` got towards being a usable LLM server.

    A 200 is not enough. Reaching *an* endpoint is not reaching *the*
    endpoint: a router's admin page, a proxy, or an unrelated service on a
    recycled address all answer 200, and the mistake then surfaces hours
    later as unparseable model output rather than as a bad address.

    But "answered correctly with nothing loaded" is not a network problem
    and must not be reported as one — the fix is to load a model, and an
    operator sent to check cables will not find it.
    """
    try:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        r = httpx.get(f"{base_url}/models", headers=headers, timeout=timeout)
    except Exception as e:
        return ProbeResult(ST_UNREACHABLE, (), f"{type(e).__name__}: {e}")

    if r.status_code != 200:
        return ProbeResult(ST_INVALID, (), f"HTTP {r.status_code} from /v1/models")
    try:
        data = r.json()
    except Exception:
        return ProbeResult(ST_INVALID, (), "/v1/models did not return JSON")
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        return ProbeResult(ST_INVALID, (), "/v1/models returned no model list")

    entries = data["data"]
    if not entries:
        return ProbeResult(ST_NO_MODELS, (), "server is up with no model loaded")
    ids = tuple(str(m["id"]) for m in entries
                if isinstance(m, dict) and m.get("id"))
    if not ids:
        return ProbeResult(ST_INVALID, (), "model list entries carry no id")
    return ProbeResult(ST_AVAILABLE, ids, None)


def _candidate(url: str, prov: str, probe: ProbeResult) -> Candidate:
    return Candidate(url, prov, probe.status, probe.models, probe.detail)


def _discover_endpoint(db_url: str, db_provenance: str, api_key: str) -> Resolution:
    """Resolve the LM Studio base URL. Never writes anything, anywhere."""
    env_url  = (os.getenv("LM_STUDIO_URL") or "").strip().rstrip("/")
    db_url   = (db_url or "").strip().rstrip("/")
    operator_url = db_url if (db_provenance or "").strip().upper() == PROV_OPERATOR else ""

    # An address the operator typed is authoritative, INCLUDING when it is
    # loopback and including when it is broken. It is still probed — the
    # health check has to be able to say which way it is broken — but a
    # failure is reported against that endpoint, never used as licence to go
    # looking for a different machine. Substituting a working server for a
    # chosen one is how you end up inferring against something nobody picked.
    for url, prov in ((env_url, PROV_ENV), (operator_url, PROV_OPERATOR)):
        if not url:
            continue
        probe = _probe_endpoint(url, api_key)
        if probe.status != ST_AVAILABLE:
            logger.warning("[LLM] The configured LM Studio endpoint %s (%s) is "
                           "%s — %s. Nothing was substituted for it.",
                           url, prov, probe.status, probe.detail)
        return Resolution(url, prov, probe.status, (_candidate(url, prov, probe),))

    # No override. Everything below is an automatic candidate: a guess, held
    # only until a better-informed one turns up.
    #
    # The port is the one part of an unmarked DB row that is real
    # configuration — nothing assigns it — so it is still worth reading even
    # when its host has been discarded as stale.
    port = (_port_of(os.getenv("LM_STUDIO_PORT") or "") or _port_of(db_url)
            or LM_STUDIO_DEFAULT_PORT)

    # Loopback is right on a native host and under WSL mirrored networking,
    # and reaches nothing under WSL2 NAT. The shipped default is a candidate,
    # not a choice, so it is probed rather than assumed.
    attempts = [(f"http://127.0.0.1:{port}/v1", PROV_LOCALHOST)]
    host = _wsl_windows_host()
    if host:
        attempts.append((f"http://{host}:{port}/v1", PROV_WSL))

    candidates = []
    for url, prov in attempts:
        probe = _probe_endpoint(url, api_key)
        candidates.append(_candidate(url, prov, probe))
        if probe.status == ST_AVAILABLE:
            if prov == PROV_WSL:
                logger.info("[LLM] LM Studio found at %s via the WSL gateway. "
                            "This address is per-boot runtime state and is "
                            "deliberately not stored.", url)
            return Resolution(url, prov, ST_AVAILABLE, tuple(candidates))
        logger.warning("[LLM] Candidate %s (%s): %s — %s",
                       url, prov, probe.status, probe.detail)

    # Report the furthest any candidate got. "One of them was up with no
    # model loaded" is a materially different message from "nothing
    # answered", and it is the one that names the actual fix.
    status = max((c.status for c in candidates),
                 key=lambda s: _STATUS_RANK[s], default=ST_UNREACHABLE)
    return Resolution(None, PROV_NONE, status, tuple(candidates))


def resolve_endpoint(db_url: str = "", db_provenance: str = "",
                     api_key: str = "", force: bool = False) -> Resolution:
    """The LM Studio endpoint for this process, probed and cached here only."""
    key = ((os.getenv("LM_STUDIO_URL") or ""), db_url or "", db_provenance or "")
    if not force:
        with _endpoint_lock:
            hit = _endpoint_cache.get(key)
        if hit:
            resolution, at = hit
            if resolution.status == ST_AVAILABLE or (time.monotonic() - at) < ENDPOINT_RETRY_S:
                return resolution

    resolution = _discover_endpoint(db_url, db_provenance, api_key)
    with _endpoint_lock:
        _endpoint_cache[key] = (resolution, time.monotonic())
    return resolution


_STATUS_FIX = {
    ST_NO_MODELS:   "Load a model in LM Studio — the address is not the problem.",
    ST_INVALID:     "Something answered, but not an OpenAI-compatible server. "
                    "Check the port and that LM Studio's server is enabled.",
    ST_UNREACHABLE: "Start LM Studio with its server enabled, or set "
                    "LM_STUDIO_URL to an explicit address.",
}


def endpoint_problem(res: Resolution) -> str:
    """Why `res` is not usable, phrased so the reader knows what to go fix."""
    fix = _STATUS_FIX.get(res.status, "")
    if res.provenance in EXPLICIT_PROVENANCE:
        # Named, not diagnosed away: the operator chose this address, so the
        # message is about that address and no other.
        detail = res.candidates[0].detail if res.candidates else ""
        return (f"The configured LM Studio endpoint {res.url} "
                f"({res.provenance}) is {res.status}"
                + (f" — {detail}" if detail else "")
                + f". It is configured explicitly, so no other endpoint was "
                  f"tried. {fix}")
    tried = ", ".join(f"{c.url} [{c.provenance}] → {c.status}"
                      for c in res.candidates) or "nothing (no candidates)"
    return (f"LM Studio is not usable — {res.status}. Tried: {tried}. {fix}")


def reset_endpoint_cache() -> None:
    """Drop the per-process resolution. Called by the health check so it
    always reports what is true now, not what was true at startup."""
    with _endpoint_lock:
        _endpoint_cache.clear()
# One GPU serving 4 parallel slots means each individual request takes
# materially longer than it would alone — the old 90s ceiling produced
# "LLM timeout after 56/66/90s" failures that tripped the circuit breaker
# and cascaded into every queued caller. Sized for the parallel case.
TIMEOUT       = float(os.getenv("LLM_TIMEOUT", "180"))

# Thinking calls on a large model are a different workload from fast
# classification and get their own, larger budget. Measured on the day
# qwen3-32b replaced the smaller model: average latency went 12-17s to
# 35s and the slowest healthy answer took 234s, so the 90s ceilings baked
# into call sites produced 229 timeout-and-cooldown errors in one hour
# while the server was UP the whole time. This floor overrides a caller's
# smaller request_timeout whenever thinking=True — callers sized their
# numbers for the old model, and a too-small budget doesn't cancel the
# generation, it abandons a nearly-finished answer and pays for it anyway.
THINKING_TIMEOUT = float(os.getenv("LLM_THINKING_TIMEOUT", "300"))

# Max tokens to request on retry with /no_think — must stay under LM Studio's hard server cap.
# Set conservatively so the model has budget for actual output after thinking tokens are stripped.
RETRY_MAX_TOKENS = 16384

# ── Shutdown flag — set on SIGINT/SIGTERM so blocking calls abort cleanly ─────
_shutdown_event = threading.Event()
_llm_circuit_lock = threading.Lock()
_llm_circuit_open_until = 0.0


def _llm_circuit_remaining():
    with _llm_circuit_lock:
        return max(0.0, _llm_circuit_open_until - time.monotonic())


def get_llm_cooldown() -> float:
    """Seconds until the LLM circuit closes again (0 when healthy). Callers
    that can afford to WAIT (batch signal generation) should sleep this out
    instead of failing instantly — one provider hiccup was killing every
    remaining batch in a run within the same second."""
    return _llm_circuit_remaining()


def _cool_down_llm(seconds=15.0):
    global _llm_circuit_open_until
    with _llm_circuit_lock:
        _llm_circuit_open_until = max(
            _llm_circuit_open_until,
            time.monotonic() + max(1.0, float(seconds)),
        )


@contextmanager
def _llm_slot(queue_timeout=None):
    cooldown = _llm_circuit_remaining()
    if cooldown > 0:
        raise RuntimeError(f"Local LLM cooling down for {cooldown:.1f}s after a provider failure")
    if queue_timeout is None:
        acquired = _llm_lock.acquire()
    else:
        acquired = _llm_lock.acquire(timeout=max(0.0, float(queue_timeout)))
    if not acquired:
        raise RuntimeError(f"LLM queue busy after {queue_timeout:.1f}s")
    try:
        cooldown = _llm_circuit_remaining()
        if cooldown > 0:
            raise RuntimeError(f"Local LLM cooling down for {cooldown:.1f}s after a provider failure")
        yield
    finally:
        _llm_lock.release()

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
_LLM_MAX_PARALLEL = int(os.getenv("LLM_MAX_PARALLEL", "1"))
_llm_lock = threading.BoundedSemaphore(_LLM_MAX_PARALLEL)

# ── Model auto-resolution cache ───────────────────────────────────────────────
# When the DB/env model is the generic placeholder, we query /v1/models and cache
# the first real loaded model ID so we don't hit LM Studio on every call.
#
# The cache EXPIRES (TTL below). A permanent cache holds the PREVIOUS
# model's name after the operator swaps loads — and a request naming an
# unloaded model triggers LM Studio's just-in-time loading, putting TWO
# models in VRAM the GPU may not have room for. Following the loaded
# model within minutes is the whole point of the placeholder config.
_resolved_model_cache: dict = {}   # keyed by base_url → (model id, resolved_at)
_model_cache_lock = threading.Lock()
MODEL_RESOLVE_TTL_S = 300.0

# ── Provider detection ─────────────────────────────────────────────────────────
OPENAI_COMPAT_PLATFORMS = {'lmstudio', 'ollama', 'openai', 'groq', 'deepseek', 'other'}
ANTHROPIC_PLATFORMS     = {'anthropic'}

PLACEHOLDER_MODELS = {'local-model', 'default', '', None}

# The model named in the most recent RESPONSE (not the request) — see the
# capture in call_llm. None until the first successful call.
_last_served_model: str | None = None


def last_served_model() -> str | None:
    return _last_served_model


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
        hit = _resolved_model_cache.get(base_url)
        if hit and (time.time() - hit[1]) < MODEL_RESOLVE_TTL_S:
            return hit[0]

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
                    _resolved_model_cache[base_url] = (real_model, time.time())
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
                db_max = int(cfg.extra_field_2 or 16384)
                db_url = (cfg.api_url or '').rstrip('/')
                api_key = cfg.api_key or ''
                if platform == 'lmstudio':
                    # A hosted provider's api_url is configuration; LM Studio's
                    # is an assigned address, so it is resolved rather than
                    # trusted. An unmarked row loses to a live probe.
                    res = resolve_endpoint(
                        db_url=db_url,
                        db_provenance=cfg.extra_field_3 or '',
                        api_key=api_key)
                    resolved = _config_from_resolution(res, db_url)
                else:
                    resolved = {'url': db_url or DEFAULT_URL,
                                'provenance': PROV_OPERATOR,
                                'endpoint_status': ST_AVAILABLE,
                                'endpoint_candidates': ()}
                return {
                    'model':      cfg.extra_field_1 or DEFAULT_MODEL,
                    'api_key':    api_key,
                    'max_tokens': db_max,
                    'platform':   platform,
                    'provider':   'anthropic' if platform == 'anthropic' else 'openai_compat',
                    **resolved,
                }
    except Exception as e:
        logger.debug(f"[LLM] DB config lookup failed: {e}")

    # Fallback to env
    api_key = os.getenv('OPENAI_API_KEY', '')
    res = resolve_endpoint(api_key=api_key)
    return {
        'model':      os.getenv('LM_STUDIO_MODEL', DEFAULT_MODEL),
        'api_key':    api_key,
        'max_tokens': int(os.getenv('LM_STUDIO_MAX_TOKENS', 16384)),
        'platform':   'lmstudio',
        'provider':   'openai_compat',
        **_config_from_resolution(res, ''),
    }


def _config_from_resolution(res: Resolution, db_url: str) -> dict:
    """The endpoint half of a config dict.

    `url` stays populated even when the endpoint is unusable — every log
    line and error message downstream reads better naming the address that
    failed than naming a placeholder.
    """
    return {
        'url':                 res.url or db_url or DEFAULT_URL,
        'provenance':          res.provenance,
        'endpoint_status':     res.status,
        'endpoint_candidates': res.candidates,
    }


def check_health() -> dict:
    """Ping the LLM server and return status."""
    # Re-resolve as well as re-probe: a health check that reports the address
    # found at startup is answering a question nobody asked.
    reset_endpoint_cache()
    cfg = get_llm_config()
    provenance = cfg.get('provenance')
    status = cfg.get('endpoint_status')
    # Every candidate the resolver touched, with how far each one got. The
    # point of carrying this: "localhost was up with no model loaded" must
    # survive to the operator, not be flattened into "unreachable".
    tried = [{'url': c.url, 'provenance': c.provenance, 'status': c.status,
              'model_count': len(c.models), 'detail': c.detail}
             for c in cfg.get('endpoint_candidates') or ()]
    base = {'platform': cfg['platform'], 'url': cfg['url'],
            'provenance': provenance, 'status': status, 'tried': tried}

    if status and status != ST_AVAILABLE:
        res = Resolution(cfg['url'], provenance, status,
                         tuple(cfg.get('endpoint_candidates') or ()))
        return {**base, 'ok': False, 'error': endpoint_problem(res)}

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
                return {**base, 'ok': bool(models), 'model': resolved,
                        'status': ST_AVAILABLE if models else ST_NO_MODELS,
                        'model_count': len(models),
                        'models': [m.get('id') for m in models[:5]]}
            return {**base, 'ok': False, 'status': ST_INVALID,
                    'status_code': r.status_code}
        elif cfg['provider'] == 'anthropic':
            return {**base, 'ok': True, 'model': cfg['model']}
    except Exception as e:
        return {**base, 'ok': False, 'status': ST_UNREACHABLE, 'error': str(e)}
    return {'ok': False, 'error': 'Unknown provider'}


def call_lm_studio(prompt: str, system: str = None, max_tokens: int = None,
                   temperature: float = 0.15, thinking: bool = True,
                   queue_timeout: float = None,
                   request_timeout: float = None,
                   stats: dict = None) -> str:
    """
    Unified LLM call — serialized via global lock so local models aren't overwhelmed.
    Aborts immediately if shutdown has been signalled.

    thinking=True  → full chain-of-thought (signal gen, position mgmt, Tier 5 review)
    thinking=False → /no_think prefix for fast classification (news tagging, heartbeat)

    Prefer lib/llm_router.call() over calling this directly: `thinking`
    defaults to True here, so a caller that does not pass it silently buys
    chain-of-thought for work that may not need it. The router makes the
    choice explicit and records it.

    `stats`, if given, is filled in with model / prompt_tokens /
    completion_tokens / finish_reason for telemetry.
    """
    if _shutdown_event.is_set():
        raise RuntimeError("LLM call aborted — shutdown in progress")
    cooldown = _llm_circuit_remaining()
    if cooldown > 0:
        raise RuntimeError(f"Local LLM cooling down for {cooldown:.1f}s after a provider failure")

    cfg = get_llm_config()
    status = cfg.get('endpoint_status')
    if status is not None and status != ST_AVAILABLE:
        # Say which way it is broken here, rather than letting it surface as a
        # connect error that reads like LM Studio crashed mid-run.
        raise RuntimeError(endpoint_problem(Resolution(
            cfg['url'], cfg.get('provenance'), status,
            tuple(cfg.get('endpoint_candidates') or ()))))
    # Auto-resolve placeholder model names (e.g. "local-model") to the real loaded model ID
    cfg['model'] = _resolve_model(cfg)
    effective_max = max_tokens or cfg['max_tokens']
    if stats is not None:
        stats['model'] = cfg['model']
        stats['platform'] = cfg.get('platform')

    # Thinking calls get the thinking budget even when the caller passed a
    # smaller number — call sites sized their 90s ceilings for a smaller
    # model, and a too-small budget doesn't cancel a generation, it abandons
    # a nearly-finished answer after paying full price for it.
    if thinking:
        request_timeout = max(float(request_timeout or 0), THINKING_TIMEOUT)

    # Qwen3 thinking toggle — only applies to local models (lmstudio / ollama)
    # /no_think prefix suppresses chain-of-thought for fast, low-stakes calls
    effective_system = system
    if not thinking and cfg.get('platform') in ('lmstudio', 'ollama'):
        prefix = '/no_think\n\n'
        effective_system = prefix + (system or '')

    with _llm_slot(queue_timeout):
        if _shutdown_event.is_set():
            raise RuntimeError("LLM call aborted — shutdown in progress")
        logger.debug(f"[LLM] Acquired lock → {cfg['platform']} @ {cfg['url']} model={cfg['model']} max_tokens={effective_max} thinking={thinking}")
        try:
            if cfg['provider'] == 'anthropic':
                return _call_anthropic(prompt, effective_system, effective_max, temperature, cfg,
                                       request_timeout=request_timeout, stats=stats)
            else:
                return _call_openai_compat(prompt, effective_system, effective_max, temperature, cfg,
                                           thinking_mode=thinking, request_timeout=request_timeout,
                                           stats=stats)
        except _EmptyThinkingResponse:
            # Qwen3 produced only <think> tokens — retry immediately with /no_think
            # Use RETRY_MAX_TOKENS (not the full requested amount) so the model has room to output
            if thinking and cfg.get('platform') in ('lmstudio', 'ollama'):
                logger.warning(f"[LLM] Retrying with /no_think + {RETRY_MAX_TOKENS} token cap — model produced thinking-only output on first attempt")
                fallback_system = '/no_think\n\n' + (system or '')
                try:
                    if stats is not None:
                        stats['thinking_retry'] = True
                    if cfg['provider'] == 'anthropic':
                        return _call_anthropic(prompt, fallback_system, RETRY_MAX_TOKENS, temperature, cfg,
                                               request_timeout=request_timeout, stats=stats)
                    else:
                        return _call_openai_compat(prompt, fallback_system, RETRY_MAX_TOKENS, temperature, cfg,
                                                   thinking_mode=False, request_timeout=request_timeout,
                                                   stats=stats)
                except _EmptyThinkingResponse:
                    # Both attempts exhausted — LM Studio token cap is overriding max_tokens.
                    # Return empty JSON array so the track degrades gracefully instead of crashing.
                    logger.error("[LLM] Both thinking and no_think attempts returned empty content. "
                                 "Check LM Studio → Model Settings → Max Response Tokens → set to 0 (unlimited).")
                    return "[]"
            raise RuntimeError("LLM returned empty content (thinking-only) and no retry possible")


def _call_openai_compat(prompt: str, system: str, max_tokens: int,
                         temperature: float, cfg: dict, thinking_mode: bool = False,
                         request_timeout: float = None, stats: dict = None) -> str:
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

    # Qwen3 thinking control for LM Studio 0.4.x:
    # enable_thinking must be a TOP-LEVEL field in the JSON payload — NOT inside extra_body.
    # LM Studio 0.4.x reads it directly from the request body and passes it to the chat template.
    # The "thinking" key and chat_template_kwargs wrapper are NOT recognized by LM Studio.
    # Reference: https://github.com/lmstudio-ai/lmstudio-bug-tracker/issues/1559
    if cfg.get('platform') in ('lmstudio', 'ollama'):
        payload["enable_thinking"] = thinking_mode

    url = f"{cfg['url']}/chat/completions"
    logger.info(f"[LLM] → POST {url} | model={cfg['model']} | ~{len(prompt)//4} tokens prompt")

    # Use a streaming-capable client with a shorter connect timeout
    # so we don't block forever if LM Studio is gone
    read_timeout = max(1.0, float(request_timeout or TIMEOUT))
    timeout = httpx.Timeout(connect=min(10.0, read_timeout), read=read_timeout,
                            write=min(30.0, read_timeout), pool=min(10.0, read_timeout))

    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.post(url, json=payload, headers=headers)
        if r.status_code == 400:
            logger.error(f"[LLM] 400 Bad Request body: {r.text[:500]}")
        r.raise_for_status()
        data    = r.json()
        choice  = data['choices'][0]
        raw_content = choice['message']['content'] or ''
        # Attribution: the server names the model that ACTUALLY answered —
        # which the configured name can't promise while LM Studio swaps
        # loads. Recorded per call (serialized by the global call lock) so
        # signals can stamp which brain generated them; per-model outcome
        # comparison is impossible without this.
        served = data.get('model')
        if served:
            global _last_served_model
            _last_served_model = str(served)
        usage   = data.get('usage') or {}
        tokens  = usage.get('completion_tokens', '?')
        finish  = choice.get('finish_reason', '?')
        if stats is not None:
            # Recorded even on the thinking-only path below: those tokens
            # were spent whether or not any content survived the strip, and
            # a cost measurement that omits the failures understates DEEP.
            stats['prompt_tokens'] = usage.get('prompt_tokens')
            stats['completion_tokens'] = usage.get('completion_tokens')
            stats['finish_reason'] = finish
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
    except httpx.ConnectError as e:
        # The host is actually unreachable — this is what the circuit
        # breaker exists for. Cool down so queued callers fail fast instead
        # of each paying a full connect timeout against a dead server.
        _cool_down_llm()
        raise RuntimeError(f"LLM unreachable ({cfg['platform']} @ {cfg['url']}): {e}")
    except httpx.TimeoutException:
        # A read timeout means the server is ALIVE and busy — the opposite
        # of an outage. Tripping the breaker here is how one slow qwen3-32b
        # answer cascaded into 229 instant "cooling down" rejections in an
        # hour: each timeout poisoned ~15s of every other caller's work
        # while the GPU was up and generating the whole time. The slow call
        # already paid its own cost; the next caller starts clean.
        raise RuntimeError(f"LLM timeout after {read_timeout:.1f}s — server busy "
                           f"(model={cfg.get('model')}); consider LLM_THINKING_TIMEOUT")
    except Exception as e:
        _cool_down_llm()
        raise RuntimeError(f"LLM call failed ({cfg['platform']} @ {cfg['url']}): {e}")


def _call_anthropic(prompt: str, system: str, max_tokens: int,
                    temperature: float, cfg: dict, request_timeout: float = None,
                    stats: dict = None) -> str:
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
    read_timeout = max(1.0, float(request_timeout or TIMEOUT))
    timeout = httpx.Timeout(connect=min(10.0, read_timeout), read=read_timeout,
                            write=min(30.0, read_timeout), pool=min(10.0, read_timeout))
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.post(url, json=payload, headers=headers)
        r.raise_for_status()
        data = r.json()
        if stats is not None:
            usage = data.get('usage') or {}
            stats['prompt_tokens'] = usage.get('input_tokens')
            stats['completion_tokens'] = usage.get('output_tokens')
            stats['finish_reason'] = data.get('stop_reason')
        return data['content'][0]['text']
    except httpx.TimeoutException:
        # Same rule as the OpenAI-compat path: slow is not down.
        raise RuntimeError(f"Anthropic API timeout after {read_timeout:.1f}s")
    except Exception as e:
        _cool_down_llm()
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
