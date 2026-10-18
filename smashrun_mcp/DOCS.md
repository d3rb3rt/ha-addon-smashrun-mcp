# Smashrun MCP

Stellt deine Smashrun-Laufdaten als MCP-Server bereit (Streamable HTTP), damit Claude am Mac, im Browser und auf dem Handy darauf zugreifen kann.

## Optionen

| Option | Bedeutung |
|---|---|
| `access_token` | Smashrun-Token (API Explorer, Client-ID `client`, oder eigener API-Key) |
| `secret_path` | Geheimer Pfadteil, mindestens 24 Zeichen aus `A-Z a-z 0-9 _ -`. Der Endpunkt liegt unter `/<secret_path>/mcp` |
| `allow_write` | Schreib-Tools (Lauf anlegen/ändern/löschen, Notiz, Gewicht) an oder aus |
| `allowed_cidrs` | Optional: nur diese Absender-IPs zulassen, z. B. `160.79.104.0/21` (Anthropic-Ausgangsbereich). Leer = kein Filter |

Das Geheimnis erzeugst du mit `openssl rand -hex 24` oder `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`.

## Absicherung

Claude erlaubt für eigene Connectoren (Stand der Doku) keinen festen Zugangsschlüssel im Header, der Zugriff wird deshalb über den geheimen Pfad geschützt:

- Wer die URL kennt, hat vollen Zugriff auf alle Tools. Behandle die URL wie ein Passwort, teile sie nicht und poste sie nirgends.
- Jeder andere Pfad antwortet mit 404. Das Zugriffslog ist aus, damit das Geheimnis nicht im App-Log steht.
- Mit `allowed_cidrs` lässt sich der Zugriff zusätzlich auf Anthropics Server beschränken. Die IP wird aus `Cf-Connecting-Ip` bzw. `X-Forwarded-For` gelesen. Das ist nur sicher, wenn der Port 8099 nicht direkt aus dem Internet erreichbar ist, sondern nur über deinen Tunnel oder Proxy.
- Wenn dir das Risiko bei Schreibzugriff zu hoch ist, setze `allow_write` auf `false`. `delete_run` verlangt zusätzlich eine ausdrückliche Bestätigung.

## Erreichbarkeit aus dem Internet

Claude im Web und auf dem Handy ruft den Server von Anthropics Servern aus auf. Er muss deshalb über HTTPS öffentlich erreichbar sein. Der Webhook-Proxy der HA-MCP-App gilt nur für diese App. Für Smashrun brauchst du einen eigenen Weg, zum Beispiel:

- Cloudflare Tunnel mit einem Hostnamen, der auf `http://<HA-IP>:8099` zeigt
- Tailscale Funnel
- einen vorhandenen Reverse Proxy mit HTTPS

## Verbindung in Claude

1. Prüfe lokal: `http://<HA-IP>:8099/healthz` muss `ok` liefern.
2. In Claude unter *Einstellungen → Konnektoren → Benutzerdefinierten Konnektor hinzufügen* die URL `https://<deine-domain>/<secret_path>/mcp` eintragen, ohne Anmeldung.
3. Der Konnektor ist danach in Claude im Web, in der Desktop-App und auf dem Handy verfügbar, wenn er für das Konto angelegt wurde.
