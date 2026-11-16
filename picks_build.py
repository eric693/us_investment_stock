"""在獨立的短命程序裡做吃記憶體的重算（由 picks.get_picks、tw_board.get_scorecard 呼叫）。

台股選股與策略成績單都要把 200 多萬筆資料轉成寬表，尖峰超過 1GB。放在常駐程序裡跑，就算物件都釋放了，
記憶體也還不回系統，再加上下一次重算就會撞到 systemd 的 MemoryMax 被 OOM 砍掉。
放在這裡跑，程序結束記憶體就全部歸還；常駐程序只讀結果檔。

用法：python picks_build.py tw|us <輸出檔> <tw_db> <us_db>
      python picks_build.py scorecard <輸出檔> <tw_db>
結束碼：0 成功；3 資料還沒準備好（沒有輸出）；其他代表失敗。
"""
import json
import os
import sys

os.environ['TW_BOARD_NO_BG'] = '1'   # 這個程序只算一次就結束，不跑任何背景工作


def _write(out, res, **kw):
    with open(out, 'w', encoding='utf-8') as fh:
        json.dump(res, fh, ensure_ascii=False, **kw)


def main():
    target, out, tw_db = sys.argv[1:4]
    import tw_board
    tw_board.DB_PATH = tw_db
    if target == 'scorecard':
        res = tw_board.build_scorecard()
        if not res:
            return 3
        _write(out, res)
        return 0
    import picks
    picks.US_DB_PATH = sys.argv[4]
    res = picks.build(target)
    if res is None:
        return 3
    _write(out, picks._clean(res), allow_nan=False)
    return 0


if __name__ == '__main__':
    sys.exit(main())
