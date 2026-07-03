import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from database import get_project_milestones, init_db, list_projects


OUTPUT_PATH = PROJECT_ROOT / "data/projects_for_actions.json"


def export_actions_data():
    """把本地 SQLite 项目数据导出为 GitHub Actions 可读取的 JSON。"""
    init_db()
    projects = []
    for project in list_projects(include_delivered=True):
        item = {
            "project_name": project["project_name"],
            "start_date": project["start_date"],
            "episodes": project.get("episodes") or 30,
            "project_level": project.get("project_level") or "自定义",
            "owner": project.get("owner") or "",
            "status": project.get("status") or "进行中",
            "remark": project.get("remark") or "",
            "milestones": [
                {
                    "name": milestone["name"],
                    "duration": int(milestone["duration"] or 1),
                    "due_date": milestone["due_date"],
                }
                for milestone in get_project_milestones(project["id"])
            ],
        }
        projects.append(item)

    payload = {
        "version": 1,
        "projects": projects,
    }
    OUTPUT_PATH.parent.mkdir(exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"已导出 {len(projects)} 个项目到 {OUTPUT_PATH}")


if __name__ == "__main__":
    export_actions_data()
