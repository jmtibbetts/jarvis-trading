"""W2 — every Helius request goes through the one client.

`lib/helius_client.py` declares itself the single access layer, and
`lib/wallet_activity.py` still had its own `httpx.get` with the key in the
query string (`?api-key=`). That put the credential into every URL and so
into any log line or exception text carrying one — the code below it then
scrubbed the key back out of error strings, treating the symptom.

It also meant the most frequently-called Helius endpoint in the system had
no pacing, no retry policy, no metrics and no normalized errors.

This test finds the pattern BY SHAPE so a third transport cannot land.
"""
import ast
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
CANONICAL = "lib/helius_client.py"
SEARCH_DIRS = ("lib", "app", "jobs", "main.py")

HTTP_CALLERS = {"httpx", "requests", "aiohttp", "urllib", "urllib3"}
HELIUS_HOST = re.compile(r"helius[-.]?(xyz|rpc)", re.IGNORECASE)


# Providers that genuinely have no header-auth option. Each needs a reason,
# not just an entry — the point of the allowlist is that adding to it is a
# conscious decision rather than a quietly loosened assertion.
QUERY_KEY_EXEMPT = {
    # Genuinely no header-auth option.
    "lib/official_data.py": "EIA v2 API accepts api_key as a query parameter only",
    # NOT a technical exemption: Twelve Data supports `Authorization: apikey`.
    # Listed so the test stays green while the migration is tracked rather
    # than blessed — moving a live market-data provider's auth belongs in its
    # own change, not in a Helius consolidation pass.
    "lib/twelvedata.py": "TODO — supports header auth; migration pending",
}


def _docstring_nodes(tree):
    """Every node that is a docstring, so prose about a bad pattern is not
    mistaken for the bad pattern."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant) and \
                    isinstance(body[0].value.value, str):
                yield body[0].value


def _py_files():
    for d in SEARCH_DIRS:
        p = ROOT / d
        if p.is_file():
            yield p
            continue
        for f in p.rglob("*.py"):
            if "__pycache__" not in f.parts:
                yield f


class SingleClientTests(unittest.TestCase):

    def test_no_helius_host_outside_the_canonical_client(self):
        """A Helius URL anywhere else is a second transport by definition."""
        offenders = []
        for f in _py_files():
            rel = f.relative_to(ROOT).as_posix()
            if rel == CANONICAL:
                continue
            src = f.read_text(encoding="utf-8", errors="ignore")
            for i, line in enumerate(src.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue          # prose about the endpoint is fine
                if HELIUS_HOST.search(line):
                    offenders.append(f"{rel}:{i}")
        self.assertEqual(offenders, [], (
            "Helius endpoints must be reached only through lib/helius_client.py "
            f"(header auth, pacing, retries, metrics): {offenders}"))

    def test_the_api_key_never_rides_in_a_query_string(self):
        """`?api-key=` puts the credential in every URL, and therefore in
        logs and exception text. Header auth works on both Helius hosts.

        Matched via AST, not text: a HEADER dict containing `x-api-key` is
        the CORRECT pattern and several providers legitimately use one, so a
        line-level regex flags exactly the code that is right. What is wrong
        is a credential in `params=` or a literal `?api-key=` in a URL.
        """
        offenders = []
        for f in _py_files():
            rel = f.relative_to(ROOT).as_posix()
            if rel in QUERY_KEY_EXEMPT:
                continue
            try:
                tree = ast.parse(f.read_text(encoding="utf-8", errors="ignore"))
            except SyntaxError:
                continue
            docstrings = {id(d) for d in _docstring_nodes(tree)}
            for node in ast.walk(tree):
                # 1. a credential passed as a URL parameter
                if isinstance(node, ast.Call):
                    for kw in node.keywords:
                        if kw.arg == "params" and isinstance(kw.value, ast.Dict):
                            for k in kw.value.keys:
                                if isinstance(k, ast.Constant) and \
                                        isinstance(k.value, str) and \
                                        "api" in k.value.lower() and \
                                        "key" in k.value.lower():
                                    offenders.append(
                                        f"{rel}:{node.lineno} params={k.value!r}")
                # 2. a literal query string in a URL, outside prose
                if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                        and "?api-key=" in node.value and id(node) not in docstrings:
                    offenders.append(f"{rel}:{node.lineno} literal ?api-key=")
        self.assertEqual(offenders, [], f"key in a query string: {offenders}")

    def test_wallet_activity_imports_no_http_library(self):
        """The specific regression: this module owned a private httpx path."""
        src = (ROOT / "lib" / "wallet_activity.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported & HTTP_CALLERS, set(),
                         "wallet_activity must reach Helius only via helius_client")
        self.assertIn("lib.helius_client",
                      {n.module for n in ast.walk(tree)
                       if isinstance(n, ast.ImportFrom) and n.module})

    def test_the_client_is_the_only_module_defining_a_helius_base(self):
        """A second base constant is a second source of truth for the host."""
        offenders = []
        for f in _py_files():
            rel = f.relative_to(ROOT).as_posix()
            if rel == CANONICAL:
                continue
            for i, line in enumerate(
                    f.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                if re.match(r"\s*[A-Z_]*BASE\s*=\s*[\"']https?://", line) \
                        and "helius" in line.lower():
                    offenders.append(f"{rel}:{i}")
        self.assertEqual(offenders, [], f"duplicate Helius base: {offenders}")


class TransportBehaviourTests(unittest.TestCase):
    """The properties the private path did not have."""

    def test_fetch_returns_a_normalized_error_never_raises(self):
        from unittest.mock import patch

        from lib import wallet_activity
        from lib.helius_client import HeliusError
        with patch("lib.helius_client.transfers",
                   side_effect=HeliusError("wallet/transfers", 429, "slow down")):
            payload, err = wallet_activity._fetch("addr")
        self.assertIsNone(payload)
        self.assertTrue(err)

    def test_fetch_uses_the_client(self):
        from unittest.mock import patch

        from lib import wallet_activity
        with patch("lib.helius_client.transfers",
                   return_value={"data": []}) as t:
            payload, err = wallet_activity._fetch("addr")
        t.assert_called_once()
        self.assertIsNone(err)
        self.assertEqual(payload, {"data": []})

    def test_a_rate_limit_is_not_reported_as_an_empty_chain(self):
        """429 must never look like 'this wallet was quiet'."""
        from unittest.mock import patch

        from lib import wallet_activity
        from lib.helius_client import HeliusError
        with patch("lib.wallet_registry.get_monitorable_wallets",
                   return_value=["JDd3hy3gQn2V982mi1zqhNqUw1GfV2UL6g76STojCJPN"]), \
             patch.dict("os.environ", {"HELIUS_API_KEY": "k" * 36}), \
             patch("lib.helius_client.transfers",
                   side_effect=HeliusError("wallet/transfers", 429, "rate limited")):
            out = wallet_activity.collect_once()
        self.assertEqual(out["observations"], 0)
        self.assertTrue(out["errors"], "a 429 must be reported, not silent")


if __name__ == "__main__":
    unittest.main()
