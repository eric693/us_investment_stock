"""管理登入帳號（存於 users.json，密碼為雜湊）。

    python manage_users.py add <帳號> [admin]  # 新增或重設密碼（互動輸入）；加 admin = 管理員
    python manage_users.py remove <帳號>
    python manage_users.py list
"""
import sys
import getpass

from auth import load_users, save_users, set_password, password_problem


def main(argv):
    users = load_users()
    if len(argv) in (2, 3) and argv[0] == 'add':
        pw = getpass.getpass('密碼：')
        if pw != getpass.getpass('再輸入一次：'):
            sys.exit('兩次輸入不同')
        if password_problem(pw):
            sys.exit(password_problem(pw))
        role = 'admin' if argv[2:] == ['admin'] else (users.get(argv[1], {}).get('role') or 'user')
        set_password(users, argv[1], pw, role=role)
        save_users(users)
        print(f'已設定帳號 {argv[1]}')
    elif len(argv) == 2 and argv[0] == 'remove':
        if users.pop(argv[1], None) is None:
            sys.exit('查無此帳號')
        save_users(users)
        print(f'已刪除帳號 {argv[1]}')
    elif argv == ['list']:
        print('\n'.join(f"{u}  ({v.get('role', 'user')})" for u, v in users.items()) or '（尚無帳號）')
    else:
        sys.exit(__doc__)


if __name__ == '__main__':
    main(sys.argv[1:])
