"""Fetching a submitted URL, considerately.

We are a small community site pulling one page at a time on a human's behalf,
which is about as benign as automated fetching gets — but that is not a reason
to skip the courtesies. We identify ourselves, respect robots.txt, cap what we
download, and refuse to touch private network addresses.

That last one is the security-relevant part: the URL comes from an
authenticated but otherwise untrusted user, and without it this becomes an
SSRF gadget pointed at anything the cluster can reach.
"""

import ipaddress
import logging
import socket
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx
from django.conf import settings

logger = logging.getLogger("enrichment.fetcher")

MAX_BYTES = 2_000_000
TIMEOUT = 15.0
ALLOWED_SCHEMES = {"http", "https"}


class FetchError(Exception):
    """The page could not be retrieved. The message is shown to the submitter."""


def _is_private_host(hostname):
    """Does this hostname resolve to a private or otherwise internal address?

    Blocks the obvious SSRF targets: loopback, link-local (including cloud
    metadata endpoints at 169.254.169.254), and RFC1918 ranges. Resolution
    happens here rather than trusting the literal string, so a DNS name
    pointing at 127.0.0.1 is caught too.
    """
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise FetchError(f"Could not resolve {hostname}.") from exc

    for info in infos:
        address = info[4][0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return True
    return False


def validate_url(url):
    """Check a submitted URL before we go anywhere near it."""
    parsed = urlparse(url)

    if parsed.scheme not in ALLOWED_SCHEMES:
        raise FetchError("Only http and https links can be read.")
    if not parsed.hostname:
        raise FetchError("That doesn't look like a complete web address.")
    if _is_private_host(parsed.hostname):
        raise FetchError("That address is not reachable from the public web.")

    return parsed


def robots_allows(url, user_agent=None):
    """Does the site's robots.txt permit us?

    A failure to fetch robots.txt is treated as permission. That is the
    conventional reading, and the alternative would make every site without one
    unsubmittable.
    """
    user_agent = user_agent or settings.USER_AGENT
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"

    parser = RobotFileParser()
    try:
        response = httpx.get(
            robots_url,
            headers={"User-Agent": user_agent},
            timeout=5.0,
            follow_redirects=True,
        )
        if response.status_code >= 400:
            return True
        parser.parse(response.text.splitlines())
    except httpx.HTTPError:
        return True

    return parser.can_fetch(user_agent, url)


def fetch(url):
    """Retrieve a page. Returns (final_url, html).

    Raises FetchError with a message written for the submitter, because that is
    where it ends up — "the page is behind a login" is more use to them than a
    status code.
    """
    validate_url(url)

    if not robots_allows(url):
        raise FetchError(
            "That site asks automated tools not to read this page. "
            "You can still enter the details yourself."
        )

    try:
        with httpx.stream(
            "GET",
            url,
            headers={
                "User-Agent": settings.USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
            },
            timeout=TIMEOUT,
            follow_redirects=True,
        ) as response:
            if response.status_code == 404:
                raise FetchError("That page could not be found.")
            if response.status_code in (401, 403):
                raise FetchError(
                    "That page needs a login, so it can't be read automatically."
                )
            if response.status_code >= 400:
                raise FetchError(
                    f"That page returned an error ({response.status_code})."
                )

            content_type = response.headers.get("content-type", "")
            if "html" not in content_type and "xml" not in content_type:
                raise FetchError("That link isn't a web page.")

            chunks = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > MAX_BYTES:
                    # Truncate rather than fail: the useful markup is almost
                    # always near the top of the document.
                    logger.info("truncated oversized page at %s", url)
                    break
                chunks.append(chunk)

            html = b"".join(chunks).decode(
                response.encoding or "utf-8", errors="replace"
            )
            return str(response.url), html

    except httpx.TimeoutException as exc:
        raise FetchError("That page took too long to respond.") from exc
    except httpx.HTTPError as exc:
        raise FetchError("That page could not be reached.") from exc
