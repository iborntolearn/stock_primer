"""TLS on networks that inspect HTTPS with their own root CA (e.g. a corporate proxy).

- `setup_tls()` makes Python's ssl (requests, httpx, the Anthropic SDK) trust the
  operating-system certificate store via truststore.
- `ca_bundle_path()` writes certifi + the Windows ROOT/CA stores into one PEM for
  libraries with their own TLS stack (curl_cffi, used by yfinance).
Verification is never disabled.
"""
from __future__ import annotations

import ssl
import sys
from functools import lru_cache
from pathlib import Path

_done = False


def setup_tls() -> None:
    global _done
    if _done:
        return
    try:
        import truststore

        truststore.inject_into_ssl()
    except ImportError:
        pass
    _done = True


@lru_cache
def ca_bundle_path() -> str:
    import certifi

    from .settings import get_settings

    if sys.platform != "win32":
        return certifi.where()
    out = Path(get_settings().cache_dir) / "ca-bundle.pem"
    out.parent.mkdir(parents=True, exist_ok=True)
    pems = [Path(certifi.where()).read_text(encoding="ascii")]
    for store in ("ROOT", "CA"):
        for der, enc, trust in ssl.enum_certificates(store):
            if enc == "x509_asn":
                pems.append(ssl.DER_cert_to_PEM_cert(der))
    out.write_text("\n".join(pems), encoding="ascii")
    return str(out)
