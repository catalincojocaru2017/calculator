# esar-report

Skill Claude Code care pregătește o sesizare e-SAR (https://hub.mai.gov.ro/esar) dintr-o poză sau un video cu o abatere rutieră: blurează fețele, extrage data/ora/locul, redactează textul și completează formularul fără să-l trimită. Instrucțiunile pentru Claude sunt în [SKILL.md](SKILL.md).

## Ce face automat și ce nu

| Pas | Cine |
|---|---|
| Blur fețe (poză și video), plăcuța rămâne lizibilă | script (`scripts/blur_faces.py`) |
| Verificare vizuală a blurului, coordonate pentru fețe ratate | Claude, pe imaginea `_check.jpg` |
| Text sesizare, data/ora/loc din metadate | Claude |
| Completarea formularului | Claude, dacă are browser |
| Autentificare (CEI, ROeID, Ghișeul.ro, cont HUB), CNP, declarație, **Trimite** | **tu** |

Sesizarea nu se mai poate anula sau modifica după trimitere. De aceea ultimul pas rămâne al tău.

## Folosire

```bash
python3 -m venv .venv
.venv/bin/pip install -r .claude/skills/esar-report/scripts/requirements.txt
.venv/bin/python .claude/skills/esar-report/scripts/blur_faces.py esar-in/POZA.jpg -o esar-out
# față ratată, coordonate de pe grila din esar-out/POZA_check.jpg:
.venv/bin/python .claude/skills/esar-report/scripts/blur_faces.py esar-in/POZA.jpg -o esar-out --box 420,310,60,70
```

În Claude Code, atașează poza și spune „raportează incidentul din poză pe e-SAR"; skill-ul pornește singur. Ca să-l ai în toate proiectele, copiază directorul în `~/.claude/skills/`.

## Limite (citește)

- **Fotografie vs. video.** e-SAR a fost lansat pentru înregistrări video; nicio sursă consultată nu menționează fotografii. Dacă formularul nu acceptă poze, poza nu poate fi depusă acolo.
- **Detecția automată ratează fețe** (profil, foarte mici, în spatele geamurilor, în oglinzi, pe ecrane). Modelul e YuNet; prinde fețe de ~10–300 px. Verifică mereu `_check.jpg` și adaugă `--box`.
- La video, `--box` acoperă aceeași zonă în toate cadrele, iar `_check.jpg` arată doar 8 cadre.
- Fișierele din `esar-in/` și `esar-out/` conțin fețe, GPS și plăcuțe: sunt în `.gitignore`, nu le comite.
- Nu retușa plăcuța și nu modifica altceva decât fețele: sesizarea trebuie să rămână o probă autentică.

## Teste

```bash
.venv/bin/pip install pytest scikit-image
.venv/bin/python -m pytest .claude/skills/esar-report/tests
```

Modelul `models/face_detection_yunet_2023mar.onnx` (YuNet, licență MIT, din OpenCV Zoo) e verificat la încărcare cu sha256 fixat în script.
