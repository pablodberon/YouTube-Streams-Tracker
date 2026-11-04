#!/usr/bin/env python3
"""
YouTube Streams Tracker -> Airtable

Recolecta, para cada canal (Channel ID) activo cargado en Airtable, los videos/streams
mas recientes (incluyendo lives en curso), y para cada video (manual o auto-descubierto)
mide: vistas, likes, comentarios, concurrentes (solo en vivo), duracion y suscriptores
del canal en ese momento. Guarda cada medicion como un registro nuevo en la tabla
"Snapshots" de Airtable (nunca pisa datos anteriores -> permite ver tendencia).

Frecuencia recomendada de ejecucion: cada 1 minuto (via tarea programada o un loop
dentro de un job de GitHub Actions).
El propio script decide cuanto medir cada video, para ahorrar cuota de YouTube:
  - SCHEDULED (programado, todavia no arranco) -> no se mide (ni se le pide detalle
    a la API) hasta 5 minutos antes de la hora de inicio programada.
  - LIVE (en vivo)                             -> se mide en cada corrida (~1 min)
  - ENDED (recien termino)                     -> se mide una ultima vez (para
    guardar el cierre real: Actual End, vistas/likes finales) y a partir de ahi
    nunca mas se vuelve a consultar ese video.
  - OFFLINE (video comun, no-live)             -> se mide como maximo cada 5 min
Ademas de la tabla Snapshots (por video), el script escribe en "Channel Snapshots"
la suma de concurrentes de TODOS los lives simultaneos de cada canal, para poder
armar un ranking diario por canal en la interfaz de Airtable.
El throttling NO depende de ningun archivo local: consulta a la propia tabla
Snapshots de Airtable cual fue la ultima medicion de cada video en los ultimos
minutos, y el propio campo "Status"/"Scheduled Start" del registro de Video en
Airtable para decidir si vale la pena pedirle detalle a la API. Por eso el script
es "stateless" y funciona igual de bien corriendo en un runner efimero (GitHub
Actions) que en una maquina que sigue prendida.

Requisitos:
  pip install requests

Variables de entorno requeridas:
  YOUTUBE_API_KEY   -> API key de YouTube Data API v3
  AIRTABLE_TOKEN    -> Personal Access Token de Airtable con permisos
                       data.records:read, data.records:write, schema.bases:read
                       sobre la base "YouTube Streams Tracker"

IDs fijos de la base (ya creada):
  BASE_ID = appFvqj21Yy73Bjcn
"""

import os
import re
import sys
import datetime

import requests

# ---------------------------------------------------------------------------
# Configuracion
# ---------------------------------------------------------------------------

BASE_ID = "appFvqj21Yy73Bjcn"

TBL_CHANNELS = "tblnRqTAN0fnm9ebE"
TBL_VIDEOS = "tbl8JGhar3VxOinIB"
TBL_SNAPSHOTS = "tblMGC70UgtS7NBWG"
TBL_CHANNEL_SNAPSHOTS = "tblaJvXiDhGDODoX6"

YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "").strip()
AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN", "").strip()

OFFLINE_MIN_INTERVAL_SECONDS = 5 * 60     # no medir offline mas seguido que esto
SCHEDULED_LOOKAHEAD_SECONDS = 5 * 60      # empezar a medir un scheduled recien 5 min antes
ARGENTINA_UTC_OFFSET_HOURS = -3           # para calcular la fecha "local" del Channel Snapshot
MAX_UPLOADS_PER_CHANNEL_CHECK = 50        # cuantos items recientes de la playlist de uploads mirar
                                           # por pagina (50 es el maximo que permite la API y no
                                           # cuesta cuota extra pedir 50 en vez de menos)
DEEP_SCAN_INTERVAL_SECONDS = 60 * 60      # cada cuanto se hace, por canal, un escaneo mas profundo
                                           # (varias paginas) de la playlist de uploads. Canales muy
                                           # activos (ej. ESPN Fans) pueden "enterrar" un stream
                                           # programado bajo decenas de subidas normales en pocas
                                           # horas, sobre todo si hubo un corte en el monitoreo; con
                                           # solo 1 pagina (50 items) ese stream se perderia para
                                           # siempre. Este escaneo profundo es la red de seguridad.
DEEP_SCAN_MAX_PAGES = 4                   # 4 paginas x 50 = hasta 200 items hacia atras, 4 unidades
                                           # de cuota por canal en vez de 1 (solo cuando toca)

AIRTABLE_API = "https://api.airtable.com/v0"
YOUTUBE_API = "https://www.googleapis.com/youtube/v3"

VIDEO_ID_RE = re.compile(r"(?:v=|youtu\.be/|/live/)([A-Za-z0-9_-]{11})")


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


if not YOUTUBE_API_KEY:
    die("Falta la variable de entorno YOUTUBE_API_KEY")
if not AIRTABLE_TOKEN:
    die("Falta la variable de entorno AIRTABLE_TOKEN")


# ---------------------------------------------------------------------------
# Helpers Airtable
# ---------------------------------------------------------------------------

def airtable_headers():
    return {
        "Authorization": f"Bearer {AIRTABLE_TOKEN}",
        "Content-Type": "application/json",
    }


def airtable_list_all(table_id, filter_formula=None):
    records = []
    params = {"pageSize": 100}
    if filter_formula:
        params["filterByFormula"] = filter_formula
    offset = None
    while True:
        if offset:
            params["offset"] = offset
        resp = requests.get(
            f"{AIRTABLE_API}/{BASE_ID}/{table_id}",
            headers=airtable_headers(),
            params=params,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        records.extend(data.get("records", []))
        offset = data.get("offset")
        if not offset:
            break
    return records


def airtable_create(table_id, records):
    created = []
    for i in range(0, len(records), 10):
        chunk = records[i : i + 10]
        resp = requests.post(
            f"{AIRTABLE_API}/{BASE_ID}/{table_id}",
            headers=airtable_headers(),
            json={"records": chunk, "typecast": True},
            timeout=30,
        )
        resp.raise_for_status()
        created.extend(resp.json().get("records", []))
    return created


def airtable_update(table_id, records):
    updated = []
    for i in range(0, len(records), 10):
        chunk = records[i : i + 10]
        resp = requests.patch(
            f"{AIRTABLE_API}/{BASE_ID}/{table_id}",
            headers=airtable_headers(),
            json={"records": chunk, "typecast": True},
            timeout=30,
        )
        resp.raise_for_status()
        updated.extend(resp.json().get("records", []))
    return updated


# ---------------------------------------------------------------------------
# Helpers YouTube
# ---------------------------------------------------------------------------

def yt_get(path, **params):
    params["key"] = YOUTUBE_API_KEY
    resp = requests.get(f"{YOUTUBE_API}/{path}", params=params, timeout=30)
    if resp.status_code != 200:
        print(f"YouTube API error ({path}): {resp.status_code} {resp.text}", file=sys.stderr)
        resp.raise_for_status()
    return resp.json()


def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def extract_video_id(text):
    """Extrae el codigo de 11 caracteres despues de v= (o youtu.be/, /live/) de una URL."""
    if not text:
        return None
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", text.strip()):
        return text.strip()
    m = VIDEO_ID_RE.search(text)
    return m.group(1) if m else None


def get_channels_info(channel_ids):
    """Devuelve dict channelId -> {uploads_playlist_id, subscriber_count, title}"""
    info = {}
    for batch in chunked(channel_ids, 50):
        data = yt_get(
            "channels",
            part="contentDetails,statistics,snippet",
            id=",".join(batch),
        )
        for item in data.get("items", []):
            info[item["id"]] = {
                "uploads_playlist_id": item["contentDetails"]["relatedPlaylists"]["uploads"],
                "subscriber_count": int(item["statistics"].get("subscriberCount", 0)),
                "title": item["snippet"]["title"],
            }
    return info


def get_recent_video_ids_for_playlist(playlist_id, max_items=MAX_UPLOADS_PER_CHANNEL_CHECK, max_pages=1):
    """Trae los video IDs mas recientes de la playlist de uploads de un canal.
    Por defecto mira 1 sola pagina (1 unidad de cuota). Con max_pages > 1 pagina
    hacia atras (1 unidad de cuota por pagina adicional) para el escaneo profundo
    periodico, que evita perder streams enterrados bajo muchas subidas normales."""
    video_ids = []
    page_token = None
    pages_fetched = 0
    while pages_fetched < max_pages:
        kwargs = {
            "part": "contentDetails",
            "playlistId": playlist_id,
            "maxResults": min(max_items, 50),
        }
        if page_token:
            kwargs["pageToken"] = page_token
        data = yt_get("playlistItems", **kwargs)
        for item in data.get("items", []):
            vid = item["contentDetails"].get("videoId")
            if vid:
                video_ids.append(vid)
        pages_fetched += 1
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return video_ids


def search_live_video_ids_for_channel(channel_id, event_type):
    """Busca directamente streams (programados o en vivo) de un canal via
    search.list con eventType, que es el equivalente oficial y exacto a lo que
    YouTube muestra en la pestaña /streams del canal (a diferencia de la
    playlist de uploads, que mezcla todo el contenido y puede "enterrar" un
    stream bajo subidas normales). Esto SI es confiable al 100%, pero cuesta
    100 unidades de cuota por llamada (vs. 1 de playlistItems/videos), asi que
    solo se usa como reconciliacion 1 vez por dia por canal, nunca en cada
    corrida."""
    video_ids = []
    try:
        data = yt_get(
            "search",
            part="id",
            channelId=channel_id,
            type="video",
            eventType=event_type,
            order="date",
            maxResults=50,
        )
    except requests.HTTPError:
        return video_ids
    for item in data.get("items", []):
        vid = item.get("id", {}).get("videoId")
        if vid:
            video_ids.append(vid)
    return video_ids


def get_videos_details(video_ids):
    """Devuelve dict videoId -> {status, concurrent, views, likes, comments, duration_min,
    title, published_at, channel_id, scheduled_start, actual_start, actual_end}"""
    details = {}
    for batch in chunked(video_ids, 50):
        data = yt_get(
            "videos",
            part="snippet,statistics,liveStreamingDetails,contentDetails",
            id=",".join(batch),
        )
        for item in data.get("items", []):
            vid = item["id"]
            snippet = item.get("snippet", {})
            stats = item.get("statistics", {})
            live = item.get("liveStreamingDetails", {})

            broadcast = snippet.get("liveBroadcastContent", "none")  # live | upcoming | none
            scheduled_start = live.get("scheduledStartTime")
            actual_start = live.get("actualStartTime")
            actual_end = live.get("actualEndTime")

            if actual_start and not actual_end:
                status = "Live"
            elif actual_end:
                status = "Ended"
            elif broadcast == "upcoming" or scheduled_start:
                status = "Scheduled"
            else:
                status = "Offline"

            concurrent = live.get("concurrentViewers")
            concurrent = int(concurrent) if concurrent is not None else None

            duration_min = None
            if actual_start and actual_end:
                try:
                    fmt = "%Y-%m-%dT%H:%M:%SZ"
                    t0 = datetime.datetime.strptime(actual_start, fmt)
                    t1 = datetime.datetime.strptime(actual_end, fmt)
                    duration_min = round((t1 - t0).total_seconds() / 60.0, 1)
                except Exception:
                    duration_min = None
            elif actual_start:
                try:
                    fmt = "%Y-%m-%dT%H:%M:%SZ"
                    t0 = datetime.datetime.strptime(actual_start, fmt)
                    now = datetime.datetime.utcnow()
                    duration_min = round((now - t0).total_seconds() / 60.0, 1)
                except Exception:
                    duration_min = None

            thumbnails = snippet.get("thumbnails", {}) or {}
            thumbnail_url = None
            for quality in ("maxres", "standard", "high", "medium", "default"):
                if quality in thumbnails:
                    thumbnail_url = thumbnails[quality].get("url")
                    break

            details[vid] = {
                "status": status,
                "concurrent": concurrent,
                "views": int(stats.get("viewCount", 0)) if "viewCount" in stats else None,
                "likes": int(stats.get("likeCount", 0)) if "likeCount" in stats else None,
                "comments": int(stats.get("commentCount", 0)) if "commentCount" in stats else None,
                "duration_min": duration_min,
                "title": snippet.get("title"),
                "published_at": snippet.get("publishedAt"),
                "channel_id": snippet.get("channelId"),
                "scheduled_start": scheduled_start,
                "actual_start": actual_start,
                "actual_end": actual_end,
                "thumbnail_url": thumbnail_url,
            }
    return details


# ---------------------------------------------------------------------------
# Throttling de mediciones offline (sin estado local: se consulta a Airtable
# cual fue la ultima medicion de cada video en los ultimos N minutos. Esto
# hace que el script sea "stateless" y funcione igual corriendo en un runner
# efimero como GitHub Actions, sin depender de ningun archivo persistente).
# ---------------------------------------------------------------------------

def _parse_airtable_timestamp(ts):
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.datetime.strptime(ts, fmt).replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            continue
    return None


def get_last_snapshot_epoch_by_video_record_id(lookback_minutes=15):
    """Devuelve dict {videoRecordId: epoch_seconds_de_la_ultima_medicion}
    mirando solo los snapshots de los ultimos `lookback_minutes` minutos
    (para no tener que traer toda la tabla, que puede crecer mucho)."""
    formula = f"IS_AFTER({{Timestamp}}, DATEADD(NOW(), -{lookback_minutes}, 'minutes'))"
    records = airtable_list_all(TBL_SNAPSHOTS, filter_formula=formula)
    last_by_video = {}
    for rec in records:
        fields = rec["fields"]
        links = fields.get("Video") or []
        ts = fields.get("Timestamp")
        if not links or not ts:
            continue
        dt = _parse_airtable_timestamp(ts)
        if dt is None:
            continue
        epoch = dt.timestamp()
        video_rid = links[0]
        if video_rid not in last_by_video or epoch > last_by_video[video_rid]:
            last_by_video[video_rid] = epoch
    return last_by_video


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    now = datetime.datetime.utcnow()
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    today_local_date_str = (now + datetime.timedelta(hours=ARGENTINA_UTC_OFFSET_HOURS)).strftime("%Y-%m-%d")
    now_epoch = now.replace(tzinfo=datetime.timezone.utc).timestamp()

    # 1) Canales activos
    channel_records = airtable_list_all(TBL_CHANNELS, filter_formula="{Active}=1")
    channel_ids = [
        r["fields"].get("Channel ID", "").strip()
        for r in channel_records
        if r["fields"].get("Channel ID")
    ]
    channel_record_by_id = {
        r["fields"].get("Channel ID", "").strip(): r for r in channel_records
    }

    channels_info = get_channels_info(channel_ids) if channel_ids else {}

    # 2) Descubrir videos nuevos desde la playlist de uploads de cada canal
    existing_video_records = airtable_list_all(TBL_VIDEOS)
    existing_video_ids = {
        r["fields"].get("Video ID", "").strip(): r
        for r in existing_video_records
        if r["fields"].get("Video ID")
    }

    new_video_ids_to_create = []
    queued_video_ids_this_run = set()
    channels_deep_scanned_this_run = set()
    channels_reconciled_today = set()
    for cid in channel_ids:
        info = channels_info.get(cid)
        if not info:
            continue
        # Canales muy activos (ej. ESPN Fans) pueden subir decenas de videos
        # normales en pocas horas, enterrando un stream programado mas alla de
        # la primera pagina de la playlist de uploads, sobre todo si hubo un
        # corte en el monitoreo. Cada DEEP_SCAN_INTERVAL_SECONDS le damos a
        # ese canal una mirada mas profunda (varias paginas) para no perder
        # nada; el resto de las corridas usan 1 sola pagina (barato).
        channel_record = channel_record_by_id.get(cid)
        last_deep_scan_raw = channel_record["fields"].get("Last Deep Scan") if channel_record else None
        last_deep_scan_dt = _parse_airtable_timestamp(last_deep_scan_raw) if last_deep_scan_raw else None
        needs_deep_scan = (
            last_deep_scan_dt is None
            or (now_epoch - last_deep_scan_dt.timestamp()) >= DEEP_SCAN_INTERVAL_SECONDS
        )
        max_pages = DEEP_SCAN_MAX_PAGES if needs_deep_scan else 1
        try:
            recent_ids = get_recent_video_ids_for_playlist(info["uploads_playlist_id"], max_pages=max_pages)
        except requests.HTTPError:
            continue
        if needs_deep_scan:
            channels_deep_scanned_this_run.add(cid)

        # Red de seguridad final, 1 vez por dia por canal: reconciliar contra
        # lo que search.list (eventType upcoming/live) devuelve, que es el
        # equivalente oficial exacto a la pestaña /streams del canal. Mas caro
        # (100 unidades por llamada) que mirar la playlist de uploads, por eso
        # no se hace en cada corrida, pero garantiza que ningun stream quede
        # sin detectar sin importar cuanto contenido normal suba el canal.
        last_reconcile = channel_record["fields"].get("Last Reconcile Date") if channel_record else None
        if last_reconcile != today_local_date_str:
            for event_type in ("upcoming", "live"):
                recent_ids.extend(search_live_video_ids_for_channel(cid, event_type))
            channels_reconciled_today.add(cid)

        for vid in recent_ids:
            if vid not in existing_video_ids and vid not in queued_video_ids_this_run:
                queued_video_ids_this_run.add(vid)
                new_video_ids_to_create.append((vid, cid))

    if new_video_ids_to_create:
        details_for_new = get_videos_details([v for v, _ in new_video_ids_to_create])
        records_to_create = []
        for vid, cid in new_video_ids_to_create:
            d = details_for_new.get(vid, {})
            status = d.get("status", "Offline")
            # No nos interesa el contenido on demand: si el item descubierto en
            # la playlist de uploads no tiene datos de liveStreamingDetails
            # (status "Offline"), no se crea el registro en Videos. Si mas
            # adelante YouTube lo marca como Scheduled/Live/Ended, se va a
            # volver a descubrir en una corrida futura (todavia no existe en
            # existing_video_ids) y ahi si se crea. Esto evita que la tabla
            # Videos se llene de videos on-demand que consumirian cuota de
            # YouTube en cada corrida para siempre.
            if status == "Offline":
                continue
            channel_record = channel_record_by_id.get(cid)
            fields = {
                "Video ID": vid,
                "Title": d.get("title", ""),
                "URL": f"https://www.youtube.com/watch?v={vid}",
                "Source": "Auto (Channel)",
                "Status": status,
            }
            if d.get("published_at"):
                fields["Published At"] = d["published_at"]
            if d.get("scheduled_start"):
                fields["Scheduled Start"] = d["scheduled_start"]
            if d.get("actual_start"):
                fields["Actual Start"] = d["actual_start"]
            if d.get("actual_end"):
                fields["Actual End"] = d["actual_end"]
            if channel_record:
                fields["Channel"] = [channel_record["id"]]
            records_to_create.append({"fields": fields})
        if records_to_create:
            created = airtable_create(TBL_VIDEOS, records_to_create)
            for rec in created:
                vid = rec["fields"].get("Video ID")
                if vid:
                    existing_video_ids[vid] = rec

    # actualizar Last Checked / Subscribers de canales, y fijar el checkpoint
    # de "suscriptores al inicio de la jornada" la PRIMERA vez que se corre en
    # el dia (hora Argentina). Ese checkpoint no se vuelve a tocar hasta el dia
    # siguiente, asi el reporte de las 3am puede comparar siempre "inicio de
    # jornada vs. ahora" de forma consistente, sin importar si justo hay un
    # stream en vivo a esa hora o no.
    channel_updates = []
    for cid, rec in channel_record_by_id.items():
        info = channels_info.get(cid)
        if not info:
            continue
        fields_update = {
            "Subscribers (last)": info["subscriber_count"],
            "Last Checked": now_iso,
        }
        if rec["fields"].get("Jornada Start Date") != today_local_date_str:
            fields_update["Subscribers at Jornada Start"] = info["subscriber_count"]
            fields_update["Jornada Start Date"] = today_local_date_str
        if cid in channels_deep_scanned_this_run:
            fields_update["Last Deep Scan"] = now_iso
        if cid in channels_reconciled_today:
            fields_update["Last Reconcile Date"] = today_local_date_str
        channel_updates.append({"id": rec["id"], "fields": fields_update})
    if channel_updates:
        airtable_update(TBL_CHANNELS, channel_updates)

    # 3) Decidir a que videos vale la pena pedirle detalle a YouTube esta corrida.
    #    Ahorro de cuota:
    #      - Los que ya estan "Ended" nunca se vuelven a consultar.
    #      - Los "Scheduled" con inicio a mas de 5 minutos no se consultan todavia
    #        (ya tenemos guardada su fecha de inicio de una corrida anterior).
    all_video_ids = list(existing_video_ids.keys())
    if not all_video_ids:
        print("No hay videos para medir todavia.")
        return

    video_ids_to_check = []
    for vid, video_rec in existing_video_ids.items():
        cached_status = video_rec["fields"].get("Status")
        if cached_status == "Ended":
            continue  # ya termino, no se vuelve a medir nunca mas
        if cached_status == "Offline" and video_rec["fields"].get("Source") == "Auto (Channel)":
            # Salvaguarda: un video on-demand descubierto automaticamente desde
            # un canal no deberia haber llegado a crearse (ver filtro en el
            # paso de descubrimiento mas arriba), pero si por algun motivo ya
            # existe un registro asi en la base (datos viejos, carrera entre
            # corridas, etc.), no lo volvemos a chequear nunca mas: no es un
            # stream y solo gastaria cuota de YouTube sin necesidad.
            continue
        if cached_status == "Scheduled":
            cached_start = video_rec["fields"].get("Scheduled Start")
            if cached_start:
                start_dt = _parse_airtable_timestamp(cached_start)
                if start_dt is not None:
                    seconds_to_start = start_dt.timestamp() - now_epoch
                    if seconds_to_start > SCHEDULED_LOOKAHEAD_SECONDS:
                        continue  # todavia falta demasiado, no gastamos cuota
        video_ids_to_check.append(vid)

    if not video_ids_to_check:
        print("Ningun video requiere chequeo esta corrida (todos Ended o Scheduled lejano).")
        return

    details = get_videos_details(video_ids_to_check)
    last_snapshot_epoch_by_record_id = get_last_snapshot_epoch_by_video_record_id()

    video_status_updates = []
    snapshot_records = []
    # channelId (de YouTube) -> {"concurrent": total, "live_count": n}
    channel_live_totals = {}

    for vid in video_ids_to_check:
        video_rec = existing_video_ids[vid]
        d = details.get(vid)
        if not d:
            continue

        status = d["status"]
        channel_id_for_video = d.get("channel_id")
        subs = None
        if channel_id_for_video and channel_id_for_video in channels_info:
            subs = channels_info[channel_id_for_video]["subscriber_count"]
        else:
            linked = video_rec["fields"].get("Channel") or []
            if linked:
                for cid, info in channels_info.items():
                    if channel_record_by_id.get(cid, {}).get("id") == linked[0]:
                        subs = info["subscriber_count"]
                        break

        # Throttling de mediciones segun el estado del video
        should_snapshot = False
        if status == "Live":
            should_snapshot = True
        elif status == "Ended":
            # Ultima medicion: se hace una sola vez, en la corrida donde se detecta
            # el pase a Ended (porque el filtro de arriba ya excluye a los que ya
            # estaban Ended en Airtable). A partir de la proxima corrida, no se
            # vuelve a chequear este video.
            should_snapshot = True
        elif status == "Scheduled":
            should_snapshot = False  # nunca se mide mientras esta programado
        else:  # Offline (video comun, no-live)
            last_ts = last_snapshot_epoch_by_record_id.get(video_rec["id"])
            if last_ts is None or (now_epoch - last_ts) >= OFFLINE_MIN_INTERVAL_SECONDS:
                should_snapshot = True

        if should_snapshot:
            snapshot_fields = {
                "Video": [video_rec["id"]],
                "Timestamp": now_iso,
                "Status": status,
            }
            if d.get("concurrent") is not None:
                snapshot_fields["Concurrent Viewers"] = d["concurrent"]
            if d.get("views") is not None:
                snapshot_fields["Views"] = d["views"]
            if d.get("likes") is not None:
                snapshot_fields["Likes"] = d["likes"]
            if d.get("comments") is not None:
                snapshot_fields["Comments"] = d["comments"]
            if d.get("duration_min") is not None:
                snapshot_fields["Duration (min)"] = d["duration_min"]
            if subs is not None:
                snapshot_fields["Subscribers"] = subs

            snapshot_records.append({"fields": snapshot_fields})
            last_snapshot_epoch_by_record_id[video_rec["id"]] = now_epoch

        # Acumular concurrentes por canal (para el ranking diario), solo lo que
        # esta en vivo AHORA en esta corrida.
        if status == "Live" and channel_id_for_video in channels_info:
            bucket = channel_live_totals.setdefault(
                channel_id_for_video, {"concurrent": 0, "live_count": 0}
            )
            bucket["concurrent"] += d.get("concurrent") or 0
            bucket["live_count"] += 1

        # Preparar update del registro de Video (status + fechas + concurrentes actuales)
        video_fields_update = {}
        current_status_in_airtable = video_rec["fields"].get("Status")
        if current_status_in_airtable != status:
            video_fields_update["Status"] = status
        if d.get("scheduled_start") and video_rec["fields"].get("Scheduled Start") != d["scheduled_start"]:
            video_fields_update["Scheduled Start"] = d["scheduled_start"]
        if d.get("actual_start") and video_rec["fields"].get("Actual Start") != d["actual_start"]:
            video_fields_update["Actual Start"] = d["actual_start"]
        if d.get("actual_end") and video_rec["fields"].get("Actual End") != d["actual_end"]:
            video_fields_update["Actual End"] = d["actual_end"]

        new_current_concurrent = d.get("concurrent") if status == "Live" else 0
        if video_rec["fields"].get("Current Concurrent Viewers") != new_current_concurrent:
            video_fields_update["Current Concurrent Viewers"] = new_current_concurrent

        # Pico de concurrentes en tiempo real (permanente: no depende de la tabla
        # Snapshots, asi que sobrevive a la purga nocturna). Se actualiza solo
        # mientras esta en vivo, cada vez que se supera el maximo anterior.
        if status == "Live" and d.get("concurrent") is not None:
            current_peak = video_rec["fields"].get("Peak Concurrents")
            if current_peak is None or d["concurrent"] > current_peak:
                video_fields_update["Peak Concurrents"] = d["concurrent"]

                actual_start_str = d.get("actual_start") or video_rec["fields"].get("Actual Start")
                start_dt = _parse_airtable_timestamp(actual_start_str) if actual_start_str else None
                if start_dt is not None:
                    video_fields_update["Peak Moment (min)"] = round((now_epoch - start_dt.timestamp()) / 60.0, 1)

                if d.get("thumbnail_url"):
                    video_fields_update["Peak Screenshot"] = [{"url": d["thumbnail_url"]}]

        if video_fields_update:
            video_status_updates.append({"id": video_rec["id"], "fields": video_fields_update})

    if snapshot_records:
        airtable_create(TBL_SNAPSHOTS, snapshot_records)
        print(f"Snapshots creados: {len(snapshot_records)}")
    else:
        print("Sin snapshots nuevos en esta corrida.")

    if video_status_updates:
        airtable_update(TBL_VIDEOS, video_status_updates)

    # 4) Channel Snapshots: concurrentes totales por canal (suma de todos sus
    #    lives simultaneos ahora mismo), para el ranking diario.
    if channel_live_totals:
        local_date = (now + datetime.timedelta(hours=ARGENTINA_UTC_OFFSET_HOURS)).strftime("%Y-%m-%d")
        channel_snapshot_records = []
        for cid, totals in channel_live_totals.items():
            channel_record = channel_record_by_id.get(cid)
            if not channel_record:
                continue
            channel_snapshot_records.append(
                {
                    "fields": {
                        "Channel": [channel_record["id"]],
                        "Timestamp": now_iso,
                        "Date": local_date,
                        "Total Concurrent Viewers": totals["concurrent"],
                        "Live Videos Count": totals["live_count"],
                    }
                }
            )
        if channel_snapshot_records:
            airtable_create(TBL_CHANNEL_SNAPSHOTS, channel_snapshot_records)
            print(f"Channel snapshots creados: {len(channel_snapshot_records)}")


if __name__ == "__main__":
    main()
