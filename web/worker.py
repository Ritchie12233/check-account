"""One bounded query process. Only trusted, bundled site definitions are used."""
import json
import sys
from pathlib import Path

from sherlock_project.sherlock import sherlock
from sherlock_project.sites import SitesInformation
import sherlock_project


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


class Progress:
    def start(self, username):
        pass

    def update(self, result):
        emit({"type": "row", "site": result.site_name,
              "url": result.site_url_user, "status": str(result.status),
              "seconds": result.query_time, "detail": result.context or ""})


if __name__ == "__main__":
    try:
        # Keep the reviewed manifest; still consult upstream false-positive exclusions.
        manifest = Path(sherlock_project.__file__).parent / "resources/data.json"
        sites = SitesInformation(str(manifest))
        sites.remove_nsfw_sites()
        selected = json.loads(sys.argv[2])
        data = {s.name: s.information for s in sites if not selected or s.name in selected}
        if not data:
            raise ValueError("所选站点暂不可用，请换一个范围。")
        emit({"type": "start", "total": len(data)})
        sherlock(sys.argv[1], data, Progress(), timeout=12)
        emit({"type": "done"})
    except Exception:
        emit({"type": "error", "message": "查询失败，请稍后重试或缩小查询范围。"})
        sys.exit(1)
