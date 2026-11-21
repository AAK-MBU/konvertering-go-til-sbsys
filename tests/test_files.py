"""Tests af filopslaget mod fileshare'ets faktiske struktur.

Bekræftet mod testdumpet 16-11-2026:

    \\\\srvsqlsbsys01\\GO_Files\\<GOSagsID>[_n]\\<DokumentID>.<endelse>

Extract skriver ``<SagsId>/<DokumentID>.<endelse>``. Opslaget skal klare
overløbsmapper, endelser med andre store/små bogstaver og dokumenter uden
endelse — og må aldrig gætte, når filen findes mere end ét sted.
"""

from __future__ import annotations

from pathlib import Path

from konvertering.config import FilIndstillinger
from konvertering.files import (
    FILSTATUS_FOR_STOR,
    FILSTATUS_MANGLER,
    FILSTATUS_OK,
    FILSTATUS_TOM,
    FILSTATUS_TVETYDIG,
    FilOpslag,
)

SAG = "PPR-2015-123456-001"


def _opslag(rod: Path, **overskriv) -> FilOpslag:
    return FilOpslag(FilIndstillinger(rod=rod, **overskriv))


def _fil(rod: Path, mappe: str, navn: str, indhold: bytes = b"indhold") -> Path:
    (rod / mappe).mkdir(parents=True, exist_ok=True)
    sti = rod / mappe / navn
    sti.write_bytes(indhold)
    return sti


# --- det normale tilfælde ---------------------------------------------

def test_fil_findes_i_sagens_mappe(tmp_path: Path):
    _fil(tmp_path, SAG, "5097.rtf")

    r = _opslag(tmp_path).undersoeg(f"{SAG}/5097.rtf")

    assert r.status == FILSTATUS_OK
    assert r.sti == tmp_path / SAG / "5097.rtf"
    assert r.stoerrelse == 7
    assert r.sha256 and len(r.sha256) == 64


def test_omvendt_skraastreg_virker(tmp_path: Path):
    """En sti kopieret fra Stifinder skal også kunne slås op."""
    _fil(tmp_path, SAG, "5097.rtf")
    assert _opslag(tmp_path).find(f"{SAG}\\5097.rtf") is not None


def test_fuld_unc_sti_laeses_relativt_til_roden(tmp_path: Path):
    r"""\\server\share er UNC-stiens anker og fjernes; resten læses relativt
    til KONV_FIL_ROD, som netop er det share."""
    _fil(tmp_path, SAG, "5097.rtf")
    assert _opslag(tmp_path).find(f"\\\\srvsqlsbsys01\\GO_Files\\{SAG}\\5097.rtf") is not None


def test_mime_udledes_af_filen(tmp_path: Path):
    _fil(tmp_path, SAG, "4571585.pdf")
    assert _opslag(tmp_path).undersoeg(f"{SAG}/4571585.pdf").mime == "application/pdf"


# --- endelser ------------------------------------------------------------

def test_endelsens_store_bogstaver_er_ligegyldige(tmp_path: Path):
    """GO's filnavn siger .tif, filen på disken hedder .TIF. Linux skelner."""
    _fil(tmp_path, SAG, "5099.TIF")

    r = _opslag(tmp_path).undersoeg(f"{SAG}/5099.tif")

    assert r.status == FILSTATUS_OK
    # Den rigtige fil — uanset hvilken stavemåde stien kommer tilbage med.
    # På Linux findes den ved at liste mappen og hedder 5099.TIF; på
    # Windows, der ikke skelner, rammer det direkte opslag med 5099.tif.
    assert r.sti.samefile(tmp_path / SAG / "5099.TIF")


def test_uden_endelse_findes_filen_paa_dokument_id(tmp_path: Path):
    """5 dokumenter i testdumpet har ingen endelse i Filnavn."""
    _fil(tmp_path, SAG, "5100.msg")

    r = _opslag(tmp_path).undersoeg(f"{SAG}/5100")

    assert r.status == FILSTATUS_OK
    assert r.sti.name == "5100.msg"


def test_uden_endelse_og_to_kandidater_er_tvetydigt(tmp_path: Path):
    """5100.pdf og 5100.msg: vi ved ikke hvilken, så vi vælger ingen."""
    _fil(tmp_path, SAG, "5100.pdf")
    _fil(tmp_path, SAG, "5100.msg")

    assert _opslag(tmp_path).undersoeg(f"{SAG}/5100").status == FILSTATUS_TVETYDIG


def test_uden_endelse_rammer_ikke_et_laengere_id(tmp_path: Path):
    """5100 må ikke matche 51001.pdf."""
    _fil(tmp_path, SAG, "51001.pdf")
    assert _opslag(tmp_path).undersoeg(f"{SAG}/5100").status == FILSTATUS_MANGLER


# --- overløbsmapper ------------------------------------------------------

def test_fil_i_overloebsmappe_findes(tmp_path: Path):
    """Over 1000 filer pr. mappe giver <SagsId>_1, _2 …"""
    _fil(tmp_path, SAG, "1.pdf")
    _fil(tmp_path, f"{SAG}_1", "1001.pdf")
    _fil(tmp_path, f"{SAG}_2", "2001.pdf")

    o = _opslag(tmp_path)
    assert o.find(f"{SAG}/1001.pdf") == tmp_path / f"{SAG}_1" / "1001.pdf"
    assert o.find(f"{SAG}/2001.pdf") == tmp_path / f"{SAG}_2" / "2001.pdf"


def test_samme_fil_i_to_af_sagens_mapper_er_tvetydigt(tmp_path: Path):
    _fil(tmp_path, SAG, "7.pdf")
    _fil(tmp_path, f"{SAG}_1", "7.pdf")

    r = _opslag(tmp_path).undersoeg(f"{SAG}/7.pdf")

    assert r.status == FILSTATUS_TVETYDIG
    assert "2 steder" in r.besked


def test_anden_sag_med_laengere_nummer_rammes_ikke(tmp_path: Path):
    """…-001 må ikke fange mappen …-0011, som er en anden sag."""
    _fil(tmp_path, f"{SAG}1", "8.pdf")
    _fil(tmp_path, f"{SAG}_1x", "8.pdf")

    assert _opslag(tmp_path).undersoeg(f"{SAG}/8.pdf").status == FILSTATUS_MANGLER


# --- der ledes ALDRIG uden for sagens egne mapper ------------------------

def test_fil_paa_en_anden_sag_findes_ikke(tmp_path: Path):
    """Den gamle udgave søgte i et indeks over hele sharet. Med filnavne der
    er rene tal, ville det kunne hente et ANDET dokuments fil og lægge den
    på en børnesag."""
    _fil(tmp_path, "ELM-2022-654321-001", "5097.rtf")

    assert _opslag(tmp_path).undersoeg(f"{SAG}/5097.rtf").status == FILSTATUS_MANGLER


def test_manglende_mappe_giver_mangler(tmp_path: Path):
    r = _opslag(tmp_path).undersoeg(f"{SAG}/5097.rtf")
    assert r.status == FILSTATUS_MANGLER
    assert not r.ok


def test_tom_sti_giver_mangler(tmp_path: Path):
    assert _opslag(tmp_path).undersoeg(None).status == FILSTATUS_MANGLER
    assert _opslag(tmp_path).undersoeg("").status == FILSTATUS_MANGLER


# --- filens egenskaber ---------------------------------------------------

def test_tom_fil_afvises(tmp_path: Path):
    _fil(tmp_path, SAG, "1.pdf", b"")
    assert _opslag(tmp_path).undersoeg(f"{SAG}/1.pdf").status == FILSTATUS_TOM


def test_tom_fil_kan_tillades_bevidst(tmp_path: Path):
    _fil(tmp_path, SAG, "1.pdf", b"")
    r = _opslag(tmp_path, tillad_tomme_filer=True).undersoeg(f"{SAG}/1.pdf")
    assert r.status == FILSTATUS_OK


def test_for_stor_fil_frasorteres(tmp_path: Path):
    _fil(tmp_path, SAG, "1.bin", b"x" * 2048)
    r = _opslag(tmp_path, maks_stoerrelse_mb=0).undersoeg(f"{SAG}/1.bin")
    assert r.status == FILSTATUS_FOR_STOR


def test_checksum_kan_springes_over(tmp_path: Path):
    _fil(tmp_path, SAG, "1.pdf")
    assert _opslag(tmp_path).undersoeg(f"{SAG}/1.pdf", beregn_checksum=False).sha256 is None


# --- journalark og workflows ---------------------------------------------

def test_journalark_og_workflow_findes_paa_sagens_mappe(tmp_path: Path):
    """Extract bygger stien af SagsId + DokumentID, ikke af ScpFolder:
    ScpFolder er '/GO_Files/<SagsId>', og med roden sat til GO_Files ville
    det give ...\\GO_Files\\GO_Files\\..."""
    _fil(tmp_path, SAG, f"{SAG}_JournalSheet.docx")
    _fil(tmp_path, SAG, f"{SAG}_GO_Workflows.txt")

    o = _opslag(tmp_path)
    assert o.find(f"{SAG}/{SAG}_JournalSheet.docx") is not None
    assert o.find(f"{SAG}/{SAG}_GO_Workflows.txt") is not None
