# Smashrun MCP (Home-Assistant-App)

MCP-Server für Smashrun-Laufdaten (Streamable HTTP) als Home-Assistant-App. Liest und schreibt Läufe, Notizen und Gewicht und wertet Läufe sekundengenau aus (`analyze_activity`), beschreibt Strecken (`describe_route`), zeichnet Kartenbilder (`render_route_map`) und liefert Höhenprofile (`get_elevation_profile`).

## Installation

1. In Home Assistant: Einstellungen → Apps → App-Store → ⋮ → Repositories, URL `https://github.com/d3rb3rt/ha-addon-smashrun-mcp` hinzufügen.
2. „Smashrun MCP" installieren.
3. Optionen setzen (`access_token`, `secret_path`, siehe `smashrun_mcp/DOCS.md`) und starten.

Token und Geheimnis stehen nur in den App-Optionen in Home Assistant, nie in diesem Repository.

## Updates

`version` in `smashrun_mcp/config.yaml` erhöhen und pushen. Home Assistant zeigt danach ein Update an.
