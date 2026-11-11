import os
import sqlite3

import pytest

from realty_scraper import db
from realty_scraper.config import load_config
from realty_scraper.notebook import DbSync, fill_rates, get_or_create_salt, listings_df, run_cli, write_config

CODE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _make_db(path, n_rows=1):
    con = db.connect(path)
    for i in range(n_rows):
        con.execute("INSERT INTO listings(platform, listing_id, price, last_parsed_at) VALUES ('avito', ?, ?, '2026')",
                    (str(i), 1_000_000 + i))
    con.commit()
    con.close()


def test_dbsync_roundtrip_and_conflict(tmp_path):
    shared, local = tmp_path / "drive" / "realty.sqlite", tmp_path / "content" / "realty.sqlite"
    sync = DbSync(shared, local)
    assert "ещё нет" in sync.pull()
    _make_db(local, 2)
    assert "сохранена" in sync.push()
    assert sqlite3.connect(shared).execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 2

    # новая сессия: локальный диск пуст, база берётся с Drive
    local.unlink()
    assert "скопирована" in sync.pull()
    assert sqlite3.connect(local).execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 2

    # коллега тем временем сохранил свою версию на Drive
    _make_db(tmp_path / "other.sqlite", 5)
    os.replace(tmp_path / "other.sqlite", shared)
    st = os.stat(shared)
    os.utime(shared, (st.st_atime, st.st_mtime + 30))
    with pytest.raises(RuntimeError, match="изменилась"):
        sync.push()
    assert sqlite3.connect(shared).execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 5  # не затёрто
    assert list(shared.parent.glob("realty_conflict_*.sqlite"))
    assert "сохранена" in sync.push(force=True)


def test_config_written_for_colab_loads(tmp_path):
    salt = get_or_create_salt(tmp_path)
    assert salt == get_or_create_salt(tmp_path) and len(salt) == 32
    p = write_config(tmp_path / "config.yaml", data_dir=tmp_path / "data", db_file=tmp_path / "local.sqlite",
                     salt=salt, proxy=None, extra={"pacing": {"delay_min_s": 1}})
    cfg = load_config(p)
    assert cfg.db_path == tmp_path / "local.sqlite" and cfg.data_path == tmp_path / "data"
    assert cfg.browser.headless is True and cfg.browser.channel is None and cfg.browser.proxy is None
    assert cfg.pacing.delay_min_s == 1 and cfg.seller_hash_salt == salt


def test_run_cli_subprocess_and_dataframes(tmp_path, capsys):
    cfg_path = write_config(tmp_path / "config.yaml", data_dir=tmp_path / "data", db_file=tmp_path / "local.sqlite",
                            salt="s")
    sync = DbSync(tmp_path / "data" / "realty.sqlite", tmp_path / "local.sqlite")
    sync.pull()
    code = run_cli(["init"], code_dir=CODE_DIR, config=cfg_path, sync=sync)
    out = capsys.readouterr().out
    assert code == 0 and "База создана" in out and "сохранена на Drive" in out
    _make_db(tmp_path / "local.sqlite", 3)
    df = listings_df(tmp_path / "local.sqlite")
    assert len(df) == 3 and fill_rates(df).loc["price", "заполнено, %"] == 100
