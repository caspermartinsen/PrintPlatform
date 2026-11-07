# Dokploy-webapp og Pi-agent

Frontend og backend kører sammen i én container. Pi'en opretter selv udgående
HTTPS til cloud og behøver ingen åbne porte. Denne version udfører kun simulerede
cloud-job; lokal live-tilstand afvises af agenten.

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
Pi-API'et forbliver på loopback. `PRINT_MODE=simulate` skal bevares.

## Funktionelt forløb

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
Pi'en skriver et holdbart journalspor før udførelse. Uafklaret udførelse bliver
unknown efter genstart; færdige resultater genafleveres uden ny udførelse, hvis
kvitteringen går tabt. Dispatched/unknown blokerer nye job og kræver manuel
gennemgang. Vælg **Luk efter kontrol**, når jobbet er kontrolleret på Pi'en;
det frigiver køen uden at genstarte eller stoppe jobbet. Ingen automatisk
køfrigivelse eller fysiske cloud-print i første version.

Pi vises offline efter 45 sekunder uden opdatering. Gamle printerfelter skjules.
Bevar `/data` og Pi'ens `/var/lib/print-agent` ved opdateringer; tab af journal
eller database kræver manuel gennemgang. Max. 1000 job og cirka 2 GiB filer;
Oprydning er manuel. Afsæt mindst 512 MiB RAM og tillad 100 MiB uploads i proxyen.
Ingen slicing, preview eller fjernannullering endnu.

## Tests

```sh
python3 -m unittest cloud.test_platform -v
cd service && python3 -m unittest -v
```

Integrationstestene bruger midlertidige lokale servere og erstatter printprocesser.
De tester godkendelse, overførsel, simulering, mistet kvittering, genstart og login.
Docker-build: `docker build -t printplatform .`.

Dokploy reference: https://docs.dokploy.com/docs/core/applications/build-type
