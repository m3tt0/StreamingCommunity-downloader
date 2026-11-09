"""Turning a failed connection into a sentence a person can act on.

The failure this exists for, measured: an Italian provider (Wind Tre) answering
for a blocked source domain with its own certificate, issued by
``securityenforcementservice.windtre.it``. DNS is not involved — 1.1.1.1 and
8.8.8.8 return the real addresses, and DNS-over-HTTPS changes nothing — the
connection is intercepted by the name in the TLS handshake. What surfaced was
``CERTIFICATE_VERIFY_FAILED ... unable to get local issuer certificate``, which
reads like a broken install, not like "your provider blocks this site".

So a verification failure is answered by connecting once more without
verifying, only to read who signed the certificate that was presented, and
naming them. Nothing is sent over that connection, and nothing it returns is
trusted: verification stays on for every request that carries data. The way
around a block like this is a different network path — a VPN, or a proxy
through ``HTTPS_PROXY`` / ``ALL_PROXY``, which every request in the panel
honours (``socks5h://`` included) — never turning verification off.
"""

import logging
import socket
import ssl

import requests

logger = logging.getLogger(__name__)

PROXY_HINT = ("Usa una VPN sul computer che esegue il pannello, oppure un proxy "
              "con HTTPS_PROXY o ALL_PROXY (anche socks5h://).")


def presented_issuer(host: str, port: int = 443, timeout: float = 6) -> str | None:
    """Who signed the certificate ``host`` presents, read without trusting it."""
    try:
        from cryptography import x509
        from cryptography.hazmat.backends import default_backend
        from cryptography.x509.oid import NameOID

        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with context.wrap_socket(raw, server_hostname=host) as tls:
                der = tls.getpeercert(binary_form=True)
        issuer = x509.load_der_x509_certificate(der, default_backend()).issuer
        parts = [a.value for oid in (NameOID.ORGANIZATION_NAME, NameOID.COMMON_NAME)
                 for a in issuer.get_attributes_for_oid(oid)]
        return " — ".join(parts) or issuer.rfc4514_string()
    except Exception:
        logger.debug("Cannot read the certificate presented by %s", host, exc_info=True)
        return None


def _is_verify_failure(exc: BaseException) -> bool:
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, ssl.SSLCertVerificationError) or \
                "CERTIFICATE_VERIFY_FAILED" in str(exc):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def explain(exc: BaseException, host: str) -> str:
    """A message for a connection to ``host`` that failed with ``exc``."""
    if _is_verify_failure(exc):
        issuer = presented_issuer(host)
        signed = (f"è firmato da «{issuer}», non da un'autorità valida" if issuer
                  else "non è firmato da un'autorità valida")
        return (f"{host} non è raggiungibile in modo sicuro: il certificato ricevuto {signed}. "
                "Di solito è il provider che blocca il sito "
                f"(blocco AGCOM) e risponde al suo posto. {PROXY_HINT}")
    if isinstance(exc, requests.ConnectionError):
        return f"{host} non risponde: {exc}"
    if isinstance(exc, requests.Timeout):
        return f"{host} non ha risposto in tempo"
    return f"{host} non raggiungibile: {exc}"
