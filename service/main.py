"""服务入口：默认使用 ./data/app.db（可用 DB_PATH 覆盖），重启后当天未结束课程可交接。"""

import os

from .api import make_server
from .db import Store


def run():
    db_path = os.getenv("DB_PATH", os.path.join("data", "app.db"))
    if db_path != ":memory:":
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    store = Store(db_path)
    port = int(os.getenv("PORT", "8000"))
    print(f"研学体验容量管家 listening on :{port}, db={db_path}")
    try:
        make_server(store, port=port).serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        store.close()


if __name__ == "__main__":
    run()
