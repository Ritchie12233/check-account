"""One bounded query process. Only trusted, bundled site definitions are used."""
import json
import sys
from pathlib import Path

from sherlock_project.sherlock import sherlock
from sherlock_project.sites import SitesInformation
from sherlock_project.result import QueryStatus
import sherlock_project
from evidence import canonical_url, public_links, timestamp


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


class Progress:
    def __init__(self, username):
        self.username = username

    def start(self, username):
        pass

    def update(self, result):
        emit({"type": "row", "site": result.site_name, "username": self.username, "checked_at": timestamp(),
              "url": result.site_url_user, "status": str(result.status),
              "seconds": result.query_time, "detail": result.context or ""})


if __name__ == "__main__":
    try:
        # Keep the reviewed manifest; still consult upstream false-positive exclusions.
        manifest = Path(sherlock_project.__file__).parent / "resources/data.json"
        sites = SitesInformation(str(manifest))
        sites.remove_nsfw_sites()
        selected = json.loads(sys.argv[2])
        definitions = {s.name: s.information for s in sites}
        data = {name: dict(info) for name, info in definitions.items() if not selected or name in selected}
        # These profile checks normally use HEAD. Use the same public URL with GET
        # so its HTML can provide link evidence, without following extracted URLs.
        for name in ("GitHub", "YouTube", "Twitch", "Medium", "Steam"):
            if name in data and not data[name].get("urlProbe") and not data[name].get("request_method"):
                data[name]["request_method"] = "GET"
        if not data:
            raise ValueError("所选站点暂不可用，请换一个范围。")
        usernames = json.loads(sys.argv[1])
        emit({"type": "start", "total": len(data) * len(usernames)})
        for username in usernames:
            results = sherlock(username, data, Progress(username), timeout=12)
            for site, result in results.items():
                if result["status"].status == QueryStatus.CLAIMED and not data[site].get("urlProbe"):
                    future = data[site].get("request_future")
                    response_url = future.result().url if future else ""
                    # A login page or unrelated redirect must not become profile evidence.
                    if not response_url or canonical_url(response_url) != canonical_url(result["url_user"]):
                        continue
                    for link in public_links(response_url, result.get("response_text"), definitions, username):
                        emit({"type": "link", **link})
        emit({"type": "done"})
    except Exception:
        emit({"type": "error", "message": "查询失败，请稍后重试或缩小查询范围。"})
        sys.exit(1)
