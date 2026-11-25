"""Who signs the certificate for a host, and does its chain verify?

Used to tell a genuinely broken site from a site whose root CA is missing from
Python's bundle (a very common situation with Russian CAs on Windows).

    python tools/tls_probe.py jazz.sber.ru
"""

from __future__ import annotations

import socket
import ssl
import sys


def main() -> int:
    host = sys.argv[1] if len(sys.argv) > 1 else "jazz.sber.ru"
    print(f"host: {host}")

    # 1. Does the default (certifi) bundle accept it?
    try:
        with socket.create_connection((host, 443), timeout=15) as sock:
            with ssl.create_default_context().wrap_socket(sock, server_hostname=host):
                print("default trust: OK")
    except Exception as error:  # noqa: BLE001
        print(f"default trust: FAIL ({type(error).__name__}: {error})")

    # 2. Which certificate is actually served?
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, 443), timeout=15) as sock:
        with context.wrap_socket(sock, server_hostname=host) as tls:
            der = tls.getpeercert(binary_form=True)
            print("tls:", tls.version())
            print("cipher:", tls.cipher()[0] if tls.cipher() else "?")

    try:
        from cryptography import x509

        cert = x509.load_der_x509_certificate(der)
        print("subject:", cert.subject.rfc4514_string())
        print("issuer :", cert.issuer.rfc4514_string())
        print("valid  :", cert.not_valid_before_utc.date(), "->", cert.not_valid_after_utc.date())
        try:
            names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
            print("SAN    :", names.value.get_values_for_type(x509.DNSName)[:8])
        except Exception:  # noqa: BLE001
            print("SAN    : n/a")
    except Exception as error:  # noqa: BLE001
        print(f"certificate details unavailable ({error})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
