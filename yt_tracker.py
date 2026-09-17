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
El propio script decide cuanto medir cada video:
  - Si el video esta EN VIVO       -> mide en cada corrida (cada ~1 min)
  - Si esta OFFLINE / ENDED        -> mide como maximo cada 5 minutos
Este throttling NO depende de ningun archivo local: consulta a la propia tabla
Snapshots de Airtable cual fue la ultima medicion de cada video en los ultimos
minutos. Por eso el script es "stateless" y funciona igual de bien corriendo en
un runner efimero (GitHub Actions) que en una maquina que sigue prendida.

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

YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "").strip()
AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN", "").strip()

OFFLINE_MIN_INTERVAL_SECONDS = 5 * 60  # no medir offline mas seguido que esto
MAX_UPLOADS_PER_CHANNEL_CHECK = 15     # cuantos items recientes de la playlist de uploads mirar

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


def get_recent_video_ids_for_playlist(playlist_id, max_items=MAX_UPLOADS_PER_CHANNEL_CHECK):
    video_ids = []
    data = yt_get(
        "playlistItems",
        part="contentDetails",
        playlistId=playlist_id,
        maxResults=min(max_items, 50),
    )
    for item in data.get("items", []):
        vid = item["contentDetails"].get("videoId")
        if vid:
            video_ids.append(vid)
    return video_ids


def get_videos_details(video_ids):
    """Devuelve dict videoId -> {status, concurrent, views, likes, comments, duration_min, title, published_at}"""
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
            actual_start = live.get("actualStartTime")
            actual_end = live.get("actualEndTime")

            if actual_start and not actual_end:
                status = "Live"
            elif actual_end:
                status = "Ended"
            elif broadcast == "upcoming":
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
    for cid in channel_ids:
        info = channels_info.get(cid)
        if not info:
            continue
        try:
            recent_ids = get_recent_video_ids_for_playlist(info["uploads_playlist_id"])
        except requests.HTTPError:
            continue
        for vid in recent_ids:
            if vid not in existing_video_ids:
                new_video_ids_to_create.append((vid, cid))

    if new_video_ids_to_create:
        details_for_new = get_videos_details([v for v, _ in new_video_ids_to_create])
        records_to_create = []
        for vid, cid in new_video_ids_to_create:
            d = details_for_new.get(vid, {})
            channel_record = channel_record_by_id.get(cid)
            fields = {
                "Video ID": vid,
                "Title": d.get("title", ""),
                "URL": f"https://www.youtube.com/watch?v={vid}",
                "Source": "Auto (Channel)",
                "Status": d.get("status", "Offline"),
            }
            if d.get("published_at"):
                fields["Published At"] = d["published_at"]
            if channel_record:
                fields["Channel"] = [channel_record["id"]]
            records_to_create.append({"fields": fields})
        if records_to_create:
            created = airtable_create(TBL_VIDEOS, records_to_create)
            for rec in created:
                vid = rec["fields"].get("Video ID")
                if vid:
                    existing_video_ids[vid] = rec

    # actualizar Last Checked / Subscribers de canales
    channel_updates = []
    for cid, rec in channel_record_by_id.items():
        info = channels_info.get(cid)
        if not info:
            continue
        channel_updates.append(
            {
                "id": rec["id"],
                "fields": {
                    "Subscribers (last)": info["subscriber_count"],
                    "Last Checked": now_iso,
                },
            }
        )
    if channel_updates:
        airtable_update(TBL_CHANNELS, channel_updates)

    # 3) Medir TODOS los videos existentes (manuales + auto)
    all_video_ids = list(existing_video_ids.keys())
    if not all_video_ids:
        print("No hay videos para medir todavia.")
        return

    details = get_videos_details(all_video_ids)
    last_snapshot_epoch_by_record_id = get_last_snapshot_epoch_by_video_record_id()

    video_status_updates = []
    snapshot_records = []

    for vid, video_rec in existing_video_ids.items():
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

        should_snapshot = False
        last_ts = last_snapshot_epoch_by_record_id.get(video_rec["id"])

        if status == "Live":
            should_snapshot = True
        else:
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

        current_status_in_airtable = video_rec["fields"].get("Status")
        if current_status_in_airtable != status:
            video_status_updates.append({"id": video_rec["id"], "fields": {"Status": status}})

    if snapshot_records:
        airtable_create(TBL_SNAPSHOTS, snapshot_records)
        print(f"Snapshots creados: {len(snapshot_records)}")
    else:
        print("Sin snapshots nuevos en esta corrida.")

    if video_status_updates:
        airtable_update(TBL_VIDEOS, video_status_updates)


if __name__ == "__main__":
    main()
