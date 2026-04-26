// Settings page platform definitions — drives the dynamic form
const PLATFORM_DEFS = {
  // ── Brokers / Exchanges ────────────────────────────────────────────────────
  alpaca_paper: {
    label: 'Alpaca Paper Trading',
    group: 'Broker',
    fields: {
      api_key:       {label:'API Key',        placeholder:'PKTEST...'},
      api_secret:    {label:'API Secret',     placeholder:'...', type:'password'},
      api_url:       {label:'Base URL',       placeholder:'https://paper-api.alpaca.markets', value:'https://paper-api.alpaca.markets'},
      extra_field_1: {label:'Mode',           placeholder:'paper', value:'paper'},
    },
    notes: 'Paper trading — no real money. Supports bracket orders, crypto 24/7.'
  },
  alpaca_live: {
    label: 'Alpaca Live Trading',
    group: 'Broker',
    fields: {
      api_key:       {label:'API Key',        placeholder:'PK...'},
      api_secret:    {label:'API Secret',     placeholder:'...', type:'password'},
      api_url:       {label:'Base URL',       placeholder:'https://api.alpaca.markets', value:'https://api.alpaca.markets'},
      extra_field_1: {label:'Mode',           placeholder:'live', value:'live'},
    },
    notes: '⚠️ REAL money. Double-check before enabling.'
  },
  coinbase: {
    label: 'Coinbase Advanced Trade',
    group: 'Crypto Exchange',
    fields: {
      api_key:       {label:'API Key',        placeholder:'organizations/.../apiKeys/...'},
      api_secret:    {label:'API Secret (PEM)',placeholder:'-----BEGIN EC PRIVATE KEY-----', type:'password'},
      extra_field_1: {label:'Mode',           placeholder:'live or sandbox', value:'live'},
    },
    notes: 'Coinbase Advanced Trade API. Use CDP API keys.'
  },
  kraken: {
    label: 'Kraken',
    group: 'Crypto Exchange',
    fields: {
      api_key:       {label:'API Key',        placeholder:'...'},
      api_secret:    {label:'Private Key',    placeholder:'...', type:'password'},
    },
    notes: 'Kraken spot trading. REST API.'
  },
  binance: {
    label: 'Binance',
    group: 'Crypto Exchange',
    fields: {
      api_key:       {label:'API Key',        placeholder:'...'},
      api_secret:    {label:'API Secret',     placeholder:'...', type:'password'},
      api_url:       {label:'Base URL',       placeholder:'https://api.binance.com'},
      extra_field_1: {label:'Testnet?',       placeholder:'false', value:'false'},
    },
    notes: 'Binance spot. US users: set api_url to https://api.binance.us'
  },
  // ── LLM Providers ──────────────────────────────────────────────────────────
  lmstudio: {
    label: 'LM Studio (Local)',
    group: 'LLM',
    fields: {
      api_url:       {label:'Base URL',       placeholder:'http://localhost:1234/v1', value:'http://localhost:1234/v1'},
      extra_field_1: {label:'Model Name',     placeholder:'mistral-7b-instruct or local-model'},
      extra_field_2: {label:'Max Tokens',     placeholder:'4096', value:'4096'},
    },
    notes: 'Local LM Studio server. No API key needed. Leave key blank.'
  },
  ollama: {
    label: 'Ollama (Local)',
    group: 'LLM',
    fields: {
      api_url:       {label:'Base URL',       placeholder:'http://localhost:11434/v1', value:'http://localhost:11434/v1'},
      extra_field_1: {label:'Model Name',     placeholder:'llama3, mistral, deepseek-r1...'},
    },
    notes: 'Ollama local server — OpenAI-compatible endpoint.'
  },
  openai: {
    label: 'OpenAI',
    group: 'LLM',
    fields: {
      api_key:       {label:'API Key',        placeholder:'sk-...'},
      api_url:       {label:'Base URL',       placeholder:'https://api.openai.com/v1', value:'https://api.openai.com/v1'},
      extra_field_1: {label:'Model',          placeholder:'gpt-4o, gpt-4-turbo...', value:'gpt-4o'},
      extra_field_2: {label:'Max Tokens',     placeholder:'4096', value:'4096'},
    },
    notes: 'OpenAI GPT-4o recommended for best signal quality.'
  },
  anthropic: {
    label: 'Anthropic Claude',
    group: 'LLM',
    fields: {
      api_key:       {label:'API Key',        placeholder:'sk-ant-...'},
      api_url:       {label:'Base URL',       placeholder:'https://api.anthropic.com', value:'https://api.anthropic.com'},
      extra_field_1: {label:'Model',          placeholder:'claude-3-5-sonnet-20241022'},
    },
    notes: 'Claude via Anthropic API. claude-3-5-sonnet recommended.'
  },
  groq: {
    label: 'Groq (Fast Inference)',
    group: 'LLM',
    fields: {
      api_key:       {label:'API Key',        placeholder:'gsk_...'},
      api_url:       {label:'Base URL',       placeholder:'https://api.groq.com/openai/v1', value:'https://api.groq.com/openai/v1'},
      extra_field_1: {label:'Model',          placeholder:'llama-3.1-70b-versatile'},
    },
    notes: 'Groq — very fast inference, free tier available. OpenAI-compatible.'
  },
  deepseek: {
    label: 'DeepSeek',
    group: 'LLM',
    fields: {
      api_key:       {label:'API Key',        placeholder:'sk-...'},
      api_url:       {label:'Base URL',       placeholder:'https://api.deepseek.com/v1', value:'https://api.deepseek.com/v1'},
      extra_field_1: {label:'Model',          placeholder:'deepseek-chat'},
    },
    notes: 'DeepSeek — very cost-effective. Good for large-batch signal gen.'
  },
  // ── Data Sources ────────────────────────────────────────────────────────────
  polygon: {
    label: 'Polygon.io (Market Data)',
    group: 'Data',
    fields: {
      api_key:       {label:'API Key',        placeholder:'...'},
      extra_field_1: {label:'Plan',           placeholder:'free / starter / developer / advanced', value:'free'},
    },
    notes: 'Real-time and historical market data. Free tier = 15-min delay.'
  },
  alpha_vantage: {
    label: 'Alpha Vantage',
    group: 'Data',
    fields: {
      api_key:       {label:'API Key',        placeholder:'...'},
    },
    notes: 'Free market data API. 25 requests/day free tier.'
  },
  // ── Notifications ───────────────────────────────────────────────────────────
  telegram: {
    label: 'Telegram Bot',
    group: 'Notifications',
    fields: {
      api_key:       {label:'Bot Token',      placeholder:'123456789:AAF...'},
      extra_field_1: {label:'Chat ID',        placeholder:'-100... or @username'},
    },
    notes: 'Get token from @BotFather. Chat ID from @userinfobot.'
  },
  other: {
    label: 'Custom / Other',
    group: 'Other',
    fields: {
      api_key:       {label:'API Key / Token', placeholder:'...'},
      api_secret:    {label:'API Secret',      placeholder:'...', type:'password'},
      api_url:       {label:'Base URL',         placeholder:'https://...'},
      extra_field_1: {label:'Extra Field 1',    placeholder:'...'},
      extra_field_2: {label:'Extra Field 2',    placeholder:'...'},
    },
    notes: ''
  },
};
