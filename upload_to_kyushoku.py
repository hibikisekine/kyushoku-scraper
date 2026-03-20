#!/usr/bin/env python3
"""
kyushoku.site 自動アップロードスクリプト
スクレイパーが生成した全市CSVを kyushoku.site にアップロードします。

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

# 各市のセンター名マッピング:
#   スクレイパーCSVの center 列の値 → Supabase の type 列に格納する値（cities.ts の center.id と一致させる）
#   None の場合はスキップ
CITY_CENTER_MAP = {
    "つくばみらい市": {
        "A中学校": "A",
        "B小学校": "B",
        "A幼稚園・小学校": None,  # 中学校と同一メニューのため除外
    },
    "つくば市": {
        "すこやか給食センター豊里": "すこやか豊里",
        "ほがらか給食センター谷田部": "ほがらか谷田部",
        "桜学校給食センター": "桜",
        "筑波学校給食センター": "筑波",
    },
    "守谷市": {
        "Aブロック": "Aブロック",
        "Bブロック": "Bブロック",
        "Cブロック": "Cブロック",
    },
    "取手市": {
        "小学校（旧取手地区）": "取手",
    },
    "龍ケ崎市": {
        "A献立": "A献立",
        "B献立": "B献立",
    },
}

# 市名 → URLスラッグ（アップロードAPIの city パラメーターに使う正式名称）
CITY_NAMES = list(CITY_CENTER_MAP.keys())


def convert_city_csv(csv_path: Path, city_name: str) -> dict[str, str]:
    """
    スクレイパー形式 (city,center,year,month,day,weekday,menus) を
    アップロード形式 (日付,曜日,献立,タイプ,備考) に変換する。
    戻り値: { type_id: csv_content } の辞書
    """
    center_map = CITY_CENTER_MAP.get(city_name, {})
    # タイプ別に行を収集
    rows_by_type: dict[str, list] = {}

    with open(csv_path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            center = row["center"]
            mapped_type = center_map.get(center, center)  # マッピングなければそのまま使用

            # None はスキップ
            if mapped_type is None:
                continue

            year = int(row["year"])
            month = int(row["month"])
            day = int(row["day"])
            date_str = f"{year:04d}/{month:02d}/{day:02d}"

            weekday = WEEKDAY_MAP.get(row["weekday"], row["weekday"] + "曜日")

            # 読点（、）区切りを改行区切りに変換
            menu = row["menus"].replace("、", "\n")

            if mapped_type not in rows_by_type:
                rows_by_type[mapped_type] = []
            rows_by_type[mapped_type].append({
                "日付": date_str,
                "曜日": weekday,
                "献立": menu,
                "タイプ": mapped_type,
                "備考": "",
            })

    # タイプごとにCSV文字列を生成
    result = {}
    for type_id, rows in rows_by_type.items():
        output = io.StringIO()
        writer = csv.DictWriter(
            output,
            fieldnames=["日付", "曜日", "献立", "タイプ", "備考"],
            quoting=csv.QUOTE_ALL,
        )
        writer.writeheader()
        writer.writerows(rows)
        result[type_id] = output.getvalue()

    return result


def upload_csv(csv_content: str, type_id: str, city_name: str, filename: str, password: str) -> bool:
    """変換済みCSVを kyushoku.site/api/upload にPOSTする"""
    if not csv_content:
        return False

    headers = {"Authorization": f"Bearer {password}"}
    files = {
        "file": (filename, csv_content.encode("utf-8"), "text/csv;charset=utf-8")
    }
    data = {
        "defaultType": type_id,
        "city": city_name,
    }

    try:
        resp = requests.post(UPLOAD_URL, headers=headers, files=files, data=data, timeout=30)
        result = resp.json()
        if result.get("success"):
            count = result.get("count", 0)
            print(f"  ✅ {type_id}: {count}件 アップロード成功")
            if result.get("errors"):
                print(f"     ⚠️  警告 ({len(result['errors'])}件): {result['errors'][:3]}")
            return True
        else:
            print(f"  ❌ {type_id} 失敗: {result.get('message', 'Unknown error')}", file=sys.stderr)
            if resp.status_code == 401:
                print("     → ADMIN_PASSWORD が正しいか確認してください", file=sys.stderr)
            return False
    except requests.exceptions.ConnectionError:
        print(f"  ❌ 接続エラー: {UPLOAD_URL} に接続できません", file=sys.stderr)
        return False
    except Exception as e:
        print(f"  ❌ エラー: {e}", file=sys.stderr)
        return False


def upload_city(city_name: str, year: int, month: int, password: str) -> bool:
    """指定した市・年月のCSVを読み込んでアップロード。成功した場合 True を返す"""
    csv_path = OUTPUT_DIR / f"{city_name}_{year}年{month:02d}月.csv"
    if not csv_path.exists():
        print(f"  ⚠️  CSVファイルが見つかりません（スキップ）: {csv_path}")
        return True  # ファイルなしはエラー扱いにしない

    print(f"  📂 {csv_path}")
    type_csvs = convert_city_csv(csv_path, city_name)

    if not type_csvs:
        print(f"  ⚠️  変換結果が空です: {city_name}")
        return True

    all_ok = True
    for type_id, csv_content in sorted(type_csvs.items()):
        filename = f"{city_name}_{year}_{month:02d}_{type_id}.csv"
        ok = upload_csv(csv_content, type_id, city_name, filename, password)
        if not ok:
            all_ok = False

    return all_ok


def main():
    # 年月の決定（コマンドライン引数 > 自動（来月）の順）
    if len(sys.argv) == 3:
        year, month = int(sys.argv[1]), int(sys.argv[2])
        print(f"指定年月: {year}年{month:02d}月")
    else:
        now = datetime.now()
        if now.month == 12:
            year, month = now.year + 1, 1
        else:
            year, month = now.year, now.month + 1
        print(f"自動検出: 来月 = {year}年{month:02d}月")

    print(f"\n=== kyushoku.site アップロード: {year}年{month:02d}月 ===\n")

    # 環境変数からパスワードを取得
    admin_password = os.environ.get("ADMIN_PASSWORD", "")
    if not admin_password:
        print("ERROR: ADMIN_PASSWORD 環境変数が設定されていません", file=sys.stderr)
        print("  GitHub repo の Settings > Secrets に ADMIN_PASSWORD を追加してください", file=sys.stderr)
        sys.exit(1)

    all_ok = True
    for city_name in CITY_NAMES:
        print(f"【{city_name}】")
        ok = upload_city(city_name, year, month, admin_password)
        if not ok:
            all_ok = False
        print()

    if all_ok:
        print("✅ 全市アップロード完了！")
    else:
        print("⚠️  一部アップロードに失敗しました", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
