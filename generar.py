"""Lluvia en Ecuador, cantón por cantón: cuánto va a llover, cuánto llovió y dónde llueve ahora.

Una corrida (cada hora, en GitHub Actions) baja las fuentes públicas, las cruza con los cantones del Ecuador
(continente y Galápagos) y escribe en public/:
- datos.json: lo que lee la página.
- estado.json.gz: la memoria de la corrida (última respuesta buena de cada fuente y las horas de satélite ya bajadas).
  La corrida siguiente la lee de la página publicada. Si una fuente falla, se conserva su último dato bueno con su
  hora y la página lo dice: nunca queda vacía.

Fuentes (todas públicas):
- Pronóstico del INAMHI: modelo WRF en pasos de 3 h, última corrida (GeoServer del visor del INAMHI,
  services.geoglows.org). Ojo: hay que pedir la corrida (INITD) explícitamente; si no, el WCS devuelve una vieja.
- Rango: las 51 corridas del ECMWF (Open-Meteo, gratis), en el centro de cada cantón, renovadas cada 6 h.
- Pluviómetros del INAMHI que están transmitiendo (inamhi.gob.ec/api_visor): lluvia medida. APAGADO por ahora:
  el INAMHI restringió esos datos a su visor; se enciende con PLUVIOMETROS=1 cuando dé acceso.
- Satélite PERSIANN-PDIR, por hora: lluvia estimada (sirve donde no hay pluviómetro).
- Rayos del satélite GOES-19 (GLM), últimos 10 minutos.

Solo biblioteca estándar. Uso: python3 generar.py   (URL_PUBLICADA=https://... para leer la memoria publicada)
"""

import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

AQUI = os.path.dirname(os.path.abspath(__file__))
PUBLICO = os.path.join(AQUI, "public")
GEOSERVER = "https://services.geoglows.org/geoserver"
INAMHI = "https://inamhi.gob.ec/api_visor"
ENSAMBLE = "https://ensemble-api.open-meteo.com/v1/ensemble"
UA = {"User-Agent": "lluvia-ecuador (pagina publica de pronostico por canton)"}
CAJA = (-81.5, -5.5, -75.0, 1.7)  # oeste, sur, este, norte (Ecuador continental)
CAJA_GALAPAGOS = (-92.2, -1.6, -89.0, 0.85)

PASOS = 16  # pasos de 3 h que se muestran: 48 h
VENTANAS = {"12": 4, "24": 8, "48": 16}  # horas -> pasos
ENSAMBLE_CADA = timedelta(hours=6)  # el ECMWF corre cada 6 h; así se cuidan las llamadas gratis de Open-Meteo
SATELITE_HORAS = 72
PLUVIO_HORAS_MIN = 18  # un pluviómetro cuenta si trae al menos 18 de las últimas 24 horas
PLUVIO_ATRASO = timedelta(hours=4)  # ...y su último dato no tiene más de 4 h


# ---------------------------------------------------------------- utilidades


def pedir(url, datos=None, timeout=90, intentos=3):
    """GET (o POST con JSON) con reintentos ante fallas de red, 429 y 5xx."""
    cab = dict(UA)
    if datos is not None:
        datos = json.dumps(datos).encode()
        cab["Content-Type"] = "application/json"
    for k in range(intentos):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=datos, headers=cab), timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code < 500 and e.code != 429 or k == intentos - 1:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if k == intentos - 1:
                raise
        time.sleep(5 * (k + 1))


def iso(t):
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fecha(texto):
    return datetime.fromisoformat(texto.replace(".000Z", "Z").replace("Z", "+00:00"))


def percentil(ordenados, p):
    if not ordenados:
        return None
    x = (len(ordenados) - 1) * p
    i = int(x)
    j = min(i + 1, len(ordenados) - 1)
    return ordenados[i] + (ordenados[j] - ordenados[i]) * (x - i)


# ---------------------------------------------------------------- territorio


class Rejilla:
    def __init__(self, x0, y0, dx, dy, filas):
        self.x0, self.y0, self.dx, self.dy, self.filas = x0, y0, dx, dy, filas


class Territorio:
    """Celdas de 0,02° con su cantón: continente (preparar_territorio.py del Monitoreo Caro, geoBoundaries / INEC) y
    Galápagos (preparar_galapagos.py)."""

    def __init__(self):
        self.rejillas, self.celdas = [], []
        for nombre in ("rejilla.json", "galapagos.json"):
            r = json.load(open(os.path.join(AQUI, "territorio", nombre)))
            ids = []
            for k in range(0, len(r["tramos"]), 2):
                ids += [str(r["tramos"][k]) if r["tramos"][k] else None] * r["tramos"][k + 1]  # ids como texto, igual que en JSON
            self.rejillas.append((r["oeste"], r["norte"], r["paso"], r["ancho"], r["alto"], ids))
            self.celdas += [(r["oeste"] + (j + 0.5) * r["paso"], r["norte"] - (i + 0.5) * r["paso"], c)
                            for i in range(r["alto"]) for j in range(r["ancho"]) if (c := ids[i * r["ancho"] + j])]
        mapa = json.load(open(os.path.join(PUBLICO, "mapa.json")))
        self.cantones = [str(c["id"]) for c in mapa["cantones"]]
        self._cruces = {}
        # Centro de cada cantón: su celda más cercana al promedio (así cae dentro aunque el cantón sea curvo).
        suma = defaultdict(lambda: [0.0, 0.0, 0])
        for lon, lat, c in self.celdas:
            s = suma[c]
            s[0] += lon
            s[1] += lat
            s[2] += 1
        medio = {c: (s[0] / s[2], s[1] / s[2]) for c, s in suma.items()}
        self.centros = {}
        for lon, lat, c in self.celdas:
            d = (lon - medio[c][0]) ** 2 + (lat - medio[c][1]) ** 2
            if c not in self.centros or d < self.centros[c][2]:
                self.centros[c] = (lon, lat, d)

    def canton_de(self, lon, lat):
        for oeste, norte, paso, ancho, alto, ids in self.rejillas:
            i, j = int((norte - lat) / paso), int((lon - oeste) / paso)
            if 0 <= i < alto and 0 <= j < ancho and ids[i * ancho + j]:
                return ids[i * ancho + j]
        return None

    def promedio(self, rej):
        """Promedio de la cuadrícula en cada cantón (el cruce celda -> píxel se calcula una vez por geometría)."""
        clave = (rej.x0, rej.y0, rej.dx, rej.dy, len(rej.filas), len(rej.filas[0]))
        if clave not in self._cruces:
            cruce = []
            for lon, lat, c in self.celdas:
                i, j = int((lat - rej.y0) / rej.dy), int((lon - rej.x0) / rej.dx)
                if 0 <= i < len(rej.filas) and 0 <= j < len(rej.filas[0]):
                    cruce.append((i, j, c))
            self._cruces[clave] = cruce
        suma, n = Counter(), Counter()
        for i, j, c in self._cruces[clave]:
            v = rej.filas[i][j]
            if 0 <= v < 1e4:  # negativos y valores enormes = sin dato
                suma[c] += v
                n[c] += 1
        return {c: suma[c] / n[c] for c in n}


# ---------------------------------------------------------------- GeoServer del visor del INAMHI

_capacidades = {}


def capacidades(espacio):
    if espacio not in _capacidades:
        _capacidades[espacio] = pedir(f"{GEOSERVER}/{espacio}/wms?service=WMS&version=1.1.1&request=GetCapabilities",
                                      timeout=180).decode()
    return _capacidades[espacio]


def dimension(espacio, capa, nombre):
    """(valor por defecto, lista) de una dimensión (time, INITD) de una capa, según el GetCapabilities WMS."""
    xml = capacidades(espacio)
    trozo = xml[xml.index(f"<Name>{capa}</Name>"):]
    trozo = trozo[:trozo.index("</Layer>")]
    m = re.search(rf'<Extent name="{nombre}"([^>]*)>([^<]*)', trozo)
    defecto = re.search(r'default="([^"]+)"', m.group(1))
    return (defecto.group(1) if defecto else None), [v for v in m.group(2).strip().split(",") if v]


def cuadricula(espacio, capa, cuando, corrida=None, caja=CAJA):
    o, s, e, n = caja
    url = (f"{GEOSERVER}/{espacio}/wcs?service=WCS&version=2.0.1&request=GetCoverage&coverageId={espacio}__{capa}"
           f"&format=text/plain&subset=Long({o},{e})&subset=Lat({s},{n})&subset=time(%22{cuando}%22)")
    if corrida:
        url += f"&subset=INITD(%22{corrida}%22)"
    texto = pedir(url, timeout=180).decode()
    par = lambda k: float(re.search(rf'{k}", ([-\d.E]+)', texto).group(1))
    filas = [[float(v) for v in linea.split()] for linea in texto.split("Band 0:")[1].strip().splitlines()]
    return Rejilla(par("elt_0_2"), par("elt_1_2"), par("elt_0_0"), par("elt_1_1"), filas)


# ---------------------------------------------------------------- fuentes


def wrf(ahora, terr):
    """Pronóstico del INAMHI: mm en cada paso de 3 h de la última corrida, desde ahora hasta 48 h."""
    corrida, corridas = dimension("wrf", "wrf_precipitation", "INITD")
    corrida = corrida or max(corridas)
    _, tiempos = dimension("wrf", "wrf_precipitation", "time")
    pasos = sorted(t for t in tiempos if fecha(t) > ahora and fecha(t) >= fecha(corrida))[:PASOS]
    if not pasos:
        raise RuntimeError(f"la corrida {corrida} no tiene pasos por delante")
    with ThreadPoolExecutor(4) as ex:
        rejillas = list(ex.map(lambda t: cuadricula("wrf", "wrf_precipitation", t, corrida), pasos))
    por_paso = [terr.promedio(r) for r in rejillas]
    return {"corrida": iso(fecha(corrida)), "pasos": [iso(fecha(t)) for t in pasos],
            # Solo los cantones que caen en la malla del WRF (no cubre Galápagos).
            "mm": {c: [round(p.get(c, 0), 1) for p in por_paso] for c in terr.cantones if any(c in p for p in por_paso)}}


def ensamble(terr):
    """51 corridas del ECMWF (Open-Meteo) en el centro de cada cantón, sumadas en pasos de 3 h que terminan a las
    01, 04, 07... UTC (los mismos del WRF del INAMHI)."""
    cantones = [c for c in terr.cantones if c in terr.centros]
    q = urllib.parse.urlencode({
        "latitude": ",".join(f"{terr.centros[c][1]:.3f}" for c in cantones),
        "longitude": ",".join(f"{terr.centros[c][0]:.3f}" for c in cantones),
        "hourly": "precipitation", "models": "ecmwf_ifs025", "forecast_days": 4, "timezone": "GMT",
    })
    respuesta = json.loads(pedir(f"{ENSAMBLE}?{q}", timeout=240))
    if isinstance(respuesta, dict):
        respuesta = [respuesta]
    miembros_por_canton = {}
    for c, d in zip(cantones, respuesta):
        h = d["hourly"]
        # Open-Meteo: la lluvia de la hora T es la de la hora que termina en T.
        fines = [(datetime.fromisoformat(t).replace(tzinfo=timezone.utc)) for t in h["time"]]
        claves = [k for k in h if k.startswith("precipitation")]
        bins = defaultdict(lambda: [0.0] * len(claves))
        for i, t in enumerate(fines):
            resto = (t.hour - 1) % 3  # 01, 04, 07... UTC cierran el paso
            fin = t + timedelta(hours=(3 - resto) % 3)
            for m, k in enumerate(claves):
                bins[iso(fin)][m] += h[k][i] or 0
        miembros_por_canton[c] = {f: [round(v, 1) for v in vs] for f, vs in bins.items()}
    return {"modelo": "ECMWF IFS 0,25° (51 corridas)", "miembros": miembros_por_canton,
            "por_canton": len(miembros_por_canton)}


def estaciones():
    return json.loads(pedir(f"{INAMHI}/station_information/estaciones/visores/?id_aplicacion=vs_1h_inh", timeout=120))


def lluvia_estacion(e, ahora):
    d = json.loads(pedir(f"{INAMHI}/station_data_automaticas/get_precipitation/", {"id_estacion": e["id_estacion"]},
                         timeout=40, intentos=2))
    serie = next((s for s in d.get("series", []) if "PRECIP" in s["name"].upper()), None)
    if not serie:
        return None
    horas = {fecha(p["date"]): p["value"] for p in serie["data"] if p["value"] is not None and p["value"] >= 0}
    ult = max(horas, default=None)
    h24 = [v for t, v in horas.items() if t > ahora - timedelta(hours=24)]
    if not ult or ult < ahora - PLUVIO_ATRASO or len(h24) < PLUVIO_HORAS_MIN:
        return None
    inicio = (ahora - timedelta(hours=24)).replace(minute=0, second=0, microsecond=0)
    return {
        "codigo": e["codigo"], "nombre": e["punto_obs"].strip(), "propietario": e["propietario"],
        "lon": round(float(e["longitud"]), 4), "lat": round(float(e["latitud"]), 4),
        "ultimo": iso(ult), "horas_24": len(h24),
        "mm_1h": round(horas.get(ult, 0), 1),
        "mm_24h": round(sum(h24), 1),
        "mm_72h": round(sum(v for t, v in horas.items() if t > ahora - timedelta(hours=72)), 1),
        "horaria": [round(horas[inicio + timedelta(hours=k)], 1) if inicio + timedelta(hours=k) in horas else None
                    for k in range(1, 25)],
        "horaria_hasta": iso(inicio + timedelta(hours=24)),
    }


def pluviometros(ahora, terr):
    lista = estaciones()
    vivas = [e for e in lista if e["id_estado_transmision"] == 1 and e["latitud"] and e["longitud"]
             and e["categoria"] != "HIDROLOGICA"]
    with ThreadPoolExecutor(16) as ex:
        res = list(ex.map(lambda e: _o_nada(lluvia_estacion, e, ahora), vivas))
    salida, vistos = [], set()
    for p in res:
        if p and p["codigo"] not in vistos:  # el INAMHI repite estaciones con dos ids
            vistos.add(p["codigo"])
            p["canton"] = terr.canton_de(p["lon"], p["lat"])
            salida.append(p)
    if len(vivas) > 20 and len(salida) < 0.2 * len(vivas):
        raise RuntimeError(f"solo respondieron {len(salida)} de {len(vivas)} estaciones")
    return {"transmitiendo": len(vivas), "con_dato": len(salida), "estaciones": salida}


def _o_nada(f, *a):
    try:
        return f(*a)
    except Exception:
        return None


def satelite(ahora, terr, previas):
    """Lluvia estimada por satélite (PERSIANN-PDIR) por hora y por cantón. Solo baja las horas que faltan."""
    _, tiempos = dimension("satellite_based_precipitation", "persiann_pdir_hourly", "time")
    recientes = sorted(t for t in tiempos if fecha(t) > ahora - timedelta(hours=SATELITE_HORAS))
    horas = {h: v for h, v in previas.items() if fecha(h) > ahora - timedelta(hours=SATELITE_HORAS)}
    faltan = [t for t in recientes if iso(fecha(t)) not in horas][-SATELITE_HORAS:]
    def bajar(t):  # continente y Galápagos; la hora cuenta solo si llegan las dos
        a = _o_nada(cuadricula, "satellite_based_precipitation", "persiann_pdir_hourly", t)
        b = a and _o_nada(cuadricula, "satellite_based_precipitation", "persiann_pdir_hourly", t, None, CAJA_GALAPAGOS)
        return (a, b) if a and b else None

    with ThreadPoolExecutor(4) as ex:
        nuevas = list(ex.map(bajar, faltan))
    for t, par in zip(faltan, nuevas):
        if par:
            prom = {**terr.promedio(par[0]), **terr.promedio(par[1])}
            horas[iso(fecha(t))] = {c: round(v, 1) for c, v in prom.items() if v >= 0.05}
    if not horas:
        raise RuntimeError("sin horas de satélite")
    return {"horas": horas, "fallidas": sum(1 for r in nuevas if r is None)}


def rayos(terr):
    """Rayos del GOES-19 (GLM) que entrega ahora el GeoServer (la última ventana de unos 10 minutos)."""
    gj = json.loads(pedir(f"{GEOSERVER}/postgis/ows?service=WFS&version=2.0.0&request=GetFeature"
                          f"&typeNames=postgis:goes_glm_l2_lcfa&outputFormat=application/json", timeout=90))
    o, s, e, n = CAJA
    puntos = []
    for f in gj.get("features", []):
        lon, lat = f["geometry"]["coordinates"][:2]
        if o <= lon <= e and s <= lat <= n:
            puntos.append([round(lon, 3), round(lat, 3), terr.canton_de(lon, lat)])
    return {"hora": iso(datetime.now(timezone.utc)), "puntos": puntos}


# ---------------------------------------------------------------- memoria entre corridas


def memoria_anterior():
    local = os.path.join(PUBLICO, "estado.json.gz")
    base = os.environ.get("URL_PUBLICADA", "").rstrip("/")
    if base:
        try:
            return json.loads(gzip.decompress(pedir(f"{base}/estado.json.gz", timeout=60)))
        except urllib.error.HTTPError as e:
            if e.code == 404 and os.environ.get("PRIMERA_VEZ") == "1":
                return {}
            raise SystemExit(f"No se pudo leer la memoria publicada ({e}); no se publica para no borrar datos.")
        except Exception as e:
            raise SystemExit(f"No se pudo leer la memoria publicada ({e}); no se publica para no borrar datos.")
    if os.path.exists(local):
        return json.loads(gzip.decompress(open(local, "rb").read()))
    return {}


# ---------------------------------------------------------------- armar la página


def armar(ahora, estado, terr):
    f = estado["fuentes"]
    w = (f.get("wrf") or {}).get("datos")
    # Los pasos de 3 h que vienen: los del WRF; si el WRF no está, los mismos horarios (01, 04, 07... UTC).
    if w:
        pasos = [p for p in w["pasos"] if fecha(p) > ahora][:PASOS]
    else:
        t = ahora.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        while (t.hour - 1) % 3:
            t += timedelta(hours=1)
        pasos = [iso(t + timedelta(hours=3 * k)) for k in range(PASOS)]
    ens = ((f.get("ensamble") or {}).get("datos") or {}).get("miembros", {})
    sat = ((f.get("satelite") or {}).get("datos") or {}).get("horas", {})
    horas_sat = sorted(sat)
    ultima_sat = horas_sat[-1] if horas_sat else None
    pl = ((f.get("pluviometros") or {}).get("datos") or {}).get("estaciones", [])
    por_canton_pl = defaultdict(list)
    for e in pl:
        if e.get("canton"):
            por_canton_pl[e["canton"]].append(e["codigo"])
    ry = (f.get("rayos") or {}).get("datos") or {}
    rayos_canton = Counter(p[2] for p in ry.get("puntos", []) if p[2])

    cantones = {}
    for c in terr.cantones:
        fila = {}
        if w and c in w["mm"]:
            idx = [w["pasos"].index(p) for p in pasos if p in w["pasos"]]
            fila["wrf"] = [w["mm"][c][i] for i in idx]
        m = ens.get(c)
        if m:
            fila["rango_pasos"] = []
            for p in pasos:
                vs = sorted(m[p]) if p in m else []
                fila["rango_pasos"].append([round(percentil(vs, 0.1), 1), round(percentil(vs, 0.5), 1),
                                            round(percentil(vs, 0.9), 1)] if vs else None)
            fila["rango"] = {}
            for nombre, n in VENTANAS.items():
                if all(p in m for p in pasos[:n]) and len(pasos) >= n:
                    tot = sorted(sum(m[p][k] for p in pasos[:n]) for k in range(len(m[pasos[0]])))
                    fila["rango"][nombre] = [round(percentil(tot, q), 1) for q in (0.1, 0.5, 0.9)]
        if "wrf" not in fila and fila.get("rango_pasos") and all(fila["rango_pasos"]):
            # Sin WRF del INAMHI (Galápagos): el pronóstico es la mediana de las 51 corridas del ECMWF.
            fila["wrf"] = [r[1] for r in fila["rango_pasos"]]
            fila["fuente"] = "ecmwf"
        if horas_sat:
            def suma(h):
                desde = fecha(ultima_sat) - timedelta(hours=h)
                return round(sum(sat[t].get(c, 0) for t in horas_sat if fecha(t) > desde), 1)
            fila["sat"] = {"1h": sat[ultima_sat].get(c, 0), "24h": suma(24), "72h": suma(72)}
        if por_canton_pl.get(c):
            fila["pluvios"] = por_canton_pl[c]
        if rayos_canton.get(c):
            fila["rayos"] = rayos_canton[c]
        cantones[c] = fila

    horas_24 = [t for t in horas_sat if ultima_sat and fecha(t) > fecha(ultima_sat) - timedelta(hours=24)]
    fuentes = {}
    for nombre, x in f.items():
        fuentes[nombre] = {"actualizado": x.get("actualizado"), "error": x.get("error")}
    if w:
        fuentes["wrf"]["corrida"] = w["corrida"]
    if ultima_sat:
        fuentes["satelite"]["ultima_hora"] = ultima_sat
        fuentes["satelite"]["horas_24"] = len(horas_24)
    if pl:
        d = f["pluviometros"]["datos"]
        fuentes["pluviometros"].update(transmitiendo=d["transmitiendo"], con_dato=d["con_dato"])
    if ry:
        fuentes["rayos"]["hora"] = ry.get("hora")
    return {
        "generado": iso(ahora),
        "pasos": pasos,
        "fuentes": fuentes,
        "cantones": cantones,
        "estaciones": [{k: e[k] for k in ("codigo", "nombre", "propietario", "lon", "lat", "canton", "ultimo",
                                          "horas_24", "mm_1h", "mm_24h", "mm_72h", "horaria", "horaria_hasta")}
                       for e in pl],
        "rayos": [p[:2] for p in ry.get("puntos", [])],
    }


def main():
    ahora = datetime.now(timezone.utc)
    terr = Territorio()
    anterior = memoria_anterior()
    viejas = anterior.get("fuentes", {})
    nuevas = {}

    def renovar(nombre, funcion, *args):
        print(f"{nombre}...", flush=True)
        t0 = time.time()
        try:
            datos = funcion(*args)
            nuevas[nombre] = {"datos": datos, "actualizado": iso(ahora), "error": None}
            print(f"  ok ({time.time() - t0:.0f} s)", flush=True)
        except Exception as e:
            vieja = dict(viejas.get(nombre) or {})
            vieja["error"] = f"{type(e).__name__}: {e}"[:300]
            nuevas[nombre] = vieja
            print(f"  FALLÓ ({time.time() - t0:.0f} s): {vieja['error']}", flush=True)

    renovar("wrf", wrf, ahora, terr)
    ens = viejas.get("ensamble") or {}
    if not ens.get("actualizado") or fecha(ens["actualizado"]) < ahora - ENSAMBLE_CADA + timedelta(minutes=20):
        renovar("ensamble", ensamble, terr)
    else:
        nuevas["ensamble"] = ens
    # Desde fines de oct 2026 el INAMHI solo entrega los datos de estaciones a su propio visor
    # ("origin_not_allowed"). Se activa con PLUVIOMETROS=1 cuando el INAMHI dé acceso.
    if os.environ.get("PLUVIOMETROS") == "1":
        renovar("pluviometros", pluviometros, ahora, terr)
    renovar("satelite", satelite, ahora, terr, ((viejas.get("satelite") or {}).get("datos") or {}).get("horas", {}))
    renovar("rayos", rayos, terr)

    estado = {"fuentes": nuevas, "generado": iso(ahora)}
    datos = armar(ahora, estado, terr)
    os.makedirs(PUBLICO, exist_ok=True)
    with open(os.path.join(PUBLICO, "datos.json"), "w") as fh:
        json.dump(datos, fh, ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(PUBLICO, "estado.json.gz"), "wb") as fh:
        fh.write(gzip.compress(json.dumps(estado, separators=(",", ":")).encode()))

    # Aviso por correo (la corrida queda en rojo) solo si una fuente lleva horas sin renovarse: un tropiezo de una
    # hora no molesta a nadie, la página muestra el último dato con su hora.
    limites = {"wrf": 6, "satelite": 6, "rayos": 6, "ensamble": 18}
    viejas_ya = [f"{n} (último dato bueno: {(nuevas.get(n) or {}).get('actualizado') or 'nunca'})"
                 for n, h in limites.items()
                 if not (nuevas.get(n) or {}).get("actualizado") or fecha(nuevas[n]["actualizado"]) < ahora - timedelta(hours=h)]
    with open(os.path.join(AQUI, "avisos.txt"), "w") as fh:
        fh.write("\n".join(viejas_ya))
    errores = {n: x["error"] for n, x in nuevas.items() if x.get("error")}
    with open(os.path.join(AQUI, "errores.json"), "w") as fh:
        json.dump(errores, fh, ensure_ascii=False, indent=1)
    sin_nada = [n for n in ("wrf", "satelite") if not (nuevas.get(n) or {}).get("datos")]
    if sin_nada:
        sys.exit(f"Sin datos de {', '.join(sin_nada)} (ni nuevos ni anteriores): no se publica.")
    print("Listo." + (f" Con errores: {errores}" if errores else ""))


if __name__ == "__main__":
    main()
