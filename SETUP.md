> **Actualización:** `yt_tracker.py` cambió (ranking diario por canal, corte de
> medición al terminar un stream, y espera hasta 5 min antes de un scheduled).
> Si ya tenías el repo funcionando, solo hace falta reemplazar el archivo y
> volver a pushearlo:
> ```bash
> git add yt_tracker.py
> git commit -m "Ranking por canal + scheduling + corte en Ended"
> git push
> ```
> No hace falta tocar Secrets ni el workflow para esto.

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
