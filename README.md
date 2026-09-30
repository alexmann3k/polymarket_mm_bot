# Polymarket Market Maker V1

Ein kleiner REST-basierter Bot für **Bid und Ask auf YES und NO**, mit exakt
**5 Shares pro neuer Order**. Kein Paar-Arbitrage-Modell und keine dynamische Größe.

## Repository structure

The application entry point is `main.py`; `bot.py` coordinates the live trading
cycle. Domain modules remain flat in the repository so they stay directly
importable and easy to test:

```text
main.py              application entry point
bot.py               runtime and trading orchestration
config.py / auth.py  configuration and CLOB authentication
scanner.py           market discovery and ranking
orderbook.py         order-book normalization
fair_value.py        fair-value calculation
quoting.py           price and direction logic
risk.py              market health and risk limits
inventory.py         inventory reconciliation and persistence
orders.py            order management and settlement
market_lifecycle.py  market transitions and cleanup
tests/               offline unit tests
```

`.env.example` is the versionable configuration template. `.env`, logs, caches,
virtual environments, and runtime data stay local and are excluded through
`.gitignore`.

## Aufteilung

- `scanner.py`: Gamma-Seiten laden, lokal filtern und Kandidaten ranken.
- `orderbook.py` / `models.py`: beide Bücher in einem Batch laden, normalisieren,
  eigene verbleibende Ordermengen aus dem sichtbaren Buch herausrechnen.
- `risk.py`: Market Health (GREEN/YELLOW/RED) und erlaubte Inventory-Seiten.
- `quoting.py`: tickgenaue Zielpreise ohne Netzwerkzugriffe.
- `orders.py`: genau eine Order pro Token/Seite, Stornos, Fills, Settlement, Heartbeat.
- `inventory.py`: bestätigte Bestände in Shares/Geldeinheiten, atomare CSV-Persistenz.
- `market_lifecycle.py`: Storno und Bestandsabgleich vor Marktwechsel.
- `main.py`: schlanker Anwendungseinstieg mit Signal-, Scan- und Shutdown-Schleife.
- `bot.py`: Laufzeit-Orchestrierung für Startup, Kontostatus, Recovery, Lifecycle und Quotes.
- `auth.py` / `config.py`: Client und validierte zentrale Konfiguration.

Scanner entscheidet **wo**, Health entscheidet **ob**, Inventory entscheidet
**welche Seiten**, Quoting entscheidet **zu welchen Preisen** gehandelt wird.

## Strategie

Gamma wird standardmäßig alle 300 Sekunden gescannt, mit wiederverwendeter
HTTP-Session und höchstens `SCANNER_REQUEST_LIMIT` Rohmärkten. Der Scan blockiert
weder Quotes noch Health Checks. Er benutzt keine zusätzlichen CLOB-Aufrufe pro Markt.

Aktive binäre YES/NO-Märkte müssen Orders akzeptieren, ausreichend weit vom
Enddatum entfernt sein und Preis-, Spread-, Liquiditäts- sowie 24h-Volumengrenzen
erfüllen. Innerhalb dieser Menge bleibt das konfigurierte mittlere Volumenband
(standardmäßig 40.–70. Perzentil). Score: Mittelwert der lokalen Perzentilränge
für Spread und Liquidität. Bei Gleichstand wird niedrigeres Volumen bevorzugt.
Volumen ist nur ein einfacher Konkurrenz-Proxy; der Bot erkennt keine
professionellen Market Maker. Die Rangfolge bezieht sich auf die geladene Stichprobe.

Der aktive Markt bleibt beim nächsten Scan aktiv, solange er weiterhin unter den
geeigneten Kandidaten ist. Andere Kandidaten liegen als Reserve im Speicher.
Beim Ausstieg werden Orders zuerst bestätigt storniert und Bestände abgeglichen.
Danach wird der nächste Kandidat anhand frischer Bücher geprüft. Der erste
Quote-Zyklus folgt erst nach einem zweiten Snapshot. Ein vollständiger Scan erfolgt
turnusmäßig oder bei erschöpfter Liste, mit mindestens 30 Sekunden Abstand zwischen
Scanversuchen, auch bei einer leeren Antwort.

Health verwendet dieselben externen Best Bids/Asks, Mids und Spreads wie Quoting.
Die letzten drei Snapshots werden standardmäßig gehalten:
- GREEN: beide Bücher haben genug Spread, gültige Preise und brauchbare Tiefe.
- YELLOW: Bid oder Ask eines Tokens bewegt sich um mehr als `MOVEMENT_THRESHOLD`.
  Alle Quotes werden sofort storniert.
- Nach `YELLOW_SNAPSHOTS` weiteren Beobachtungen: nur bei durchgehend stabilen
  Beobachtungen zurück zu GREEN; andernfalls RED.
- Spread Collapse, fehlende Buchseiten, Resolution Cutoff oder fehlender Edge
  nach Rundung führen unmittelbar zu RED.

Quotes pro Token:

```text
bid = floor_to_tick(best_bid + tick)
ask = ceil_to_tick(max(best_ask - tick, mid))
```

Bid und Ask müssen mindestens `MIN_QUOTE_SPREAD` auseinanderliegen, innerhalb der
Preisgrenzen bleiben und dürfen weder einander noch die Gegenbuchseite kreuzen.
Alle Orders sind post-only. Bei 0.40/0.46 und Tick 0.01 werden 0.41/0.45 gestellt.
Ein Buch mit nur ein oder zwei Ticks Spread erhält keine Quotes.

Unveränderte Zielpreise behalten ihre bestehenden Orders und ihre Queue-Position.
Ein bestätigter Fill führt vor weiteren Quotes zum Bestandsabgleich aller bekannten
Märkte. Verbotene Richtungen werden storniert; erlaubte Quotes bleiben bestehen,
sofern Health, Risk oder Quote-Refresh keinen Storno verlangen. Bei ungeklärtem
Settlement werden weiterhin alle Quotes storniert und neue Orders pausiert.
Neue Orders haben immer Größe 5, Teilorders werden
nicht mit dynamischer Größe aufgefüllt.

## Inventory und Kapital

Normalerweise sind bis zu vier Orders möglich: YES BUY/SELL und NO BUY/SELL.
Verkäufe benötigen mindestens fünf tatsächlich vorhandene Shares.
**Ohne Tokenbestand startet der Bot deshalb nur mit Kaufquotes.**
Er erzeugt keine Tokenpaare und verkauft nicht leer.

Entscheidend ist ausschließlich der tatsächlich gefüllte Bestand: `net = YES - NO`.
Offene, ungefüllte Orders zählen nicht als Bestand.

| Bestand | Erlaubte Kaufquotes |
| --- | --- |
| `net == 0` | YES und NO |
| `net > 0` | nur NO; offene YES BUY stornieren |
| `net < 0` | nur YES; offene NO BUY stornieren |

Bei einem Überhang gibt es bewusst keine SELL-Quote: LONG_YES erhält ausschließlich
NO BUY, LONG_NO ausschließlich YES BUY. Damit können zwei gleichzeitig gefüllte
Reduktionsorders den Bestand nicht auf die andere Seite überdrehen. Alle Preis-,
Spread-, Health-, Cash-, Positions- und Kapitalgrenzen gelten zusätzlich.
`INVENTORY_THRESHOLD` bleibt aus Kompatibilitätsgründen in der Konfiguration,
beeinflusst die Seitenwahl aber nicht mehr. Moduswechsel zwischen BALANCED,
LONG_YES und LONG_NO werden einmal pro Wechsel auf INFO geloggt.
Die feste Ordergröße 5 kann einen kleineren Überhang über null hinaus ausgleichen;
nach dem Fill und Bestandsabgleich wechselt dann die erlaubte Richtung.

`MAX_TOTAL_CAPITAL` ist bewusst konservativ: **1 Geldeinheit pro gehaltenem Share
und pro geplantem/offenem BUY-Share**, über alle im CSV bekannten Märkte.
Cash wird zusätzlich zum tatsächlichen Kaufpreis reserviert. Deshalb können
Orders schon vor Ausschöpfung des Kontoguthabens gesperrt werden. Der Bot
berechnet weder einen NAV noch tägliches PnL.

`Bot.active_market` (weiterhin auch `Bot.market`) bezeichnet den normalen aktiven Markt.
Ausgestiegene Märkte mit `net != 0` bleiben separat in `Bot.inventory_markets` und
werden neben dem aktiven Markt in Richtung Bestandsausgleich verwaltet. GREEN
erlaubt Quotes, YELLOW/RED pausieren sie. Nach RED sind zwei neue gesunde Snapshots
nötig. Resolution Cutoff und bekannte geschlossene Märkte bleiben ohne Quotes
gespeichert. Ein Markt verlässt diese Verwaltung erst bei `net == 0` und bestätigtem
Storno aller Orders; ein Fill während des Stornos wird erneut abgeglichen.

Restbestände bleiben auch danach im CSV und verbrauchen weiterhin Kapitalbudget.
Offene Orders anderer Märkte reservieren ebenfalls Cash, Kapital und Order-Slots.
Beim Start werden alle gespeicherten Märkte mit YES/NO-Token-IDs über die CLOB-
Balance-API abgeglichen und die CSV mit den bestätigten Werten aktualisiert; der
USDC-Bestand wird dabei einmal kontoweit gelesen. Erst danach werden unbalancierte
Märkte auch außerhalb der Scanner-Kandidaten über Gamma wiederhergestellt.
Fehlende Lifecycle-Metadaten pausieren den Start, statt Bestände zu vergessen.
Das alte CSV-Format mit Rohbeträgen wird beim
Laden als 6-Dezimal-Format migriert; fehlende Token-IDs werden einmalig gesammelt
über Gamma aufgelöst. CSV nicht zwischen Botläufen löschen.

## Order-Sicherheit und API-Verhalten

Dieses Programm setzt ein **dediziertes Trading-Konto ohne parallele Bots oder
manuelle Orders** voraus. Beim Live-Start und Shutdown werden **alle offenen Orders
dieses Kontos** storniert, einschließlich übrig gebliebener Orders früherer Läufe.
Unbekannte Orders während des Betriebs führen zum Sicherheitsstopp.
Bestände außerhalb der vom Bot gespeicherten Märkte werden nicht als vollständiges
Wallet-Portfolio entdeckt.

Pro unverändertem Zyklus:
- ein Batch-Request für beide Bücher pro aktiv verwaltetem, handelbarem Markt;
- ein Request für offene Orders (bei paginierten Antworten entsprechend mehr);
- drei Balance-Requests pro bekanntem Markt alle 30 Sekunden oder nach relevanten Änderungen;
- ein Heartbeat ungefähr alle 5–6 Sekunden bei gesunden Quotes.

Zusätzliche Order-/Trade-Detailabfragen erfolgen bei Fills, verschwundenen Orders
und Stornos. Startup-Recovery liest einmalig auch die paginierte Trade-Historie,
um noch offene Settlements zu erkennen. Keine Requests nur für Health oder Mid.

Stornoantworten werden geprüft; ein verschwundener Order-Eintrag wird nie
automatisch als Fill interpretiert. Änderungen von `size_matched` werden geloggt,
zugehörige Trades bis CONFIRMED/FAILED verfolgt und erst danach Balance-Werte
für neue Quotes verwendet. Ein ungeklärtes Settlement stoppt nach 120 Sekunden.

Fehlgeschlagene Balance-Requests erhalten den bisherigen Bestand und pausieren
den Handel. Bestätigte HTTP-Ablehnungen sind begrenzt wiederholbar.
Ein Timeout beim Platzieren hat ein unbekanntes Ergebnis: Der Bot storniert beim
Shutdown kontoweit und stoppt, statt die Order blind zu wiederholen.

`QUOTE_REFRESH_SECONDS` steuert den regelmäßigen Cancel/Replace normaler GTC-Quotes.
Inventory-Recovery-Quotes verwenden stattdessen GTD mit
`INVENTORY_QUOTE_EXPIRATION_SECONDS=35`: Unix-Expiration = aktuelle Zeit + dem größeren von
dieser Dauer und `GTD_MIN_EXPIRATION_SECONDS=240`. Der CLOB verlangt aktuell mehr als 180
Sekunden Vorlauf; standardmäßig läuft die GTD-Order daher etwa 240 Sekunden später ab.
Nach bestätigtem Ablauf und Fill-Abgleich quotet der bestehende Loop bei weiterem
Ungleichgewicht erneut. Risk-, Health-, Exit- und Shutdown-Cancels gelten weiterhin sofort.
`ORDER_TTL_SECONDS` ist weiterhin ein **Watchdog für ausbleibende gesunde Zyklen**.
Der CLOB-Heartbeat
wird nur aus einem gesunden Trading-Zyklus erneuert. Laut Polymarket werden bei
ausbleibendem Heartbeat offene Orders nach ungefähr 10 Sekunden, mit bis zu
5 Sekunden Puffer, serverseitig storniert. Eine erfolgreiche Live-Anbindung wurde
durch die Offline-Tests nicht nachgewiesen.

## Konfiguration

Bestehende `.env`-Werte überschreiben Defaults. Credentials werden nicht geändert.
Insbesondere ein bisher gesetztes Scan-Intervall von 60 Sekunden bleibt 60, bis
du es selbst auf 300 setzt. Für Neuinstallationen gibt es `.env.example`.

| Variable | Default | Bedeutung |
| --- | --- | --- |
| DRY_RUN | true | Orders nur im Speicher; keine simulierten Fills |
| MARKET_SCAN_INTERVAL_SECONDS | 300 | regulärer Vollscan |
| POLL_INTERVAL_SECONDS | 3 | Trading-Pause, maximal 5 Sekunden |
| MIN_SPREAD / MAX_SPREAD | 0.03 / 0.10 | zulässiger externer Spread |
| MIN_QUOTE_SPREAD | 0.01 | Mindestabstand zwischen unseren Quotes |
| MIN_PRICE / MAX_PRICE | 0.05 / 0.95 | Preisbereich |
| MOVEMENT_THRESHOLD | 0.03 | absolute Preisbewegung, keine Prozentzahl |
| YELLOW_SNAPSHOTS | 2 | weitere Beobachtungen nach einem Sprung |
| INVENTORY_THRESHOLD | 5 | einseitige Reduktion ab diesem Überhang |
| MAX_POSITION_PER_SIDE | 10 | Hard Limit je Token, inklusive Zielorders |
| ORDER_SIZE / SANITY_TEST_SIZE | 5 / 5 | andere Werte werden abgelehnt |
| MAX_OPEN_ORDERS | 10 | zusätzlicher globaler Orderdeckel, V1 normalerweise max. 4 |
| MAX_TOTAL_CAPITAL | 100 | konservatives Budget, siehe oben |
| MIN_VOLUME_24H / MAX_VOLUME_24H | 1000 / 100000 | Aktivitätsband |
| MIN_LIQUIDITY | 1000 | Mindestliquidität laut Gamma |
| PERCENTILE_LOW / PERCENTILE_HIGH | 40 / 70 | zusätzliches lokales Volumenband |
| SCANNER_REQUEST_LIMIT | 1000 | maximal geladene Rohmärkte |
| SCANNER_MIN_BOOK_DEPTH | 1 | Mindestzahl Preislevel je Buchseite |
| MIN_RESOLUTION_HOURS | 1 | Abstand zum Enddatum |
| INVENTORY_REFRESH_SECONDS | 30 | Abgleich auch ohne erkannte Fills |
| ORDER_TTL_SECONDS | 15 | Watchdog |
| QUOTE_REFRESH_SECONDS | 10 | regulärer Cancel/Replace-Intervall |
| SCAN_RETRY_SECONDS | 30 | Mindestabstand bei Scanfehlern/erschöpfter Liste |
| MAX_CONSECUTIVE_ERRORS | 5 | danach kontrollierter Stopp |
| SETTLEMENT_TIMEOUT_SECONDS | 120 | maximal ungeklärtes Settlement |

Alte Variablen `QUOTE_OFFSET`, `QUOTE_OFFSET_MODE`, `TICK_SIZE`, `VOL_WINDOW`,
`VOL_THRESHOLD`, `ARB_FEE_BUFFER` und `MAX_DAILY_LOSS` werden nicht mehr verwendet.
Ein funktionierender Daily-Loss-Stop wird nicht vorgetäuscht; vorher wurde der
Verlustwert im Projekt überhaupt nicht aktualisiert.

## Ausführen und prüfen

Vorhandene virtuelle Umgebung verwenden. Das bereits installierte und geprüfte
SDK ist in `requirements.txt` auf `py-clob-client-v2==1.1.0` festgehalten.
Es wurden keine neuen Abhängigkeiten installiert.

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe main.py
```

Auch DRY_RUN verwendet authentifizierte Lesezugriffe und echte Balance-Snapshots;
es ist kein vollständiger Paper-Trading-Simulator. Vor dem Start DRY_RUN prüfen.

`sanity_check.py` verwendet vorhandene Credentials, falls verfügbar. Ohne
`--place-test-order` sendet es keine Order. Der explizite Live-Test benötigt
DRY_RUN=false, sendet eine post-only Order mit fünf Shares und storniert sie direkt
wieder; wie der Bot verwendet er kontoweite Recovery/Cleanup. Ein sofortiger Fill
ist trotz sofortiger Stornierung grundsätzlich möglich.

Nicht implementiert: WebSockets, automatische Liquidation, Merge/Split/Redemption,
automatische Allowances, PnL-Buchhaltung/Daily-Loss-Stop, gleichzeitiger Handel mehrerer
Märkte, dynamische Größen oder Avellaneda–Stoikov.

API-Verträge wurden mit dem installierten SDK und offiziellen Quellen abgeglichen:
[Order-Status und Einheiten](https://docs.polymarket.com/api-reference/trade/get-single-order-by-id),
[Stornoantworten](https://docs.polymarket.com/api-reference/trade/cancel-single-order),
[Heartbeat und Order-Patterns](https://github.com/Polymarket/agent-skills/blob/main/order-patterns.md).
