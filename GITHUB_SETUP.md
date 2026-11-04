> **Actualización 3 (importante): el cron nativo de GitHub dejó de ser confiable.**
> Detectamos que el disparador `schedule` de `tracker.yml` (el `*/5 12-23,0-4 * * *`)
> empezó a saltear corridas por horas enteras (vimos huecos de 6-14 horas sin
> ninguna ejecución, en vez de cada 5 minutos). Esto no es un bug nuestro: es un
> comportamiento conocido y documentado de GitHub Actions — el evento `schedule`
> es "best effort" y GitHub puede demorarlo o directamente saltearlo en períodos
> de carga, sobre todo con intervalos tan frecuentes como cada 5 minutos. No hay
> forma de arreglarlo desde el YAML.
>
> **Solución:** disparar el workflow desde afuera de GitHub, usando un servicio de
> cron externo que llama a la API de GitHub (`workflow_dispatch`) cada 5 minutos.
> Esa llamada SÍ es inmediata y confiable (no pasa por la cola interna de
> `schedule`). `tracker.yml` ya tiene `workflow_dispatch: {}` habilitado, así que
> no hace falta tocar el workflow — solo configurar el disparador externo:
>
> 1. **Crear un Personal Access Token (fine-grained) en GitHub**, solo para esto:
>    - Entrá a https://github.com/settings/personal-access-tokens/new
>    - "Resource owner": tu usuario. "Repository access": "Only select
>      repositories" → elegí `YouTube-Streams-Tracker`.
>    - En "Permissions" → "Repository permissions" → "Actions" → ponelo en
>      **Read and write**.
>    - Generá el token y copialo (empieza con `github_pat_...`). Es la única vez
>      que se muestra completo.
> 2. **Crear una cuenta gratis en https://cron-job.org** (no pide tarjeta).
> 3. **Crear un cronjob nuevo** con estos datos:
>    - URL: `https://api.github.com/repos/pablodberon/YouTube-Streams-Tracker/actions/workflows/tracker.yml/dispatches`
>    - Método: `POST`
>    - Request body (JSON): `{"ref":"main"}`
>    - Headers:
>      - `Authorization: Bearer TU_TOKEN_AQUI`
>      - `Accept: application/vnd.github+json`
>      - `Content-Type: application/json`
>    - Schedule: cada 5 minutos, solo entre las 12:00 y 04:59 UTC (= 09:00 a
>      01:59 ART) — cron-job.org permite restringir por horario del día.
> 4. Guardá el cronjob y mirá en la pestaña **Actions** del repo: deberían
>    empezar a aparecer corridas con el evento "workflow_dispatch" cada 5
>    minutos, sin huecos.
>
> El disparador `schedule` original lo dejamos como respaldo (no molesta, y el
> `concurrency` del workflow ya evita que se pisen dos corridas si llegan a
> coincidir), pero el que de ahora en más hace el trabajo real es el cronjob
> externo.

> **Actualización 2:** se agregó `yt_daily_report.py`, que corre todos los días
> a las 3am hora Argentina (06:00 UTC, ANTES de la purga de las 4am) y manda
> por mail el ranking de canales, el ranking de streams y el detalle por canal
> de la jornada 09:00→02:00 que acaba de terminar. Requiere 3 Secrets nuevos:
>
> 1. `GMAIL_ADDRESS` → la cuenta de Gmail que va a enviar el mail (puede ser
>    una cuenta nueva, tipo `reportes.streams@gmail.com`, no hace falta que
>    sea tu cuenta personal).
> 2. `GMAIL_APP_PASSWORD` → una "contraseña de aplicación" de esa cuenta:
>    - Entrá a https://myaccount.google.com/security
>    - Activá la verificación en 2 pasos si no la tenés activada (es requisito
>      para poder generar App Passwords).
>    - Buscá "Contraseñas de aplicaciones" (o entrá directo a
>      https://myaccount.google.com/apppasswords), creá una nueva, nombrala
>      "YouTube Tracker" y copiá el código de 16 caracteres que te da.
>    - **Importante:** si esa cuenta de Gmail es corporativa (de Disney) y no
>      te deja activar verificación en 2 pasos o generar App Passwords porque
>      la política de la organización lo bloquea, no vamos a poder usar este
>      método — avisame y armamos la alternativa 100% en Airtable con Resend
>      en vez de esto (ver más abajo).
> 3. `REPORT_RECIPIENTS` → la lista de mails que reciben el reporte, separados
>    por coma, por ejemplo: `pablo.d.beron@disney.com,otro@disney.com`. Para
>    agregar o sacar destinatarios más adelante, solo hay que editar este
>    Secret — no hace falta tocar código.
>
> Cargalos igual que los otros dos (Settings → Secrets and variables →
> Actions → New repository secret) y pusheá `yt_daily_report.py` y
> `.github/workflows/daily_report.yml`.
>
> **Si el Gmail corporativo no funciona:** la alternativa es armar este mismo
> reporte 100% dentro de Airtable (Automations: trigger "At a scheduled time"
> a las 3am ART + un script que arma los datos + `fetch()` a la API de Resend,
> que es un servicio de mail transaccional con plan gratuito). Avisame si
> llegás a ese punto y te lo armo — no requiere GitHub para nada en ese caso,
> pero sí crear una cuenta gratuita en Resend, porque los scripts de Airtable
> no pueden hablar SMTP (no pueden usar Gmail con contraseña de aplicación),
> solo pueden llamar APIs por HTTP.

> **Actualización:** se agregó un segundo workflow, `yt_daily_consolidate.py`,
> que corre una vez por día (4am hora Argentina) y hace 2 cosas: (1) guarda en
> cada Video un resumen permanente del stream (promedio de concurrentes, vistas
> finales, duración promedio estimada, suscriptores netos, y 2 gráficos como
> imagen) y (2) borra todos los registros de la tabla Snapshots para liberar
> espacio. El pico de concurrentes (Peak Concurrents/Peak Moment/Peak
> Screenshot) se calcula aparte, en tiempo real, dentro de `yt_tracker.py`.
>
> Si ya tenías el repo funcionando, agregá los archivos nuevos y pusheá:
> ```bash
> git add yt_tracker.py yt_daily_consolidate.py requirements.txt .github/workflows/daily_consolidate.yml
> git commit -m "Consolidacion diaria + purga de Snapshots + pico en tiempo real"
> git push
> ```
> No hace falta tocar Secrets (usa el mismo `AIRTABLE_TOKEN`).
>
> **Dos cosas para tener en cuenta con esto:**
> 1. **"Duración promedio de visualización" es una estimación**, no el dato
>    oficial de YouTube Analytics (ese solo está disponible vía YouTube
>    Analytics API, y únicamente para canales propios). Se calcula como
>    minutos-espectador totales (estimados a partir de las muestras de
>    concurrentes) dividido las vistas finales — es la mejor aproximación
>    posible con datos públicos, pero no es exacta.
> 2. **El "Peak Screenshot" es el thumbnail que YouTube va actualizando durante
>    la transmisión**, capturado en el momento en que se detecta un nuevo pico
>    — no es un frame exacto al segundo del pico, sino la imagen más cercana
>    disponible públicamente (se actualiza aprox. cada 1 minuto mientras está
>    en vivo).
> 3. **La purga diaria puede tardar horas si el volumen es grande** (Airtable
>    solo permite borrar 10 registros por llamada). Con ~450.000 registros/día
>    calculá algo así como 1-3 horas solo para el borrado. Por eso se programó
>    a las 4am, con margen hasta que el tracker retoma a las 9am. Si alguna vez
>    ves que no llega a terminar a tiempo, avisame y ajustamos el horario o el
>    volumen de canales monitoreados.

# Desplegar el tracker en GitHub Actions (sin depender de tu máquina)

Con esto el tracker corre solo, en los servidores de GitHub, sin que tengas que tener
ninguna app abierta. La tarea programada local de Cowork ya la dejé **desactivada**
para que no se dupliquen las mediciones en Airtable.

Ajustamos el alcance: en vez de 24/7, el workflow ahora solo corre entre **09:00 y
~02:00 hora Argentina** (ventana `12-23,0-4` en UTC dentro de `tracker.yml`). Si tu
horario real es distinto, avisame y te ajusto los números de hora en el cron.

## Se agotó la cuota gratis: qué pasó y qué hacer

Tu repo es **privado, bajo tu cuenta personal** de GitHub. Los repos privados solo
traen minutos de Actions limitados por mes:

| Plan | Minutos incluidos/mes | Alcanza para este tracker corriendo 16 hs/día? |
|---|---|---|
| Free | 2.000 | No, ni recortando la frecuencia a cada 5 min con el loop de 1 min |
| Pro (USD 4/mes) | 3.000 | Tampoco alcanza con el loop de 1 min. Solo alcanzaría si medimos cada ~15 minutos plano (sin distinguir vivo/offline) |

Con el diseño actual (loop de 5 corridas cada 5 minutos, ~1 muestra por minuto en
vivo) el consumo estimado ronda **~29.000 minutos/mes**, incluso limitado a la
ventana de 16 horas. Ni Free ni Pro alcanzan para sostener esa precisión en un repo
privado. Por eso, antes de pagar, conviene resolver esto con el cambio de abajo,
que no cuesta nada y no te hace perder precisión:

## Por qué el repo conviene que sea público

Elegiste el modo "loop de 1 minuto" (5 corridas del script por cada disparo de 5
minutos), que consume muchos minutos de Actions porque el job queda corriendo casi
sin parar, las 24 horas. Los repos **públicos** tienen minutos de GitHub Actions
**ilimitados y gratis**. Los repos privados solo traen 2.000 minutos gratis por mes
(free) o 3.000 (plan Pro, USD 4/mes) — con este esquema 24/7 se te acaban en 1-2 días
y a partir de ahí GitHub cobra por minuto adicional (podría rondar USD 100-150/mes).

**Recomendación:** repo público. El código y el workflow quedan visibles, pero:
- Las credenciales (API keys) NUNCA quedan en el código: van como *GitHub Secrets*,
  que no son visibles ni siquiera para colaboradores del repo.
- El único dato "de negocio" visible sería el ID de la base de Airtable, que no es
  sensible por sí solo (sin el token, nadie puede leer ni escribir nada).

Como ya tenés el repo creado (privado, cuenta personal), el cambio es mínimo: no hay
que recrear nada, solo cambiarle la visibilidad y volver a subir el `tracker.yml`
actualizado (con la ventana horaria 09:00–02:00 ART).

## 1. Cambiar el repo a público

1. Entrá al repo en GitHub → **Settings** → **General**.
2. Bajá hasta **Danger Zone** → **Change visibility** → **Change to public**.
3. Confirmá escribiendo el nombre del repo. Esto no borra nada: historial, Secrets
   y Actions quedan intactos. Lo único que cambia es que el código pasa a ser
   visible públicamente (las credenciales, al ser Secrets, siguen ocultas).

## 2. Actualizar el workflow con la ventana horaria

Reemplazá tu `.github/workflows/tracker.yml` por la versión nueva que te compartí
(agrega la ventana `12-23,0-4` en UTC = 09:00–02:00 hora Argentina) y pusheá:

```bash
git add .github/workflows/tracker.yml
git commit -m "Limitar tracker a ventana horaria 09-02 ART"
git push
```

## 3. Confirmar los Secrets (si todavía no los cargaste)

1. En el repo en GitHub: Settings → Secrets and variables → Actions → **New repository secret**
2. Creá dos secrets:
   - `YOUTUBE_API_KEY` → tu API key de YouTube Data API v3
   - `AIRTABLE_TOKEN` → tu Personal Access Token de Airtable
   (Ver `SETUP.md` si todavía no generaste estas credenciales.)

## 4. Probar

1. Andá a la pestaña **Actions** del repo.
2. Buscá el workflow "YouTube Streams Tracker" y usá **Run workflow** para probarlo
   manualmente una vez y confirmar que no tira errores (revisá los logs).
3. Si sale bien, no hace falta nada más: entre las 09:00 y ~02:00 (hora Argentina)
   va a correr solo cada 5 minutos, con 5 muestras internas espaciadas ~1 minuto,
   sin gastar un centavo ni consumir cuota, porque el repo ahora es público.

## Cosas a tener en cuenta

- **Repos inactivos:** si el repo no recibe ningún commit ni actividad durante 60
  días, GitHub desactiva automáticamente los workflows programados. Un commit
  cada tanto (o simplemente seguir usando el repo) lo evita.
- **Timing aproximado:** GitHub no garantiza el minuto exacto del disparo, sobre
  todo en horarios pico. Vas a ver una cadencia de "más o menos" cada 5 minutos,
  con ráfagas internas de 5 muestras espaciadas ~60 segundos.
- **Solapamientos:** si una corrida se llega a atrasar y se cruza con el siguiente
  disparo, el workflow ya está configurado (`concurrency`) para que la nueva
  corrida espere en cola en vez de pisarse con la anterior.
- **Costo:** mientras el repo sea público, esto no te cuesta nada, sin importar
  cuánto tiempo lo dejes corriendo dentro de la ventana horaria configurada.
- **Cambiar el horario:** si en algún momento cambia el rango 09:00–02:00, avisame
  y actualizo la línea `cron` de `tracker.yml` (está en hora UTC, hay que restar
  3 horas respecto a la hora Argentina).
