"""HTTP-klient mod SBSYS.

Ansvar: transport, retry, throttling, korrelations-id, kaldelog.
Klienten kender IKKE til sager og dokumenter — det ligger i endpoints.py,
så en ændring i SBSYS' API kun rammer ét sted.

Throttling er ikke pænhed. SBSYS er et driftssystem med sagsbehandlere på;
en konvertering der kører med 64 parallelle tråde kan gøre det langsomt for
dem. Standard er bevidst forsigtig og skal aftales med SBSYS-driften.
"""

from __future__ import annotations

import logging
import random
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from ..config import SbsysIndstillinger, httpx_verify
from .auth import BrugerAdgangskode, ClientCredentials, StatiskToken, TokenUdbyder
from .errors import (
    AutentifikationsFejl,
    CertifikatFejl,
    ForbigaaendeFejl,
    PermanentFejl,
    VaertsnavnFejl,
    er_certifikatfejl,
    er_navneopslagsfejl,
    raad_om_certifikat,
)

log = logging.getLogger(__name__)

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


@dataclass
class Svar:
    status: int
    krop: Any
    headers: dict[str, str]
    korrelations_id: str
    varighed_ms: int


class Hastighedsbegraenser:
    """Simpel token bucket. Tråd-sikker, fordi loaderne kører parallelt."""

    def __init__(self, kald_pr_sekund: float) -> None:
        self._interval = 1.0 / kald_pr_sekund if kald_pr_sekund > 0 else 0.0
        self._naeste = 0.0
        self._laas = threading.Lock()

    def vent(self) -> None:
        if self._interval <= 0:
            return
        with self._laas:
            nu = time.monotonic()
            if self._naeste > nu:
                time.sleep(self._naeste - nu)
                nu = time.monotonic()
            self._naeste = nu + self._interval


KaldLogger = Callable[..., None]


class SbsysKlient:
    def __init__(
        self,
        indstillinger: SbsysIndstillinger,
        *,
        transport: httpx.BaseTransport | None = None,
        kald_logger: KaldLogger | None = None,
        korrelations_praefiks: str = "mbu-konv",
    ) -> None:
        self._i = indstillinger
        self._praefiks = korrelations_praefiks
        self._kald_logger = kald_logger
        self._begraenser = Hastighedsbegraenser(indstillinger.kald_pr_sekund)
        self._token: TokenUdbyder | None = self._byg_token_udbyder(indstillinger)
        self._klient = httpx.Client(
            base_url=indstillinger.base_url,
            timeout=indstillinger.timeout_sekunder,
            verify=httpx_verify(indstillinger.verificer_tls),
            transport=transport,
            follow_redirects=False,
        )

    @staticmethod
    def _byg_token_udbyder(i: SbsysIndstillinger) -> TokenUdbyder | None:
        if i.statisk_token:
            return StatiskToken(i.statisk_token)
        if not (i.token_url and i.client_id):
            return None
        # Bruger før klient: er der en navngiven bruger, er det den SBSYS
        # skal handle som — også selv om der også er en client_secret, for
        # den indgår i password-formularen.
        if i.brugernavn and i.kodeord:
            return BrugerAdgangskode(
                i.token_url, i.client_id, i.brugernavn, i.kodeord,
                i.client_secret, i.scope, i.verificer_tls,
            )
        if i.client_secret:
            return ClientCredentials(
                i.token_url, i.client_id, i.client_secret, i.scope, i.verificer_tls
            )
        return None

    # -- offentligt api ------------------------------------------------

    def kald(
        self,
        metode: str,
        sti: str,
        *,
        operation: str,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
        filer: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        forventede_status: tuple[int, ...] = (200, 201, 202, 204),
        kontekst: dict[str, Any] | None = None,
    ) -> Svar:
        korrelations_id = f"{self._praefiks}-{uuid.uuid4().hex[:16]}"
        sidste_fejl: Exception | None = None

        for forsoeg in range(1, self._i.maks_forsoeg + 1):
            self._begraenser.vent()
            start = time.monotonic()
            try:
                svar = self._klient.request(
                    metode,
                    sti,
                    json=json,
                    params=params,
                    files=filer,
                    data=data,
                    headers=self._headers(korrelations_id),
                )
                varighed = int((time.monotonic() - start) * 1000)
                self._log_kald(
                    korrelations_id, operation, metode, sti, svar.status_code,
                    varighed, forsoeg, kontekst, None,
                )

                if svar.status_code in (401, 403):
                    raise AutentifikationsFejl(
                        f"SBSYS afviste servicekontoen (HTTP {svar.status_code})",
                        http_status=svar.status_code,
                        korrelations_id=korrelations_id,
                        krop=svar.text[:500],
                    )

                if svar.status_code in forventede_status:
                    return Svar(
                        status=svar.status_code,
                        krop=self._krop(svar),
                        headers=dict(svar.headers),
                        korrelations_id=korrelations_id,
                        varighed_ms=varighed,
                    )

                if svar.status_code in RETRY_STATUS:
                    sidste_fejl = ForbigaaendeFejl(
                        f"{operation}: HTTP {svar.status_code}",
                        http_status=svar.status_code,
                        korrelations_id=korrelations_id,
                        krop=svar.text[:500],
                    )
                    self._sov(forsoeg, svar.headers.get("Retry-After"))
                    continue

                raise PermanentFejl(
                    f"{operation}: HTTP {svar.status_code} — {svar.text[:300]}",
                    http_status=svar.status_code,
                    korrelations_id=korrelations_id,
                    krop=svar.text[:2000],
                )

            except (httpx.TimeoutException, httpx.TransportError) as e:
                varighed = int((time.monotonic() - start) * 1000)
                self._log_kald(
                    korrelations_id, operation, metode, sti, None,
                    varighed, forsoeg, kontekst, str(e),
                )

                # Et værtsnavn der ikke kan slås op er en konfigurationsfejl,
                # ikke et netværksglimt. At genforsøge fem gange med voksende
                # ventetid koster et halvt minut på hvert eneste objekt — og
                # svaret bliver det samme.
                # Samme logik for certifikatet: fem genforsøg ændrer ikke
                # hvem der har underskrevet det.
                if er_certifikatfejl(e):
                    raise CertifikatFejl(
                        f"{operation}: {raad_om_certifikat(self._i.base_url)}",
                        korrelations_id=korrelations_id,
                    ) from e

                if er_navneopslagsfejl(e):
                    raise VaertsnavnFejl(
                        f"{operation}: værtsnavnet i {self._i.base_url} kunne ikke slås op. "
                        f"Tjek SBSYS_BASE_URL — og om maskinen har adgang til netværket "
                        f"hvor SBSYS står.",
                        korrelations_id=korrelations_id,
                    ) from e

                sidste_fejl = ForbigaaendeFejl(
                    f"{operation}: netværksfejl {e}", korrelations_id=korrelations_id
                )
                self._sov(forsoeg, None)

        assert sidste_fejl is not None
        raise sidste_fejl

    def luk(self) -> None:
        self._klient.close()

    def __enter__(self) -> SbsysKlient:
        return self

    def __exit__(self, *_: Any) -> None:
        self.luk()

    # -- internt ------------------------------------------------------

    def _headers(self, korrelations_id: str) -> dict[str, str]:
        h = {
            "Accept": "application/json",
            # Sendes med, så SBSYS-driften kan finde vores kald i deres logs
            "X-Correlation-Id": korrelations_id,
            "User-Agent": "mbu-go-konvertering/1.0",
        }
        if self._token:
            h["Authorization"] = f"Bearer {self._token.token()}"
        return h

    @staticmethod
    def _krop(svar: httpx.Response) -> Any:
        if svar.status_code == 204 or not svar.content:
            return None
        if "json" in svar.headers.get("content-type", ""):
            try:
                return svar.json()
            except ValueError:
                return svar.text
        return svar.content

    def _sov(self, forsoeg: int, retry_after: str | None) -> None:
        if retry_after:
            try:
                time.sleep(min(float(retry_after), 120.0))
                return
            except ValueError:
                pass
        # Eksponentiel backoff med jitter, så parallelle workers ikke
        # genforsøger i takt og rammer SBSYS med en bølge.
        ventetid = min(2.0 ** forsoeg, 60.0) * (0.5 + random.random() / 2)
        time.sleep(ventetid)

    def _log_kald(
        self,
        korrelations_id: str,
        operation: str,
        metode: str,
        sti: str,
        status: int | None,
        varighed_ms: int,
        forsoeg: int,
        kontekst: dict[str, Any] | None,
        fejl: str | None,
    ) -> None:
        if self._kald_logger is None:
            return
        try:
            self._kald_logger(
                korrelations_id=korrelations_id,
                operation=operation,
                metode=metode,
                sti=sti,
                http_status=status,
                varighed_ms=varighed_ms,
                forsoeg_nr=forsoeg,
                kontekst=kontekst or {},
                fejl=fejl,
            )
        except Exception:  # kaldeloggen må aldrig vælte en kørsel
            log.exception("Kunne ikke skrive kaldelog")
