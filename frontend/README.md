# Dokploy-webapp og Pi-agent

## Klargjort live-funktion (deaktiveret som standard)

Fysisk print kræver alle tre indstillinger: `ALLOW_LIVE_PRINT=1` i Dokploy samt
`PRINT_MODE=live` og `ALLOW_REMOTE_LIVE=1` i Pi'ens `/etc/print-service/environment`.
De sættes ikke automatisk af installation eller opdateringer. Aktivér dem først
efter særskilt aftale om fysisk test og verificering af driveropsætningen.
Eksisterende simuleringsjob forbliver simulerede, også når live senere aktiveres.

Når begge sider er aktiveret, får hvert jobs dialog et særskilt valg af fysisk
printstart. Standard er altid simulering. Fysisk start kræver tom-pladebekræftelse,
AMS-valg og teksten `START PRINT`. Både cloud og Pi kræver en ledig printer, ingen
rapporteret printerfejl, frisk AMS-status, P2S-slicing og matchende filamentprofil.
En accepteret farveforskel tillades; materialet og filamentprofilen skal passe.
IDLE/FINISH accepteres. FAILED efter et afbrudt print accepteres også, når
status er frisk, fejlkoden er 0, og den rapporterede resttid er 0 minutter.
FAILED omdøbes ikke til IDLE. Manglende data, egentlig fejl eller aktivt/pauset
print afvises fortsat. Operatøren skal kontrollere og bekræfte ryddet byggeplade.

Agenten gemmer lokalt job-id før startkaldet og følger derefter kun status via GET.
Ved genstart genoptages statusaflæsningen, ikke printkommandoen. Mistet kvittering
genafleveres uden ny start. Driverkode 0 bliver `submitted`: **driver har accepteret
printkommandoen**, ikke dokumentation for færdigt print eller første lag.
Fejl eller timeout bliver unknown og genforsøges aldrig automatisk.

Jobkvitteringer har sanerede tekniske fejlkoder: proces-exitkode, undtagelsestype
og højst 12 udvalgte driverhændelser med numeriske retur-/statuskoder.
Rå beskeder, stdout/stderr, adgangskoder, certifikater og forbindelsesparametre
gemmes ikke. UI viser **Tekniske fejlkoder**, når sådanne data findes.
Ældre job kan ikke få deres allerede kasserede driveroutput genskabt.

Driverprocessen arver ignoreret SIGPIPE, så en lukket forbindelse under nedlukning
ikke automatisk afslutter processen med signal 13. Hvis driveren alligevel afslutter
med SIGPIPE, accepteres kun den særlige sekvens `action_dispatched rc=0` efterfulgt
af `finished status=6 code=0`. Den gemmes som submitted med advarslen
`SIGPIPE_AFTER_ACCEPT` og den oprindelige exitkode. Manglende eller negative
kvitteringer forbliver unknown; andre signaler genfortolkes ikke som succes.
Submitted betyder fortsat accepteret startkommando, ikke færdigt fysisk print.

Submitted reserverer både cloud-kø og lokal kø. Efter fysisk kontrol af færdig/tom
printer kan **Luk efter kontrol** frigive cloud-jobbet. Agenten frigiver derefter
den lokale reservation, når den samme kontrol melder printeren ledig uden fejl.
Print slutter ikke automatisk ud fra løs telemetri, fordi den ikke entydigt
identificerer jobbet. Ingen fjernstop eller automatisk genstart er tilføjet.

Driveren skal være installeret i `/opt/open-bamboo-persano`, kompileret til ABI
02.05.02, med params i `/etc/print-service/print.params.json` og certifikat på den
konfigurerede sti. Den eksisterende home-installation bevares. Forberedelse og
kompilering starter ingen print. Hold live-indstillingerne deaktiveret indtil test.

`/etc/print-service` skal være root:print-service med 0750, så servicebrugeren
kan læse `print.params.json` (root:print-service, 0640). Miljøfiler med hemmeligheder
forbliver root:root, 0600 og indlæses af systemd; servicebrugeren kan ikke læse dem.

## Automatiske Pi-opdateringer

Efter engangsinstallation kan `sudo systemctl enable --now print-updater.timer`
aktivere automatisk kontrol af GitHub `main` cirka hvert andet minut. Pi'en
opretter selv de udgående forbindelser; der kræves ingen åbne porte eller GitHub-nøgle
til det nuværende offentligt læsbare repo. Push til main er herefter en softwareudrulning
til Pi'en, uafhængigt af hvornår Dokploy er færdig med at deploye webappen.

Kun fire godkendte runtime-filer opdateres: server, telemetri, agent og printplan.
De hentes fra det præcise commit over HTTPS og syntakskontrolleres uden at eksekvere
kode som root. Updateren kører ikke install.sh fra GitHub. Nye afhængigheder,
systemd-units, ændringer af updateren og konfiguration kræver fortsat manuel installation.
Repoets kode kører som den eksisterende servicebruger efter installation.

Fælles proceslås for agent og startkommandoer forhindrer opdatering midt i en
opgave. Aktive/uklare printjob, uafleverede agentresultater og printertilstandene
RUNNING/PREPARE/PAUSE/SLICING udskyder opdateringen. Stoppet service startes ikke
automatisk af updateren. Opdatering stopper kort API og agent, tager lokal backup
af kode og kødatabase og starter API'et igen. API-helbredstjek skal bestå før
agenten genstartes. Hvis den nye API-version fejler, gendannes kode og database.
Et fejlet commit prøves ikke igen før næste commit eller manuel fjernelse af
`/var/lib/print-updater/version.json`. Cloud-database og printerdriver ændres ikke.
Hemmeligheder og PRINT_MODE bevares. Opdatering starter ikke print.

```sh
systemctl list-timers print-updater.timer
sudo journalctl -u print-updater.service -n 20 --no-pager
sudo cat /var/lib/print-updater/version.json
sudo systemctl start print-updater.service
```

Slå automatisk opdatering fra med `sudo systemctl disable --now print-updater.timer`.
Lokale kildefiler i `~/PrintPlatform` opdateres ikke; det er den installerede kode
i `/opt/print-platform/service`, der opdateres.

Frontend og backend kører sammen i én container. Pi'en opretter selv udgående
HTTPS til cloud og behøver ingen åbne porte. Standard er simulering; fysisk
print kræver den særskilte aktivering beskrevet øverst.

## Dokploy

- Repo: `caspermartinsen/PrintPlatform`, branch `main`.
- Build type: Dockerfile. Dockerfile path: `Dockerfile`. Docker context: `.`.
- Container port: **3000**. Tilknyt domæne med HTTPS.
- Permanent volume: **`/data`**, skrivbart for UID/GID 10001. Brug én replica.
- Runtime-miljøvariabler (ikke build arguments):

```dotenv
PUBLIC_URL=https://print.martinsen.family
WEB_USER=casper
WEB_PASSWORD=REPLACE_WITH_RANDOM_PASSWORD_AT_LEAST_16_CHARACTERS
AGENT_TOKEN=REPLACE_WITH_RANDOM_TOKEN_AT_LEAST_32_CHARACTERS
PORT=3000
DATA_DIR=/data
```

Generér separate tilfældige hemmeligheder. Login er browserens almindelige
HTTP Basic-dialog over HTTPS. Ingen brugeradministration eller logout-knap i
første version. Agentnøglen er til én Pi og sendes aldrig til browseren.
Printerkoden forbliver på Pi'en. Healthcheck: `GET /healthz` uden login.

## Pi-agent

Opdatér Pi'ens projektfiler før installation. Installer opdateret API og agent
sammen; eksisterende telemetri og hemmeligheder bevares:

```sh
cd ~/PrintPlatform
sudo sh deploy/install.sh
sudo systemctl restart print-service
sudo install -m 600 /dev/null /etc/print-service/agent-environment
sudo nano /etc/print-service/agent-environment
```

Indsæt samme agentnøgle som i Dokploy:

```dotenv
PLATFORM_URL=https://print.martinsen.family
AGENT_TOKEN=REPLACE_WITH_THE_SAME_TOKEN_AS_DOKPLOY
```

```sh
sudo systemctl enable --now print-agent
sudo systemctl status print-agent --no-pager
```

Agenten bruger den lokale API-nøgle fra `/etc/print-service/environment`, henter
opgaver og sender status hvert femte sekund. Kun udgående HTTPS/443 til cloud.
Pi-API'et forbliver på loopback. Bevar `PRINT_MODE=simulate` under udvikling og test.

## Funktionelt forløb

### Filamentplan og AMS-valg

Upload analyserer `Metadata/slice_info.config` for faktisk anvendte filamenter,
ikke antallet af gemte profiler i projektet. Navn på profil hentes fra
`project_settings.config`. Flere anvendte filamenter og huller i filamentindeks understøttes nu. Der vælges én AMS-plads pr. anvendt filament; ukendte materialer eller manglende metadata kan fortsat ikke godkendes.
Metadata er oplysninger fra sliceren, ikke en analyse af alle G-code-bevægelser.

Godkendelsesdialogen viser filnavn, filamentkrav og AMS-valg. Materialet skal være
det samme (fx PLA); farve-/profilforskelle vises og kræver særskilt accept.
Slicerens temperaturer ændres ikke. Tom byggeplade skal bekræftes, også i testen.
Serveren kontrollerer de seneste AMS-oplysninger; Pi'en kontrollerer dem igen ved
modtagelsen. Skiftet spole eller gamle data afviser udførelsen og kræver gennemgang.

Godkendt valg gemmes i cloud og lokal database. I simuleringen beregner Pi'en de
samme `ams_mapping`/`ams_mapping2`-parametre som driveradapteren bruger. Valgt
AMS-plads vises i køen. Agentens plan-version forhindrer levering til ældre agenter.
Opdatér både Pi og Dokploy. Ældre uploadede filer analyseres også uden gen-upload.
Rigtig cloud-printstart er deaktiveret som standard; næste fysiske test kræver særskilt
godkendelse og aktivering. Ingen ændring i `PRINT_MODE` foretages af opdateringer.

AMS-kort viser pladser (nummereret fra 1), printerens RGBA-farve, filamentnavn,
materiale og estimeret restprocent. Tomme pladser markeres kun som tomme, når
printeren rapporterer det. Ukendte/gamle data vises uden farve og restmængde.
RFID-numre og spoleidentifikatorer sendes ikke videre til cloud. AMS-visningen
vælger ikke filament og ændrer ikke printparametre.

Log ind, kontrollér printerstatus og upload en betroet enkeltplade `.gcode.3mf`
(maks. 100 MiB). Jobbet bliver i køen indtil **Godkend simulering** og den
efterfølgende dialog er godkendt. Pi'en henter filen, verificerer SHA-256 og
udfører lokal simulering. Jobbet ender som **Simulering gennemført**.

Cloud markerer jobbet dispatched før afsendelse og genudsender aldrig automatisk.
Pi'en skriver et holdbart journalspor før udførelse. Et gemt lokalt job-id følges
med statusaflæsning efter genstart; uafklaret udførelse uden lokalt id bliver unknown.
Færdige resultater genafleveres uden ny udførelse, hvis
kvitteringen går tabt. Dispatched/unknown blokerer nye job og kræver manuel
gennemgang. Vælg **Luk efter kontrol**, når jobbet er kontrolleret på Pi'en;
det frigiver køen uden at genstarte eller stoppe jobbet. Ingen automatisk
køfrigivelse. Fysiske print kræver de særskilte live-indstillinger øverst.

Pi vises offline efter 45 sekunder uden opdatering. Gamle printerfelter skjules.
Bevar `/data` og Pi'ens `/var/lib/print-agent` ved opdateringer; tab af journal
eller database kræver manuel gennemgang. Max. 1000 job og cirka 2 GiB filer;
Oprydning er manuel. Afsæt mindst 512 MiB RAM og tillad 100 MiB uploads i proxyen.
STL-upload har slicing og 3D-preview; færdige .gcode.3mf-filer følger køforløbet uden nyt preview. Fjernannullering understøttes ikke.

## Tests

```sh
node --test frontend/app.test.cjs
python3 -m unittest cloud.test_platform -v
cd service && python3 -m unittest -v
```

Godkendelsesdialogen bliver åben ved serverafvisning og viser fejlen ved valgene.
Dialogen lukker først efter serverens kvittering. Dobbeltklik under afsendelse
sender ikke endnu en godkendelse. Fysisk bekræftelse kræver præcis `START PRINT`.

Integrationstestene bruger midlertidige lokale servere og erstatter printprocesser.
De tester godkendelse, overførsel, simulering, mistet kvittering, genstart og login.
Docker-build: `docker build -t printplatform .`.

Dokploy reference: https://docs.dokploy.com/docs/core/applications/build-type


## Stabilitet og enklere printflow

Upload ligger først. Efter STL-upload vælges filament og kvalitet; infill og
laghøjde kan justeres under Avancerede indstillinger. Det valgte filament
foreslås igen ved godkendelse og kontrolleres fortsat af både cloud og Pi.
Godkendelse kan åbnes direkte fra modelområdet. Afsluttede og gennemgåede job
vises i historikken; dette sletter ikke filer eller frigør lager.

Et afsendt job kan ikke lukkes, før Pi'ens resultat er modtaget. Sene kvitteringer
for tidligere lukkede job gemmes. Hvis kvitteringen afslører en lokal reservation
eller accepteret printstart, åbnes jobbet til ny fysisk kontrol. Gentagelse af
samme kvittering gentager aldrig printstart.

Filnavne kan indeholde danske bogstaver og andre Unicode-tegn. Browseren sender
navnet i X-Filename-Encoded (UTF-8 percent-encoding); X-Filename understøttes
fortsat for eksisterende klienter. Cloud og agent bruger UUID-navne mod Pi'en,
så ældre Pi-versioner fortsat kan modtage filer.

Cloud håndterer op til 16 samtidige forbindelser med timeout fra accept af
forbindelsen. Databaseovergange, filuploads og ændringer af samme job er låst,
så samtidige polls ikke får samme job. Brug fortsat én proces/replica og det
samme permanente volume. Dette erstatter ikke behovet for en produktionsserver
og egentlig brugeradskillelse ved senere udvidelse.

Nye regressionstests kan køres uden at åbne lokale netværksporte:

```sh
python3 -m unittest cloud.test_regressions -v
node --test frontend/app.test.cjs
```

GitHub Actions kører desuden de eksisterende HTTP-tests på Python 3.11 og 3.13,
frontend-build og webcontainer-build. De starter ingen fysiske print.

Agenten fortsætter med at sende printerstatus, mens den afventer driverens resultat.
Et afventende journalført job holder køen optaget og sender ingen ny startkommando.

## Filtyper og farver

| Fil | Forløb |
| --- | --- |
| STL | Geometri uden farver; vælg ét filament og klargør |
| Bambu Studio 3MF-projekt | Én plade med dele/malede farver; vælg en spole pr. projektfilament og klargør |
| Færdigslicet .gcode.3mf | Bevar eksisterende farveskift, kontrollér printbaner og vælg AMS-pladser |

En færdigslicet fil med endelsen .3mf genkendes også ud fra indholdet. Modeller og rå 3MF-filer uploades med hhv. højst 50 og 100 MiB. 3MF skal være i millimeter og uden eksterne referencer. Flere plader afvises.

Farvevalgene vises enkeltvis i godkendelsesdialogen. Farveforskelle kræver accept; profilkonflikter på bare ét filament blokerer fysisk start. Spoleskift kontrolleres for alle valgte filamenter på Pi’en. Nye job lagrer required_plan_version, så en opdateret agent sammen med en gammel lokal service ikke kan modtage flerfarvejob.

De slicede printbaner farves efter G-code-værktøjsskift. Den oprindelige 3MF-modelvisning kan ikke vise alle Bambu-malingsudvidelser; kontrollér den færdigslicede farvefordeling. Filen konverteres ikke til STL under slicing. Browserbaseret maling af STL er ikke en del af denne ændring.
