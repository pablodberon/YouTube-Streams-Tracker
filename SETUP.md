# YouTube Streams Tracker — Puesta en marcha

Base de Airtable creada: **YouTube Streams Tracker** (workspace ESPN Digital Research)
Interfaces:
- **Gestión de Canales y Videos** → páginas "Canales" y "Videos" para cargar Channel ID / Video ID a mano.
- **Estadísticas de Streams** → dashboard con números clave, gráficos de tendencia y tablas.

El script `yt_tracker.py` (en esta misma carpeta) hace el trabajo pesado: descubre videos nuevos
de cada canal activo, y mide vistas, likes, comentarios, concurrentes, duración y suscriptores,
guardando cada medición como un registro nuevo en la tabla Snapshots (así se arma la tendencia).

Para que funcione hacen falta 2 credenciales:

## 1. YouTube Data API v3 Key (gratis)

1. Entrá a https://console.cloud.google.com/
2. Creá un proyecto nuevo (o usá uno existente).
3. Menú ☰ → "APIs y servicios" → "Biblioteca".
4. Buscá **"YouTube Data API v3"** y hacé clic en **Habilitar**.
5. Menú ☰ → "APIs y servicios" → "Credenciales" → **Crear credenciales** → **Clave de API**.
6. Copiá la clave generada (empieza distinto cada vez, tipo `AIza...`).
7. Opcional pero recomendado: restringí la clave para que solo pueda usar "YouTube Data API v3".

Cuota gratuita: 10.000 unidades/día. Este script consume muy poco por corrida
(1 unidad por canal + 1 unidad cada 50 videos), así que corriendo cada 1 minuto
alcanza sin problema para varias decenas de canales.

## 2. Airtable Personal Access Token

1. Entrá a https://airtable.com/create/tokens
2. Creá un token nuevo, por ejemplo "YouTube Tracker Script".
3. Scopes necesarios: `data.records:read`, `data.records:write`, `schema.bases:read`.
4. Acceso: agregá la base **YouTube Streams Tracker**.
5. Copiá el token generado (empieza con `pat...`).

## 3. Completar el archivo de credenciales

Renombrá `yt_tracker.env.example` a `yt_tracker.env` (misma carpeta) y completá:

```
YOUTUBE_API_KEY=tu_api_key_de_youtube
AIRTABLE_TOKEN=tu_personal_access_token_de_airtable
```

## 4. Cargar canales y/o videos

En la interfaz **Gestión de Canales y Videos**:
- Página "Canales": agregá filas con el Channel ID (ej: `UCxxxxxxxxxxxxxxxxxxxxxx`) y tildá "Active".
- Página "Videos": si querés forzar un video puntual (no de un canal cargado), agregá una fila
  con el Video ID (el código de 11 caracteres que aparece después de `v=` en la URL de YouTube)
  y Source = "Manual".

## 5. Cómo se ejecuta el script 24/7

Para que esto funcione todo el tiempo sin depender de tu computadora ni de tener una
app abierta, lo dejamos corriendo en **GitHub Actions** (gratis, en la nube). Los pasos
para desplegarlo están en `GITHUB_SETUP.md`.

El archivo `yt_tracker.env` de este mismo paquete es solo para probar el script en tu
compu antes de subirlo (o si en algún momento preferís correrlo localmente); en GitHub
las mismas dos credenciales se cargan como *Secrets* del repositorio, nunca como archivo.

Lógica de frecuencia (ya programada dentro del script, no hace falta tocar nada):
- Video **en vivo** → se mide con la mayor frecuencia posible (~cada 1 minuto, ver detalle
  de cómo se logra esto en GitHub Actions en `GITHUB_SETUP.md`).
- Video **offline / finalizado** → se mide como máximo cada 5 minutos (para no gastar cuota
  ni llenar la tabla de datos redundantes).

Nota: la tarea programada local que se había creado en Cowork quedó **desactivada**, para
que no se dupliquen las mediciones una vez que GitHub Actions esté funcionando.
