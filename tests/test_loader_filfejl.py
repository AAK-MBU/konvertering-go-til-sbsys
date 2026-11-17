"""En ubrugelig fil må afvise ét dokument — ikke hele bølgen.

Baggrund: filen verificeres inde i send(), fordi den skal være på plads FØR
metadata oprettes i SBSYS. Men send() lå i et try/except der ikke kendte
FilFejl, så den første manglende fil stoppede kørslen:

    Kørsel stoppet: Ikke fundet: PPR-2023-000002-001/99999999.txt

I testdataene er det ét dokument ud af fire, og det er med vilje. I bølge
"digitale mapper" ville det være ét ud af hundredtusinde — og alt efter det
ville aldrig blive indlæst.

Den modsatte fejl er lige så dyr: peger KONV_FIL_ROD forkert, mangler ALLE
filer, og så skal vi netop ikke skrive hundredtusinde afvisninger ned, men
stoppe og sige hvad der er galt. Derfor grænsen — og derfor at den kun
gælder så længe ingen fil er fundet.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from konvertering.loaders.base import FIL_MANGLER_GRAENSE, Kontekst, Loader
from konvertering.models import Objekttype
from konvertering.sbsys.errors import FilFejl, KonverteringsFejl


class FalskIdMap:
    def __init__(self) -> None:
        self.fejlede: list[str] = []
        self.indlaeste: list[str] = []

    def skal_indlaeses(self, *_: Any) -> tuple[bool, str]:
        return True, "ny"

    def reserver(self, *_: Any) -> None:
        pass

    def markér_fejlet(self, _type: Any, noegle: str, _besked: str) -> None:
        self.fejlede.append(noegle)

    def markér_indlaest(self, _type: Any, noegle: str, *_: Any) -> None:
        self.indlaeste.append(noegle)


class TestLoader(Loader):
    __test__ = False          # pytest skal ikke samle den op som testklasse
    objekt_type = Objekttype.DOKUMENT

    def __init__(self, k: Kontekst, manglende: set[str]) -> None:
        super().__init__(k)
        self.manglende = manglende
        self.sendte: list[str] = []

    def hent_arbejde(self, graense: int | None = None):
        return iter(())

    def byg(self, raekke: dict[str, Any]) -> Any:
        return SimpleNamespace(kilde_noegle=str(raekke["kilde_noegle"]))

    def payload(self, objekt: Any) -> dict[str, Any]:
        return {"ekstern_noegle": f"GO:{objekt.kilde_noegle}"}

    def send(self, objekt: Any, payload: dict[str, Any]) -> tuple[str, str | None]:
        if objekt.kilde_noegle in self.manglende:
            raise FilFejl(f"Ikke fundet: {objekt.kilde_noegle}.txt",
                          regel="fil_mangler", sti=f"{objekt.kilde_noegle}.txt")
        self.sendte.append(objekt.kilde_noegle)
        return f"sbsys-{objekt.kilde_noegle}", None


def _loader(manglende: set[str]) -> TestLoader:
    afviste: list[tuple] = []
    k = Kontekst(
        settings=SimpleNamespace(dry_run=False, maskér_persondata=True),
        db=SimpleNamespace(udfoer=lambda _sql, *p: afviste.append(p)),
        idmap=FalskIdMap(),
        klient=None,
        endpoints=None,
        kodelister=None,
        filer=SimpleNamespace(rod="C:/Temp/go-filer"),
        run_id=1,
        boelge="ppr",
    )
    loader = TestLoader(k, manglende)
    loader.afviste = afviste
    return loader


def _koer(loader: TestLoader, noegler: list[str]):
    loader.hent_arbejde = lambda graense=None: iter(  # type: ignore[method-assign]
        [{"kilde_noegle": n, "boelge": "ppr"} for n in noegler]
    )
    return loader.koer()


def test_manglende_fil_stopper_ikke_resten():
    loader = _loader({"d002"})
    r = _koer(loader, ["d001", "d002", "d003", "d004"])

    assert r.behandlet == 4
    assert r.indlaest == 3
    assert r.afvist == 1
    assert loader.sendte == ["d001", "d003", "d004"]


def test_manglende_fil_skrives_til_afvisningsrapporten():
    loader = _loader({"d002"})
    _koer(loader, ["d001", "d002"])

    assert len(loader.afviste) == 1
    assert "fil_mangler" in loader.afviste[0]
    assert "d002" in loader.afviste[0]


def test_manglende_fil_markeres_fejlet_og_ikke_manual():
    """'failed' er ikke terminal: lægges filen på plads, kommer dokumentet
    med i næste kørsel helt af sig selv. Det er samme reparationssløjfe som
    ved manglende mapning."""
    loader = _loader({"d002"})
    _koer(loader, ["d001", "d002"])

    assert loader.k.idmap.fejlede == ["d002"]
    assert loader.k.idmap.indlaeste == ["d001"]


def test_alt_mangler_stopper_koerslen_med_et_fingerpeg_om_roden():
    noegler = [f"d{i:03}" for i in range(FIL_MANGLER_GRAENSE + 5)]
    loader = _loader(set(noegler))

    with pytest.raises(KonverteringsFejl) as e:
        _koer(loader, noegler)

    assert "KONV_FIL_ROD" in str(e.value)
    assert "C:/Temp/go-filer" in str(e.value)


def test_graensen_gaelder_ikke_naar_en_fil_er_fundet():
    """Virker roden, er resten huller i leverancen — dem skal fagpersonen
    have en liste over, ikke en afbrudt kørsel."""
    noegler = [f"d{i:03}" for i in range(FIL_MANGLER_GRAENSE + 5)]
    loader = _loader(set(noegler[1:]))     # den første findes

    r = _koer(loader, noegler)

    assert r.indlaest == 1
    assert r.afvist == len(noegler) - 1


def test_sbsys_afvisning_logges_med_grunden(caplog):
    """16-11-2026: kørslen viste kun "fejlet=1". Grunden — en 400 med
    "Behandler skal derfor medsendes" — skulle graves frem med konv status."""
    from konvertering.sbsys.errors import PermanentFejl

    loader = _loader(set())

    def afvis(objekt, payload):
        raise PermanentFejl("opret_sag: HTTP 400 — Behandler skal derfor medsendes")

    loader.send = afvis  # type: ignore[method-assign]
    with caplog.at_level("WARNING"):
        r = _koer(loader, ["d001"])

    assert r.fejlet == 1
    assert "Behandler skal derfor medsendes" in caplog.text
