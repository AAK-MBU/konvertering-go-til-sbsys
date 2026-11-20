/*
  997_udvalg_kilde.sql — ÉN rigtig sag fra GOStaging, til testkonverteringen.

  Kopierer én sags raekker fra GOStaging til skemaet udvalg, med PRAECIS
  GOStaging's form. Saa koeres den rigtige extract mod udvalg, og stg
  indeholder kun den ene sag — ingen load-kommando kan tage flere med.

      uv run konv db koer sql\997_udvalg_kilde.sql
      uv run konv db koer sql\100_extract_skabelon.sql --kilde SBSYS_KONV.udvalg
      uv run konv mapping load

  Sagsnummeret (Cases.GOSagsID) laeses fra TEST_ABA_SAGS_ID i .env, saa det
  aldrig staar i en fil eller paa kommandolinjen.

  Med:     sagen, dens kontakter (parter) og dens foranstaltningsdata.
  Ikke med: dokumenter, journalnotater og workflows — tabellerne oprettes
           TOMME, saa extract kan koere. Dokumenterne tages i en senere runde.

  Stopper, hvis sagsnummeret ikke giver PRAECIS én sag.
  Koeres igen og igen: tabellerne genskabes hver gang.
*/

SET NOCOUNT ON;

IF SCHEMA_ID('udvalg') IS NULL
    EXEC ('CREATE SCHEMA udvalg');
GO

DROP TABLE IF EXISTS udvalg.Cases;
DROP TABLE IF EXISTS udvalg.Contacts;
DROP TABLE IF EXISTS udvalg.Documents;
DROP TABLE IF EXISTS udvalg.DocumentSHA;
DROP TABLE IF EXISTS udvalg.Foranstaltningsdata;
DROP TABLE IF EXISTS udvalg.GOWorkflows;
DROP TABLE IF EXISTS udvalg.JournalNotes;

/* Den ene sag og det der haenger paa den */
SELECT * INTO udvalg.Cases
FROM   GOStaging.dbo.Cases
WHERE  GOSagsID = N'$(TEST_ABA_SAGS_ID)';

SELECT * INTO udvalg.Contacts
FROM   GOStaging.dbo.Contacts
WHERE  SagsId = N'$(TEST_ABA_SAGS_ID)';

SELECT * INTO udvalg.Foranstaltningsdata
FROM   GOStaging.dbo.Foranstaltningsdata
WHERE  SagsId = N'$(TEST_ABA_SAGS_ID)';

/* Tomme: dokumenter er ikke med i denne runde */
SELECT TOP (0) * INTO udvalg.Documents    FROM GOStaging.dbo.Documents;
SELECT TOP (0) * INTO udvalg.DocumentSHA  FROM GOStaging.dbo.DocumentSHA;
SELECT TOP (0) * INTO udvalg.GOWorkflows  FROM GOStaging.dbo.GOWorkflows;
SELECT TOP (0) * INTO udvalg.JournalNotes FROM GOStaging.dbo.JournalNotes;
GO

/* Praecis én sag — ellers er sagsnummeret forkert eller mangler i .env */
IF (SELECT COUNT(*) FROM udvalg.Cases) <> 1
BEGIN
    RAISERROR('udvalg.Cases har ikke praecis én raekke. Tjek TEST_ABA_SAGS_ID i .env.', 16, 1);
    DROP TABLE udvalg.Cases;   -- saa extract heller ikke kan koere videre paa den
    SET NOEXEC ON;
END
GO

/* Hvad der blev kopieret. Ingen persondata: kun typer og antal.
   COLLATE DATABASE_DEFAULT: SELECT INTO tager GOStaging's sorteringsregel
   (Danish_Norwegian_CI_AS) med, og tekst med DEN regel kommer ud af
   driveren i Windows-1252 — "Åben" gav UnicodeDecodeError, også efter CAST
   til NVARCHAR (20-11-2026, tools/tegnsaet_tjek.py). stg har databasens
   egen regel og læses fint. */
SELECT CAST(c.Sagsprofil   AS NVARCHAR(256)) COLLATE DATABASE_DEFAULT AS Sagsprofil,
       CAST(c.Sagsstatus   AS NVARCHAR(256)) COLLATE DATABASE_DEFAULT AS Sagsstatus,
       CAST(c.Organisering AS NVARCHAR(256)) COLLATE DATABASE_DEFAULT AS Organisering,
       (SELECT COUNT(*) FROM udvalg.Contacts)            AS kontakter,
       (SELECT COUNT(*) FROM udvalg.Foranstaltningsdata) AS foranstaltninger
FROM   udvalg.Cases AS c;
GO

SET NOEXEC OFF;
GO
