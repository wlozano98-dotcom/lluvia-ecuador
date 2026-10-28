"""Galápagos para el mapa y los promedios (se corre una vez; lo continental lo preparó Monitoreo Caro).

Lee territorio/galapagos.geojson (geoBoundaries ECU ADM2, CC BY 3.0 IGO: Isabela, San Cristóbal, Santa Cruz) y escribe:
- territorio/galapagos.json: celdas de 0,02° con su cantón (mismo formato que rejilla.json).
- public/mapa.json: agrega los tres cantones en un recuadro en el mar, a la izquierda del continente (otra escala).
"""

import json
import os

AQUI = os.path.dirname(os.path.abspath(__file__))
OESTE, NORTE, ESTE, SUR, PASO = -92.1, 0.75, -89.1, -1.5, 0.02
RECUADRO = {"x": -132, "y": 8, "escala": 40}  # px por grado (el continente usa 100), en el mar a la izquierda
IDS = {"Isabela": ("901", "Isabela"), "San Cristobal": ("902", "San Cristóbal"), "Santa Cruz": ("903", "Santa Cruz")}


def poligonos(geom):
    return geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]


def dentro(x, y, anillo):
    c = False
    for (x1, y1), (x2, y2) in zip(anillo, anillo[1:] + anillo[:1]):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            c = not c
    return c


def main():
    feats = json.load(open(os.path.join(AQUI, "territorio", "galapagos.geojson")))["features"]
    ancho, alto = round((ESTE - OESTE) / PASO), round((NORTE - SUR) / PASO)
    ids = [0] * (ancho * alto)
    for f in feats:
        cid = int(IDS[f["properties"]["shapeName"]][0])
        for pol in poligonos(f["geometry"]):
            xs, ys = [p[0] for p in pol[0]], [p[1] for p in pol[0]]
            for i in range(max(0, int((NORTE - max(ys)) / PASO)), min(alto, int((NORTE - min(ys)) / PASO) + 1)):
                lat = NORTE - (i + 0.5) * PASO
                for j in range(max(0, int((min(xs) - OESTE) / PASO)), min(ancho, int((max(xs) - OESTE) / PASO) + 1)):
                    lon = OESTE + (j + 0.5) * PASO
                    if dentro(lon, lat, pol[0]) and not any(dentro(lon, lat, h) for h in pol[1:]):
                        ids[i * ancho + j] = cid
    tramos, previo, n = [], None, 0
    for v in ids:
        if v == previo:
            n += 1
        else:
            if previo is not None:
                tramos += [previo, n]
            previo, n = v, 1
    tramos += [previo, n]
    json.dump({"oeste": OESTE, "norte": NORTE, "paso": PASO, "ancho": ancho, "alto": alto, "tramos": tramos},
              open(os.path.join(AQUI, "territorio", "galapagos.json"), "w"), separators=(",", ":"))

    R = RECUADRO
    px = lambda lon, lat: (R["x"] + (lon - OESTE) * R["escala"], R["y"] + (NORTE - lat) * R["escala"])
    mapa = json.load(open(os.path.join(AQUI, "public", "mapa.json")))
    mapa["cantones"] = [c for c in mapa["cantones"] if str(c["id"]) not in {v[0] for v in IDS.values()}]
    for f in feats:
        cid, nombre = IDS[f["properties"]["shapeName"]]
        partes, sx, sy, k = [], 0, 0, 0
        for pol in poligonos(f["geometry"]):
            for anillo in pol:
                pts = [px(x, y) for x, y in anillo]
                if len(pts) < 4:
                    continue
                partes.append("M" + "L".join(f"{x:.1f} {y:.1f}" for x, y in pts) + "Z")
                sx += sum(p[0] for p in pts); sy += sum(p[1] for p in pts); k += len(pts)
        mapa["cantones"].append({"id": cid, "canton": nombre, "provincia": "Galápagos", "d": "".join(partes),
                                 "cx": round(sx / k, 1), "cy": round(sy / k, 1)})
    x0, y0 = px(OESTE, NORTE)
    x1, y1 = px(ESTE, SUR)
    mapa["galapagos"] = {"oeste": OESTE, "norte": NORTE, "escala": R["escala"], "x": R["x"], "y": R["y"],
                         "caja": [round(x0, 1), round(y0, 1), round(x1 - x0, 1), round(y1 - y0, 1)]}
    # El recuadro queda a la izquierda del continente: se ensancha la vista hacia ese lado.
    ancho_cont = round((-75.0 + 81.1) * 100)
    mapa["vista"] = [round(x0) - 6, 0, ancho_cont - round(x0) + 6, mapa["vista"][3]]
    json.dump(mapa, open(os.path.join(AQUI, "public", "mapa.json"), "w"), ensure_ascii=False, separators=(",", ":"))
    print("celdas por cantón:", {v: ids.count(int(v)) for v, _ in IDS.values()}, "recuadro:", mapa["galapagos"]["caja"])


if __name__ == "__main__":
    main()
