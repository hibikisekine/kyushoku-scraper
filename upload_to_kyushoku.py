#!/usr/bin/env python3
"""
kyushoku.site 自動アップロードスクリプト
スクレイパーが生成したつくばみらい市CSVを kyushoku.site にアップロードします。

使い方:
  python upload_to_kyushoku.py           # 来月のデータを自動検出
  python upload_to_kyushoku.py 2026 4    # 年月を指定

環境変数:
  ADMIN_PASSWORD: kyushoku.site の管理者パスワード（GitHub Secrets に設定）
"""

import os
import sys
import csv
import io
import requests
from datetime import datetime
from pathlib import Path

UPLOAD_URL = "https://kyushoku.site/api/upload"
OUTPUT_DIR = Path("kyushoku_output")

# 曜日変換（スクレイパー出力 → アプリ形式）
WEEKDAY_MAP = {
    "月": "月曜日",
    "火": "火曜日",
    "水": "水曜日",
    "木": "木曜日",
    "金": "金曜日",
    "土": "土曜日",
    "日": "日曜日",
}

# つくばみらい市: センター名 → A/B タイプ
# A幼稚園・小学校はA中学校とほぼ同一メニューのひらがな版のためスキップ
CENTER_TYPE_MAP = {
    "A中学校": "A",
    "B小学校": "B",
}


def convert_tsukubamirai_csv(csv_path: Path, target_type: str) -> str:
    """
    スクレイパー形式 (city,center,year,month,day,weekday,menus) を
    アップロード形式 (日付,曜日,献立,タイプ,備考) に変換する
    """
    rows = []
    with open(csv_path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            center = row["center"]
            mapped_type = CENTER_TYPE_MAP.get(center)
            if mapped_type != target_type:
                continue

            year = int(row["year"])
            month = int(row["month"])
            day = int(row["day"])
            date_str = f"{year:04d}/{month:02d}/{day:02d}"

            weekday = WEEKDAY_MAP.get(row["weekday"], row["weekday"] + "曜日")

            # 読点（、）区切りを改行区切りに変換（既存Supabaseデータ形式に合わせる）
            menu = row["menus"].replace("、", "\n")

            rows.append({
                "日付": date_str,
                "曜日": weekday,
                "献立": menu,
                "タイプ": target_type,
                "備考": "",
            })

    if not rows:
        print(f"  ⚠️  {target_type}献立: 対象データが見つかりませんでした")
        return ""

    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=["日付", "曜日", "献立", "タイプ", "備考"],
        quoting=csv.QUOTE_ALL,
    )
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def upload_csv(csv_content: str, target_type: str, filename: str, password: str) -> bool:
    """変換済みCSVを kyushoku.site/api/upload にPOSTする"""
    if not csv_content:
        return False

    headers = {"Authorization": f"Bearer {password}"}
    files = {
        "file": (filename, csv_content.encode("utf-8"), "text/csv;charset=utf-8")
    }
    data = {"defaultType": target_type}

    try:
        resp = requests.post(UPLOAD_URL, headers=headers, files=files, data=data, timeout=30)
        result = resp.json()
        if result.get("success"):
            count = result.get("count", 0)
            print(f"  ✅ {target_type}献立: {count}件 アップロード成功")
            if result.get("errors"):
                print(f"     ⚠️  警告 ({len(result['errors'])}件): {result['errors'][:3]}")
            return True
        else:
            print(f"  ❌ {target_type}献立 失敗: {result.get('message', 'Unknown error')}", file=sys.stderr)
            if resp.status_code == 401:
                print("     → ADMIN_PASSWORD が正しいか確認してください", file=sys.stderr)
            return False
    except requests.exceptions.ConnectionError:
        print(f"  ❌ 接続エラー: {UPLOAD_URL} に接続できません", file=sys.stderr)
        return False
    except Exception as e:
        print(f"  ❌ エラー: {e}", file=sys.stderr)
        return False


def main():
    # 年月の決定（コマンドライン引数 > 自動（来月）の順）
    if len(sys.argv) == 3:
        year, month = int(sys.argv[1]), int(sys.argv[2])
        print(f"  指定年月: {year}年{month:02d}月")
    else:
        now = datetime.now()
        if now.month == 12:
            year, month = now.year + 1, 1
        else:
            year, month = now.year, now.month + 1
        print(f"  自動検出: 来月 = {year}年{month:02d}月")

    print(f"\n=== kyushoku.site アップロード: {year}年{month:02d}月 ===")

    # 環境変数からパスワードを取得
    admin_password = os.environ.get("ADMIN_PASSWORD", "")
    if not admin_password:
        print("ERROR: ADMIN_PASSWORD 環境変数が設定されていません", file=sys.stderr)
        print("  GitHub repo の Settings > Secrets に ADMIN_PASSWORD を追加してください", file=sys.stderr)
        sys.exit(1)

    # CSVファイルの存在確認
    csv_path = OUTPUT_DIR / f"つくばみらい市_{year}年{month:02d}月.csv"
    if not csv_path.exists():
        print(f"  ❌ CSVファイルが見つかりません: {csv_path}", file=sys.stderr)
        sys.exit(1)

    print(f"  📂 読み込み: {csv_path}")

    # A献立（A中学校）をアップロード
    csv_a = convert_tsukubamirai_csv(csv_path, "A")
    ok_a = upload_csv(csv_a, "A", f"tsukubamirai_{year}_{month:02d}_A.csv", admin_password)

    # B献立（B小学校）をアップロード
    csv_b = convert_tsukubamirai_csv(csv_path, "B")
    ok_b = upload_csv(csv_b, "B", f"tsukubamirai_{year}_{month:02d}_B.csv", admin_password)

    if ok_a and ok_b:
        print("\n✅ 完了！")
    else:
        print("\n⚠️  一部アップロードに失敗しました", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
