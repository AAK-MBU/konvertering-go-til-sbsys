/*
  997_udvalg_kilde.sql — UDVALGTE rigtige sager fra GOStaging, til testkonverteringen.

  Kopierer de udvalgte sagers raekker fra GOStaging til skemaet udvalg, med
  PRAECIS GOStaging's form. Saa koeres den rigtige extract mod udvalg, og stg
  indeholder kun de sager — ingen load-kommando kan tage flere med.

      uv run konv db koer sql\997_udvalg_kilde.sql
      uv run konv db koer sql\100_extract_skabelon.sql --kilde SBSYS_KONV.udvalg
      uv run konv mapping load

  Sagsnumrene (Cases.GOSagsID) laeses fra .env, saa de aldrig staar i en fil
  eller paa kommandolinjen. Én linje, kommasepareret (27-11-2026):

      TEST_SAGS_IDER=BOR-2026-000001-001,BOR-2026-000002-001

  TEST_ABA_SAGS_ID (én sag) virker stadig; begge kan bruges paa én gang.

  Med: sagerne, deres kontakter (parter), foranstaltningsdata, dokumenter
       (inkl. kladder), deres checksummer, journalnotater og workflows.

  Stopper, hvis ikke HVERT sagsnummer giver PRAECIS én sag.
  Koeres igen og igen: tabellerne genskabes hver gang.
*/

SET NOCOUNT ON;

IF SCHEMA_ID('udvalg') IS NULL
    EXEC ('CREATE SCHEMA udvalg');
GO

DROP TABLE IF EXISTS udvalg.Ider;
DROP TABLE IF EXISTS udvalg.Cases;
DROP TABLE IF EXISTS udvalg.Contacts;
DROP TABLE IF EXISTS udvalg.Documents;
DROP TABLE IF EXISTS udvalg.DocumentSHA;
DROP TABLE IF EXISTS udvalg.Foranstaltningsdata;
DROP TABLE IF EXISTS udvalg.GOWorkflows;
DROP TABLE IF EXISTS udvalg.JournalNotes;

/* De udvalgte sagsnumre. Én tabel i stedet for STRING_SPLIT i hver WHERE,
   og COLLATE DATABASE_DEFAULT paa GOStaging-siden: GOStaging har en anden
   sorteringsregel end denne database. */
SELECT DISTINCT id = CAST(LTRIM(RTRIM(value)) AS NVARCHAR(36)) COLLATE DATABASE_DEFAULT
INTO   udvalg.Ider
FROM   STRING_SPLIT(N'$(TEST_SAGS_IDER|)' + N',' + N'$(TEST_ABA_SAGS_ID|)', N',')
WHERE  LTRIM(RTRIM(value)) <> N'';

/* Sagerne og det der haenger paa dem */
SELECT * INTO udvalg.Cases
FROM   GOStaging.dbo.Cases
WHERE  GOSagsID COLLATE DATABASE_DEFAULT IN (SELECT id FROM udvalg.Ider);

SELECT * INTO udvalg.Contacts
FROM   GOStaging.dbo.Contacts
WHERE  SagsId COLLATE DATABASE_DEFAULT IN (SELECT id FROM udvalg.Ider);

SELECT * INTO udvalg.Foranstaltningsdata
FROM   GOStaging.dbo.Foranstaltningsdata
WHERE  SagsId COLLATE DATABASE_DEFAULT IN (SELECT id FROM udvalg.Ider);

SELECT * INTO udvalg.Documents
FROM   GOStaging.dbo.Documents
WHERE  SagsId COLLATE DATABASE_DEFAULT IN (SELECT id FROM udvalg.Ider);

/* Checksummer for netop sagernes dokumenter */
SELECT s.* INTO udvalg.DocumentSHA
FROM   GOStaging.dbo.DocumentSHA AS s
WHERE  EXISTS (SELECT 1 FROM udvalg.Documents AS d WHERE d.DokumentID = s.DocumentId);

SELECT * INTO udvalg.JournalNotes
FROM   GOStaging.dbo.JournalNotes
WHERE  SagsId COLLATE DATABASE_DEFAULT IN (SELECT id FROM udvalg.Ider);

SELECT * INTO udvalg.GOWorkflows
FROM   GOStaging.dbo.GOWorkflows
WHERE  SagsId COLLATE DATABASE_DEFAULT IN (SELECT id FROM udvalg.Ider);
GO

/* Hvert sagsnummer skal give praecis én sag — ellers er et nummer forkert
   eller mangler. Ingen numre i .env er ogsaa en fejl. */
IF (SELECT COUNT(*) FROM udvalg.Ider) = 0
   OR (SELECT COUNT(*) FROM udvalg.Cases) <> (SELECT COUNT(*) FROM udvalg.Ider)
   OR EXISTS (SELECT 1 FROM udvalg.Ider i
              WHERE NOT EXISTS (SELECT 1 FROM udvalg.Cases c
                                WHERE c.GOSagsID COLLATE DATABASE_DEFAULT = i.id))
BEGIN
    DECLARE @n INT = (SELECT COUNT(*) FROM udvalg.Ider),
            @f INT = (SELECT COUNT(*) FROM udvalg.Cases);
    RAISERROR('udvalg: %d sagsnumre i .env, %d sager fundet. Tjek TEST_SAGS_IDER / TEST_ABA_SAGS_ID.',
              16, 1, @n, @f);
    DROP TABLE udvalg.Cases;   -- saa extract heller ikke kan koere videre paa dem
    SET NOEXEC ON;
END
GO

/* Hvad der blev kopieret, én raekke pr. sag. Ingen persondata og intet
   sagsnummer: kun et loebenummer, typer og antal.
   COLLATE DATABASE_DEFAULT: SELECT INTO tager GOStaging's sorteringsregel
   (Danish_Norwegian_CI_AS) med, og tekst med DEN regel kommer ud af
   driveren i Windows-1252 — "Åben" gav UnicodeDecodeError, også efter CAST
   til NVARCHAR (20-11-2026, tools/tegnsaet_tjek.py). stg har databasens
   egen regel og læses fint. */
SELECT ROW_NUMBER() OVER (ORDER BY c.GOSagsID) AS nr,
       CAST(c.Sagsprofil   AS NVARCHAR(256)) COLLATE DATABASE_DEFAULT AS Sagsprofil,
       CAST(c.Sagsstatus   AS NVARCHAR(256)) COLLATE DATABASE_DEFAULT AS Sagsstatus,
       CAST(c.Organisering AS NVARCHAR(256)) COLLATE DATABASE_DEFAULT AS Organisering,
       (SELECT COUNT(*) FROM udvalg.Contacts x            WHERE x.SagsId = c.GOSagsID) AS kontakter,
       (SELECT COUNT(*) FROM udvalg.Foranstaltningsdata x WHERE x.SagsId = c.GOSagsID) AS foranstaltninger,
       (SELECT COUNT(*) FROM udvalg.Documents x           WHERE x.SagsId = c.GOSagsID) AS dokumenter,
       (SELECT COUNT(*) FROM udvalg.Documents x           WHERE x.SagsId = c.GOSagsID
                                                            AND (x.Journaliseret <> 1 OR x.Journaliseret IS NULL))
                                                                                  AS heraf_kladder,
       (SELECT COUNT(*) FROM udvalg.JournalNotes x        WHERE x.SagsId = c.GOSagsID) AS journalnotater
FROM   udvalg.Cases AS c;
GO

SET NOEXEC OFF;
GO
