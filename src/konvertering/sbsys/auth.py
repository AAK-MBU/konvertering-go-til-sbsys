"""Autentifikation mod SBSYS.

Tre former understøttes, fordi SBSYS-installationer er sat forskelligt op:

* OAuth2 password — SBSYS' egen snitflade kræver en navngiven bruger
  (SBSYS_USERNAME/SBSYS_PASSWORD) OVEN I klienten, fordi API'et udfører
  handlinger i den brugers navn og respekterer dens rettigheder i SBSYS.
  Det er den form Aarhus skal bruge.
* OAuth2 client_credentials — hvor klienten selv er rettighedsbærer
* statisk bearer-token — til attrappen og hurtige lokale afprøvninger

Bemærk konsekvensen af password-formen: alt hvad konverteringen opretter,
står i SBSYS som udført af den konto. Den skal derfor være en navngiven
servicekonto (ikke en medarbejders), og den skal have rettigheder til ALLE
de afdelinger der konverteres — ellers fejler kun en delmængde, og først
når bølgen rammer den afdeling.

Tokenet fornyes proaktivt 60 sekunder før udløb. En konverteringskørsel kan
tage timer, og et token der udløber midt i et 5.000-dokumenters batch må
ikke koste andet end et ekstra HTTP-kald.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Protocol

import httpx

from .errors import AutentifikationsFejl, er_certifikatfejl, raad_om_certifikat

log = logging.getLogger(__name__)

_FORNY_FOER_UDLOEB_SEK = 60


class TokenUdbyder(Protocol):
    def token(self) -> str: ...


class StatiskToken:
    def __init__(self, vaerdi: str) -> None:
        self._vaerdi = vaerdi

    def token(self) -> str:
        return self._vaerdi


class TokenEndpoint:
    """Fælles maskineri: hent, cache og forny et token fra et token-endpoint.

    Kun selve formularen adskiller de to grant-typer, så den er det eneste
    underklasserne bestemmer. Fornyelsen, låsen og fejlbeskederne er ens.
    """

    grant_type = ""

    def __init__(
        self,
        token_url: str,
        client_id: str,
        client_secret: str | None = None,
        scope: str | None = None,
        verificer_tls: bool = True,
        timeout: float = 30.0,
    ) -> None:
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._scope = scope
        self._verificer_tls = verificer_tls
        self._timeout = timeout
        self._token: str | None = None
        self._udloeber: float = 0.0
        self._laas = threading.Lock()

    def token(self) -> str:
        with self._laas:
            if self._token and time.monotonic() < self._udloeber - _FORNY_FOER_UDLOEB_SEK:
                return self._token
            self._hent_nyt()
            assert self._token is not None
            return self._token

    def _formular(self) -> dict[str, str]:
        data = {"grant_type": self.grant_type, "client_id": self._client_id}
        if self._client_secret:
            data["client_secret"] = self._client_secret
        return data

    def _hent_nyt(self) -> None:
        data = self._formular()
        if self._scope:
            data["scope"] = self._scope
        try:
            svar = httpx.post(
                self._token_url,
                data=data,
                timeout=self._timeout,
                verify=self._verificer_tls,
            )
        except httpx.HTTPError as e:
            if er_certifikatfejl(e):
                raise AutentifikationsFejl(
                    f"Token-endpointet kunne ikke nås: {raad_om_certifikat(self._token_url)}"
                ) from e
            raise AutentifikationsFejl(f"Kunne ikke nå token-endpoint: {e}") from e

        if svar.status_code >= 400:
            raise AutentifikationsFejl(
                f"Token-endpoint afviste {self._hvem()} (HTTP {svar.status_code}, "
                f"grant_type={self.grant_type})",
                http_status=svar.status_code,
                krop=svar.text[:500],
            )

        krop = svar.json()
        self._token = krop.get("access_token")
        if not self._token:
            raise AutentifikationsFejl("Token-svaret indeholdt ikke access_token")
        levetid = float(krop.get("expires_in", 3600))
        self._udloeber = time.monotonic() + levetid
        log.info("Nyt SBSYS-token hentet (%s), levetid %.0f sekunder",
                 self.grant_type, levetid)

    def _hvem(self) -> str:
        return f"klienten {self._client_id}"


class ClientCredentials(TokenEndpoint):
    """Klienten er selv rettighedsbærer. Bruges hvor SBSYS er sat op til det."""

    grant_type = "client_credentials"


class BrugerAdgangskode(TokenEndpoint):
    """OAuth2 password grant: klient + navngiven SBSYS-bruger.

    Dette er formen SBSYS' snitflade kræver. Brugernavnet logges — det er
    driftsinformation og ikke persondata om en borger — mens adgangskoden
    aldrig må optræde i hverken log, fejlbesked eller repr.
    """

    grant_type = "password"

    def __init__(
        self,
        token_url: str,
        client_id: str,
        brugernavn: str,
        kodeord: str,
        client_secret: str | None = None,
        scope: str | None = None,
        verificer_tls: bool = True,
        timeout: float = 30.0,
    ) -> None:
        super().__init__(token_url, client_id, client_secret, scope, verificer_tls, timeout)
        self._brugernavn = brugernavn
        self._kodeord = kodeord

    def _formular(self) -> dict[str, str]:
        data = super()._formular()
        data["username"] = self._brugernavn
        data["password"] = self._kodeord
        return data

    def _hvem(self) -> str:
        return f"brugeren {self._brugernavn}"

    def __repr__(self) -> str:
        """Uden denne ville adgangskoden kunne havne i et stacktrace."""
        return f"BrugerAdgangskode(bruger={self._brugernavn!r}, kodeord=***)"
