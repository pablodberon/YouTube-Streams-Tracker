#!/usr/bin/env python3
"""
YouTube Streams Tracker -> Reporte diario por mail

Corre UNA VEZ POR DIA a las 3am hora Argentina (06:00 UTC), ANTES de que
yt_daily_consolidate.py (4am ART) borre la tabla Snapshots. Reporta la
"jornada" que acaba de terminar: 09:00 ART del dia anterior hasta 02:00 ART
del dia en que corre. Ejemplo: si corre el 22/9, reporta 21/9 09:00 -> 22/9
02:00 (hora Argentina).

Solo incluye STREAMS EN VIVO (videos con "Actual Start" seteado, es decir que
efectivamente arrancaron un vivo) que hayan arrancado dentro de esa jornada.
Los videos on-demand (nunca en vivo) quedan afuera.

Para cada stream calcula:
  - Avg Concurrents (RECORTADO): promedio de concurrentes ignorando los
    primeros 15 minutos y los ultimos 15 minutos del vivo (para no ensuciar
    el promedio con la subida/bajada de audiencia al inicio/fin).
  - Peak Concurrents: se lee directo del registro del Video (ya se calcula en
    tiempo real dentro de yt_tracker.py).
  - Views: vistas finales del stream (maximo visto en sus snapshots de la
    jornada).

Para cada canal (agregando sus streams de la jornada):
  - Avg Concurrents: promedio simple de los Avg Concurrents (recortados) de
    sus streams.
  - Net Subscribers: suscriptores del canal al final de la jornada menos al
    inicio (puede ser negativo).

El mail tiene 3 bloques: ranking de canales, ranking de streams (ambos
ordenados por Avg Concurrents descendente), y despues el detalle stream por
stream agrupado por canal.

Requisitos:
  pip install requests

Variables de entorno requeridas:
  AIRTABLE_TOKEN
  GMAIL_ADDRESS         -> cuenta de Gmail que envia el mail
  GMAIL_APP_PASSWORD    -> App Password de 16 caracteres de esa cuenta
  REPORT_RECIPIENTS     -> lista de destinatarios separados por coma
"""

import os
import sys
import smtplib
import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from collections import defaultdict

import requests

# ---------------------------------------------------------------------------
# Configuracion
# ---------------------------------------------------------------------------

BASE_ID = "appFvqj21Yy73Bjcn"
TBL_VIDEOS = "tbl8JGhar3VxOinIB"
TBL_SNAPSHOTS = "tblMGC70UgtS7NBWG"

AIRTABLE_API = "https://api.airtable.com/v0"

ARGENTINA_UTC_OFFSET_HOURS = -3
TRIM_MINUTES = 15  # minutos a ignorar al inicio y al final de cada stream

AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN", "").strip()
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "").strip()
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
REPORT_RECIPIENTS = [r.strip() for r in os.environ.get("REPORT_RECIPIENTS", "").split(",") if r.strip()]


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


if not AIRTABLE_TOKEN:
    die("Falta la variable de entorno AIRTABLE_TOKEN")
if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
    die("Faltan GMAIL_ADDRESS / GMAIL_APP_PASSWORD")
if not REPORT_RECIPIENTS:
    die("Falta REPORT_RECIPIENTS (lista de mails separados por coma)")


# ---------------------------------------------------------------------------
# Airtable helpers
# ---------------------------------------------------------------------------

def airtable_headers():
    return {"Authorization": f"Bearer {AIRTABLE_TOKEN}", "Content-Type": "application/json"}


def airtable_list_all(table_id, filter_formula=None, fields=None):
    records = []
    params = {"pageSize": 100}
    if filter_formula:
        params["filterByFormula"] = filter_formula
    if fields:
        params["fields[]"] = fields
    offset = None
    while True:
        if offset:
            params["offset"] = offset
        resp = requests.get(
            f"{AIRTABLE_API}/{BASE_ID}/{table_id}",
            headers=airtable_headers(),
            params=params,
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        records.extend(data.get("records", []))
        offset = data.get("offset")
        if not offset:
            break
    return records


def _parse_airtable_timestamp(ts):
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.datetime.strptime(ts, fmt).replace(tzinfo=datetime.timezone.utc)
        except (ValueError, TypeError):
            continue
    return None


def to_airtable_iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Ventana de la jornada a reportar
# ---------------------------------------------------------------------------

def jornada_window_utc(run_time_utc):
    """09:00 ART del dia anterior (a la fecha ART de run_time_utc) hasta
    02:00 ART del dia de run_time_utc, expresado en UTC."""
    local_now = run_time_utc + datetime.timedelta(hours=ARGENTINA_UTC_OFFSET_HOURS)
    today_local_date = local_now.date()
    yesterday_local_date = today_local_date - datetime.timedelta(days=1)

    start_local = datetime.datetime.combine(yesterday_local_date, datetime.time(9, 0))
    end_local = datetime.datetime.combine(today_local_date, datetime.time(2, 0))

    start_utc = start_local - datetime.timedelta(hours=ARGENTINA_UTC_OFFSET_HOURS)
    end_utc = end_local - datetime.timedelta(hours=ARGENTINA_UTC_OFFSET_HOURS)
    return start_utc.replace(tzinfo=datetime.timezone.utc), end_utc.replace(tzinfo=datetime.timezone.utc), yesterday_local_date, today_local_date


# ---------------------------------------------------------------------------
# Calculo de metricas
# ---------------------------------------------------------------------------

def trimmed_avg_concurrents(points, start_dt, end_dt):
    """points: lista de (epoch, concurrent) ordenada. Promedia solo los puntos
    entre (start_dt + TRIM_MINUTES) y (end_dt - TRIM_MINUTES). Si eso queda
    vacio (stream corto), usa todos los puntos disponibles."""
    if not points:
        return None
    trim_start = start_dt.timestamp() + TRIM_MINUTES * 60
    trim_end = end_dt.timestamp() - TRIM_MINUTES * 60
    trimmed = [c for t, c in points if trim_start <= t <= trim_end]
    if not trimmed:
        trimmed = [c for _, c in points]
    if not trimmed:
        return None
    return sum(trimmed) / len(trimmed)


def main():
    run_time_utc = datetime.datetime.now(datetime.timezone.utc)
    window_start, window_end, yday, today = jornada_window_utc(run_time_utc)
    print(f"Jornada a reportar: {yday} 09:00 ART -> {today} 02:00 ART")

    # 1) Videos que arrancaron un vivo dentro de la jornada
    formula = (
        f"AND({{Actual Start}} != '', "
        f"IS_AFTER({{Actual Start}}, '{to_airtable_iso(window_start)}'), "
        f"IS_BEFORE({{Actual Start}}, '{to_airtable_iso(window_end)}'))"
    )
    videos = airtable_list_all(
        TBL_VIDEOS,
        filter_formula=formula,
        fields=["Title", "Channel", "Actual Start", "Actual End", "Peak Concurrents"],
    )
    print(f"Streams en vivo en la jornada: {len(videos)}")

    if not videos:
        send_email(yday, today, [], [], {})
        return

    video_by_id = {v["id"]: v for v in videos}

    # 2) Canales referenciados (para nombre)
    channel_ids_needed = set()
    for v in videos:
        for cid in v["fields"].get("Channel") or []:
            channel_ids_needed.add(cid)
    channels_meta = {}
    if channel_ids_needed:
        channel_records = airtable_list_all("tblnRqTAN0fnm9ebE", fields=["Channel Name"])
        for c in channel_records:
            if c["id"] in channel_ids_needed:
                channels_meta[c["id"]] = c["fields"].get("Channel Name", "(sin nombre)")

    # 3) Snapshots de la jornada (una sola lectura grande, se agrupa despues)
    snap_formula = (
        f"AND(IS_AFTER({{Timestamp}}, '{to_airtable_iso(window_start)}'), "
        f"IS_BEFORE({{Timestamp}}, '{to_airtable_iso(window_end)}'))"
    )
    snapshots = airtable_list_all(
        TBL_SNAPSHOTS,
        filter_formula=snap_formula,
        fields=["Video", "Timestamp", "Concurrent Viewers", "Views", "Subscribers"],
    )
    print(f"Snapshots en la jornada: {len(snapshots)}")

    points_by_video = defaultdict(list)     # video_record_id -> [(epoch, concurrent)]
    views_by_video = defaultdict(list)      # video_record_id -> [views,...]
    subs_by_video = defaultdict(list)       # video_record_id -> [(epoch, subs)]

    for rec in snapshots:
        fields = rec["fields"]
        links = fields.get("Video") or []
        if not links or links[0] not in video_by_id:
            continue
        vid = links[0]
        ts = fields.get("Timestamp")
        dt = _parse_airtable_timestamp(ts) if ts else None
        if dt is None:
            continue
        epoch = dt.timestamp()
        if fields.get("Concurrent Viewers") is not None:
            points_by_video[vid].append((epoch, fields["Concurrent Viewers"]))
        if fields.get("Views") is not None:
            views_by_video[vid].append(fields["Views"])
        if fields.get("Subscribers") is not None:
            subs_by_video[vid].append((epoch, fields["Subscribers"]))

    stream_rows = []
    subs_points_by_channel = defaultdict(list)  # channel_record_id -> [(epoch, subs)]

    for vid, v in video_by_id.items():
        f = v["fields"]
        actual_start = _parse_airtable_timestamp(f.get("Actual Start"))
        actual_end = _parse_airtable_timestamp(f.get("Actual End")) or window_end
        if actual_start is None:
            continue

        points = sorted(points_by_video.get(vid, []), key=lambda p: p[0])
        avg_concurrents = trimmed_avg_concurrents(points, actual_start, actual_end)
        peak_concurrents = f.get("Peak Concurrents")
        if peak_concurrents is None and points:
            peak_concurrents = max(c for _, c in points)
        views = max(views_by_video.get(vid, [0])) if views_by_video.get(vid) else None

        channel_links = f.get("Channel") or []
        channel_id = channel_links[0] if channel_links else None
        channel_name = channels_meta.get(channel_id, "(sin canal)")

        if channel_id:
            subs_points_by_channel[channel_id].extend(subs_by_video.get(vid, []))

        stream_rows.append(
            {
                "channel_id": channel_id,
                "channel_name": channel_name,
                "title": f.get("Title") or "(sin titulo)",
                "avg_concurrents": avg_concurrents,
                "peak_concurrents": peak_concurrents,
                "views": views,
            }
        )

    # 4) Agregacion por canal
    channels_agg = {}
    streams_by_channel = defaultdict(list)
    for row in stream_rows:
        streams_by_channel[row["channel_id"]].append(row)

    for channel_id, rows in streams_by_channel.items():
        avgs = [r["avg_concurrents"] for r in rows if r["avg_concurrents"] is not None]
        channel_avg = sum(avgs) / len(avgs) if avgs else None

        subs_points = sorted(subs_points_by_channel.get(channel_id, []), key=lambda p: p[0])
        net_subs = None
        if len(subs_points) >= 2:
            net_subs = subs_points[-1][1] - subs_points[0][1]

        channels_agg[channel_id] = {
            "channel_name": rows[0]["channel_name"],
            "avg_concurrents": channel_avg,
            "net_subscribers": net_subs,
            "streams": rows,
        }

    send_email(yday, today, stream_rows, list(channels_agg.values()), channels_agg)


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def fmt_num(n, decimals=0):
    if n is None:
        return "-"
    return f"{n:,.{decimals}f}".replace(",", ".")


# Paleta ESPN
C_RED = "#FF2925"
C_WHITE = "#FFFFFF"
C_GRAY = "#DADADA"
C_BLACK = "#231F20"

FONT = "'Helvetica Neue', Helvetica, Arial, sans-serif"


def build_html(yday, today, stream_rows, channels_list):
    channels_sorted = sorted(
        [c for c in channels_list if c["avg_concurrents"] is not None],
        key=lambda c: c["avg_concurrents"],
        reverse=True,
    )
    streams_sorted = sorted(
        [r for r in stream_rows if r["avg_concurrents"] is not None],
        key=lambda r: r["avg_concurrents"],
        reverse=True,
    )

    th = (
        f"padding:9px 12px;background:{C_BLACK};color:{C_WHITE};text-align:left;"
        f"font-size:11px;text-transform:uppercase;letter-spacing:0.5px;font-weight:bold;"
    )
    h2 = (
        f"font-family:{FONT};color:{C_BLACK};text-transform:uppercase;letter-spacing:1px;"
        f"font-size:16px;font-weight:bold;margin:30px 0 4px 0;"
        f"border-bottom:3px solid {C_RED};padding-bottom:6px;"
    )
    table_style = f"border-collapse:collapse;width:100%;font-family:{FONT};margin-top:6px;"

    def row_style(i, extra=""):
        bg = C_WHITE if i % 2 == 0 else "#F2F2F2"
        border = f"border-left:4px solid {C_RED};" if i <= 3 else "border-left:4px solid transparent;"
        return f"background:{bg};{border}{extra}"

    def td(bold=False):
        weight = "font-weight:bold;" if bold else ""
        return f"padding:8px 12px;font-size:13px;color:{C_BLACK};border-bottom:1px solid {C_GRAY};{weight}"

    def fmt_net(net):
        if net is None:
            return "-"
        if net > 0:
            return f"<span style='color:{C_RED};font-weight:bold;'>+{fmt_num(net)}</span>"
        if net < 0:
            return f"<span style='color:#555;font-weight:bold;'>{fmt_num(net)}</span>"
        return fmt_num(net)

    parts = []

    # Banner superior estilo ESPN
    parts.append(
        f"<div style='font-family:{FONT};max-width:700px;margin:0 auto;background:{C_WHITE};'>"
        f"<table role='presentation' width='100%' style='border-collapse:collapse;'>"
        f"<tr><td style='background:{C_BLACK};padding:22px 24px;'>"
        f"<div style='color:{C_RED};font-size:22px;font-weight:bold;text-transform:uppercase;"
        f"letter-spacing:1px;'>Reporte Diario</div>"
        f"<div style='color:{C_WHITE};font-size:13px;text-transform:uppercase;letter-spacing:0.5px;"
        f"margin-top:4px;'>Streams &middot; {yday.strftime('%d/%m/%Y')} 09:00 &rarr; "
        f"{today.strftime('%d/%m/%Y')} 02:00 (ART)</div>"
        f"</td></tr>"
        f"<tr><td style='background:{C_RED};height:6px;line-height:6px;font-size:1px;'>&nbsp;</td></tr>"
        f"</table>"
        f"<div style='padding:20px 24px;'>"
    )

    if not stream_rows:
        parts.append(
            f"<p style='font-size:14px;color:{C_BLACK};font-style:italic;'>"
            f"No hubo transmisiones en vivo durante esta jornada.</p>"
        )
        parts.append("</div></div>")
        return "".join(parts)

    # Ranking de canales
    parts.append(f"<h2 style='{h2}'>Ranking de canales</h2>")
    parts.append(
        f"<table style='{table_style}'><tr>"
        f"<th style='{th}'>#</th><th style='{th}'>Canal</th>"
        f"<th style='{th}'>Avg. Concurrents</th><th style='{th}'>Suscriptores netos</th></tr>"
    )
    for i, c in enumerate(channels_sorted, 1):
        parts.append(
            f"<tr style='{row_style(i)}'>"
            f"<td style='{td(bold=i<=3)}'>{i}</td><td style='{td(bold=i<=3)}'>{c['channel_name']}</td>"
            f"<td style='{td(bold=i<=3)}'>{fmt_num(c['avg_concurrents'])}</td>"
            f"<td style='{td()}'>{fmt_net(c['net_subscribers'])}</td></tr>"
        )
    parts.append("</table>")

    # Ranking de streams (Top 10, para no sobrecargar el mail)
    parts.append(f"<h2 style='{h2}'>Ranking de streams (Top 10)</h2>")
    if len(streams_sorted) > 10:
        parts.append(
            f"<div style='font-size:11px;color:#888;margin-bottom:2px;'>"
            f"Mostrando los 10 mejores de {len(streams_sorted)} streams totales de la jornada.</div>"
        )
    parts.append(
        f"<table style='{table_style}'><tr>"
        f"<th style='{th}'>#</th><th style='{th}'>Canal</th><th style='{th}'>Stream</th>"
        f"<th style='{th}'>Avg. Concurrents</th><th style='{th}'>Peak Concurrents</th>"
        f"<th style='{th}'>Views</th></tr>"
    )
    for i, r in enumerate(streams_sorted[:10], 1):
        parts.append(
            f"<tr style='{row_style(i)}'>"
            f"<td style='{td(bold=i<=3)}'>{i}</td><td style='{td()}'>{r['channel_name']}</td>"
            f"<td style='{td(bold=i<=3)}'>{r['title']}</td><td style='{td(bold=i<=3)}'>{fmt_num(r['avg_concurrents'])}</td>"
            f"<td style='{td()}'>{fmt_num(r['peak_concurrents'])}</td><td style='{td()}'>{fmt_num(r['views'])}</td></tr>"
        )
    parts.append("</table>")

    # Detalle por canal
    parts.append(f"<h2 style='{h2}'>Detalle por canal</h2>")
    for c in channels_sorted:
        parts.append(
            f"<div style='margin-top:20px;padding:4px 0 8px 10px;border-left:4px solid {C_RED};'>"
            f"<span style='font-family:{FONT};color:{C_BLACK};text-transform:uppercase;"
            f"font-weight:bold;font-size:14px;letter-spacing:0.5px;'>{c['channel_name']}</span></div>"
        )
        parts.append(
            f"<table style='{table_style}'><tr>"
            f"<th style='{th}'>Stream</th><th style='{th}'>Avg. Concurrents</th>"
            f"<th style='{th}'>Peak Concurrents</th><th style='{th}'>Views</th></tr>"
        )
        for i, r in enumerate(sorted(c["streams"], key=lambda r: (r["avg_concurrents"] or 0), reverse=True), 1):
            parts.append(
                f"<tr style='{row_style(i)}'>"
                f"<td style='{td()}'>{r['title']}</td><td style='{td()}'>{fmt_num(r['avg_concurrents'])}</td>"
                f"<td style='{td()}'>{fmt_num(r['peak_concurrents'])}</td><td style='{td()}'>{fmt_num(r['views'])}</td></tr>"
            )
        parts.append("</table>")

    parts.append(
        f"<div style='margin-top:28px;padding-top:12px;border-top:1px solid {C_GRAY};"
        f"font-size:11px;color:#888;'>Generado automáticamente &middot; YouTube Streams Tracker</div>"
    )
    parts.append("</div></div>")
    return "".join(parts)


def send_email(yday, today, stream_rows, channels_list, channels_agg):
    html = build_html(yday, today, stream_rows, channels_list)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Reporte diario de streams - {yday.strftime('%d/%m')} a {today.strftime('%d/%m')}"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = ", ".join(REPORT_RECIPIENTS)
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, REPORT_RECIPIENTS, msg.as_string())

    print(f"Mail enviado a: {', '.join(REPORT_RECIPIENTS)}")


if __name__ == "__main__":
    main()
