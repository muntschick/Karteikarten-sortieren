# Karteikarten sortieren
App zum Zuordnen von Karteikarten zu verschiedenen Kategorien, im Hintergrund gibt es ein Auswertungsdashboard für fast zeitgleiche Analysen.  

.streamlit/secrets.toml ist wie folgt aufgebaut:
```
ACCESS_CODE = "Insert_Accesscode"
ADMIN_CODE = "Insert_Admincode"
"postgresql://link-zur-Datenbank"
APP_PROFICIENCY = "gewünschte_Fertigkeit"
ACCESS_LINK_TOKEN = "token-für-passwortfreien-Direktlink"
```
Die App lädt die Karten aus einer Datenbank, auch die Ergebnisse werden zurück an die Datenbank gesendet.

Die Datenbank hat folgendes Schema:  

<img width="732" height="626" alt="Datenbank_Schema" src="https://github.com/user-attachments/assets/99f0ecf6-cf2e-479f-937b-6d401e261f96" />

Besonders wichtig ist die Variable "proficiency", hierüber wird gesteuert, welche Karten aus der Datenbank angezeigt werden.
