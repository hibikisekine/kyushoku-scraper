"""
給食献立スクレイパー v2
対応市: つくば市, 守谷市, 取手市, 龍ケ崎市, つくばみらい市

使い方:
    pip install requests beautifulsoup4 pdfplumber
    python kyushoku_scraper.py

出力: kyushoku_output/ フォルダに市区別CSV
"""

import requests
import re
import csv
import os
from urllib.parse import urljoin
import time
import calendar
import datetime as dt
from datetime import datetime
from bs4 import BeautifulSoup
import pdfplumber
from io import BytesIO

OUTPUT_DIR = "kyushoku_output"
os.makedirs(OUTPUT_DIR, exist_ok=True)

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}


# ──────────────────────────────────────────
# 共通ユーティリティ
# ──────────────────────────────────────────

def download_pdf(url):
    """PDFをダウンロードしてバイト列を返す"""
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        return r.content
    except Exception as e:
        print(f"  [ERROR] ダウンロード失敗: {url} → {e}")
        return None


def save_csv(rows, filename):
    """CSVに保存"""
    if not rows:
        print(f"  [SKIP] データなし: {filename}")
        return
    path = os.path.join(OUTPUT_DIR, filename)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f, fieldnames=["city", "center", "year", "month", "day", "weekday", "menus"]
        )
        writer.writeheader()
        writer.writerows(sorted(rows, key=lambda r: (r["month"], r["day"], r["center"])))
    print(f"  → 保存: {path} ({len(rows)}件)")


# ──────────────────────────────────────────
# テーブル形式PDFパーサー（つくば市向け）
# ──────────────────────────────────────────

def parse_pdf_table(pdf_bytes, city, center, year, month):
    """pdfplumberのテーブル抽出を使って献立を取得"""
    results = []
    try:
        with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                tables = page.extract_tables()
                for table in tables:
                    current_day = None
                    current_weekday = None
                    current_menus = []

                    for row in table:
                        col0 = str(row[0] or "").strip()
                        col1 = str(row[1] or "").strip()
                        col2 = str(row[2] or "").strip().replace("\n", " ")

                        if col0 == "日" or (col0 == "" and col1 == "" and col2 == ""):
                            continue

                        day_m = re.match(r"^(\d{1,2})", col0)
                        wday_m = re.match(r"^([月火水木金土日])", col1)

                        if day_m and wday_m:
                            if current_day and current_menus:
                                results.append({
                                    "city": city, "center": center,
                                    "year": year, "month": month,
                                    "day": int(current_day),
                                    "weekday": current_weekday,
                                    "menus": "、".join(current_menus),
                                })
                            current_day = day_m.group(1)
                            current_weekday = col1.split("\n")[0]
                            current_menus = []
                            if col2 and col2 not in ("牛乳",):
                                current_menus.append(col2)
                        elif current_day:
                            if col2 and col2 not in ("牛乳", ""):
                                current_menus.append(col2)

                    if current_day and current_menus:
                        results.append({
                            "city": city, "center": center,
                            "year": year, "month": month,
                            "day": int(current_day),
                            "weekday": current_weekday,
                            "menus": "、".join(current_menus),
                        })
    except Exception as e:
        print(f"  [WARN] PDF解析エラー: {e}")
    return results


# ──────────────────────────────────────────
# 守谷市専用パーサー（座標ベース）
# ──────────────────────────────────────────

def parse_moriya_pdf(pdf_bytes, city, center, year, month):
    """
    守谷市PDF: 座標ベースで献立を抽出
    DATE_X=[31,55], WDAY_X=[55,68], MENU_X=[68,182]
    """
    results = []
    try:
        with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                words = page.extract_words(
                    keep_blank_chars=False,
                    x_tolerance=2,
                    y_tolerance=2,
                )
                if not words:
                    continue

                day_markers = []
                wday_lookup = {}
                menu_items = []

                for w in words:
                    x = w["x0"]
                    text = w["text"].strip()
                    y = w["top"]

                    if not text:
                        continue

                    if 31 <= x <= 55 and re.match(r"^\d{1,2}$", text):
                        day_markers.append({"day": int(text), "y": y})
                    elif 55 <= x <= 68 and re.match(r"^[月火水木金土日]$", text):
                        wday_lookup[y] = text
                    elif 68 <= x <= 182 and text and text not in {"牛乳"}:
                        menu_items.append({"text": text, "y": y})

                if not day_markers:
                    continue

                day_markers.sort(key=lambda d: d["y"])

                for i, dm in enumerate(day_markers):
                    next_dm = day_markers[i + 1] if i + 1 < len(day_markers) else None
                    min_y = dm["y"]
                    max_y = next_dm["y"] if next_dm else float("inf")

                    wday = ""
                    for y, wd in sorted(wday_lookup.items()):
                        if min_y <= y < max_y:
                            wday = wd
                            break

                    day_menus = [mi["text"] for mi in menu_items if min_y <= mi["y"] < max_y]

                    if day_menus:
                        results.append({
                            "city": city, "center": center,
                            "year": year, "month": month,
                            "day": dm["day"],
                            "weekday": wday,
                            "menus": "、".join(day_menus),
                        })
    except Exception as e:
        print(f"  [WARN] PDF解析エラー: {e}")
    return results


# ──────────────────────────────────────────
# 取手市専用パーサー（座標ベース、テキストPDFのみ）
# ──────────────────────────────────────────

def parse_toride_pdf(pdf_bytes, city, center, year, month):
    """
    取手市PDF: 座標+フォント高で献立を抽出（テキストPDFのみ）

    新レイアウト（2026年5月〜）:
      - 日付: x≈12-15
      - 曜日: x≈27
      - 献立: x≈35-150
      - ふりがな: 高さ≈3px（本文≈5-7px）→ 高さでフィルタ
    """
    results = []
    weekday_map = {0: "月", 1: "火", 2: "水", 3: "木", 4: "金", 5: "土", 6: "日"}

    try:
        with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
            total_chars = sum(len(page.chars) for page in pdf.pages)
            if total_chars < 100:
                print(f"    [SKIP] 画像PDFのため解析スキップ（文字数: {total_chars}）")
                return results

            for page in pdf.pages:
                words = page.extract_words(
                    keep_blank_chars=False, x_tolerance=2, y_tolerance=2,
                )
                if not words:
                    continue

                day_markers = []   # [(y, day)]
                wday_lookup = {}   # y → 曜日
                menu_words = []    # [(y, x, text)]

                for w in words:
                    x = w["x0"]
                    y = w["top"]
                    text = w["text"].strip()
                    height = w["bottom"] - w["top"]
                    if not text:
                        continue
                    # ふりがな（小さい文字）は除外
                    if height < 4.0:
                        continue

                    # 日付（x≈10-20）
                    if 10 <= x <= 20 and re.match(r"^\d{1,2}$", text):
                        day_markers.append((y, int(text)))
                    # 曜日（x≈25-35）
                    elif 25 <= x <= 35 and re.match(r"^[月火水木金土日]$", text):
                        wday_lookup[y] = text
                    # 献立（x≈35-150）
                    elif 35 <= x <= 150:
                        if re.match(r"^[\d\.\s]+$", text):
                            continue
                        menu_words.append((y, x, text))

                if not day_markers:
                    continue
                day_markers.sort()

                for i, (dm_y, dm_day) in enumerate(day_markers):
                    next_y = day_markers[i+1][0] if i+1 < len(day_markers) else float("inf")

                    # 曜日：日付と同じ y±3 の範囲で探す
                    wday = ""
                    for wy, wd in wday_lookup.items():
                        if dm_y - 3 <= wy <= dm_y + 3:
                            wday = wd
                            break
                    if not wday:
                        try:
                            wday = weekday_map[dt.date(year, month, dm_day).weekday()]
                        except ValueError:
                            wday = ""

                    # この日のメニューワードを抽出（次の日の手前まで）
                    day_words = [
                        (my, mx, mt) for (my, mx, mt) in menu_words
                        if dm_y - 3 <= my < next_y - 3
                    ]

                    # y座標でグループ化（±2px）
                    rows_by_y = {}
                    for (my, mx, mt) in day_words:
                        matched_key = None
                        for ky in rows_by_y:
                            if abs(ky - my) <= 2:
                                matched_key = ky
                                break
                        if matched_key is None:
                            matched_key = my
                            rows_by_y[matched_key] = []
                        rows_by_y[matched_key].append((mx, mt))

                    dishes = []
                    for ky in sorted(rows_by_y.keys()):
                        row = sorted(rows_by_y[ky])
                        dish = "".join(t for (_x, t) in row).strip()
                        if not dish:
                            continue
                        # 短いひらがなのみのノイズを除外
                        if re.match(r"^[ぁ-ん]+$", dish) and len(dish) <= 2:
                            continue
                        dishes.append(dish)

                    if not dishes:
                        continue

                    # 重複除去（順序保持）
                    seen = set()
                    unique = [d for d in dishes if not (d in seen or seen.add(d))]

                    results.append({
                        "city": city, "center": center,
                        "year": year, "month": month,
                        "day": dm_day, "weekday": wday,
                        "menus": "、".join(unique),
                    })
    except Exception as e:
        print(f"  [WARN] PDF解析エラー: {e}")
    return results


# ──────────────────────────────────────────
# つくばみらい市専用パーサー（座標ベース、2ページ構成）
# ──────────────────────────────────────────

def parse_tsukubamirai_pdf(pdf_bytes, city, center, year, month):
    """
    つくばみらい市PDF: ページ独立型座標ベースパーサー
    - 日付マーカー: x≈40-65, "N日" パターン
    - 曜日マーカー: x≈40-65, "(曜)" パターン
    - 料理: x≈65-195
    - 各ページを独立処理して cross-page y座標衝突を回避
    - 後処理で前日からのメニュー混入を除去
    """
    results = []

    # ノイズパターン（つくばみらい市PDF特有）
    NOISE_PATTERNS = [
        r"^―",
        r"^（[よ幼しょ]",
        r"しょく→",
        r"^\d+$",
        r"^[０-９]+$",
        r"^(エネルギー|たんぱく質|脂質|食塩|カルシウム)",
        r"^[A-Za-zＡ-Ｚａ-ｚ]+$",  # 英字のみ
    ]

    def is_noise(text):
        return any(re.search(p, text) for p in NOISE_PATTERNS)

    try:
        with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
            all_day_data = {}

            for page_num, page in enumerate(pdf.pages, 1):
                words = page.extract_words(
                    keep_blank_chars=False,
                    x_tolerance=2,
                    y_tolerance=2,
                )
                if not words:
                    continue

                day_markers = []
                wday_markers = {}
                menu_items = []

                for w in words:
                    x = w["x0"]
                    text = w["text"].strip()
                    y = w["top"]

                    if not text or is_noise(text):
                        continue

                    # 日付マーカー: "N日" パターン
                    if 40 <= x <= 65 and re.match(r"^\d{1,2}日$", text):
                        day_num = int(re.match(r"^(\d{1,2})日$", text).group(1))
                        day_markers.append({"day": day_num, "y": y})
                    # 曜日マーカー: "(曜)" パターン
                    elif 40 <= x <= 65 and re.match(r"^\([月火水木金土日]\)$", text):
                        wday = re.match(r"^\(([月火水木金土日])\)$", text).group(1)
                        wday_markers[y] = wday
                    # メニュー
                    elif 65 <= x <= 195 and len(text) >= 2:
                        menu_items.append({"text": text, "y": y})

                if not day_markers:
                    continue

                # y座標で昇順ソート（上から下）
                day_markers.sort(key=lambda d: d["y"])

                for i, dm in enumerate(day_markers):
                    prev_dm = day_markers[i - 1] if i > 0 else None
                    next_dm = day_markers[i + 1] if i + 1 < len(day_markers) else None

                    # 上側境界: 前日マーカーとの中間点（最初の日はページ先頭）
                    if prev_dm:
                        lower_y = (prev_dm["y"] + dm["y"]) / 2
                    else:
                        lower_y = 0

                    # 下側境界: 次日マーカーとの中間点
                    if next_dm:
                        mid_y = (dm["y"] + next_dm["y"]) / 2
                    else:
                        mid_y = float("inf")

                    # 曜日（日付マーカーの近傍から取得）
                    wday = ""
                    for y, wd in sorted(wday_markers.items()):
                        if dm["y"] - 25 <= y <= dm["y"] + 60:
                            wday = wd
                            break

                    # この日のメニュー（日付マーカー前後を含む中間点範囲で取得）
                    day_menus = [
                        mi["text"] for mi in menu_items
                        if lower_y <= mi["y"] < mid_y
                    ]

                    if day_menus and dm["day"] not in all_day_data:
                        all_day_data[dm["day"]] = {
                            "day": dm["day"],
                            "weekday": wday,
                            "menus": day_menus,
                        }

            # 後処理: 前日から翌日に混入したメニューを除去
            sorted_days = sorted(all_day_data.values(), key=lambda d: d["day"])
            for i in range(1, len(sorted_days)):
                prev_set = set(sorted_days[i - 1]["menus"])
                sorted_days[i]["menus"] = [
                    m for m in sorted_days[i]["menus"] if m not in prev_set
                ]

            for d in sorted_days:
                if d["menus"]:
                    results.append({
                        "city": city, "center": center,
                        "year": year, "month": month,
                        "day": d["day"],
                        "weekday": d["weekday"],
                        "menus": "、".join(d["menus"]),
                    })

    except Exception as e:
        print(f"  [WARN] PDF解析エラー: {e}")

    # ────────────────────────────────────────────────────────────────
    # 後処理: 学事暦形式PDFの日付ずれ補正
    # つくばみらい市PDFは月の初日の曜日によらず「第1日目=月曜」として
    # 日付を並べるため、月が水曜始まりの場合などに土日に誤ってデータが
    # 割り当てられる。該当エントリを次の未割り当て平日に再マッピングする。
    # ────────────────────────────────────────────────────────────────
    from datetime import date as _date, timedelta as _timedelta
    _WDAY_NAMES = ["月", "火", "水", "木", "金", "土", "日"]

    def _fix_weekend_shift(entries, yr, mo):
        weekday_entries = []
        weekend_entries = []
        for r in entries:
            try:
                d = _date(yr, mo, r["day"])
            except ValueError:
                continue
            (weekday_entries if d.weekday() < 5 else weekend_entries).append(r)

        assigned = {r["day"] for r in weekday_entries}

        for r in sorted(weekend_entries, key=lambda r: r["day"]):
            d = _date(yr, mo, r["day"])
            nd = d + _timedelta(days=1)
            while nd.month == mo and (nd.weekday() >= 5 or nd.day in assigned):
                nd += _timedelta(days=1)
            if nd.month == mo:
                r["day"] = nd.day
                r["weekday"] = _WDAY_NAMES[nd.weekday()]
                assigned.add(nd.day)
                weekday_entries.append(r)
            else:
                print(f"  [WARN] 月をまたぐため再割り当て不可: 元{d} → スキップ")

        return sorted(weekday_entries, key=lambda r: r["day"])

    results = _fix_weekend_shift(results, year, month)

    # 曜日フィールドを実際のカレンダーに基づいて補正（PDFの学事曜日表記を上書き）
    for r in results:
        try:
            actual_wd = _WDAY_NAMES[_date(year, month, r["day"]).weekday()]
            r["weekday"] = actual_wd
        except ValueError:
            pass

    return results


# ──────────────────────────────────────────
# つくば市
# ──────────────────────────────────────────

TSUKUBA_CENTERS = [
    ("筑波学校給食センター",
     "https://www.city.tsukuba.lg.jp/kosodate/kyoiku/kyushoku/kondate/1001186.html"),
    ("すこやか給食センター豊里",
     "https://www.city.tsukuba.lg.jp/kosodate/kyoiku/kyushoku/kondate/1003401.html"),
    ("ほがらか給食センター谷田部",
     "https://www.city.tsukuba.lg.jp/kosodate/kyoiku/kyushoku/kondate/1010041.html"),
    ("桜学校給食センター",
     "https://www.city.tsukuba.lg.jp/kosodate/kyoiku/kyushoku/kondate/24199.html"),
]


def scrape_tsukuba(year, month):
    """つくば市: 各センターページからPDFリンクを取得してパース"""
    reiwa_year = year - 2018
    all_rows = []

    for center_name, page_url in TSUKUBA_CENTERS:
        print(f"  つくば市 [{center_name}]")
        try:
            r = requests.get(page_url, headers=HEADERS, timeout=30)
            soup = BeautifulSoup(r.content, "html.parser")
        except Exception as e:
            print(f"    [ERROR] ページ取得失敗: {e}")
            continue

        target_text = f"{month}月"
        pdf_url = None

        def resolve_url(href, base="https://www.city.tsukuba.lg.jp"):
            if href.startswith("http"):
                return href
            elif href.startswith("//"):
                return "https:" + href
            else:
                return base + href

        all_pdf_links = soup.find_all("a", href=re.compile(r"\.pdf", re.I))

        # 令和年を含むPDFリンクを優先
        for a in all_pdf_links:
            text = a.get_text(strip=True)
            href = a["href"]
            if target_text in text and str(reiwa_year) in href:
                pdf_url = resolve_url(href)
                break

        # フォールバック: 月名のみで検索
        if not pdf_url:
            for a in all_pdf_links:
                href = a["href"]
                if target_text in a.get_text(strip=True):
                    pdf_url = resolve_url(href)
                    break

        if not pdf_url:
            print(f"    [SKIP] PDF見つからず")
            continue

        print(f"    PDF: {pdf_url}")
        pdf_bytes = download_pdf(pdf_url)
        if pdf_bytes:
            rows = parse_pdf_table(pdf_bytes, "つくば市", center_name, year, month)
            print(f"    抽出: {len(rows)}日分")
            all_rows.extend(rows)
        time.sleep(1)

    save_csv(all_rows, f"つくば市_{year}年{month:02d}月.csv")


# ──────────────────────────────────────────
# 守谷市
# ──────────────────────────────────────────

MORIYA_PAGE = "https://www.city.moriya.ibaraki.jp/kosodate_kyouiku/kyouiku/1002903/1002956/1010242.html"
MORIYA_BLOCKS = ["A", "B", "C"]


def scrape_moriya(year, month):
    """守谷市: A/B/Cブロック別PDFを取得"""
    try:
        r = requests.get(MORIYA_PAGE, headers=HEADERS, timeout=30)
        soup = BeautifulSoup(r.content, "html.parser")
    except Exception as e:
        print(f"  [ERROR] 守谷市ページ取得失敗: {e}")
        return

    base = "https://www.city.moriya.ibaraki.jp"
    all_rows = []

    for block in MORIYA_BLOCKS:
        print(f"  守谷市 [{block}ブロック]")
        target = f"{block}ブロック {month}月の献立"
        pdf_url = None

        for a in soup.find_all("a", href=re.compile(r"\.pdf", re.I)):
            if target in a.get_text(strip=True):
                href = a["href"]
                pdf_url = urljoin(MORIYA_PAGE, href)
                break

        if not pdf_url:
            print(f"    [SKIP] PDF見つからず")
            continue

        print(f"    PDF: {pdf_url}")
        pdf_bytes = download_pdf(pdf_url)
        if pdf_bytes:
            rows = parse_moriya_pdf(pdf_bytes, "守谷市", f"{block}ブロック", year, month)
            print(f"    抽出: {len(rows)}日分")
            all_rows.extend(rows)
        time.sleep(1)

    save_csv(all_rows, f"守谷市_{year}年{month:02d}月.csv")


# ──────────────────────────────────────────
# 取手市
# ──────────────────────────────────────────

TORIDE_PAGE = "https://www.city.toride.ibaraki.jp/hoken-k/kurashi/kosodate/gakko/gakkokyusyoku/kyusyoku-kondate.html"

# 注意: 中学校・藤代センターは画像PDFのため除外
# 各ターゲットは (表示名, 必須キーワード, 除外キーワード) のタプル
# テキスト判定で柔軟にPDFを探す（URLのファイル名規則が時々変わるため）
TORIDE_TARGETS = [
    ("小学校（旧取手地区）", ["小学校", "旧・取手市地区"], ["中学校", "幼稚園"]),
]


def scrape_toride(year, month):
    """取手市: テキストPDFのみ取得（小学校のみ）"""
    reiwa_year = year - 2018

    try:
        r = requests.get(TORIDE_PAGE, headers=HEADERS, timeout=30)
        soup = BeautifulSoup(r.content, "html.parser")
    except Exception as e:
        print(f"  [ERROR] 取手市ページ取得失敗: {e}")
        return

    base = "https://www.city.toride.ibaraki.jp"
    all_rows = []

    # 月の表記揺れに対応（5月 / 05月）
    month_patterns = [f"{month}月", f"{month:02d}月"]
    target_year_text = f"令和{reiwa_year}年"

    for school_name, must_words, must_not_words in TORIDE_TARGETS:
        print(f"  取手市 [{school_name}]")
        pdf_url = None

        for a in soup.find_all("a", href=re.compile(r"\.pdf$", re.I)):
            text = a.get_text(strip=True)
            # 年・月のチェック
            if target_year_text not in text:
                continue
            if not any(mp in text for mp in month_patterns):
                continue
            # 必須キーワードのチェック
            if not all(w in text for w in must_words):
                continue
            # 除外キーワードのチェック
            if any(w in text for w in must_not_words):
                continue

            href = a["href"]
            pdf_url = href if href.startswith("http") else base + href
            break

        if not pdf_url:
            print(f"    [SKIP] PDF見つからず")
            continue

        print(f"    PDF: {pdf_url}")
        pdf_bytes = download_pdf(pdf_url)
        if pdf_bytes:
            rows = parse_toride_pdf(pdf_bytes, "取手市", school_name, year, month)
            print(f"    抽出: {len(rows)}日分")
            all_rows.extend(rows)
        time.sleep(1)

    save_csv(all_rows, f"取手市_{year}年{month:02d}月.csv")


# ──────────────────────────────────────────
# 龍ケ崎市（HTML形式 / A・B献立）
# ──────────────────────────────────────────

RYUGASAKI_BASE = "https://www.city.ryugasaki.ibaraki.jp"


def parse_ryugasaki_day(html_text, year, month, day):
    """
    龍ケ崎市の1日分HTMLからA献立・B献立を抽出
    本文ここから〜本文ここまで の範囲を使用
    A献立/B献立ブロックを・区切りで解析
    """
    # 本文エリアを切り出す
    content = html_text
    start_marker = html_text.find("本文ここから")
    end_marker = html_text.find("本文ここまで")
    if start_marker != -1:
        content = html_text[start_marker:end_marker if end_marker != -1 else len(html_text)]

    soup = BeautifulSoup(content, "html.parser")
    text_lines = soup.get_text(separator="\n").split("\n")

    weekday_map = {0: "月", 1: "火", 2: "水", 3: "木", 4: "金", 5: "土", 6: "日"}
    try:
        weekday = weekday_map[dt.date(year, month, day).weekday()]
    except ValueError:
        weekday = ""

    a_menus = []
    b_menus = []
    current_block = None

    for line in text_lines:
        line = line.strip()
        if not line:
            continue

        if re.search(r"[AＡ]献立", line):
            current_block = "A"
            continue
        if re.search(r"[BＢ]献立", line):
            current_block = "B"
            continue
        # カロリー行で終了
        if re.search(r"キロカロリー|kcal|カロリー", line):
            current_block = None
            continue

        if current_block in ("A", "B"):
            items = line.split("・")
            for item in items:
                item = item.strip()
                # 学校名プレフィックスを除去（例: "中根台中 ご飯" → "ご飯"）
                item = re.sub(r"^[\S]*[小中幼]\s+", "", item).strip()
                if item and len(item) >= 2 and re.search(r"[ぁ-んァ-ン一-龥]", item):
                    if current_block == "A":
                        a_menus.append(item)
                    else:
                        b_menus.append(item)

    result = []

    def dedup(lst):
        seen = set()
        return [x for x in lst if not (x in seen or seen.add(x))]

    if a_menus:
        result.append({
            "city": "龍ケ崎市", "center": "A献立",
            "year": year, "month": month, "day": day, "weekday": weekday,
            "menus": "、".join(dedup(a_menus)),
        })
    if b_menus:
        result.append({
            "city": "龍ケ崎市", "center": "B献立",
            "year": year, "month": month, "day": day, "weekday": weekday,
            "menus": "、".join(dedup(b_menus)),
        })

    return result


# 龍ケ崎市の祝日／除外用語
RYUGASAKI_HOLIDAYS = {
    "みどりの日", "こどもの日", "振替休日", "海の日", "山の日",
    "敬老の日", "文化の日", "勤労感謝の日", "天皇誕生日",
    "建国記念の日", "春分の日", "秋分の日", "成人の日", "元日",
    "創立記念日", "開校記念日",
}


def _ryugasaki_clean_items(raw_items):
    """献立リストをクリーンアップ（★/括弧/祝日/空白分割）"""
    result = []
    seen = set()
    for raw in raw_items:
        if raw.startswith("★"):
            continue
        if raw.startswith("(") or raw.startswith("（"):
            continue
        for item in re.split(r"[ 　]+", raw):
            item = item.strip("、，,")
            if not item:
                continue
            if item in RYUGASAKI_HOLIDAYS:
                continue
            if item in seen:
                continue
            seen.add(item)
            result.append(item)
    return result


def parse_ryugasaki_pdf(pdf_bytes, year, month, center):
    """龍ケ崎市の月間献立PDFを解析してrowsを返す"""
    weekday_map = {0: "月", 1: "火", 2: "水", 3: "木", 4: "金", 5: "土", 6: "日"}
    days_data = {}

    import io
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        current_day = None
        for page in pdf.pages:
            tables = page.extract_tables() or []
            for table in tables:
                for row in table:
                    if not row or len(row) < 4:
                        continue
                    day_cell = (row[1] or "").strip() if row[1] else ""
                    menu_cell = (row[3] or "").strip() if row[3] else ""

                    if day_cell.isdigit():
                        current_day = int(day_cell)
                        if current_day not in days_data:
                            days_data[current_day] = []

                    if current_day is None:
                        continue

                    if menu_cell:
                        items = [x.strip() for x in re.split(r"\n+", menu_cell) if x.strip()]
                        for item in items:
                            if item == "献立名":
                                continue
                            days_data[current_day].append(item)

    rows = []
    for day, raw_menus in sorted(days_data.items()):
        cleaned = _ryugasaki_clean_items(raw_menus)
        if not cleaned:
            continue
        try:
            weekday = weekday_map[dt.date(year, month, day).weekday()]
        except ValueError:
            weekday = ""
        rows.append({
            "city": "龍ケ崎市", "center": center,
            "year": year, "month": month, "day": day, "weekday": weekday,
            "menus": "、".join(cleaned),
        })
    return rows


def _ryugasaki_find_pdf_urls(index_html, year, month):
    """インデックスHTMLから A献立/B献立 のPDF URLを抽出"""
    soup = BeautifulSoup(index_html, "html.parser")
    a_url = None
    b_url = None
    # KON{年下2桁 or 1桁の令和年}-{月}-{A or B}.pdf 形式を想定
    pdf_pattern = re.compile(r"KON\d+-\d+-([AB])\.pdf$", re.IGNORECASE)
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = pdf_pattern.search(href)
        if not m:
            continue
        url = href if href.startswith("http") else (RYUGASAKI_BASE + ("/" if not href.startswith("/") else "") + href.lstrip("/")) if href.startswith("/") else f"{RYUGASAKI_BASE}/kyoiku/kyusyoku/{href}"
        # 相対パス処理
        if not href.startswith("http"):
            if href.startswith("/"):
                url = RYUGASAKI_BASE + href
            else:
                url = f"{RYUGASAKI_BASE}/kyoiku/kyusyoku/{href}"
        kind = m.group(1).upper()
        if kind == "A" and not a_url:
            a_url = url
        elif kind == "B" and not b_url:
            b_url = url
    return a_url, b_url


def scrape_ryugasaki(year, month):
    """龍ケ崎市: 月間PDFから A献立・B献立 を抽出"""
    print(f"  龍ケ崎市")

    # インデックスURLの候補（非パディング優先）
    index_url_candidates = [
        f"{RYUGASAKI_BASE}/kyoiku/kyusyoku/{year}{month}KON.html",
        f"{RYUGASAKI_BASE}/kyoiku/kyusyoku/{year}{month:02d}KON.html",
    ]

    a_pdf_url = None
    b_pdf_url = None
    found_index = None

    for index_url in index_url_candidates:
        try:
            r = requests.get(index_url, headers=HEADERS, timeout=30)
            if r.status_code == 200:
                a_pdf_url, b_pdf_url = _ryugasaki_find_pdf_urls(r.content, year, month)
                if a_pdf_url or b_pdf_url:
                    found_index = index_url
                    break
        except Exception as e:
            print(f"    [WARN] インデックスページ取得失敗 ({index_url}): {e}")

    if found_index:
        print(f"    インデックス: {found_index}")
    if a_pdf_url:
        print(f"    A献立PDF: {a_pdf_url}")
    if b_pdf_url:
        print(f"    B献立PDF: {b_pdf_url}")

    if not (a_pdf_url or b_pdf_url):
        print(f"    [ERROR] PDFリンクが見つからない。インデックス構造の確認が必要")
        return

    rows = []

    for url, center in [(a_pdf_url, "A献立"), (b_pdf_url, "B献立")]:
        if not url:
            continue
        try:
            pdf_bytes = download_pdf(url)
            if not pdf_bytes:
                print(f"    [WARN] {center} PDFダウンロード失敗")
                continue
            day_rows = parse_ryugasaki_pdf(pdf_bytes, year, month, center)
            print(f"    {center}: {len(day_rows)}日分")
            rows.extend(day_rows)
            time.sleep(0.5)
        except Exception as e:
            print(f"    [WARN] {center} 解析失敗: {e}")

    print(f"    抽出: {len(rows)}行")
    save_csv(rows, f"龍ケ崎市_{year}年{month:02d}月.csv")


# ──────────────────────────────────────────
# つくばみらい市
# ──────────────────────────────────────────

TSUKUBAMIRAI_PAGE = "https://www.city.tsukubamirai.lg.jp/edu-board/kyuushoku/page006623.html"

# センター識別キーワード（リンクテキストとその周辺コンテキストで照合）
TSUKUBAMIRAI_CENTERS = [
    ("A幼稚園・小学校", ["Ａ幼", "A幼", "幼小", "幼・小", "幼稚園・小学校", "幼小学校"]),
    ("A中学校",         ["Ａ中", "A中", "中学"]),
    ("B小学校",         ["Ｂ小", "B小", "Ｂ幼", "B幼"]),
]


def find_tsukubamirai_pdfs(soup, base_url, year, month):
    """
    つくばみらい市のページからPDFリンクをセンター別に取得
    リンクテキストと周辺コンテキストからセンターを判定
    """
    reiwa_year = year - 2018
    month_keywords = [
        f"令和{reiwa_year}年{month}月",
        f"{month}月献立",
        f"{month}月の献立",
    ]

    # PDFリンクを全収集
    pdf_links = []
    for a in soup.find_all("a", href=re.compile(r"\.pdf$", re.I)):
        href = a["href"]
        url = href if href.startswith("http") else base_url + href
        link_text = a.get_text(strip=True)

        # 周辺コンテキスト（親・祖父母要素のテキスト）
        context = link_text
        parent = a.parent
        if parent:
            context += " " + parent.get_text(separator=" ", strip=True)
            grandparent = parent.parent
            if grandparent:
                context += " " + grandparent.get_text(separator=" ", strip=True)

        pdf_links.append({
            "url": url,
            "text": link_text,
            "context": context,
        })

    # センター別にPDFを特定
    center_pdfs = {}

    for center_name, keywords in TSUKUBAMIRAI_CENTERS:
        # リンクテキストで直接マッチ（最優先）
        for link in pdf_links:
            is_target_month = any(kw in link["text"] for kw in month_keywords)
            has_center_kw = any(kw in link["text"] for kw in keywords)
            if is_target_month and has_center_kw and center_name not in center_pdfs:
                center_pdfs[center_name] = link["url"]
                break

        # フォールバック: リンクテキストのセンターキーワードのみ
        if center_name not in center_pdfs:
            for link in pdf_links:
                if any(kw in link["text"] for kw in keywords):
                    center_pdfs[center_name] = link["url"]
                    break

    return center_pdfs


def scrape_tsukubamirai(year, month):
    """つくばみらい市: 献立ページからPDFリンクを取得してパース"""
    print(f"  つくばみらい市")

    try:
        r = requests.get(TSUKUBAMIRAI_PAGE, headers=HEADERS, timeout=30)
        soup = BeautifulSoup(r.content, "html.parser")
    except Exception as e:
        print(f"  [ERROR] つくばみらい市ページ取得失敗: {e}")
        return

    base = "https://www.city.tsukubamirai.lg.jp"
    center_pdfs = find_tsukubamirai_pdfs(soup, base, year, month)

    all_rows = []

    for center_name, keywords in TSUKUBAMIRAI_CENTERS:
        print(f"    [{center_name}]")
        pdf_url = center_pdfs.get(center_name)

        if not pdf_url:
            print(f"      [SKIP] PDF見つからず")
            continue

        print(f"      PDF: {pdf_url}")
        pdf_bytes = download_pdf(pdf_url)
        if pdf_bytes:
            rows = parse_tsukubamirai_pdf(pdf_bytes, "つくばみらい市", center_name, year, month)
            print(f"      抽出: {len(rows)}日分")
            all_rows.extend(rows)
        time.sleep(1)

    save_csv(all_rows, f"つくばみらい市_{year}年{month:02d}月.csv")


# ──────────────────────────────────────────
# メイン実行
# ──────────────────────────────────────────

def main():
    import sys

    # 環境変数 or コマンドライン引数で年月を指定可能
    # 例: python kyushoku_scraper.py 2026 5
    # 例: python kyushoku_scraper.py ryugasaki 2026 5  （特定の市だけ）
    target_city = None
    args = sys.argv[1:]
    if args and not args[0].isdigit():
        target_city = args.pop(0)

    if len(args) >= 2:
        year, month = int(args[0]), int(args[1])
    else:
        now = datetime.now()
        # 毎月28日実行 → 翌月分の献立を取得
        if now.month == 12:
            year, month = now.year + 1, 1
        else:
            year, month = now.year, now.month + 1
    # ↓ 手動で指定する場合はここをコメントアウト解除して変更
    # year, month = 2026, 4

    print(f"\n{'='*50}")
    print(f"給食献立スクレイパー 実行: {year}年{month}月")
    if target_city:
        print(f"対象: {target_city} のみ")
    print(f"{'='*50}\n")

    runners = {
        "tsukuba":      lambda: scrape_tsukuba(year, month),
        "moriya":       lambda: scrape_moriya(year, month),
        "toride":       lambda: scrape_toride(year, month),
        "ryugasaki":    lambda: scrape_ryugasaki(year, month),
        "tsukubamirai": lambda: scrape_tsukubamirai(year, month),
    }

    if target_city:
        if target_city not in runners:
            print(f"[ERROR] 不明な市名: {target_city}")
            print(f"        指定可能: {', '.join(runners.keys())}")
            return
        runners[target_city]()
    else:
        for fn in runners.values():
            fn()

    print(f"\n完了! 結果は {OUTPUT_DIR}/ に保存されました。")


if __name__ == "__main__":
    main()
