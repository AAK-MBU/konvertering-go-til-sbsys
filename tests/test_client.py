"""Tests af retry-, throttling- og fejlklassifikationslogikken.

Kører mod httpx' MockTransport — ingen netværk, ingen SBSYS. Det er de
regler her der afgør om en natkørsel klarer sig selv gennem en kort
udfald hos SBSYS, så de skal være dækket.
"""

from __future__ import annotations

import httpx
import pytest

from konvertering.config import SbsysIndstillinger
from konvertering.sbsys.client import SbsysKlient
from konvertering.sbsys.errors import (
    AutentifikationsFejl,
    ForbigaaendeFejl,
    PermanentFejl,
    VaertsnavnFejl,
    er_navneopslagsfejl,
)


def _indstillinger(**overskriv) -> SbsysIndstillinger:
    standard = dict(
        base_url="https://sbsys.test",
        statisk_token="token",
        maks_forsoeg=3,
        kald_pr_sekund=0,          # slå throttling fra i test
        timeout_sekunder=5.0,
    )
    standard.update(overskriv)
    return SbsysIndstillinger(**standard)


def _klient(handler, **overskriv) -> SbsysKlient:
    return SbsysKlient(_indstillinger(**overskriv), transport=httpx.MockTransport(handler))


def test_succes_returnerer_krop():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer token"
        assert request.headers["X-Correlation-Id"].startswith("mbu-konv-")
        return httpx.Response(201, json={"Id": 42})

    with _klient(handler) as k:
        svar = k.kald("POST", "/api/sag", operation="opret_sag", json={"Titel": "x"})
    assert svar.status == 201
    assert svar.krop == {"Id": 42}


def test_femhundrede_genforsoeges_og_lykkes():
    kald = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        kald["n"] += 1
        if kald["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    with _klient(handler) as k:
        svar = k.kald("GET", "/api/sag", operation="hent")
    assert kald["n"] == 3
    assert svar.status == 200


def test_femhundrede_giver_op_efter_maks_forsoeg():
    kald = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        kald["n"] += 1
        return httpx.Response(500)

    with _klient(handler, maks_forsoeg=3) as k, pytest.raises(ForbigaaendeFejl):
        k.kald("GET", "/api/sag", operation="hent")
    assert kald["n"] == 3


def test_fire_hundrede_genforsoeges_ikke():
    """400 betyder forkerte data. At sende dem igen giver samme svar —
    og brænder tid vi ikke har i et konverteringsvindue."""
    kald = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        kald["n"] += 1
        return httpx.Response(400, text="Titel er påkrævet")

    with _klient(handler) as k, pytest.raises(PermanentFejl) as fejl:
        k.kald("POST", "/api/sag", operation="opret_sag", json={})
    assert kald["n"] == 1
    assert fejl.value.http_status == 400


def test_401_stopper_koerslen():
    with _klient(lambda r: httpx.Response(401)) as k, pytest.raises(AutentifikationsFejl):
        k.kald("GET", "/api/sag", operation="hent")


def test_401_kan_ses_naar_kalderen_beder_om_det():
    """`konv kald` venter alle statusser og skal kunne vise hvorfor."""
    svar401 = httpx.Response(401, text="audience er forkert",
                             headers={"www-authenticate": 'Bearer error="invalid_token"'})
    with _klient(lambda r: svar401) as k:
        svar = k.kald("GET", "/bu_api/paragraf", operation="kald",
                      forventede_status=tuple(range(100, 600)))
    assert svar.status == 401
    assert "invalid_token" in svar.headers["www-authenticate"]


def test_timeout_genforsoeges():
    kald = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        kald["n"] += 1
        if kald["n"] == 1:
            raise httpx.ConnectTimeout("timeout", request=request)
        return httpx.Response(200, json={})

    with _klient(handler) as k:
        svar = k.kald("GET", "/api/sag", operation="hent")
    assert svar.status == 200
    assert kald["n"] == 2


def test_429_respekterer_retry_after():
    kald = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        kald["n"] += 1
        if kald["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={})

    with _klient(handler) as k:
        assert k.kald("GET", "/x", operation="hent").status == 200


def test_kaldelog_kaldes_for_hvert_forsoeg():
    logget: list[dict] = []
    kald = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        kald["n"] += 1
        return httpx.Response(503 if kald["n"] == 1 else 200, json={})

    klient = SbsysKlient(
        _indstillinger(),
        transport=httpx.MockTransport(handler),
        kald_logger=lambda **kw: logget.append(kw),
    )
    with klient as k:
        k.kald("GET", "/x", operation="hent")

    assert [p["forsoeg_nr"] for p in logget] == [1, 2]
    assert [p["http_status"] for p in logget] == [503, 200]
    # Samme korrelations-id for alle forsøg på samme logiske kald
    assert len({p["korrelations_id"] for p in logget}) == 1


def test_kaldelog_fejl_vaelter_ikke_koerslen():
    def daarlig_logger(**kw):
        raise RuntimeError("databasen er nede")

    klient = SbsysKlient(
        _indstillinger(),
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
        kald_logger=daarlig_logger,
    )
    with klient as k:
        assert k.kald("GET", "/x", operation="hent").status == 200


# --- DNS-fejl er konfiguration, ikke netværksglimt -------------------
#
# Et værtsnavn der ikke kan slås op giver samme svar hver gang. Fem forsøg
# med voksende ventetid koster et halvt minut — pr. objekt.

@pytest.mark.parametrize(
    "besked",
    [
        "[Errno 11001] getaddrinfo failed",
        "[Errno -2] Name or service not known",
        "nodename nor servname provided, or not known",
        "Temporary failure in name resolution",
    ],
)
def test_navneopslagsfejl_genkendes(besked):
    assert er_navneopslagsfejl(Exception(besked))


def test_almindelig_netvaerksfejl_er_ikke_navneopslag():
    assert not er_navneopslagsfejl(Exception("Connection reset by peer"))
    assert not er_navneopslagsfejl(Exception("timed out"))


def test_navneopslagsfejl_findes_i_kaeden():
    """httpx pakker den oprindelige OSError ind."""
    indre = OSError("[Errno 11001] getaddrinfo failed")
    ydre = Exception("ConnectError")
    ydre.__cause__ = indre
    assert er_navneopslagsfejl(ydre)


def test_dns_fejl_genforsoeges_ikke():
    kald = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        kald["n"] += 1
        raise httpx.ConnectError("[Errno 11001] getaddrinfo failed", request=request)

    with _klient(handler, maks_forsoeg=5) as k, pytest.raises(VaertsnavnFejl) as fejl:
        k.kald("GET", "/api/version", operation="hent_version")

    assert kald["n"] == 1, "DNS-fejl må kun forsøges én gang"
    assert "SBSYS_BASE_URL" in str(fejl.value)


def test_dns_fejl_er_permanent_ikke_forbigaaende():
    """Arveforholdet afgør om loaderen afviser objektet eller stopper kørslen."""
    assert issubclass(VaertsnavnFejl, PermanentFejl)
    assert not issubclass(VaertsnavnFejl, ForbigaaendeFejl)
