"""Tests af autentifikationen mod SBSYS.

SBSYS' snitflade bruger OAuth2 password grant: klienten identificerer
integrationen, og brugeren identificerer hvem handlingerne udføres som. Det
er en vigtig forskel fra client_credentials, for alt konverteringen opretter
kommer til at stå i SBSYS som udført af netop den konto.

Fejler autentifikationen, fejler ALT — derfor er formen værd at teste, og
derfor skal adgangskoden aldrig kunne slippe ud i et stacktrace.
"""

from __future__ import annotations

import httpx
import pytest

from konvertering.config import SbsysIndstillinger
from konvertering.sbsys.auth import BrugerAdgangskode, ClientCredentials
from konvertering.sbsys.client import SbsysKlient
from konvertering.sbsys.errors import AutentifikationsFejl


def _token_server(opsamler: list[dict], svar: dict | None = None, status: int = 200):
    def handler(forespoergsel: httpx.Request) -> httpx.Response:
        krop = dict(httpx.QueryParams(forespoergsel.content.decode()))
        opsamler.append(krop)
        return httpx.Response(status, json=svar or {"access_token": "t0", "expires_in": 3600})

    return httpx.MockTransport(handler)


@pytest.fixture
def opsamlede(monkeypatch):
    """Fang formularen der sendes til token-endpointet."""
    sendt: list[dict] = []
    transport = _token_server(sendt)

    def falsk_post(url, **kw):
        klient = httpx.Client(transport=transport)
        return klient.post(url, data=kw.get("data"))

    monkeypatch.setattr("konvertering.sbsys.auth.httpx.post", falsk_post)
    return sendt


def test_password_grant_sender_bruger_og_kodeord(opsamlede):
    udbyder = BrugerAdgangskode(
        "https://sbsys/connect/token", "mbu-konv", "svc_konvertering", "hemmeligt",
        client_secret="klienthemmelighed",
    )
    assert udbyder.token() == "t0"

    form = opsamlede[0]
    assert form["grant_type"] == "password"
    assert form["username"] == "svc_konvertering"
    assert form["password"] == "hemmeligt"
    # Klienten skal MED i password-formularen — den identificerer integrationen
    assert form["client_id"] == "mbu-konv"
    assert form["client_secret"] == "klienthemmelighed"


def test_password_grant_virker_uden_client_secret(opsamlede):
    """Nogle opsætninger har en offentlig klient. Så må feltet ikke sendes tomt."""
    BrugerAdgangskode("https://sbsys/token", "mbu-konv", "svc", "hem").token()
    assert "client_secret" not in opsamlede[0]


def test_client_credentials_sender_ikke_bruger(opsamlede):
    ClientCredentials("https://sbsys/token", "mbu-konv", "hemmelig").token()
    form = opsamlede[0]
    assert form["grant_type"] == "client_credentials"
    assert "username" not in form


def test_token_genbruges_indtil_det_naermer_sig_udloeb(opsamlede):
    udbyder = BrugerAdgangskode("https://sbsys/token", "k", "svc", "hem")
    udbyder.token()
    udbyder.token()
    udbyder.token()
    assert len(opsamlede) == 1, "tokenet skal caches, ikke hentes pr. kald"


def test_afvist_konto_naevner_brugeren_og_grant_typen(monkeypatch):
    transport = _token_server([], svar={"error": "invalid_grant"}, status=400)
    monkeypatch.setattr(
        "konvertering.sbsys.auth.httpx.post",
        lambda url, **kw: httpx.Client(transport=transport).post(url, data=kw.get("data")),
    )
    udbyder = BrugerAdgangskode("https://sbsys/token", "k", "svc_konvertering", "forkert")

    with pytest.raises(AutentifikationsFejl) as e:
        udbyder.token()

    besked = str(e.value)
    assert "svc_konvertering" in besked
    assert "password" in besked
    assert "forkert" not in besked, "adgangskoden må ikke med i fejlbeskeden"


def test_kodeord_laekker_ikke_i_repr():
    udbyder = BrugerAdgangskode("https://sbsys/token", "k", "svc", "hemmeligt123")
    assert "hemmeligt123" not in repr(udbyder)


# --- hvilken form vælger klienten? ----------------------------------

def _i(**kw) -> SbsysIndstillinger:
    return SbsysIndstillinger(base_url="https://sbsys", **kw)


def test_bruger_vinder_over_ren_klient():
    """Er der en navngiven bruger, er det den SBSYS skal handle som — også
    selv om der også ligger en client_secret, for den indgår i formularen."""
    udbyder = SbsysKlient._byg_token_udbyder(_i(
        token_url="https://sbsys/token", client_id="k", client_secret="s",
        brugernavn="svc", kodeord="hem",
    ))
    assert isinstance(udbyder, BrugerAdgangskode)


def test_uden_bruger_falder_vi_tilbage_til_client_credentials():
    udbyder = SbsysKlient._byg_token_udbyder(_i(
        token_url="https://sbsys/token", client_id="k", client_secret="s",
    ))
    assert isinstance(udbyder, ClientCredentials)


def test_statisk_token_vinder_over_alt():
    """Attrappen kører på et statisk token. Glemmer man at fjerne det når
    det rigtige miljø sættes op, skal man kunne se hvorfor 401 kommer."""
    udbyder = SbsysKlient._byg_token_udbyder(_i(
        statisk_token="attrap", token_url="https://sbsys/token",
        client_id="k", brugernavn="svc", kodeord="hem",
    ))
    assert udbyder.token() == "attrap"


@pytest.mark.parametrize(
    "kw,forventet",
    [
        ({"statisk_token": "attrap"}, "statisk token"),
        ({}, "SBSYS_TOKEN_URL mangler"),
        ({"token_url": "u"}, "SBSYS_CLIENT_ID mangler"),
        ({"token_url": "u", "client_id": "k", "brugernavn": "svc"}, "SBSYS_PASSWORD mangler"),
        ({"token_url": "u", "client_id": "k", "kodeord": "hem"}, "SBSYS_USERNAME mangler"),
        ({"token_url": "u", "client_id": "k"}, "hverken"),
        ({"token_url": "u", "client_id": "k", "client_secret": "s"}, "client_credentials"),
        ({"token_url": "u", "client_id": "k", "brugernavn": "svc", "kodeord": "h"},
         "password grant som svc"),
    ],
)
def test_auth_form_beskriver_opsaetningen(kw, forventet):
    """`konv env` og `konv selvtest` viser denne linje. Den skal pege på det
    der mangler — et 401 fra SBSYS ligner til forveksling en forkert kode."""
    assert forventet in _i(**kw).auth_form


def test_hemmeligheder_laekker_ikke_i_indstillingernes_repr():
    i = _i(token_url="u", client_id="k", client_secret="klienthemmelighed",
           brugernavn="svc", kodeord="hemmeligt123")
    assert "hemmeligt123" not in repr(i)
    assert "klienthemmelighed" not in repr(i)
