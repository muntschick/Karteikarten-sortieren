# Karteikarten sortieren
## Oberfläche
App zum Zuordnen von Karteikarten zu verschiedenen Kategorien, in diesem Code sind es die GER-Niveaus Pre-A1, A1, A2, B1 und B2.
<img width="2738" height="105" alt="image" src="https://github.com/user-attachments/assets/4da5c5aa-2770-47b8-a5ef-7de9d292dbe6" />


Im Hintergrund gibt es ein **Auswertungsdashboard** für fast zeitgleiche Analysen:
<img width="1421" height="59" alt="image" src="https://github.com/user-attachments/assets/b9b3d5b1-c1d1-4a4b-8a48-b0a28cb66327" />

Beide Oberflächen sind mit Passwörtern geschützt (ACCESS_CODE und ADMIN_CODE), welche in den Streamlit-Secrets festgelegt werden.

## Streamlit-Secrets
.streamlit/secrets.toml ist wie folgt aufgebaut:
```
ACCESS_CODE = "Insert_Accesscode"
ADMIN_CODE = "Insert_Admincode"
"postgresql://link-zur-Datenbank"
APP_PROFICIENCY = "gewünschte_Fertigkeit"
ACCESS_LINK_TOKEN = "token-für-passwortfreien-Direktlink"
```  
## Datenbank
Die App lädt die Karten aus einer Datenbank, auch die Ergebnisse werden zurück an die Datenbank gesendet.

Die Datenbank hat folgendes Schema:  

<img width="496" height="563" alt="image" src="https://github.com/user-attachments/assets/703f45ab-ce5d-4c35-954c-02566b3b5249" />  

(die Tabelle group_memberships braucht man nur, wenn man rundenvergleich_app.py verwendet und zwei Eingaben derselben Gruppe (einmal Einzel- und einmal Gruppenarbeit) vergleichen möchte)  

Besonders wichtig ist die Variable "proficiency", hierüber wird gesteuert, welche Karten aus der Datenbank angezeigt werden.
