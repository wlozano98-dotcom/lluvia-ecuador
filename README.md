# Monitor Nacional de Lluvias

Página pública que responde, para cada cantón del Ecuador (Galápagos incluido): **¿cuánto va a llover?** (12, 24 y 48 h), **¿cuánto llovió?**
(24 y 72 h) y **¿dónde llueve ahora?** Se actualiza sola cada hora y no necesita servidor ni base de datos.

## Cómo funciona

```
GitHub Actions (cada hora, a los :25)
  └─ generar.py ── baja las fuentes públicas ── cruza con los cantones ── public/datos.json
                   (si una fuente falla, usa su último dato bueno, que lee de la página publicada)
  └─ publica public/ en GitHub Pages  (y en Cloudflare Pages si están sus claves)
La página (public/index.html) solo lee datos.json y mapa.json.
```

| Qué | Fuente | Frecuencia |
|---|---|---|
| Va a llover | WRF del INAMHI, pasos de 3 h (GeoServer de inamhi.geoglows.org) | cada corrida diaria |
| Rango | 51 corridas del ECMWF vía Open-Meteo (gratis, uso no comercial) | cada 6 h |
| Llovió / llueve ahora | Satélite PERSIANN-PDIR por hora (mismo GeoServer) | cada hora |
| Rayos | GOES-19 GLM (mismo GeoServer) | cada hora (últimos ~10 min) |
| Pluviómetros | INAMHI api_visor — **apagado**: el INAMHI restringió esos datos a su visor | `PLUVIOMETROS=1` |

Detalle importante del WRF: el GeoServer guarda varias corridas; hay que pedir la última con `INITD`, si no el
WCS entrega una vieja.

## Si algo falla

- Una fuente que falla una hora: la página muestra su último dato con la hora (chip amarillo si está viejo).
- Una fuente sin renovarse por 6 h (18 h el ECMWF): la corrida queda en rojo y GitHub manda correo.
- Si no se puede leer la memoria publicada, **no se publica** (para no reemplazar una página buena por una vacía).
- GitHub apaga los horarios de repositorios sin movimiento en 60 días: el robot hace un commit chico cada lunes.

## Correr a mano

```
python3 generar.py                 # escribe public/datos.json
cd public && python3 -m http.server 8777   # y abrir http://127.0.0.1:8777
```

## Incrustar en otra página (Secretaría de Riesgos, etc.)

```html
<iframe id="lluvia" src="https://DIRECCION-DE-LA-PAGINA/" style="width:100%;border:0;height:1900px"
        title="Lluvia por cantón" loading="lazy"></iframe>
<script>
  addEventListener("message", (e) => {
    if (e.data && e.data.lluviaEcuadorAlto) document.getElementById("lluvia").style.height = e.data.lluviaEcuadorAlto + "px";
  });
</script>
```

Se puede abrir filtrada por provincia (`?provincia=Guayas`) o directo en un cantón (`?canton=14`).

## Logo del Gobierno

El logo no está en el repositorio. El robot lo arma al publicar desde el secreto `LOGO_GOBIERNO_B64` (el PNG en
base64, máximo 48 KB; con 300 px de ancho alcanza). Sin el secreto, la página sale sin logo. Para cargarlo:
`base64 -i gobierno-ecuador.png | pbcopy` y pegarlo en Settings → Secrets and variables → Actions → New secret.

## Pasar a Cloudflare Pages (cuenta propia)

1. En Cloudflare: crear un proyecto de Pages llamado `lluvia-ecuador` (Workers & Pages → Create → Pages → subir
   cualquier archivo) y un token de API con permiso "Cloudflare Pages: Edit".
2. En GitHub (Settings → Secrets and variables → Actions): secretos `CLOUDFLARE_API_TOKEN` y
   `CLOUDFLARE_ACCOUNT_ID`; variable `CF_PROYECTO` si el proyecto tiene otro nombre.
3. Cuando la dirección de Cloudflare funcione, cambiar la variable `URL_PUBLICADA` a esa dirección.

## Activar los pluviómetros

Cuando el INAMHI dé acceso, poner la variable `PLUVIOMETROS` = `1`. Cada cantón con estación mostrará la lluvia
medida (24 h, última hora) además de la del satélite.
