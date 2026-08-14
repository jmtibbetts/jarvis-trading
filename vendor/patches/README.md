# Vendored dependency patches

## order_book-1.0.1-msvc.patch

`cryptofeed` depends on the `order_book` C extension, which does not
compile under MSVC upstream: its architecture gates recognize only
GCC/Clang macros (`__x86_64__`, `__aarch64__`) and end in `#error`, and
`orderbook.c` calls GCC's `__builtin_expect`. Two minimal, behavior-safe
patches fix the Windows build:

1. **utils.h** — `EXPECT()` becomes a no-op under `_MSC_VER`.
   `__builtin_expect` is purely a branch-prediction hint; the portable
   definition is the expression itself.
2. **utils.c** — the `#error` fallback branch becomes a portable
   bit-serial CRC-32 (same reflected polynomial `0xEDB88320` as the
   accelerated ARM/PCLMUL paths). The library's own comments already
   anticipate hardware-less operation via `crc32_init()`; this makes the
   compile match that design. Checksum validation is slower on this
   path, correct on all of them.

### To rebuild (fresh machine / venv)

```
pip download order-book==1.0.1 --no-binary :all: --no-deps -d /tmp/ob
cd /tmp/ob && tar -xzf order_book-1.0.1.tar.gz && cd order_book-1.0.1
git apply <repo>/vendor/patches/order_book-1.0.1-msvc.patch
pip install .
pip install cryptofeed
```

Requires MSVC Build Tools (C++ workload). Verified 2026-08-14 against
MSVC 14.50 / Python 3.12.10: builds clean, live Kraken probe agreed with
the incumbent adapter at 0.0 bps median on every shared bucket.

Upstream: worth offering as a PR to bmoscon/order_book — the patch is
strictly additive and keeps every accelerated path intact.
