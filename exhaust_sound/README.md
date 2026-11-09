# Fake exhaust sound (OBD2 RPM → sunet de motor)

Citește RPM de la un adaptor ELM327 (Hyundai i20 MK1 1.2 benzină) și redă în timp real un sunet de motor
sintetizat, a cărui tonalitate și intensitate urmează turația. Ieșirea audio poate fi orice dispozitiv,
inclusiv un soundbar Bluetooth. Nu folosește Car Scanner (adaptoarele acceptă un singur client).

Doar Mode 01 (citire live): `010C` RPM și `0111` clapetă (dacă e suportat). Nu se scrie nimic în ECU.

## Instalare
```bash
pip install -r exhaust_sound/requirements.txt
```

## Test fără mașină (simulator)
```bash
python -m exhaust_sound.main --source sim --profile ferrari_v8
python -m exhaust_sound.main --source sim --profile lambo_v10 --wav demo.wav   # doar fișier WAV
python -m exhaust_sound.main --list-devices                                    # găsește soundbar-ul
python -m exhaust_sound.main --source sim --device "soundbar"
```
Profile: `ferrari_v8`, `lambo_v10`, `bmw_i6`, `muscle_v8`.
Maparea: ralantiul real (800) și limita reală (6000) se mapează pe intervalul „virtual" al profilului
(ex. Ferrari 1000-8700 rpm). Ajustează cu `--real-idle` / `--real-redline`.

## Cu mașina (laptop sau Raspberry Pi în mașină)
Adaptor Bluetooth Classic, Linux:
```bash
bluetoothctl            # scan on, pair <MAC> (PIN 1234 sau 0000), trust <MAC>
sudo rfcomm bind 0 <MAC>
python -m exhaust_sound.main --source serial --port /dev/rfcomm0 --device "soundbar"
```
Windows: după asociere apare un port COMx, folosește `--port COM5`.
Adaptor WiFi: `--source tcp --host 192.168.0.10 --tcp-port 35000`.
Contactul trebuie să fie pus (ignition ON).

## Teste
```bash
python -m unittest discover -s exhaust_sound/tests -t . -v
```

## Limitări cunoscute
- Latența A2DP spre soundbar: ~150-300 ms. Pentru latență mică folosește cablu AUX.
- Pe telefon, Bluetooth-ul Classic al adaptorului și A2DP al soundbar-ului pot interfera.
- Sunetul este sintetizat (nu înregistrare), deci e un punct de pornire pentru reglaj, nu un clon exact de Ferrari.
