"""Fail-closed public HTTPS URL validation and platform classification for Product Due-Diligence."""
from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlsplit, urlunsplit

MAX_URL_LENGTH = 2_048
DEFAULT_HTTPS_PORT = 443
HOSTNAME_LABEL_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

# High-signal review platform taxonomy (platform-agnostic — Reddit is just one community source).
PLATFORM_TAXONOMY: dict[str, tuple[str, str, str]] = {
    "rtings.com": ("RTINGS", "Independent Lab & Measurements", "Web IQ Passage + Permitted BAT Read-Only"),
    "nytimes.com": ("Wirecutter", "Editorial Long-Term Testing", "Web IQ Passage"),
    "soundguys.com": ("SoundGuys", "Acoustic & Audio Lab Review", "Web IQ Passage + Permitted BAT Read-Only"),
    "head-fi.org": ("Head-Fi Forums", "Enthusiast Owner Community", "Web IQ Passage + Permitted BAT Read-Only"),
    "audiosciencereview.com": ("AudioScienceReview", "Objective Measurement Forum", "Web IQ Passage + Permitted BAT Read-Only"),
    "notebookcheck.net": ("Notebookcheck", "Display & Hardware Lab Review", "Web IQ Passage + Permitted BAT Read-Only"),
    "tftcentral.co.uk": ("TFTCentral", "Display Calibration & Panel Lab", "Web IQ Passage + Permitted BAT Read-Only"),
    "macrumors.com": ("MacRumors Forums", "Platform Ecosystem Owner Forum", "Web IQ Passage + Permitted BAT Read-Only"),
    "blurbusters.com": ("Blur Busters", "Motion Clarity & Refresh Community", "Web IQ Passage + Permitted BAT Read-Only"),
    "home-barista.com": ("Home-Barista Forums", "Specialty Coffee Enthusiast Forum", "Web IQ Passage + Permitted BAT Read-Only"),
    "coffeegeek.com": ("CoffeeGeek", "Equipment Long-Term Owner Reviews", "Web IQ Passage + Permitted BAT Read-Only"),
    "seattlecoffeegear.com": ("Seattle Coffee Gear", "Specialist Retailer & Barista Tests", "Web IQ Passage + Permitted BAT Read-Only"),
    "trustpilot.com": ("Trustpilot", "Verified Buyer Review Platform", "Web IQ Passage"),
    "consumerreports.org": ("Consumer Reports", "Independent Consumer Testing", "Web IQ Passage"),
    "reddit.com": ("Reddit", "Community Discussion", "Public read-only visit subject to site access"),
    "amazon.com": ("Amazon", "Retailer Reviews & Owner Discussion", "Public read-only visit subject to site access"),
    "youtube.com": ("YouTube", "Review-Page Text & Public Comments", "Public read-only visit; no media processing"),
    "youtu.be": ("YouTube", "Review-Page Text & Public Comments", "Public read-only visit; no media processing"),
}


class UrlValidationError(ValueError):
    """Raised when a URL cannot be used for public product due-diligence."""


def is_review_page_url(url: str) -> bool:
    """Keep document pages, using host boundaries rather than substring matching."""
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    excluded = ("youtube-nocookie.com", "vimeo.com",
                "dailymotion.com", "tiktok.com", "twitch.tv")
    return (
        bool(host)
        and not any(host == domain or host.endswith("." + domain) for domain in excluded)
        and not parsed.path.lower().endswith((".mp4", ".webm", ".mov", ".m4v", ".mp3", ".m3u8"))
    )


def _normalized_hostname(hostname: str | None) -> str:
    if not hostname:
        raise UrlValidationError("URL must include a valid host.")
    try:
        normalized = hostname.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as error:
        raise UrlValidationError("URL host is malformed.") from error
    if not normalized or normalized == "localhost" or normalized.endswith(".localhost"):
        raise UrlValidationError("Localhost URLs are not allowed.")
    if len(normalized) > 253 or any(
        not HOSTNAME_LABEL_PATTERN.fullmatch(label) for label in normalized.split(".")
    ):
        raise UrlValidationError("URL host is malformed.")
    try:
        ipaddress.ip_address(normalized)
    except ValueError:
        return normalized
    raise UrlValidationError("IP-literal hosts are not allowed.")


def _url_parts(raw_url: str) -> tuple[str, str, int, str, str]:
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise UrlValidationError("URL must be a non-empty HTTPS URL.")
    if len(raw_url) > MAX_URL_LENGTH:
        raise UrlValidationError(f"URL must not exceed {MAX_URL_LENGTH} characters.")
    if "\\" in raw_url or any(ord(character) < 32 or ord(character) == 127 for character in raw_url):
        raise UrlValidationError("URL contains invalid control characters or backslashes.")
    try:
        parsed = urlsplit(raw_url.strip())
    except ValueError as error:
        raise UrlValidationError("URL is malformed.") from error
    if parsed.scheme.lower() != "https":
        raise UrlValidationError("Only HTTPS URLs are allowed.")
    if not parsed.netloc or "@" in parsed.netloc:
        raise UrlValidationError("URL userinfo is not allowed.")
    try:
        port = parsed.port
    except ValueError as error:
        raise UrlValidationError("URL port is malformed.") from error
    if port not in (None, DEFAULT_HTTPS_PORT):
        raise UrlValidationError("Only the default HTTPS port is allowed.")
    hostname = _normalized_hostname(parsed.hostname)
    return parsed.scheme.lower(), hostname, DEFAULT_HTTPS_PORT, parsed.path or "/", parsed.query


def validate_public_https_url(raw_url: str, *, resolve_dns: bool = True) -> str:
    """Normalize a public HTTPS URL after rejecting private or unresolved hosts."""
    scheme, hostname, _, path, query = _url_parts(raw_url)
    if resolve_dns:
        try:
            addresses = socket.getaddrinfo(
                hostname,
                DEFAULT_HTTPS_PORT,
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror as error:
            raise UrlValidationError("URL host could not be resolved.") from error
        if not addresses:
            raise UrlValidationError("URL host could not be resolved.")
        for _, _, _, _, sockaddr in addresses:
            try:
                address = ipaddress.ip_address(sockaddr[0])
            except ValueError as error:
                raise UrlValidationError("URL host resolved to an invalid address.") from error
            if not address.is_global:
                raise UrlValidationError("URL host must resolve only to globally routable addresses.")
    return urlunsplit((scheme, hostname, path, query, ""))


def same_origin(
    raw_url: str,
    origin: tuple[str, str, int] | None = None,
) -> tuple[str, str, int] | bool:
    """Return a normalized origin or compare a URL to a known validated origin."""
    scheme, hostname, port, _, _ = _url_parts(raw_url)
    current_origin = (scheme, hostname, port)
    return current_origin if origin is None else current_origin == origin


def safe_display_url(raw_url: str) -> str:
    """Remove fragments and query strings before returning a validated URL to the UI."""
    scheme, hostname, _, path, _ = _url_parts(raw_url)
    return urlunsplit((scheme, hostname, path, "", ""))


def classify_review_platform(raw_url: str) -> dict[str, str | bool]:
    """Classify a source without mistaking a public visit for guaranteed site permission."""
    _, hostname, _, _, _ = _url_parts(raw_url)
    for domain, (name, category, access_mode) in PLATFORM_TAXONOMY.items():
        if hostname == domain or hostname.endswith(f".{domain}"):
            return {
                "hostname": hostname,
                "platform": name,
                "category": category,
                "access_mode": "Search grounding / public read-only visit subject to site access",
                "bat_allowed": True,
            }
    return {
        "hostname": hostname,
        "platform": hostname.removeprefix("www."),
        "category": "Independent Web / Retailer Review",
        "access_mode": "Search grounding / public read-only visit subject to site access",
        "bat_allowed": True,
    }
