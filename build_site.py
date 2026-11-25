#!/usr/bin/env python3
"""
Собирает сайт-учебник (MkDocs Material) из папок, которые создал gen_lessons.py.

Ожидаемая структура до запуска:
    book/
      l1/        (index.md + 001-....md, 002-....md, ...)
      l2/
      sysadmin/
      devops/

Что делает:
    1. Копирует главы в docs/<трек>/NNN.md (короткие ссылки вместо кириллицы).
    2. Создаёт главную страницу и обзор каждого трека.
    3. Генерирует mkdocs.yml с готовым меню: вкладки = треки, разделы = модули.

Запуск:
    pip install -r requirements.txt
    python build_site.py            # собрать каркас
    mkdocs serve -a 0.0.0.0:8000    # смотреть: http://localhost:8000
    mkdocs build                    # готовый статический сайт в папке site/

Запускай build_site.py заново после каждой новой партии уроков: меню обновится само.
"""

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

PREFERRED_ORDER = ["l1", "l2", "sysadmin", "devops"]

MKDOCS_HEAD = """site_name: {site_name}
site_description: "Личный учебник по ИТ — от L1 до DevOps"
docs_dir: docs
use_directory_urls: true

theme:
  name: material
  language: ru
  features:
    - navigation.tabs
    - navigation.sections
    - navigation.indexes
    - navigation.top
    - navigation.footer
    - navigation.instant
    - search.suggest
    - search.highlight
    - toc.follow
    - content.code.copy
  palette:
    - media: "(prefers-color-scheme: light)"
      scheme: default
      primary: indigo
      accent: indigo
      toggle:
        icon: material/weather-night
        name: Тёмная тема
    - media: "(prefers-color-scheme: dark)"
      scheme: slate
      primary: indigo
      accent: indigo
      toggle:
        icon: material/weather-sunny
        name: Светлая тема

plugins:
  - search:
      lang:
        - ru
        - en

markdown_extensions:
  - admonition
  - attr_list
  - tables
  - toc:
      permalink: true
  - pymdownx.highlight:
      anchor_linenums: true
  - pymdownx.inlinehilite
  - pymdownx.superfences
  - pymdownx.details
  - pymdownx.tasklist:
      custom_checkbox: true
"""


CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")


def fix_nested_fences(text: str) -> str:
    """Блоки кода внутри нумерованных шагов: если строки внутри блока (например, here-string
    PowerShell) начинаются с нулевой колонки, MkDocs ломает список и блок. Выравниваем отступ."""
    out, fence_indent = [], None
    for line in text.split("\n"):
        stripped = line.lstrip(" ")
        if fence_indent is None:
            if stripped.startswith("```"):
                fence_indent = len(line) - len(stripped)
            out.append(line)
        elif stripped.startswith("```"):
            out.append(" " * fence_indent + stripped if fence_indent else line)
            fence_indent = None
        else:
            if fence_indent and line.strip() and len(line) - len(stripped) < fence_indent:
                line = " " * fence_indent + line
            out.append(line)
    return "\n".join(out)


def check_chapter(text: str):
    """Возвращает список предупреждений о типовых дефектах сгенерированной главы."""
    problems = []
    fences = sum(1 for ln in text.split("\n") if ln.lstrip(" ").startswith("```"))
    if fences % 2:
        problems.append("нечётное число ``` (блок кода не закрыт)")
    m = CJK_RE.search(text)
    if m:
        a = max(0, m.start() - 25)
        problems.append(f"иероглифы в тексте: …{text[a:m.end() + 25].strip()}…".replace("\n", " "))
    return problems


def q(value: str) -> str:
    """Строка в двойных кавычках: безопасно для YAML (JSON-строка это валидный YAML)."""
    return json.dumps(value, ensure_ascii=False)


def parse_track_index(index_path: Path):
    """Читает index.md, созданный gen_lessons.py: название трека, модули и ссылки на уроки."""
    title, modules, current = None, [], None
    for raw in index_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        m = re.match(r"^#\s+(.+?):\s*оглавление\s*$", line)
        if m and title is None:
            title = m.group(1).strip()
            continue
        if line.startswith("## "):
            current = {"name": line[3:].strip(), "items": []}
            modules.append(current)
            continue
        m = re.match(r"^-\s+\[(.+?)\]\((.+?)\)\s*$", line)
        if m and current is not None:
            current["items"].append((m.group(1).strip(), m.group(2).strip()))
    return title, [mod for mod in modules if mod["items"]]


def track_sort_key(path: Path):
    name = path.name.lower()
    return (PREFERRED_ORDER.index(name), name) if name in PREFERRED_ORDER else (len(PREFERRED_ORDER), name)


def main():
    ap = argparse.ArgumentParser(description="Сборка каркаса сайта-учебника (MkDocs Material)")
    ap.add_argument("--src", default="book", help="Папка с треками (по умолчанию book)")
    ap.add_argument("--docs", default="docs", help="Куда класть страницы сайта (по умолчанию docs)")
    ap.add_argument("--config", default="mkdocs.yml", help="Куда писать конфиг")
    ap.add_argument("--site-name", default="Мой ИТ-учебник", help="Название сайта")
    args = ap.parse_args()

    src, docs = Path(args.src), Path(args.docs)
    if not src.is_dir():
        sys.exit(f"Папка {src} не найдена. Сначала сгенерируй уроки через gen_lessons.py")

    track_dirs = sorted(
        (d for d in src.iterdir() if d.is_dir() and (d / "index.md").exists()),
        key=track_sort_key,
    )
    if not track_dirs:
        sys.exit(f"В {src} нет треков с index.md")

    docs.mkdir(parents=True, exist_ok=True)
    tracks, warnings = [], []

    for d in track_dirs:
        title, modules = parse_track_index(d / "index.md")
        title = title or d.name

        dest = docs / d.name
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)

        nav_modules, overview, total = [], [f"# {title}\n"], 0
        for mod in modules:
            items = []
            for label, fname in mod["items"]:
                source_file = d / fname
                if not source_file.exists():
                    continue
                num = re.match(r"^(\d+)", fname)
                new_name = f"{int(num.group(1)):03d}.md" if num else fname
                text = source_file.read_text(encoding="utf-8")
                warnings += [f"{d.name}/{fname}: {w}" for w in check_chapter(text)]
                (dest / new_name).write_text(fix_nested_fences(text), encoding="utf-8")
                items.append((label, new_name))
                total += 1
            if items:
                nav_modules.append((mod["name"], items))
                overview.append(f"\n## {mod['name']}\n")
                overview.extend(f"- [{label}]({fn})" for label, fn in items)

        (dest / "index.md").write_text("\n".join(overview) + "\n", encoding="utf-8")
        tracks.append({"dir": d.name, "title": title, "modules": nav_modules, "count": total})
        print(f"✓ {title}: {total} уроков, {len(nav_modules)} модулей")

    # главная страница
    home = [
        f"# {args.site_name}\n",
        "Личный учебник по пути **L1 → L2 → сисадмин → DevOps**. "
        "Каждая глава устроена одинаково: зачем это нужно, теория, аналогия, команды, "
        "лабораторная на виртуалках, ошибки новичков, вопросы с собеседований, источники.\n",
        "## Треки\n",
    ]
    for t in tracks:
        home.append(f"- [{t['title']}]({t['dir']}/index.md): {t['count']} уроков")
    home += [
        "\n## Как пользоваться\n",
        "- Иди по трекам по порядку: каждый следующий опирается на предыдущий.",
        "- Лабораторные выполняй на виртуалках сам, а не просто читай.",
        "- Команды и ссылки из глав проверяй по официальной документации.",
        "- Поиск вверху работает по всем главам сразу.",
    ]
    (docs / "index.md").write_text("\n".join(home) + "\n", encoding="utf-8")

    # mkdocs.yml
    nav = ["nav:", f"  - {q('Главная')}: index.md"]
    for t in tracks:
        nav.append(f"  - {q(t['title'])}:")
        nav.append(f"      - {q('Обзор')}: {t['dir']}/index.md")
        for mod_name, items in t["modules"]:
            nav.append(f"      - {q(mod_name)}:")
            for label, fn in items:
                nav.append(f"          - {q(label)}: {t['dir']}/{fn}")

    config = MKDOCS_HEAD.format(site_name=q(args.site_name)) + "\n" + "\n".join(nav) + "\n"
    Path(args.config).write_text(config, encoding="utf-8")

    if warnings:
        print(f"\n⚠ Найдено дефектов в главах: {len(warnings)}. Поправь текст или перегенерируй главу с --force:")
        for w in warnings[:40]:
            print("  -", w)
        if len(warnings) > 40:
            print(f"  ... и ещё {len(warnings) - 40}")

    print(f"\nГотово: {args.config} и папка {docs}/. Дальше: mkdocs serve -a 0.0.0.0:8000")


if __name__ == "__main__":
    main()
