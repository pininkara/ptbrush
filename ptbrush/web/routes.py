from flask import Blueprint, render_template, jsonify, request
from db import Torrent, BrushTorrent, QBStatus, SystemMessage
from model import Torrent as TorrentModel
import peewee
from datetime import datetime, timedelta
import json
import re
import tomlkit
from loguru import logger
from config.config import CONFIG_FILE_PATH, PTBrushConfig

main_bp = Blueprint("main", __name__)


@main_bp.route("/")
def dashboard():
    """Dashboard page"""
    return render_template("dashboard.html", title="PTBrush - Dashboard")


@main_bp.route("/state")
def state():
    """State page"""
    return render_template("state.html", title="PTBrush - State")


@main_bp.route("/config")
def config_page():
    """Config page"""
    return render_template("config.html", title="PTBrush - Config")


@main_bp.route("/api/stats/dashboard")
@main_bp.route("/api/dashboard/data")
def get_dashboard_stats():
    """Get statistics for the dashboard"""
    try:
        # Latest QBStatus
        latest_status = QBStatus.select().order_by(QBStatus.created_time.desc()).first()

        # Active Count
        active_count = len([t for t in Torrent.select().where(Torrent.brushed == True)])
        if not latest_status:
            return jsonify(
                {
                    "status": {
                        "upspeed": 0,
                        "dlspeed": 0,
                        "free_space": 0,
                        "active_count": 0,
                    },
                    "period_stats": {},
                }
            )

        # Period Stats Calculation
        periods = {"1d": 1, "3d": 3, "7d": 7}
        period_stats = {}
        now = datetime.now()

        for p_name, days in periods.items():
            start_time = now - timedelta(days=days)

            # Count Added/Deleted from SystemMessage
            added = (
                SystemMessage.select()
                .where(
                    (SystemMessage.created_time >= start_time)
                    & (SystemMessage.category == "ADD_TORRENT")
                )
                .count()
            )

            deleted = (
                SystemMessage.select()
                .where(
                    (SystemMessage.created_time >= start_time)
                    & (SystemMessage.category == "DELETE_TORRENT")
                )
                .count()
            )

            # Traffic
            # Find closest status records to start_time
            # We need status at start_time.
            past_status = (
                QBStatus.select()
                .where(QBStatus.created_time <= start_time)
                .order_by(QBStatus.created_time.desc())
                .first()
            )
            # If no record before start_time, take the earliest we have
            if not past_status:
                past_status = (
                    QBStatus.select().order_by(QBStatus.created_time.asc()).first()
                )

            up_traffic = 0
            dl_traffic = 0
            if past_status:
                up_traffic = max(
                    0, latest_status.up_total_size - past_status.up_total_size
                )
                dl_traffic = max(
                    0, latest_status.dl_total_size - past_status.dl_total_size
                )

            period_stats[p_name] = {
                "added_count": added,
                "deleted_count": deleted,
                "upload_traffic": up_traffic,
                "download_traffic": dl_traffic,
            }

        return jsonify(
            {
                "status": {
                    "upspeed": latest_status.upspeed,
                    "dlspeed": latest_status.dlspeed,
                    "free_space": latest_status.free_space_size,
                    "active_count": active_count,
                    "timestamp": latest_status.created_time.strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                },
                "period_stats": period_stats,
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@main_bp.route("/api/state/torrents")
def get_state_torrents():
    """Get active torrents and candidates"""
    try:
        # Get all active torrents (brushed=True), sorted by deletion priority (Score ASC, Oldest First)
        # This matches the logic in services.check_disk_space_and_cleanup
        active_torrents = (
            Torrent.select()
            .where(Torrent.brushed == True)
            .order_by(Torrent.score.asc(), Torrent.created_time.asc())
        )

        torrents_data = []
        for t in active_torrents:
            # Get latest speed info for this torrent
            bt = (
                BrushTorrent.select()
                .where(BrushTorrent.torrent == t)
                .order_by(BrushTorrent.created_time.desc())
                .first()
            )

            upspeed = bt.upspeed if bt else 0
            dlspeed = bt.dlspeed if bt else 0
            up_total = bt.up_total_size if bt else 0
            dl_total = bt.dl_total_size if bt else 0

            torrents_data.append(
                {
                    "hash": "",  # No hash in DB, use name as key
                    "name": t.name,
                    "site": t.site,
                    "size": t.size,
                    "upspeed": upspeed,
                    "dlspeed": dlspeed,
                    "up_total": up_total,
                    "dl_total": dl_total,
                    "free_end_time": t.free_end_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "score": t.score,
                }
            )

        # Calculate Candidates
        # Since the list is already sorted by priority (Lowest Score First),
        # the first few items are the candidates for deletion.
        candidates = [t["name"] for t in torrents_data[:5]]

        config = PTBrushConfig()
        return jsonify(
            {
                "torrents": torrents_data,
                "candidates": candidates,
                "max_active": config.brush.max_active_torrents,
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@main_bp.route("/api/state/logs")
def get_logs():
    filter_level = request.args.get("filter", "")
    try:
        query = (
            SystemMessage.select()
            .order_by(SystemMessage.created_time.desc())
            .limit(100)
        )
        if filter_level:
            if filter_level == "DELETE_TORRENT":
                query = query.where(SystemMessage.category == "DELETE_TORRENT")
            else:
                query = query.where(SystemMessage.message_type == filter_level)

        logs = []
        for msg in query:
            logs.append(
                {
                    "time": msg.created_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "type": msg.message_type,
                    "category": msg.category,
                    "content": msg.content,
                }
            )
        return jsonify(logs)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@main_bp.route("/api/logs/clear", methods=["POST"])
def clear_logs():
    try:
        import tasks

        # Clear all logs (hours=0 means cutoff is now)
        # However, clean_db_logs implementation might handle hours=0 correctly (cutoff=now, so created_time < now).
        # We need to make sure we call it correctly.
        # tasks.clean_db_logs is decorated.
        tasks.clean_db_logs(hours=0)
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@main_bp.route("/api/config")
def get_config_json():
    # Existing implementation adapted
    from config.config import PTBrushConfig

    try:
        config = PTBrushConfig()
        return jsonify(
            {
                "brush": {
                    "work_time": config.brush.work_time,
                    "min_disk_space": config.brush.min_disk_space,
                    "pt_fetch_interval": config.brush.pt_fetch_interval,
                    "max_active_torrents": config.brush.max_active_torrents,
                    "upload_cycle": config.brush.upload_cycle,
                    "download_cycle": config.brush.download_cycle,
                    "expect_upload_speed": config.brush.expect_upload_speed,
                    "expect_download_speed": config.brush.expect_download_speed,
                    "torrent_max_size": config.brush.torrent_max_size,
                    "max_no_activate_time": config.brush.max_no_activate_time,
                },
                "downloader": {
                    "url": config.downloader.url if config.downloader else "",
                    "username": config.downloader.username if config.downloader else "",
                },
                "sites": [{"name": s.name} for s in config.sites],
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@main_bp.route("/api/config/raw", methods=["GET"])
def get_config_raw():
    if not CONFIG_FILE_PATH.exists():
        return jsonify({"content": ""})

    with open(CONFIG_FILE_PATH, "r", encoding="utf-8") as f:
        content = f.read()

    try:
        doc = tomlkit.parse(content)

        # Mask downloader password
        if "downloader" in doc and "password" in doc["downloader"]:
            doc["downloader"]["password"] = "******"

        # Mask site header values
        if "sites" in doc:
            for site in doc["sites"]:
                if "headers" in site:
                    for header in site["headers"]:
                        if "value" in header:
                            header["value"] = "******"

        return jsonify({"content": doc.as_string()})
    except Exception as e:
        logger.error(f"Error parsing config for masking: {e}")
        # Fallback to raw content if parsing fails
        return jsonify({"content": content})


@main_bp.route("/api/config/raw", methods=["POST"])
def save_config_raw():
    data = request.get_json()
    new_content = data.get("content")

    try:
        new_doc = tomlkit.parse(new_content)

        # Read old config to recover secrets
        if CONFIG_FILE_PATH.exists():
            with open(CONFIG_FILE_PATH, "r", encoding="utf-8") as f:
                old_doc = tomlkit.parse(f.read())

            # Unmask downloader password
            if "downloader" in new_doc and "password" in new_doc["downloader"]:
                if new_doc["downloader"]["password"] == "******":
                    if "downloader" in old_doc and "password" in old_doc["downloader"]:
                        new_doc["downloader"]["password"] = old_doc["downloader"][
                            "password"
                        ]

            # Unmask site header values
            if "sites" in new_doc:
                for new_site in new_doc["sites"]:
                    # Find matching old site by name
                    site_name = new_site.get("name")
                    if not site_name:
                        continue

                    old_site = None
                    if "sites" in old_doc:
                        for s in old_doc["sites"]:
                            if s.get("name") == site_name:
                                old_site = s
                                break

                    if old_site and "headers" in new_site:
                        for new_header in new_site["headers"]:
                            if new_header.get("value") == "******":
                                header_key = new_header.get("key")
                                if header_key and "headers" in old_site:
                                    # Find matching header in old site
                                    for old_header in old_site["headers"]:
                                        if old_header.get("key") == header_key:
                                            new_header["value"] = old_header.get(
                                                "value"
                                            )
                                            break

        # Write to file
        with open(CONFIG_FILE_PATH, "w", encoding="utf-8") as f:
            f.write(new_doc.as_string())

        return jsonify({"status": "success"})

    except Exception as e:
        logger.error(f"Error saving config: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@main_bp.route("/api/config/update", methods=["POST"])
def config_update_gui():
    try:
        data = request.json
        brush_data = data.get("brush", {})

        # Read file
        content = CONFIG_FILE_PATH.read_text(encoding="utf-8")

        # Simple regex replacement for keys in [brush] section
        # We assume keys are unique or locally grouped?
        # Standard TOML from Pydantic usually groups them.
        # But to be safe, we just replace "key = value" globally if unique enough,
        # or use a smarter regex looking for [brush] context.
        # Since we know the key names are specific (e.g. min_disk_space), they shouldn't conflict easily.

        def replace_toml_key(text, key, value):
            # Quote string values if needed (work_time)
            if isinstance(value, str):
                val_str = f'"{value}"'
            else:
                val_str = str(value)
            # Regex: look for key = ...
            return re.sub(
                f"^{key}\\s*=\\s*.*$", f"{key} = {val_str}", text, flags=re.MULTILINE
            )

        for cw_key, cw_val in brush_data.items():
            content = replace_toml_key(content, cw_key, cw_val)

        CONFIG_FILE_PATH.write_text(content, encoding="utf-8")
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500
