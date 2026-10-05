"""管理登入帳號（存於 users.json，密碼為雜湊）。

    python manage_users.py add <帳號>        # 新增或重設密碼（互動輸入）
    python manage_users.py remove <帳號>
    python manage_users.py list
"""
import sys
import json
import getpass
import os

os.environ.setdefault('TW_BOARD_NO_BG', '1')

from werkzeug.security import generate_password_hash

from app import USERS_FILE, _load_users


def _save(users):
    with open(USERS_FILE, 'w', encoding='utf-8') as f:
        json.dump(users, f, ensure_ascii=False, indent=2)


def main(argv):
    users = _load_users()
    if len(argv) == 2 and argv[0] == 'add':
        pw = getpass.getpass('密碼：')
        if len(pw) < 8 or pw != getpass.getpass('再輸入一次：'):
            sys.exit('密碼需至少 8 碼且兩次輸入相同')
        users[argv[1]] = generate_password_hash(pw)
        _save(users)
        print(f'已設定帳號 {argv[1]}')
    elif len(argv) == 2 and argv[0] == 'remove':
        if users.pop(argv[1], None) is None:
            sys.exit('查無此帳號')
        _save(users)
        print(f'已刪除帳號 {argv[1]}')
    elif argv == ['list']:
        print('\n'.join(users) or '（尚無帳號）')
    else:
        sys.exit(__doc__)


if __name__ == '__main__':
    main(sys.argv[1:])
