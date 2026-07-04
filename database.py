import sqlite3
from datetime import datetime
from datetime import date, timedelta
from pathlib import Path

import pandas as pd


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "sop_projects.db"

PROJECT_COLUMNS = [
    "project_name",
    "start_date",
    "episodes",
    "project_level",
    "owner",
    "status",
    "remark",
]


def get_connection():
    """获取 SQLite 连接。"""
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(default_reminder_time: str = "10:00"):
    """初始化数据库表和默认配置。"""
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_name TEXT NOT NULL,
                start_date TEXT NOT NULL,
                episodes INTEGER DEFAULT 30,
                project_level TEXT DEFAULT '自定义',
                owner TEXT,
                status TEXT DEFAULT '进行中',
                remark TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS project_milestones (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                duration INTEGER NOT NULL,
                due_date TEXT NOT NULL,
                sort_order INTEGER NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS notification_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                send_date TEXT NOT NULL,
                send_type TEXT NOT NULL,
                status TEXT NOT NULL,
                message TEXT,
                error TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES('reminder_time', ?)",
            (default_reminder_time,),
        )
        conn.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES('reminder_times', ?)",
            (default_reminder_time,),
        )
        conn.commit()


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def add_project(data: dict):
    """新增项目。"""
    timestamp = now_text()
    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO projects (
                project_name, start_date, episodes, project_level, owner,
                status, remark, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data.get("project_name"),
                data.get("start_date"),
                int(data.get("episodes") or 30),
                data.get("project_level") or "自定义",
                data.get("owner") or "",
                data.get("status") or "进行中",
                data.get("remark") or "",
                timestamp,
                timestamp,
            ),
        )
        project_id = cursor.lastrowid
        insert_project_milestones(conn, project_id, data.get("milestones") or [])
        conn.commit()
    return project_id


def insert_project_milestones(conn, project_id: int, milestones: list[dict]):
    """写入项目节点排期。"""
    for index, item in enumerate(milestones, start=1):
        conn.execute(
            """
            INSERT INTO project_milestones (
                project_id, name, duration, due_date, sort_order
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                project_id,
                item.get("name"),
                int(item.get("duration") or 1),
                item.get("due_date") or item.get("date"),
                index,
            ),
        )


def replace_project_milestones(project_id: int, milestones: list[dict]):
    """替换某个项目的全部节点排期。"""
    with get_connection() as conn:
        conn.execute("DELETE FROM project_milestones WHERE project_id=?", (project_id,))
        insert_project_milestones(conn, project_id, milestones)
        conn.commit()


def rebuild_milestones_by_delivery_date(milestones: list[dict], delivery_date) -> tuple[str, list[dict]]:
    """保持阶段耗时不变，根据新的交付日期向前重算所有节点。"""
    if not milestones:
        raise ValueError("这个项目没有节点排期，无法按交付日期重算。")

    if isinstance(delivery_date, str):
        final_date = date.fromisoformat(delivery_date)
    else:
        final_date = delivery_date

    total_days = sum(int(item["duration"] or 1) for item in milestones)
    start_date = final_date - timedelta(days=total_days - 1)
    cursor = start_date
    rebuilt = []

    for item in milestones:
        duration = int(item["duration"] or 1)
        due_date = cursor + timedelta(days=duration - 1)
        rebuilt.append(
            {
                "name": item["name"],
                "duration": duration,
                "due_date": due_date.strftime("%Y-%m-%d"),
            }
        )
        cursor = due_date + timedelta(days=1)

    return start_date.strftime("%Y-%m-%d"), rebuilt


def update_project(project_id: int, data: dict):
    """更新项目。"""
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE projects
            SET project_name=?, start_date=?, episodes=?, project_level=?,
                owner=?, status=?, remark=?, updated_at=?
            WHERE id=?
            """,
            (
                data.get("project_name"),
                data.get("start_date"),
                int(data.get("episodes") or 30),
                data.get("project_level") or "自定义",
                data.get("owner") or "",
                data.get("status") or "进行中",
                data.get("remark") or "",
                now_text(),
                project_id,
            ),
        )
        conn.commit()


def delete_project(project_id: int):
    """删除项目。"""
    with get_connection() as conn:
        conn.execute("DELETE FROM project_milestones WHERE project_id=?", (project_id,))
        conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
        conn.commit()


def list_projects(include_delivered: bool = True) -> list[dict]:
    """读取项目列表。"""
    sql = "SELECT * FROM projects"
    params = ()
    if not include_delivered:
        sql += " WHERE status != ?"
        params = ("已交付",)
    sql += " ORDER BY start_date DESC, id DESC"

    with get_connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def get_project(project_id: int) -> dict | None:
    """按 ID 获取单个项目。"""
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    return dict(row) if row else None


def get_project_milestones(project_id: int) -> list[dict]:
    """读取项目自己的节点排期。"""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM project_milestones
            WHERE project_id=?
            ORDER BY sort_order ASC, due_date ASC
            """,
            (project_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_setting(key: str, default: str = "") -> str:
    """读取系统配置。"""
    with get_connection() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    """写入系统配置。"""
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO settings(key, value) VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, value),
        )
        conn.commit()


def add_notification_log(send_type: str, status: str, message: str = "", error: str = ""):
    """记录飞书推送日志。"""
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO notification_logs (
                send_date, send_type, status, message, error, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                date.today().strftime("%Y-%m-%d"),
                send_type,
                status,
                message[:1000],
                error[:1000],
                now_text(),
            ),
        )
        conn.commit()


def list_notification_logs(limit: int = 20) -> list[dict]:
    """读取最近的飞书推送日志。"""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM notification_logs
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def has_successful_auto_log(send_date: str | None = None) -> bool:
    """判断某天是否已有自动推送或自动补发成功记录。"""
    send_date = send_date or date.today().strftime("%Y-%m-%d")
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT id FROM notification_logs
            WHERE send_date=? AND send_type IN ('auto', 'catchup') AND status='success'
            LIMIT 1
            """,
            (send_date,),
        ).fetchone()
    return row is not None


def import_projects_from_excel(file) -> int:
    """从 Excel 导入项目。列名支持中文字段。"""
    df = pd.read_excel(file)
    rename_map = {
        "项目名": "project_name",
        "开始制作日期": "start_date",
        "开始日期": "start_date",
        "集数": "episodes",
        "项目等级": "project_level",
        "负责人": "owner",
        "当前状态": "status",
        "状态": "status",
        "备注": "remark",
    }
    df = df.rename(columns=rename_map)

    count = 0
    for _, row in df.iterrows():
        if pd.isna(row.get("project_name")) or pd.isna(row.get("start_date")):
            continue

        start_date = pd.to_datetime(row.get("start_date")).strftime("%Y-%m-%d")
        add_project(
            {
                "project_name": str(row.get("project_name")).strip(),
                "start_date": start_date,
                "episodes": int(row.get("episodes") or 30),
                "project_level": str(row.get("project_level") or "自定义"),
                "owner": str(row.get("owner") or ""),
                "status": str(row.get("status") or "进行中"),
                "remark": str(row.get("remark") or ""),
            }
        )
        count += 1
    return count


def projects_to_dataframe() -> pd.DataFrame:
    """导出项目数据为 DataFrame。"""
    rows = list_projects(include_delivered=True)
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["项目名", "开始制作日期", "集数", "项目等级", "负责人", "当前状态", "备注"])
    return df.rename(
        columns={
            "id": "ID",
            "project_name": "项目名",
            "start_date": "开始制作日期",
            "episodes": "集数",
            "project_level": "项目等级",
            "owner": "负责人",
            "status": "当前状态",
            "remark": "备注",
            "created_at": "创建时间",
            "updated_at": "更新时间",
        }
    )


def seed_sample_data():
    """写入示例数据；仅在数据库为空时执行。"""
    if list_projects(include_delivered=True):
        return

    samples = [
        {
            "project_name": "逆袭女王",
            "start_date": "2026-06-23",
            "episodes": 30,
            "project_level": "B级",
            "owner": "Alice",
            "status": "进行中",
            "remark": "今天应重点确认交付材料。",
        },
        {
            "project_name": "豪门错爱",
            "start_date": "2026-06-25",
            "episodes": 30,
            "project_level": "B级",
            "owner": "Ben",
            "status": "进行中",
            "remark": "第11-30集批量制作中。",
        },
        {
            "project_name": "重生之后",
            "start_date": "2026-06-29",
            "episodes": 30,
            "project_level": "B级",
            "owner": "Cindy",
            "status": "进行中",
            "remark": "首集关键节点。",
        },
        {
            "project_name": "契约恋人",
            "start_date": "2026-06-21",
            "episodes": 30,
            "project_level": "B级",
            "owner": "Dora",
            "status": "延期",
            "remark": "已超过默认周期，需确认延期原因。",
        },
        {
            "project_name": "白月光归来",
            "start_date": "2026-06-20",
            "episodes": 30,
            "project_level": "B级",
            "owner": "Eva",
            "status": "已交付",
            "remark": "已交付项目不会出现在今日提醒。",
        },
    ]
    for item in samples:
        add_project(item)
