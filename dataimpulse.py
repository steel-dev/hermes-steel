# ABOUTME: Dataimpulse proxy URL construction and country validation for Steel
# ABOUTME: browser sessions; never logs or returns credentials in errors.
"""Build a Dataimpulse proxy URL for a Steel session.

Country is part of the Dataimpulse authentication username, so every session
that enables the proxy gets a username built for its requested country.
"""

from __future__ import annotations

import re
from urllib.parse import quote

from .config import secret_value

# ISO 3166-1 alpha-2. Kept explicit so a typo like "ZZ" or "Germany" is rejected
# instead of being sent to Dataimpulse and failing at navigation time.
_ISO_COUNTRY_CODES = frozenset(
    """
    AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ
    BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ
    CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ
    DE DJ DK DM DO DZ
    EC EE EG EH ER ES ET
    FI FJ FK FM FO FR
    GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY
    HK HM HN HR HT HU
    ID IE IL IM IN IO IQ IR IS IT
    JE JM JO JP
    KE KG KH KI KM KN KP KR KW KY KZ
    LA LB LC LI LK LR LS LT LU LV LY
    MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ
    NA NC NE NF NG NI NL NO NP NR NU NZ
    OM
    PA PE PF PG PH PK PL PM PN PR PS PT PW PY
    QA
    RE RO RS RU RW
    SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ
    TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ
    UA UG UM US UY UZ
    VA VC VE VG VI VN VU
    WF WS
    YE YT
    ZA ZM ZW
    """.split()
)

_COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
_PROXY_HOST_RE = re.compile(r"^[A-Za-z0-9.-]+$")


def normalize_country(country: object) -> str:
    """Return an uppercase ISO 3166-1 alpha-2 code, or raise ValueError."""
    if not isinstance(country, str) or not country.strip():
        raise ValueError(
            "proxy_country is required when enabling the Dataimpulse proxy "
            "(ISO 3166-1 alpha-2 code, for example \"DE\")."
        )
    normalized = country.strip().upper()
    if not _COUNTRY_RE.fullmatch(normalized) or normalized not in _ISO_COUNTRY_CODES:
        raise ValueError(
            f"Invalid proxy_country {country!r}: use an ISO 3166-1 alpha-2 "
            "country code such as \"DE\" or \"US\"."
        )
    return normalized


def build_dataimpulse_username(
    login: str,
    country: str,
    anonymous: bool = True,
) -> str:
    """Build the Dataimpulse auth username; country goes in the username."""
    if not login:
        raise ValueError("DATAIMPULSE_PROXY_USERNAME is not configured.")
    username = f"{login}__cr.{normalize_country(country).lower()}"
    if anonymous:
        username += ";anon.1"
    return username


def _required_secret(name: str) -> str:
    value = (secret_value(name) or "").strip()
    if not value:
        raise ValueError(
            f"{name} is not configured. Dataimpulse proxy credentials are "
            "required when the agent enables the proxy."
        )
    return value


def _proxy_host() -> str:
    host = _required_secret("DATAIMPULSE_PROXY_HOST")
    if (
        not _PROXY_HOST_RE.fullmatch(host)
        or host.startswith(".")
        or host.endswith(".")
        or ".." in host
    ):
        raise ValueError(
            "DATAIMPULSE_PROXY_HOST is invalid: use a hostname or IP address "
            "without a scheme, path, or port."
        )
    return host


def _proxy_port() -> str:
    raw = _required_secret("DATAIMPULSE_PROXY_PORT")
    if not raw.isdigit() or not 1 <= int(raw) <= 65535:
        raise ValueError("DATAIMPULSE_PROXY_PORT must be a TCP port from 1 to 65535.")
    return raw


def build_dataimpulse_proxy_url(
    country: str,
    anonymous: bool = True,
) -> str:
    """Return a URL-encoded ``http://user:pass@host:port`` proxy URL.

    Raises ValueError when a credential is missing or the country is invalid.
    Never include this URL in logs or error messages.
    """
    username = build_dataimpulse_username(
        login=_required_secret("DATAIMPULSE_PROXY_USERNAME"),
        country=country,
        anonymous=anonymous,
    )
    password = _required_secret("DATAIMPULSE_PROXY_PASSWORD")
    host = _proxy_host()
    return (
        f"http://{quote(username, safe='')}:{quote(password, safe='')}"
        f"@{host}:{_proxy_port()}"
    )
