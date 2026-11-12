"""Tests af filopslaget.

Dette er erfaringsmæssigt der hvor en GO-konvertering taber flest timer:
stier i basen matcher ikke disken. Testene beskriver de tilfælde vi VED
kommer, så de er løst inden bacpac'en lander.
"""

from __future__ import annotations

from pathlib import Path

from konvertering.config import FilIndstillinger
from konvertering.files import FILSTATUS_MANGLER, FILSTATUS_OK, FILSTATUS_TOM, FilOpslag


def _opslag(rod: Path, **overskriv) -> FilOpslag:
    return FilOpslag(FilIndstillinger(rod=rod, **overskriv))


def test_finder_fil_via_relativ_sti(tmp_path: Path):
    (tmp_path / "2020" / "sag-1").mkdir(parents=True)
    fil = tmp_path / "2020" / "sag-1" / "bilag.pdf"
    fil.write_bytes(b"indhold")

    r = _opslag(tmp_path).undersoeg("2020/sag-1/bilag.pdf")
    assert r.status == FILSTATUS_OK
    assert r.stoerrelse == 7
    assert r.sha256 and len(r.sha256) == 64
    assert r.mime == "application/pdf"


def test_windows_sti_med_drevbogstav_haandteres(tmp_path: Path):
    (tmp_path / "Sager").mkdir()
    (tmp_path / "Sager" / "notat.docx").write_bytes(b"x")

    assert _opslag(tmp_path).find(r"C:\GO\Sager\notat.docx", "notat.docx") is not None


def test_urlkodet_mellemrum_haandteres(tmp_path: Path):
    (tmp_path / "Min mappe").mkdir()
    (tmp_path / "Min mappe" / "fil.pdf").write_bytes(b"x")

    assert _opslag(tmp_path).find("Min%20mappe/fil.pdf") is not None


def test_fald_tilbage_til_filnavnsopslag(tmp_path: Path):
    """Stien i basen er forkert, men filen findes et andet sted på sharet."""
    (tmp_path / "andet" / "sted").mkdir(parents=True)
    (tmp_path / "andet" / "sted" / "unik.pdf").write_bytes(b"x")

    o = _opslag(tmp_path)
    assert o.find("forkert/sti/unik.pdf", "unik.pdf") is not None


def test_tvetydigt_filnavn_returneres_ikke(tmp_path: Path):
    """To filer med samme navn: vi gætter ikke. Et forkert bilag på en
    børnesag er værre end et manglende bilag."""
    for mappe in ("a", "b"):
        (tmp_path / mappe).mkdir()
        (tmp_path / mappe / "bilag.pdf").write_bytes(b"x")

    assert _opslag(tmp_path).find("findes/ikke/bilag.pdf", "bilag.pdf") is None


def test_manglende_fil_giver_status_mangler(tmp_path: Path):
    r = _opslag(tmp_path).undersoeg("findes/ikke.pdf", "ikke.pdf")
    assert r.status == FILSTATUS_MANGLER
    assert not r.ok


def test_tom_fil_afvises(tmp_path: Path):
    (tmp_path / "tom.pdf").write_bytes(b"")
    assert _opslag(tmp_path).undersoeg("tom.pdf").status == FILSTATUS_TOM


def test_tom_fil_kan_tillades_bevidst(tmp_path: Path):
    (tmp_path / "tom.pdf").write_bytes(b"")
    r = _opslag(tmp_path, tillad_tomme_filer=True).undersoeg("tom.pdf")
    assert r.status == FILSTATUS_OK


def test_for_stor_fil_frasorteres(tmp_path: Path):
    (tmp_path / "stor.bin").write_bytes(b"x" * 2048)
    r = _opslag(tmp_path, maks_stoerrelse_mb=0).undersoeg("stor.bin")
    assert r.status == "for_stor"


def test_checksum_kan_springes_over(tmp_path: Path):
    (tmp_path / "f.pdf").write_bytes(b"x")
    assert _opslag(tmp_path).undersoeg("f.pdf", beregn_checksum=False).sha256 is None
