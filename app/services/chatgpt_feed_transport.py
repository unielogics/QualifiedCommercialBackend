"""Bounded public-network probes and pinned-key SFTP delivery.

Only completed snapshots move into the watched import directory. A separate
staging directory and OpenSSH's atomic overwrite extension are mandatory; an
unsupported server leaves the previous snapshot intact.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import io
import ipaddress
import posixpath
import socket
import ssl
import time
import uuid
from urllib.parse import urlsplit


class FeedTransportError(ValueError):
    """Messages are fixed operator-facing guidance, never raw SSH/HTTP errors."""


def public_addresses(host: str, port: int) -> list[str]:
    if not host or "/" in host or "@" in host or host.endswith("."):
        raise FeedTransportError("Enter the public hostname provided by OpenAI.")
    try:
        addresses = list(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
    except OSError:
        raise FeedTransportError("The configured hostname could not be resolved. Check OpenAI's connection details.") from None
    if not addresses or any(not ipaddress.ip_address(value).is_global for value in addresses):
        raise FeedTransportError("Only public network destinations are allowed; private or local addresses are blocked.")
    return addresses


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname: str, address: str):
        super().__init__(hostname, port=443, timeout=8, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        # Connect to the exact address we validated, while TLS still checks
        # the original hostname. A later DNS response cannot rebind this probe.
        plain = socket.create_connection((self.address, 443), timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(plain, server_hostname=self.host)
        except BaseException:
            plain.close()
            raise


def validate_public_asset(url: str, *, image: bool = False) -> None:
    parts = urlsplit(url)
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or parts.port not in (None, 443) or parts.fragment or parts.query):
        raise FeedTransportError("Product and image URLs must be public HTTPS links without credentials, queries or fragments.")
    address = public_addresses(parts.hostname, 443)[0]
    conn = _PinnedHTTPSConnection(parts.hostname, address)
    try:
        conn.request("HEAD", parts.path or "/", headers={"User-Agent": "QualifiedCommercial-CatalogValidator/1.0"})
        response = conn.getresponse()
        if response.status in (405, 501):
            response.close()
            conn.close()
            conn = _PinnedHTTPSConnection(parts.hostname, address)
            conn.request("GET", parts.path or "/", headers={"User-Agent": "QualifiedCommercial-CatalogValidator/1.0", "Range": "bytes=0-1023"})
            response = conn.getresponse()
        if response.status not in (200, 206):
            # Redirects are deliberately not followed: the canonical public URL
            # must be entered instead of redirecting to an unvalidated host.
            raise FeedTransportError("A product page or image is unavailable or redirects. Save its final public HTTPS URL and retry.")
        content_type = (response.getheader("Content-Type") or "").split(";", 1)[0].lower()
        allowed = {"image/jpeg", "image/png"} if image else {"text/html", "application/xhtml+xml"}
        if content_type not in allowed:
            raise FeedTransportError("A product page must return HTML, and its main image must return JPEG or PNG.")
    except FeedTransportError:
        raise
    except (OSError, ssl.SSLError, http.client.HTTPException, ValueError):
        raise FeedTransportError("A public product page or image could not be verified. Check the URL and retry.") from None
    finally:
        conn.close()


def _fingerprint(key) -> str:
    return "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")


def _open_sftp(config: dict, private_key: str):
    import paramiko

    expected = config.get("server_fingerprint", "").strip()
    if not expected.startswith("SHA256:") or len(expected) != 50:
        raise FeedTransportError("Enter the server's SHA256 host-key fingerprint from a trusted OpenAI source.")

    class PinnedHostKey(paramiko.MissingHostKeyPolicy):
        def missing_host_key(self, client, hostname, key):
            if not hmac.compare_digest(_fingerprint(key), expected):
                raise FeedTransportError("The SFTP server fingerprint does not match. Verify it with OpenAI before reconnecting.")
            client.get_host_keys().add(hostname, key.get_name(), key)

    addresses = public_addresses(config.get("sftp_host", ""), config.get("sftp_port", 22))
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(PinnedHostKey())
    connected_socket = None
    try:
        key = paramiko.Ed25519Key.from_private_key(io.StringIO(private_key))
        connected_socket = socket.create_connection((addresses[0], config["sftp_port"]), timeout=8)
        client.connect(hostname=config["sftp_host"], port=config["sftp_port"], username=config["sftp_username"],
                       pkey=key, sock=connected_socket, allow_agent=False, look_for_keys=False,
                       timeout=8, banner_timeout=8, auth_timeout=8, channel_timeout=8)
        sftp = client.open_sftp()
        sftp.get_channel().settimeout(8)
        return client, sftp
    except FeedTransportError:
        client.close()
        if connected_socket:
            connected_socket.close()
        raise
    except Exception:
        client.close()
        if connected_socket:
            connected_socket.close()
        raise FeedTransportError("SFTP authentication failed or timed out. Verify the registered public key, username, hostname and port.") from None


def _directories(sftp, config: dict) -> tuple[str, str]:
    if not config.get("staging_directory"):
        raise FeedTransportError("Configure a writable staging directory outside OpenAI's watched import directory.")
    remote = posixpath.normpath(sftp.normalize(config["remote_directory"]))
    staging = posixpath.normpath(sftp.normalize(config["staging_directory"]))
    # Relative paths are resolved by the server, including symlinks. The
    # importer sees every root file, so even a hidden child is not isolation.
    nested = staging.startswith(remote.rstrip("/") + "/")
    if not remote.startswith("/") or not staging.startswith("/") or staging == remote:
        raise FeedTransportError("The staging directory must be distinct from the watched import directory.")
    if nested and not config.get("staging_isolation_confirmed"):
        raise FeedTransportError("Ask OpenAI to confirm that the staging folder is excluded from ingestion, then record that confirmation in Settings.")
    return remote, staging


def test_connection(config: dict, private_key: str) -> None:
    client, sftp = _open_sftp(config, private_key)
    first = second = None
    try:
        _, staging = _directories(sftp, config)
        stamp = uuid.uuid4().hex
        first = posixpath.join(staging, f"qc-probe-{stamp}.tmp")
        second = posixpath.join(staging, f"qc-probe-{stamp}.done")
        with sftp.file(first, "wx") as handle:
            handle.write(b"Qualified Commercial connection test\n")
        # Verify the atomic overwrite extension, outside the import directory.
        with sftp.file(second, "wx") as handle:
            handle.write(b"probe")
        sftp.posix_rename(first, second)
        first = None
    except FeedTransportError:
        raise
    except Exception:
        raise FeedTransportError("SFTP connected, but staging or atomic overwrite is unavailable. Ask OpenAI for an isolated writable staging path with atomic rename support.") from None
    finally:
        for path in (first, second):
            if path:
                try:
                    sftp.remove(path)
                except Exception:
                    pass
        sftp.close()
        client.close()


def upload_snapshot(config: dict, private_key: str, content: bytes, items: list[dict]) -> None:
    if not content or len(content) > 5 * 1024 * 1024:
        raise FeedTransportError("The catalog export is empty or exceeds the five-megabyte delivery limit.")
    started = time.monotonic()
    seen: set[tuple[str, bool]] = set()
    for item in items:
        if not item.get("eligible") or item.get("is_eligible_search") is False:
            continue
        for field, image in (("url", False), ("image_url", True)):
            target = (item[field], image)
            if target not in seen:
                if time.monotonic() - started > 120:
                    raise FeedTransportError("Public-link verification timed out. No catalog was replaced; retry with a smaller approved sample.")
                validate_public_asset(target[0], image=image)
                seen.add(target)
    client, sftp = _open_sftp(config, private_key)
    temporary = None
    try:
        remote, staging = _directories(sftp, config)
        temporary = posixpath.join(staging, f"qc-feed-{uuid.uuid4().hex}.tmp")
        with sftp.file(temporary, "wx") as handle:
            for offset in range(0, len(content), 65536):
                if time.monotonic() - started > 180:
                    raise FeedTransportError("The upload timed out before publication. The previous catalog remains current.")
                handle.write(content[offset:offset + 65536])
            handle.flush()
        if sftp.stat(temporary).st_size != len(content):
            raise FeedTransportError("The staged upload is incomplete. The previous catalog remains current.")
        sftp.posix_rename(temporary, posixpath.join(remote, config["filename"]))
        temporary = None
    except FeedTransportError:
        raise
    except Exception:
        raise FeedTransportError("SFTP delivery did not complete. Check the connection and atomic staging permissions, then retry. Ingestion has not been verified.") from None
    finally:
        if temporary:
            try:
                sftp.remove(temporary)
            except Exception:
                pass
        sftp.close()
        client.close()
