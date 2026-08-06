"""执行钉钉本地文件、manifest和RAG数据库全流程一致性验收。"""

import argparse
import json
import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import settings  # noqa: E402
from app.core.dingtalk_acceptance import DingTalkAcceptanceVerifier  # noqa: E402
from app.core.dingtalk_storage import DingTalkLocalStorage  # noqa: E402
from app.models.database import get_engine, get_session, init_db  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="核对钉钉原文件、Markdown、manifest和RAG数据库一致性。"
    )
    parser.add_argument(
        "--fail-on-source-blockers",
        action="store_true",
        help="存在钉钉私有/加密源文件等转换阻塞时也返回失败。",
    )
    args = parser.parse_args()

    engine = get_engine(settings.database_url)
    init_db(engine)
    db = get_session(engine)
    try:
        result = DingTalkAcceptanceVerifier(
            db,
            DingTalkLocalStorage(),
        ).verify()
    finally:
        db.close()
        engine.dispose()

    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    if not result["valid"]:
        return 1
    if args.fail_on_source_blockers and result["blocked_count"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
