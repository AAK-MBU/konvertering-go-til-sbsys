/*
  rettigheder.sql — mindste nødvendige rettigheder til kørselsbrugeren.

  Hedder bevidst ikke 0xx, så `konv db init` ikke kører den automatisk:
  den kræver et brugernavn.

    konv db koer sql/rettigheder.sql --var bruger=konv_runtime

  Tanken er to brugere frem for én:

    konv_admin    opretter skemaet (`konv db init`). Bruges én gang pr. miljø
                  og skal IKKE stå i .env eller i credential-storen bagefter.
    konv_runtime  kører konverteringen. Kan læse og skrive rækker, men kan
                  ikke oprette, ændre eller droppe en tabel.

  Hvorfor: et konverteringsjob har ingen grund til at kunne droppe stg.sag.
  Dagen hvor et script får en fejl i en WHERE-klausul, er forskellen mellem
  "nogle rækker er forkerte" og "tabellen er væk".
*/

SET NOCOUNT ON;

DECLARE @bruger SYSNAME = N'$(bruger)';

IF @bruger IS NULL OR @bruger = N'' OR @bruger LIKE N'$(%'
BEGIN
    RAISERROR('Angiv brugernavn: konv db koer sql/rettigheder.sql --var bruger=<navn>', 16, 1);
    RETURN;
END

IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = @bruger)
BEGIN
    DECLARE @opret NVARCHAR(400) =
        N'CREATE USER ' + QUOTENAME(@bruger) + N' FOR LOGIN ' + QUOTENAME(@bruger) + N';';
    EXEC sp_executesql @opret;
    PRINT 'Databasebruger oprettet: ' + @bruger;
END

DECLARE @sql NVARCHAR(MAX) = N'';

/* Læse og skrive rækker i de tre skemaer — men ingen DDL */
SELECT @sql = @sql
     + N'GRANT SELECT, INSERT, UPDATE, DELETE ON SCHEMA::' + QUOTENAME(s.navn)
     + N' TO ' + QUOTENAME(@bruger) + N';' + CHAR(10)
FROM (VALUES ('konv'), ('stg'), ('map')) AS s(navn);

/* map.fn_slaa_op kaldes fra extract-laget */
SET @sql = @sql + N'GRANT EXECUTE ON SCHEMA::map TO ' + QUOTENAME(@bruger) + N';' + CHAR(10);

/* Nødvendig for at kunne læse sys.database_principals m.m. i selvtest */
SET @sql = @sql + N'GRANT VIEW DEFINITION ON SCHEMA::konv TO ' + QUOTENAME(@bruger) + N';' + CHAR(10);

EXEC sp_executesql @sql;

PRINT 'Rettigheder tildelt til ' + @bruger + ':';
PRINT '  SELECT/INSERT/UPDATE/DELETE paa konv, stg og map';
PRINT '  EXECUTE paa map';
PRINT '  INGEN CREATE/ALTER/DROP — skemaaendringer kraever konv_admin';
PRINT '';
PRINT 'Husk at skifte KONV_DB_BRUGER i .env til denne bruger.';
