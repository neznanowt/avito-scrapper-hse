import sqlite3
import zipfile

import yaml

from realty_scraper import db, local_setup
from realty_scraper.config import load_config


def test_setup_wizard_finds_drive_and_writes_config(tmp_path, monkeypatch):
    drive = tmp_path / "G" / "Мой диск"
    drive.mkdir(parents=True)
    monkeypatch.setattr(local_setup, "find_drive_roots", lambda: [drive])
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    # на Drive уже лежит база от пробного запуска в Colab
    shared = drive / "Scraper_avito" / "data" / "realty.sqlite"
    con = db.connect(shared)
    con.execute("INSERT INTO listings(platform, listing_id) VALUES ('avito', '1')")
    con.commit()
    con.close()
    (drive / "Scraper_avito" / "seller_salt.txt").write_text("team-salt")

    cfg_path = local_setup.run_setup(tmp_path / "config.yaml", answers=[""])  # Enter — папка по умолчанию
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert raw["browser"]["mode"] == "connect"
    assert raw["seller_hash_salt"] == "team-salt"  # соль та же, что у команды в Colab
    cfg = load_config(cfg_path)
    assert cfg.data_path == drive / "Scraper_avito" / "data"
    assert cfg.db_path == tmp_path / "appdata" / "AvitoScraper" / "realty.sqlite"
    assert cfg.db_copy_path == shared
    assert sqlite3.connect(cfg.db_path).execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 1

    # повторный запуск мастера не затирает настройки без согласия
    cfg_path.write_text(cfg_path.read_text(encoding="utf-8") + "# моя правка\n", encoding="utf-8")
    local_setup.run_setup(cfg_path, answers=["n"])
    assert "# моя правка" in cfg_path.read_text(encoding="utf-8")


def test_update_from_github_zip_keeps_settings_and_data(tmp_path):
    root = tmp_path / "project"
    (root / "realty_scraper").mkdir(parents=True)
    (root / "realty_scraper" / "__init__.py").write_text("old")
    (root / "README.md").write_text("old readme")
    (root / "config.yaml").write_text("my: settings")
    (root / ".venv").mkdir()
    (root / ".venv" / "marker").write_text("venv")
    (root / "data").mkdir()
    (root / "data" / "realty.sqlite").write_text("db")

    archive = tmp_path / "main.zip"
    with zipfile.ZipFile(archive, "w") as z:  # так устроен архив, который отдаёт GitHub
        z.writestr("avito-scrapper-hse-main/realty_scraper/__init__.py", "new")
        z.writestr("avito-scrapper-hse-main/README.md", "new readme")
        z.writestr("avito-scrapper-hse-main/windows/run.bat", "@echo off\r\n")

    msg = local_setup.run_update(root, zip_url=archive.as_uri())
    assert "обновлён" in msg
    assert (root / "realty_scraper" / "__init__.py").read_text() == "new"
    assert (root / "README.md").read_text() == "new readme"
    assert (root / "windows" / "run.bat").exists()
    assert (root / "config.yaml").read_text() == "my: settings"
    assert (root / ".venv" / "marker").exists() and (root / "data" / "realty.sqlite").exists()
