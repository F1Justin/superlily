"""离线生成控制面 scrypt verifier；密码不会进入 argv、日志或数据库。"""

from __future__ import annotations

import getpass

from .control_plane import hash_control_password


def main() -> int:
    first = getpass.getpass("Control-plane password: ")
    second = getpass.getpass("Repeat password: ")
    if first != second:
        raise SystemExit("passwords do not match")
    print(hash_control_password(first))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
