from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
import requests
import os

app = FastAPI(
    title="API SUiv'Eau",
    description="Backend de relais pour les données Hub'Eau",
    version="1.0.0"
)

# --- CONFIGURATION CORS ---
# Autorise votre frontend (peu importe où il est hébergé) à interroger cette API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # En production, remplacez "*" par l'URL de votre GitHub Pages
    allow_credentials=True,
    allow_methods=["GET"],
    allow_headers=["*"],
)

# En-tête pour éviter les blocages de l'État (Erreur 10060)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json"
}


@app.get("/api/stations")
def get_stations(dept: str = Query("69", description="Code du département")):
    url = "https://hubeau.eaufrance.fr/api/v1/hydrometrie/stations"  # Retour en V1
    params = {"code_departement": dept, "en_service": "true", "size": 100}

    try:
        response = requests.get(url, params=params, headers=HEADERS, timeout=10)
        response.raise_for_status()
        data = response.json().get('data', [])

        data.sort(key=lambda x: x.get('libelle_station', ''))
        return data

    except requests.exceptions.RequestException as e:
        # ON AFFICHE LE VRAI BLOCAGE DANS LE TERMINAL
        print(f"\n[ERREUR RESEAU] Blocage de la requête : {e}\n")
        raise HTTPException(status_code=502, detail=f"Erreur Hub'Eau: {str(e)}")


@app.get("/api/observations/{code_station}")
def get_observations(code_station: str):
    url = "https://hubeau.eaufrance.fr/api/v1/hydrometrie/observations_tr"  # Retour en V1
    params = {"code_entite": code_station, "grandeur_hydro": "H", "size": 60}

    try:
        response = requests.get(url, params=params, headers=HEADERS, timeout=10)
        response.raise_for_status()
        data = response.json().get('data', [])

        return list(reversed(data))

    except requests.HTTPError as e:
        body = e.response.text[:300] if e.response is not None else ""
        print(f"[HUBEAU] {e.response.status_code} {path} {params} body={body!r}")
        if hit:
            return hit[1]
        raise HTTPException(status_code=502, detail=f"Hub'Eau {e.response.status_code}")
    except (requests.RequestException, ValueError) as e:
        print(f"[HUBEAU] {path} -> {e!r}")
        if hit:
            return hit[1]
        raise HTTPException(status_code=502, detail="Hub'Eau injoignable")
